# -*- coding: utf-8 -*-
"""
回归测试：接收端再慢，也不能把发送端的采集线程拖死。

## 这条测试守的是什么

最早的实现里，TCP 模式是"宁愿背压也不丢数据"：队列满了就让采集线程 sleep。
于是出现一条慢速通路：

    接收端解码慢 → TCP 收缓冲满 → 窗口关闭 → sendall 阻塞
    → 队列涨到 96 帧 → 采集线程被 sleep 卡住
    → ffmpeg 的 stdout 不再被读走 → 编码器停摆

用户看到的现象就是**画面冻在某一帧再也不动**（"只有静态画面，不能实时同步"），
而不是"掉帧"。投屏类应用必须丢帧保实时，不能背压。

## 判定标准

用一个"只按 1/8 速度消费"的假消费者模拟慢接收端，让生产者按 30fps 跑 5 秒：

* 生产者的实际耗时必须接近 5 秒（被阻塞就会明显更久）；
* 生产者必须一直在产出（总帧数接近 150），不能中途停住；
* 队列必须严格有界；
* 必须发生了丢帧，且**所有关键帧都被放行**（丢帧不能丢到恢复不了）。

用法：python tools/queue_realtime_test.py
"""
from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

from castlink.protocol import (  # noqa: E402
    CODEC_AAC, CODEC_H265, STREAM_AUDIO, STREAM_VIDEO,
)
from castlink_sender.transport import FrameTask, MediaSender, MODE_TCP  # noqa: E402

FPS = 30
SECONDS = 5.0
# 帧大小按真实 1080p 码流取值：普通帧 32KB，关键帧 256KB
# （关键帧大是硬件编码器的常态，也正是旧实现"几千毫秒才收到一块"的来源）
NORMAL_FRAME = 32 * 1024
KEY_FRAME = 256 * 1024
# 每条报文 50ms —— 相当于链路吞吐明显低于编码产出，必然形成积压
PACKET_SLEEP = 0.05


class SlowSocket:
    """假装是条很慢的连接：sendall 会真的睡一会儿，模拟窗口关闭时的阻塞。"""

    def __init__(self, per_packet_sleep: float = PACKET_SLEEP):
        self.sleep = per_packet_sleep
        self.bytes = 0

    def sendall(self, data):
        time.sleep(self.sleep)
        self.bytes += len(data)

    def setsockopt(self, *a):
        pass

    def close(self):
        pass


def main() -> int:
    log = lambda m: None            # noqa: E731  测试里不需要日志噪音
    sender = MediaSender(log=log)
    slow = SlowSocket()
    sender.mode = MODE_TCP
    sender.sock = slow
    congestion = []
    sender.on_congestion = lambda why: congestion.append(why)

    # 只让 MediaSender 的发送线程跑；它会从队列里取数据并"发"到慢 socket
    sender._thread = threading.Thread(target=sender._loop, daemon=True)
    sender._thread.start()

    key_frames_offered = 0
    key_frames_accepted = 0
    accepted = 0
    video_id = 0
    audio_id = 0

    t0 = time.perf_counter()
    deadline = t0 + SECONDS
    n = 0
    while time.perf_counter() < deadline:
        n += 1
        video_id += 1
        is_key = (n % FPS == 0)          # 每 1 秒一个关键帧
        if is_key:
            key_frames_offered += 1
        if sender.put(FrameTask(STREAM_VIDEO, CODEC_H265, video_id,
                                int(n * 33333),
                                b"v" * (KEY_FRAME if is_key else NORMAL_FRAME), is_key),
                      drop_if_full=False):
            accepted += 1
            if is_key:
                key_frames_accepted += 1
        # 音频：AAC 每帧 21.3ms
        if n % 2 == 0:
            audio_id += 1
            sender.put(FrameTask(STREAM_AUDIO, CODEC_AAC, audio_id,
                                 int(n * 21333), b"a" * 256), drop_if_full=True)
        # 严格按 30fps 的节奏产出 —— 生产者应该始终跟得上自己的钟
        target = t0 + n / FPS
        slack = target - time.perf_counter()
        if slack > 0:
            time.sleep(slack)

    elapsed = time.perf_counter() - t0
    sender.stop()

    sent = sender.stats.frames_total
    dropped = sender.stats.dropped
    dropped_v = sender.stats.dropped_video
    dropped_a = sender.stats.dropped_audio
    queue_left = sender.queue_size

    print("模拟：链路吞吐明显低于编码产出（普通帧 %dKB / 关键帧 %dKB，每报文 %dms）"
          % (NORMAL_FRAME // 1024, KEY_FRAME // 1024, int(PACKET_SLEEP * 1000)))
    print("     生产者按 %dfps 跑 %.0f 秒，接收端消费不过来" % (FPS, SECONDS))
    print()
    print("  生产者实际耗时     : %.3f 秒（基准 %.1f 秒，被阻塞就会明显更长）"
          % (elapsed, SECONDS))
    print("  产出视频帧         : %d（期望 ≈ %d）" % (n, int(SECONDS * FPS)))
    print("  成功入队           : %d 帧" % accepted)
    print("  丢弃               : %d（视频 %d / 音频 %d）" % (dropped, dropped_v, dropped_a))
    print("  真正发出           : %d 帧（%d 字节）" % (sent, slow.bytes))
    print("  结束时队列残留     : %d（上限 %d+%d）"
          % (queue_left, MediaSender.QUEUE_MAX_VIDEO_RELIABLE, MediaSender.QUEUE_MAX_AUDIO))
    print("  关键帧             : 提出 %d / 放行 %d" % (key_frames_offered, key_frames_accepted))
    print("  拥塞回调           : %d 次" % len(congestion))
    print()

    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✔ " if cond else "  ✘ ") + msg)
        if not cond:
            ok = False

    check(elapsed < SECONDS + 1.0,
          "生产者没有被接收端拖慢（未被背压阻塞）")
    check(n >= int(SECONDS * FPS) - 2,
          "生产者始终按时产出，没有中途停摆")
    check(queue_left <= MediaSender.QUEUE_MAX_VIDEO_RELIABLE + MediaSender.QUEUE_MAX_AUDIO,
          "队列严格有界（旧实现的 96 帧无界队列就是冻结的根源）")
    check(dropped > 0, "确实发生了丢帧（丢帧是实时投屏的正确行为，不是缺陷）")
    check(key_frames_accepted == key_frames_offered,
          "所有关键帧都被放行（丢帧不会丢到无法恢复）")
    check(len(congestion) > 0, "拥塞被上报，可供自适应降档使用")

    print()
    if ok:
        print("✅ 通过：接收端再慢也不会把发送端冻住，画面会持续更新（只是掉帧）")
        return 0
    print("❌ 失败：发送端存在被背压拖死的风险，画面会冻结")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
