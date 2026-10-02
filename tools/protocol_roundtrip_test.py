# -*- coding: utf-8 -*-
"""
协议层往返一致性测试（不需要网络、不需要投影仪）。

验证的是整条 CAST 链路里最容易出错、最难靠肉眼发现的一环：

    编码帧 --pack_frame--> N 个报文 --字节流/乱序--> FrameAssembler --> 编码帧

断言：重组出来的字节必须与原帧**逐字节相同**。
任何一位不同，解码器就会报 "Could not find ref" 或直接花屏，
而这类错误在真实投屏里表现为"偶发花屏"，极难定位。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "sender"), os.path.join(ROOT, "receiver")):
    sys.path.insert(0, p)

from castlink import ffmpeg as ff
from castlink.protocol import pack_frame, STREAM_VIDEO, CODEC_H265, MAX_TCP_PAYLOAD
from castlink_sender.capture import VideoPipeline, make_plan, primary_resolution
from castlink_receiver.frames import FrameAssembler


def collect_frames(seconds: float = 3.0, quality: str = "lossless",
                   resolution: str = "native", encoder: str = "libx265"):
    """真采一段桌面，拿到若干编码帧（含 4K 无损的大关键帧）。"""
    ffmpeg = ff.find()
    if not ffmpeg:
        raise SystemExit("找不到 ffmpeg")
    plan = make_plan(primary_resolution(), 30, resolution, False, quality, "444")
    frames = []
    pipe = VideoPipeline(ffmpeg, log=lambda m: None)
    pipe.start(VideoPipeline.build_command(ffmpeg, plan, encoder, "gdigrab",
                                           draw_mouse=False),
               lambda payload, key, ts: frames.append((payload, key)),
               codec="h265")
    import time
    time.sleep(seconds)
    pipe.stop()
    return frames


def test_roundtrip(frames):
    ok = True
    # 场景 A：TCP 字节流，且故意把报文边界劈开（模拟 recv 分块）
    got_a = []
    asm = FrameAssembler(lambda s, c, p, d, k: got_a.append((d, k)))
    buf = bytearray()
    for i, (payload, key) in enumerate(frames):
        for pkt in pack_frame(STREAM_VIDEO, CODEC_H265, i + 1, i * 33333,
                             payload, key, MAX_TCP_PAYLOAD):
            buf.extend(pkt)
        # 每攒够 40KB 就喂一次 —— 保证大量报文在中间被劈成两半
        if len(buf) >= 40960:
            asm.feed_bytes(bytes(buf))
            buf.clear()
    asm.feed_bytes(bytes(buf))

    for i, (orig, key) in enumerate(frames):
        if i >= len(got_a):
            print(f"  ✗ 帧 {i} 丢失（只重组出 {len(got_a)}/{len(frames)}）")
            ok = False
            break
        data, k = got_a[i]
        if data != orig:
            print(f"  ✗ 帧 {i} 字节不一致: {len(data)} != {len(orig)}")
            ok = False
            break
        if k != key:
            print(f"  ✗ 帧 {i} 关键帧标记不一致: {k} != {key}")
            ok = False
            break
    print(f"  TCP 字节流往返: {len(got_a)}/{len(frames)} 帧，逐字节一致 = {ok}")

    # 场景 B：UDP 报文乱序到达
    got_b = []
    asm2 = FrameAssembler(lambda s, c, p, d, k: got_b.append((d, k)))
    for i, (payload, key) in enumerate(frames):
        pkts = pack_frame(STREAM_VIDEO, CODEC_H265, i + 1, i * 33333,
                          payload, key, 1152)
        for pkt in reversed(pkts):          # 完全倒序
            asm2.feed_packet(pkt)
    same = len(got_b) == len(frames) and all(
        got_b[i][0] == frames[i][0] for i in range(len(got_b)))
    print(f"  UDP 乱序往返: {len(got_b)}/{len(frames)} 帧，逐字节一致 = {same}")
    return ok and same


def test_decode(frames, tag):
    """把重组后的流分别用「文件」和「管道」两种方式解码，比较报错。"""
    ffmpeg = ff.find()
    got = []
    asm = FrameAssembler(lambda s, c, p, d, k: got.append(d))
    buf = bytearray()
    for i, (payload, key) in enumerate(frames):
        for pkt in pack_frame(STREAM_VIDEO, CODEC_H265, i + 1, i * 33333,
                             payload, key, MAX_TCP_PAYLOAD):
            buf.extend(pkt)
    asm.feed_bytes(bytes(buf))
    stream = b"".join(got)

    d = tempfile.mkdtemp(prefix="castlink_")
    path = os.path.join(d, "s.hevc")
    with open(path, "wb") as f:
        f.write(stream)

    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    # 文件输入
    p1 = subprocess.run(base + ["-f", "hevc", "-i", path, "-f", "null", "-"],
                        stderr=subprocess.PIPE, timeout=180)
    e1 = p1.stderr.decode("utf-8", "ignore")
    # 管道输入（与接收端渲染器完全相同的参数）
    p2 = subprocess.run(base + ["-flags", "low_delay",
                                "-analyzeduration", "0", "-probesize", str(1 << 18),
                                "-f", "hevc", "-i", "pipe:0", "-f", "null", "-"],
                        input=stream, stderr=subprocess.PIPE, timeout=180)
    e2 = p2.stderr.decode("utf-8", "ignore")
    c = "Could not find ref"
    print(f"  [{tag}] 文件输入: ref错误={e1.count(c)} 其它={len(e1.strip().splitlines())} 行")
    print(f"  [{tag}] 管道输入: ref错误={e2.count(c)} 其它={len(e2.strip().splitlines())} 行")
    for line in e2.strip().splitlines()[:3]:
        print("      |", line[:150])
    return e1.count(c), e2.count(c)


def main() -> int:
    print("采集 3 秒 4K 无损桌面…")
    frames = collect_frames(seconds=3.0)
    print(f"采集到 {len(frames)} 帧，"
          f"关键帧 {sum(1 for _, k in frames if k)} 个，"
          f"最大帧 {max(len(p) for p, _ in frames)} 字节")
    if len(frames) < 5:
        print("帧太少，跳过")
        return 1
    print("协议往返一致性:")
    ok = test_roundtrip(frames)
    print("解码对比:")
    test_decode(frames, "4K无损")
    print("结论:", "协议层正确 ✅" if ok else "协议层有损 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
