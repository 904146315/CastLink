# -*- coding: utf-8 -*-
"""
接收端音频链路回归测试：真 ADTS -> AudioSink -> PCM -> 扬声器。

回答的问题：**收到的音频帧到底能不能变成声音。**

不测这一环的话，"投影仪/接收端没声音"永远只能靠猜 —— 发送端那边
（WASAPI 回环 -> ADTS）早就实测是通的，问题必然落在这后半段：
    ADTS 能否被解出 PCM、PCM 能否真的写进输出设备。

断言：
  * 确实拿到了真实 ADTS 帧（用发送端同一套 ffmpeg 命令现场生成）
  * AudioSink 启动成功
  * 喂进去以后 decoded_bytes > 0（= PCM 真的写进了扬声器）
"""
from __future__ import annotations

import os
import sys
import time

sys.stdout.reconfigure(line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

FFMPEG = os.path.join(ROOT, "tools", "ffmpeg.exe")


def real_adts_frames(seconds: float = 3.0):
    """用发送端同一套音源拿到真实 ADTS 帧；拿不到就现场合成。"""
    from castlink_sender import audio as A
    frames = []
    pipe = A.AudioPipeline(FFMPEG, log=lambda m: None)
    if pipe.start(lambda b, ts: frames.append(b)):
        t = time.time()
        while time.time() - t < seconds and len(frames) < 60:
            time.sleep(0.05)
        pipe.stop()
    if not frames:
        # 回环设备不可用时退回"自己造一段 ADTS"，保证这条测试永远可跑
        print("（回环没采到，改用 ffmpeg 合成 ADTS）")
        import subprocess
        raw = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={seconds}",
             "-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", "160k",
             "-f", "adts", "pipe:1"], stdout=subprocess.PIPE).stdout
        from castlink.protocol import AdtsSplitter
        frames = AdtsSplitter().feed(raw)
    return frames


def main() -> int:
    sys.path.insert(0, os.path.join(ROOT, "receiver-win"))
    import castlink_receiver_win as RW

    print("=" * 68)
    print("1) 取真实 ADTS 帧")
    print("=" * 68)
    frames = real_adts_frames()
    print(f"拿到 {len(frames)} 帧，首帧 {len(frames[0]) if frames else 0} 字节")
    if not frames:
        print("❌ 拿不到任何 ADTS 帧")
        return 2
    print("✅ 有真实 ADTS 数据")

    print()
    print("=" * 68)
    print("2) 启动 AudioSink（ADTS -> PCM -> 扬声器）")
    print("=" * 68)
    sink = RW.AudioSink(FFMPEG, log=lambda m: print("   [audio]", m))
    if not sink.start():
        print(f"❌ 音频输出启动失败：{sink.error}")
        print("   （这台机器没有可用输出设备 / 缺 pyaudiowpatch 时会这样）")
        return 3
    print("✅ 音频输出已就绪")

    print()
    print("=" * 68)
    print("3) 喂帧并观察是否真的出声")
    print("=" * 68)
    fed = 0
    for f in frames:
        if sink.feed(f):
            fed += 1
        time.sleep(0.021)
        if sink.decoded_bytes > 0 and fed > 20:
            break
    # 再等一会儿让解码器把尾巴吐完
    time.sleep(0.5)
    print(f"喂入 {fed} 帧 ADTS，解出 PCM {sink.decoded_bytes} 字节，状态={sink.status}")
    sink.stop()

    ok = sink.decoded_bytes > 0
    print()
    print("=" * 68)
    print("结论：", "✅ ADTS 能解成 PCM 并写进扬声器" if ok
          else "❌ 没有解出任何 PCM —— 接收端音频链路仍然不通")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
