# -*- coding: utf-8 -*-
"""
音频链路实测：本机播放一段测试音，看 WASAPI 回环能不能真的捕到并编成 ADTS。

这是"能不能有声音"的判决性测试：
  - 能捕到 > 0 字节的 ADTS  -> 音频链路通了
  - 捕不到                   -> 回环不可用，需要换方案
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

from castlink_sender import audio as A  # noqa: E402

FFMPEG = os.path.join(ROOT, "tools", "ffmpeg.exe")
WAV = os.path.join(HERE, "_tone.wav")


def make_tone():
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-ac", "2", "-ar", "44100", WAV], check=True)
    print(f"已生成测试音: {WAV} ({os.path.getsize(WAV)} 字节)")


def play_async():
    import winsound
    winsound.PlaySound(WAV, winsound.SND_FILENAME | winsound.SND_ASYNC)
    print("正在播放测试音（440Hz，4 秒）...")


def main() -> int:
    print("=" * 72)
    print("音源解析")
    print("=" * 72)
    src = A.resolve_source(FFMPEG)
    if src is None:
        print("❌ 解析不出任何音源")
        return 1
    print(f"音源: {src.label}")
    print(f"  kind={src.kind} name={src.name!r} proto={src.proto} "
          f"index={src.index} rate={src.rate} ch={src.channels}")

    print()
    print("=" * 72)
    print("启动音频流水线并播放测试音")
    print("=" * 72)
    make_tone()

    frames = []
    t_ref = [time.monotonic()]

    def on_frame(payload: bytes, ts: int):
        frames.append((time.monotonic(), len(payload)))

    pipe = A.AudioPipeline(FFMPEG, log=lambda m: print("   [log]", m))
    t0 = time.monotonic()
    ok = pipe.start(on_frame)
    print(f"start() -> {ok}，耗时 {time.monotonic() - t0:.2f}s，"
          f"源={pipe.source_label}")

    if not ok:
        print("❌ 音频没起来")
        return 2

    play_async()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.25)
    pipe.stop()

    n = len(frames)
    total = sum(sz for _, sz in frames)
    print()
    print("=" * 72)
    print("结果")
    print("=" * 72)
    print(f"ADTS 帧数: {n}，总字节: {total}")
    if n:
        span = frames[-1][0] - frames[0][0]
        print(f"覆盖时长: {span:.2f}s，平均码率 "
              f"{total * 8 / max(span, 0.001) / 1000:.0f} kbps")
        print(f"平均帧大小: {total / n:.0f} 字节（AAC 一帧 1024 采样，"
              f"48kHz 下约 47 帧/秒）")
        print("\n✅ 结论：系统声音回环可用，音频链路已打通。")
        return 0
    print("\n❌ 结论：一帧都没抓到。回环可能不可用，或测试音没真正出声。")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
