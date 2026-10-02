# -*- coding: utf-8 -*-
"""
单机端到端：真发送端 -> 真 Windows 接收端，四件事一次全部验证。

覆盖用户在本轮报的三条：
  1. 第一次能投屏
  2. **停止之后再开始，还能不能投屏**（本轮的核心 bug）
  3. 音频有没有真的发出去
  4. 音频有没有真的收下来、解成 PCM 播出去

不含这个测试的话，"有画面没声音"永远只能靠猜：
发送端说"我发了"、接收端说"我没收到"，中间没有任何一处是实证。

用法：python tools/pc2pc_e2e_test.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

sys.stdout.reconfigure(line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender"), os.path.join(ROOT, "receiver-win")):
    if p not in sys.path:
        sys.path.insert(0, p)

from castlink.config import SenderConfig                        # noqa: E402
from castlink.identity import load_identity                     # noqa: E402
from castlink_sender.encoders import probe_capabilities         # noqa: E402
from castlink_sender.session import CastSession                 # noqa: E402
import castlink_receiver_win as RW                              # noqa: E402

FFMPEG = os.path.join(ROOT, "tools", "ffmpeg.exe")
CTRL_PORT = RW.CTRL_PORT


class InstrumentedReceiver(RW.WinReceiver):
    """记下每轮会话收了多少视频帧、多少音频帧、解出多少 PCM 字节。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.round = {"video": 0, "audio": 0, "pcm": 0, "audio_status": "—"}
        self._sink = None

    def _begin_media(self, conn, start, addr):
        self.round = {"video": 0, "audio": 0, "pcm": 0, "audio_status": "—"}
        # 借用父类流程：把 _handle_packet 的计数改成推到 self.round
        return super()._begin_media(conn, start, addr)

    def _handle_packet(self, pkt, frames, player, stats, audio=None):
        before_v = stats.get("frames", 0)
        super()._handle_packet(pkt, frames, player, stats, audio)
        if stats.get("frames", 0) > before_v:
            self.round["video"] += 1
        self.round["audio"] = stats.get("audioRecv", 0)
        if audio is not None:
            self.round["pcm"] = audio.decoded_bytes
            self.round["audio_status"] = audio.status
        self._sink = audio


def wait_port_free(port, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            s.close()
            return True
        except Exception:
            s.close()
            time.sleep(0.3)
    return False


def run_round(idx, session, rx, seconds=4.5):
    print()
    print("=" * 72)
    print(f"第 {idx} 轮：连接 -> 观测 {seconds:.0f} 秒 -> 停止")
    print("=" * 72)
    t0 = time.time()
    ok = session.connect_to("127.0.0.1", CTRL_PORT, "本机接收端")
    print(f"connect_to() -> {ok}  状态={session.state}")
    while time.time() - t0 < seconds:
        time.sleep(0.5)
    r = dict(rx.round)
    st = session.media.stats.sample()
    print(f"  接收端：视频 {r['video']} 帧 / 音频 {r['audio']} 帧 / PCM {r['pcm']} 字节"
          f" / 音频状态 {r['audio_status']}")
    print(f"  发送端：{st}  音频源={session.audio_note}")
    session.stop()
    time.sleep(0.8)
    return r


def main() -> int:
    if not wait_port_free(CTRL_PORT):
        print(f"❌ 控制端口 {CTRL_PORT} 被占用（是不是 CastLink 接收端还在跑？）")
        return 2

    rx = InstrumentedReceiver(FFMPEG, "000000", "本机接收端", False,
                              log=lambda m: print("   [recv]", m),
                              render_mode="null")
    rx.cap_w, rx.cap_h, rx.cap_fps = 1920, 1080, 60
    rx.start()
    time.sleep(0.8)

    cfg = SenderConfig()
    cfg.resolution_mode = "1080p"
    cfg.fps = 30
    cfg.quality = "smooth"
    cfg.transport = "tcp"
    cfg.enable_audio = True
    cfg.adaptive = False
    cfg.remembered_peers = {}

    caps = probe_capabilities(FFMPEG, deep=True)
    session = CastSession(cfg, caps, FFMPEG, load_identity("测试机"),
                          log=lambda m: None)

    results = []
    try:
        for i in (1, 2, 3):
            results.append(run_round(i, session, rx))
    finally:
        session.shutdown()
        rx.stop()

    print()
    print("=" * 72)
    vids = [r["video"] for r in results]
    auds = [r["audio"] for r in results]
    pcms = [r["pcm"] for r in results]
    print(f"三轮视频帧：{vids}")
    print(f"三轮音频帧：{auds}")
    print(f"三轮解出 PCM：{pcms} 字节")
    ok = True
    if not all(v > 0 for v in vids):
        print("❌ 有轮次一个视频帧都没渲染（停止后无法重新投屏）")
        ok = False
    else:
        print("✅ 每轮都有视频 —— 可反复停止/开始")
    if not all(a > 0 for a in auds):
        print("❌ 有轮次没收到音频帧")
        ok = False
    elif not all(p > 0 for p in pcms):
        print("❌ 收到了音频但没解出 PCM（接收端音频解码有问题）")
        ok = False
    else:
        print("✅ 每轮音频都收到了，并且真的解成了 PCM")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
