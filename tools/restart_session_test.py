# -*- coding: utf-8 -*-
"""
回归测试：同一个 MediaSender 被「开始 -> 停止 -> 再开始」复用之后还能不能发数据。

背景（用户实测症状）
--------------------
"第一次启动发送端可以正常投屏画面，但是点击停止投屏之后再次点击开始投屏，
 则会无法正常传输画面，需要重启程序才行。"

根因：MediaSender.stop() 往两条队列里各塞一个 None 当作哨兵唤醒发送线程，
但 start() **没有重建队列**，残留的 None 会在下一次 start() 被新线程立刻取到，
于是 _loop 撞上 `if task is None: break` 当场退出 —— 发送线程一秒都没活，
所有帧只进不出。发送端界面一切"正常"（编码器在跑、码率在涨），接收端颗粒无收。

本测试刻意模拟用户的操作序列：start -> 发几帧 -> stop -> start -> 再发几帧，
断言第二轮依然有字节真的被写到 socket 上。
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
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

from castlink import protocol as P                      # noqa: E402
from castlink_sender.transport import MediaSender, FrameTask   # noqa: E402


def make_pair():
    """返回 (sender 侧 socket, receiver 侧 socket)。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    cli = socket.create_connection(("127.0.0.1", port), timeout=3.0)
    conn, _ = srv.accept()
    conn.settimeout(0.3)
    srv.close()
    return cli, conn


def drain(sock, seconds):
    """收下 seconds 秒内到达的所有字节，返回 (字节数, 收到的帧数)。"""
    total = 0
    frames = set()
    end = time.time() + seconds
    while time.time() < end:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        except Exception:
            break
        if not chunk:
            break
        total += len(chunk)
        frames.add(chunk[8:12])   # 只看第一片的 frame_id 就够判断有没有在动
    return total, len(frames)


def push_frames(sender: MediaSender, n: int, tag: bytes, keyfirst: bool = True):
    for i in range(n):
        payload = tag * 4096
        sender.put(FrameTask(P.STREAM_VIDEO,
                             P.CODEC_H265, i + 1, i * 16000, payload,
                             keyframe=(keyfirst and i == 0)), drop_if_full=False)


def main() -> int:
    ok = True
    sender = MediaSender(log=lambda m: print("   [log]", m))
    cli, conn = make_pair()
    sender.attach_tcp(cli)

    # ---------------- 第一轮：正常投屏 ----------------
    print("=" * 66)
    print("第一轮：start -> 发 20 帧")
    print("=" * 66)
    sender.start()
    push_frames(sender, 20, b"A")
    time.sleep(0.6)
    got1, frames1 = drain(conn, 0.8)
    print(f"接收端收到 {got1} 字节 / {frames1} 个不同帧号")
    if got1 <= 0:
        print("❌ 第一轮就没数据 —— 测试环境本身有问题")
        return 2
    print("✅ 第一轮正常")

    # 把 socket 换掉（真实场景里 stop 会关掉旧 socket，重启时新建一条）
    sender.stop()
    print(f"stop() 之后：发送线程存活 = {bool(sender._thread)}")
    try:
        cli.close()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    cli, conn = make_pair()
    sender.attach_tcp(cli)

    # ---------------- 第二轮：模拟用户"再次点击开始投屏" ----------------
    print()
    print("=" * 66)
    print("第二轮：再次 start -> 发 20 帧")
    print("=" * 66)
    sender.start()
    time.sleep(0.35)      # 给线程一点时间 —— 有 bug 的话它已经退出了
    alive = sender._thread is not None and sender._thread.is_alive()
    print(f"start() 0.35 秒后发送线程是否存活 = {alive}")

    push_frames(sender, 20, b"B")
    time.sleep(0.6)
    got2, frames2 = drain(conn, 0.8)
    print(f"接收端收到 {got2} 字节 / {frames2} 个不同帧号")

    if not alive:
        print("❌ 发送线程在第二轮一开始就退出了（None 哨兵残留）")
        ok = False
    if got2 <= 0:
        print("❌ 第二轮一个字节都没发出去")
        ok = False
    else:
        print("✅ 第二轮数据正常送出")

    # ---------------- 第三轮：再来一次，确认可反复重启 ----------------
    print()
    print("=" * 66)
    print("第三轮：第三次 start -> 发 20 帧")
    print("=" * 66)
    sender.stop()
    try:
        cli.close()
        conn.close()
    except Exception:
        pass
    cli, conn = make_pair()
    sender.attach_tcp(cli)
    sender.start()
    push_frames(sender, 20, b"C")
    time.sleep(0.6)
    got3, frames3 = drain(conn, 0.8)
    print(f"接收端收到 {got3} 字节 / {frames3} 个不同帧号")
    if got3 <= 0:
        print("❌ 第三轮没数据")
        ok = False
    else:
        print("✅ 第三轮正常")

    sender.close()
    try:
        cli.close()
        conn.close()
    except Exception:
        pass

    print()
    print("=" * 66)
    print("结论：", "✅ 全部通过（可反复停止/开始）" if ok else "❌ 存在复用缺陷")
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
