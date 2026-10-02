# -*- coding: utf-8 -*-
"""
用**打包后的真实产物**做端到端验证。

前面那些测试跑的都是源码，证明不了"交付出去的那个 exe 真的能用"——
本项目已经吃过一次亏：APK 被改名成 .apk1，用户装到的还是旧包，
所有排查都建立在错误的前提上。

这个测试启动 `dist/win_receiver_build/CastLink-Receiver-Win/CastLink-Receiver-Win.exe`
（--render null，无头），再驱动一个真实的 CastSession 连它两轮，断言：
  * 接收端报回来的 stats 里视频帧在涨
  * **接收端报回来 audioStatus=OK 且 audioDec > 0** —— 即音频真的被解成了 PCM
  * 停止后能再次连上并恢复（本轮修复的重点）

用法：python tools/packaged_e2e_test.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

sys.stdout.reconfigure(line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

from castlink.config import SenderConfig                        # noqa: E402
from castlink.identity import load_identity                     # noqa: E402
from castlink_sender.encoders import probe_capabilities         # noqa: E402
from castlink_sender.session import CastSession                 # noqa: E402

FFMPEG = os.path.join(ROOT, "tools", "ffmpeg.exe")
RECV_EXE = os.path.join(ROOT, "dist", "win_receiver_build",
                        "CastLink-Receiver-Win", "CastLink-Receiver-Win.exe")
CTRL_PORT = 47000


def port_free(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def wait_listening(port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    if not os.path.isfile(RECV_EXE):
        print("❌ 找不到打包产物:", RECV_EXE)
        return 2
    print("被测产物:", RECV_EXE,
          f"({os.path.getsize(RECV_EXE)} 字节)")
    if not port_free(CTRL_PORT):
        print(f"❌ 端口 {CTRL_PORT} 被占用，请先关掉其它 CastLink 接收端")
        return 2

    logf = open(os.path.join(ROOT, ".tmp", "packaged_e2e_recv.log"), "w",
                encoding="utf-8", errors="replace")
    proc = subprocess.Popen([RECV_EXE, "--render", "null", "--pin", "000000",
                             "--name", "打包产物验证"],
                            stdout=logf, stderr=subprocess.STDOUT)
    try:
        if not wait_listening(CTRL_PORT):
            print("❌ 打包的接收端没有起来（控制端口未监听）")
            return 3
        print("✅ 打包接收端已在监听", CTRL_PORT)

        cfg = SenderConfig()
        cfg.resolution_mode = "1080p"
        cfg.fps = 30
        cfg.quality = "smooth"
        cfg.transport = "tcp"
        cfg.enable_audio = True
        cfg.adaptive = False
        cfg.remembered_peers = {}
        caps = probe_capabilities(FFMPEG, deep=True)
        session = CastSession(cfg, caps, FFMPEG, load_identity("验证机"),
                              log=lambda m: None)

        rounds = []
        for i in (1, 2):
            print()
            print("=" * 68)
            print(f"第 {i} 轮（打包产物）")
            print("=" * 68)
            t0 = time.time()
            ok = session.connect_to("127.0.0.1", CTRL_PORT, "打包接收端")
            print(f"connect_to() -> {ok} 状态={session.state}")
            if not ok:
                rounds.append(None)
                continue
            while time.time() - t0 < 5.0:
                time.sleep(0.5)
            peer = dict(session.peer_stats or {})
            print(f"  接收端回报：{ {k: peer.get(k) for k in
                                     ('assembled', 'rendered', 'fps',
                                      'audioStatus', 'audioRecv', 'audioDec')} }")
            rounds.append(peer)
            session.stop()
            time.sleep(1.0)
        session.shutdown()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        logf.close()

    print()
    print("=" * 68)
    ok = True
    for i, p in enumerate(rounds, 1):
        if not p:
            print(f"第 {i} 轮：❌ 没连上")
            ok = False
            continue
        v = int(p.get("assembled") or 0)
        ad = int(p.get("audioDec") or 0)
        st = p.get("audioStatus")
        good = v > 0 and ad > 0 and st == "OK"
        print(f"第 {i} 轮：视频 {v} 帧 / 音频解出 {ad} 帧 / 状态 {st} "
              f"{'✅' if good else '❌'}")
        ok = ok and good
    print("=" * 68)
    print("结论：", "✅ 打包产物本身可反复投屏，且音频真的出声" if ok
          else "❌ 打包产物有问题（源码测试通过但产物不行 —— 优先怀疑打包漏了依赖）")
    return 0 if ok else 1


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, ".tmp"), exist_ok=True)
    raise SystemExit(main())
