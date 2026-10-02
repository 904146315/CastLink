# -*- coding: utf-8 -*-
"""
发送端流水线实测探针（诊断"视频几秒一帧 + 无声音"）。

分三部分，全部基于真实代码路径，不做任何模拟：

1. **音频**：复刻 `audio.AudioPipeline.build_command()` 的探测逻辑，
   看这台机器上到底能不能找到可用的系统声音回环设备。
2. **视频码率/帧率**：按 `session._build_plan()` 的默认配置
   （native + visual + 60fps）生成真实 ffmpeg 命令，跑若干秒，
   统计"编码器实际产出多少帧、多少 Mbps"——这是链路能不能承载的上限。
3. **读取粒度**：对比 `proc.stdout.read(1MB)` 与 `os.read(64KB)`
   两种读法的分块到达间隔，验证 `capture.py` 的 1MB 阻塞读会不会
   把帧攒成"大块突发"发出去。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.stdout.reconfigure(line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

from castlink.protocol import AnnexBSplitter, ParameterSetCache, CODEC_H265  # noqa: E402
from castlink_sender import audio as audio_mod  # noqa: E402
from castlink_sender.capture import StreamPlan, primary_resolution  # noqa: E402
from castlink_sender.capture import VideoPipeline, make_plan  # noqa: E402
from castlink_sender.encoders import (  # noqa: E402
    probe_capabilities, VIDEO_CODEC_HEVC,
)

FFMPEG = os.path.join(ROOT, "tools", "ffmpeg.exe")
SECONDS = float(os.environ.get("PROBE_SECONDS", "6"))


def hr(title: str):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ------------------------------------------------------------------ 1) 音频
def probe_audio():
    hr("1) 音频回环设备探测（复刻 audio.py 的逻辑）")
    devs = audio_mod.list_devices(FFMPEG)
    print(f"list_devices() 共返回 {len(devs)} 个设备：")
    for proto, name in devs:
        print(f"    [{proto}] {name}")
    picked = audio_mod.pick_loopback(FFMPEG)
    print(f"\npick_loopback() -> {picked!r}")
    cmd = audio_mod.build_command(FFMPEG)
    print(f"build_command() -> {cmd!r}")
    if cmd is None:
        print("\n结论：❌ 找不到任何可用的系统声音回环设备 —— 这就是'声音没投过去'的根因。")
        print("      AudioPipeline.start() 会直接返回 False，日志写 '未找到可用的系统声音回环设备，已关闭音频'。")
    else:
        print("\n结论：✅ 找到回环设备，音频有戏。")
    # 顺便直接问 ffmpeg 有哪些 dshow 音频设备
    print("\n--- 直接枚举 dshow 音频设备（原始输出）---")
    p = subprocess.run(
        [FFMPEG, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
    txt = (p.stdout or b"").decode("utf-8", "ignore")
    for line in txt.splitlines():
        if "(audio)" in line or "Alternative name" in line or "audio devices" in line:
            print("   ", line.strip())


# ------------------------------------------------------------------ 2) 视频
def build_default_cmd(plan: StreamPlan, encoder_name: str, capture_method: str):
    return VideoPipeline.build_command(
        FFMPEG, plan, encoder_name, capture_method,
        draw_mouse=True, bitrate_cap_mbps=0.0, low_latency=True)


def measure(plan: StreamPlan, encoder_name: str, capture_method: str,
            label: str, read_mode: str = "fast", seconds: float = SECONDS):
    """
    跑真实命令，统计帧率与码率。

    read_mode:
      fast   —— os.read(64KB)，读取端不构成瓶颈（近似"编码器真实产出"）
      block1m—— proc.stdout.read(1MB)，复刻 capture.py 当前的做法
    """
    cmd = build_default_cmd(plan, encoder_name, capture_method)
    print(f"\n[{label}] 编码器={encoder_name} 采集={capture_method} "
          f"输出={plan.out_w}x{plan.out_h}@{plan.fps} 估算码率={plan.bitrate_mbps}Mbps")
    print("    命令: " + " ".join(cmd))

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, bufsize=0)
    except Exception as e:
        print(f"    启动失败: {e}")
        return None

    splitter = AnnexBSplitter(CODEC_H265)
    psc = ParameterSetCache()
    fd = proc.stdout.fileno()

    frames = 0
    keys = 0
    nbytes = 0
    chunks = []
    t0 = time.perf_counter()
    last_chunk_t = t0

    try:
        while time.perf_counter() - t0 < seconds:
            if read_mode == "block1m":
                chunk = proc.stdout.read(1 << 20)   # 会一直阻塞到凑满 1MB
            else:
                chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            now = time.perf_counter()
            chunks.append((round(now - last_chunk_t, 4), len(chunk)))
            last_chunk_t = now
            nbytes += len(chunk)
            for au, is_key in splitter.feed(chunk):
                frames += 1
                if is_key:
                    keys += 1
                    psc.scan(au, CODEC_H265)
    except Exception as e:
        print(f"    读取异常: {e}")
    dt = time.perf_counter() - t0
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        proc.kill()

    mbps = nbytes * 8 / 1e6 / dt
    fps = frames / dt
    print(f"    实测: {frames} 帧（关键帧 {keys}） / {dt:.2f}s  = {fps:.1f} fps，"
          f"{mbps:.1f} Mbps")
    if chunks:
        gaps = [g for g, _ in chunks]
        mx = max(gaps)
        big = [g for g in gaps if g > 0.2]
        print(f"    分块: {len(chunks)} 块，最大间隔 {mx * 1000:.0f} ms，"
              f">200ms 的块 {len(big)} 个")
        if big:
            print(f"          ↳ 存在明显突发（最长 {mx:.2f}s 才收到一块数据）")
    return {"fps": fps, "mbps": mbps, "frames": frames, "keys": keys}


def probe_video():
    hr("2) 视频码率与帧率实测")
    desktop = primary_resolution()
    print(f"本机主显示器物理分辨率: {desktop[0]}x{desktop[1]}")

    caps = probe_capabilities(FFMPEG)
    cands = [n for _, n in caps.hevc_candidates(False)]
    print(f"HEVC 候选编码器: {cands}")
    if not cands:
        print("没有可用 HEVC 编码器，跳过")
        return
    enc = cands[0]

    from castlink_sender.capture import detect_capture_method
    method = detect_capture_method(FFMPEG)
    print(f"采集方式: {method}")

    # 默认配置：native 分辨率 + visual 画质 + 60fps（这正是用户当前的设置）
    plan_default = make_plan(desktop, 60, "native", False, "visual", "420", 3840, 2160,
                             VIDEO_CODEC_HEVC)
    r1 = measure(plan_default, enc, method, "默认配置 native/visual/60fps")
    if r1:
        print(f"    → 默认配置的目标码率 {plan_default.bitrate_mbps} Mbps，"
              f"实际 {r1['mbps']:.1f} Mbps、{r1['fps']:.1f} fps")
        if r1["mbps"] > 50:
            print(f"    ⚠ 码率超过普通 Wi-Fi 可稳定承载的上限（约 30~50Mbps），"
                  f"TCP 必然背压、帧率塌陷")

    # 对照：1080p + 均衡画质 + 30fps（带宽友好）
    plan_lite = make_plan(desktop, 30, "1080p", False, "balanced", "420", 3840, 2160,
                          VIDEO_CODEC_HEVC)
    measure(plan_lite, enc, method, "对照 1080p/balanced/30fps")


# ------------------------------------------------------------------ 3) 读法
def probe_read_granularity():
    hr("3) 读取粒度：read(1MB) vs os.read(64KB)")
    desktop = primary_resolution()
    caps = probe_capabilities(FFMPEG)
    cands = [n for _, n in caps.hevc_candidates(False)]
    if not cands:
        return
    enc = cands[0]
    from castlink_sender.capture import detect_capture_method
    method = detect_capture_method(FFMPEG)
    plan = make_plan(desktop, 60, "1080p", False, "balanced", "420", 3840, 2160,
                     VIDEO_CODEC_HEVC)
    measure(plan, enc, method, "读法 fast (os.read 64KB)", read_mode="fast", seconds=5)
    measure(plan, enc, method, "读法 block1m (capture.py 现状)", read_mode="block1m",
            seconds=5)


if __name__ == "__main__":
    probe_audio()
    probe_video()
    probe_read_granularity()
    print("\n全部探测完成。")
