# -*- coding: utf-8 -*-
"""
端到端联调脚本：在本机同时拉起接收端与发送端，跑完一整条链路。

不需要投影仪实机就能验证：采集 → 编码 → CAST 分帧 → 网络 → 重组 → 解码。
渲染器设为空输出（null），因此也可以在无显示环境 / CI 里跑。

用法：
    tools/pyenv/Scripts/python.exe tools/e2e_test.py [--seconds 12]
                                   [--resolution native|4k|1440p|1080p]
                                   [--quality lossless|visual|high|balanced|smooth]
                                   [--fps 30] [--chroma 444|420] [--transport tcp|udp]

例（验证 4K 无损这条最苛刻的路径）：
    tools/pyenv/Scripts/python.exe tools/e2e_test.py --resolution 4k --quality lossless \
        --chroma 444 --transport tcp --seconds 12
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "sender"), os.path.join(ROOT, "receiver")):
    sys.path.insert(0, p)


def main() -> int:
    opts = {"--seconds": "12", "--resolution": "1080p", "--quality": "smooth",
            "--fps": "30", "--chroma": "420", "--transport": "tcp"}
    # 同时支持 "--key=value" 与 "--key value" 两种写法
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if "=" in arg:
            k, v = arg.split("=", 1)
            if k in opts:
                opts[k] = v
        elif arg in opts and i + 1 < len(argv):
            opts[arg] = argv[i + 1]
            i += 1
        i += 1
    seconds = float(opts["--seconds"])

    from castlink import ffmpeg as ff
    ffmpeg = ff.find()
    print(f"ffmpeg = {ffmpeg}")
    if not ffmpeg:
        print("找不到 ffmpeg")
        return 2

    from castlink.config import ReceiverConfig, SenderConfig
    from castlink.identity import load_identity
    from castlink_sender.encoders import probe_capabilities
    from castlink_sender.session import CastSession
    from castlink_receiver.server import ReceiverServer

    rcfg = ReceiverConfig.load()
    rcfg.port = 47000
    server = ReceiverServer(ffmpeg, rcfg, log=print, enable_discovery=False)
    server.renderer.null_output = True
    if not server.start():
        print("接收端启动失败")
        return 3

    scfg = SenderConfig.load()
    scfg.resolution_mode = opts["--resolution"]
    scfg.fps = int(opts["--fps"])
    scfg.quality = opts["--quality"]
    scfg.enable_audio = False
    scfg.transport = opts["--transport"]
    scfg.chroma = opts["--chroma"]

    caps = probe_capabilities(ffmpeg, deep=True)
    available = [c.ffmpeg_name for n, c in caps.encoders.items()
                 if c.available and c.ffmpeg_name.startswith("hevc")]
    print("可用 HEVC 编码器:", ", ".join(available) or "无")

    identity = load_identity()
    session = CastSession(scfg, caps, ffmpeg, identity, log=print,
                          on_state=lambda s, t: print(f"[状态] {s} {t}"))
    ok = session.connect_to("127.0.0.1", 47000, "本机联调接收端")
    print("connect ->", ok, session.state, session.message)
    if not ok:
        server.stop()
        return 4

    deadline = time.time() + seconds
    last = 0.0
    while time.time() < deadline:
        time.sleep(0.25)
        if time.time() - last >= 2.0:
            last = time.time()
            s = session.media.stats.sample()
            print(f"  码率 {s['bitrate']:.2f} Mbps | 编码 {s['fps']:.1f} fps | "
                  f"队列 {s['queued']} | 丢弃 {s['dropped']} | RTT {session.rtt_ms:.1f} ms | "
                  f"对端 {session.peer_stats}")

    stats = session.media.stats.sample()
    peer = session.peer_stats or {}
    session.stop(silent=True)
    server.stop()

    print("\n=== 联调结果 ===")
    print("计划:", session.plan)
    print("发送帧:", stats["frames"], "字节:", stats["bytes"])
    print("对端组装帧:", peer.get("assembled"), "组装后帧数:", peer.get("rendered"))
    print("对端丢帧:", peer.get("dropped"), "丢包:", peer.get("lost_packets"))
    ok = stats["frames"] > 5 and peer.get("assembled", 0) > 5
    print("结论:", "链路可用 ✅" if ok else "未收到足够数据 ❌")
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
