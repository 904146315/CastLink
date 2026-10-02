# -*- coding: utf-8 -*-
"""
回归测试：端到端延迟必须被**钉死在一个固定量级**，而不是随缓冲慢慢漂。

## 这条测试守的是什么

用户报的原话是"画面有 1s 钟左右的延迟，声音也是有延迟"。核对下来，
1 秒这个数字不是巧合，而是三处"写死的字节数"叠出来的：

  * MediaSender.QUEUE_MAX_VIDEO_RELIABLE = 24 帧，24fps 下正好 **1.0 秒**；
  * SO_SNDBUF = 512KB，在 4 Mbps 下正好 **1.0 秒**；
  * 接收端 TCP_RCVBUF = 1MB，在 4 Mbps 下 **2.0 秒**。

所以本测试锁死三件事：

  1. **队列深度**必须保持在"亚秒"量级（帧数 × 帧间隔 < 0.3 秒）；
  2. **socket 缓冲**必须按"时间预算"换算，而非写死字节数 ——
     同一个字节数在 4 Mbps 和 100 Mbps 下延迟差 25 倍，这是本质错误；
  3. 编码器吐出来的帧若已经在流水线里滞留超过 LATENCY_CAP_US，
     必须被丢掉并进入"丢到下一个关键帧"状态（否则实时性无从保证）。

## 为什么用断言常量而不是"跑一遍看感觉"

延迟这种东西没有短期可观测的报错 —— 它只是"慢"。把数值写成断言，
任何一次"顺手把队列调大一倍"的改动都会立刻红。

用法：python tools/latency_budget_test.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

from castlink.protocol import CODEC_AAC, CODEC_H265, STREAM_AUDIO, STREAM_VIDEO  # noqa: E402
from castlink_sender.transport import FrameTask, MediaSender  # noqa: E402

# 各档位下"一帧占多少毫秒"—— 用来把队列深度换算成时间
FPS_CHOICES = (60, 30, 24)
# 队列允许占用的最长时间（秒）。0.3 秒是"看得见但可以接受"的门槛。
QUEUE_BUDGET_S = 0.30
# 用户实际用的那一档
USER_FPS = 24


def main() -> int:
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✔ " if cond else "  ✘ ") + msg)
        if not cond:
            ok = False

    print("=== 1) 发送队列深度（帧数 → 时间）===")
    for name, limit in (("低延迟", MediaSender.QUEUE_MAX_VIDEO),
                        ("可靠(TCP)", MediaSender.QUEUE_MAX_VIDEO_RELIABLE)):
        worst = limit / USER_FPS
        print("  %-10s %2d 帧  最坏积压 %.0f ms @%dfps" % (name, limit, worst * 1000, USER_FPS))
        check(worst <= QUEUE_BUDGET_S,
              "%s视频队列最坏积压 %.0fms ≤ %.0fms（旧值 24 帧 @24fps = 1000ms）"
              % (name, worst * 1000, QUEUE_BUDGET_S * 1000))
    aq = MediaSender.QUEUE_MAX_AUDIO
    a_ms = aq * 1024 / 48000 * 1000      # AAC 每帧 1024 采样
    print("  %-10s %2d 帧  最坏积压 %.0f ms（AAC 1024 样点/帧）" % ("音频", aq, a_ms))
    check(a_ms <= 350,
          "音频队列最坏积压 %.0fms ≤ 350ms（旧值 64 帧 ≈ 1365ms）" % a_ms)

    print()
    print("=== 2) 发送缓冲按时间预算换算，而不是写死字节数 ===")
    s = MediaSender(log=lambda m: None)
    rows = []
    for mbps in (4.0, 20.0, 45.0, 100.0):
        s.set_bitrate(mbps)
        n = s.sndbuf_bytes()
        rows.append((mbps, n, n * 8 / 1e6 / mbps * 1000))
        print("  %6.1f Mbps → 缓冲 %7.0f KB  实占 %.0f ms" % (mbps, n / 1024, rows[-1][2]))
    check(all(60 <= r[2] <= 300 for r in rows),
          "4 个码率档下，发送缓冲占用的时间都落在 60~300ms（不再出现 1000ms）")
    check(rows[0][1] < 512 * 1024,
          "低码率档的缓冲比旧的固定 512KB 小（这是把 1 秒延迟砍掉的关键）")
    check(rows[-1][1] > rows[0][1],
          "高码率档自动放大缓冲，不会因小缓冲限住吞吐")

    print()
    print("=== 3) 延迟上限：滞留过久的帧必须被丢弃 ===")
    s2 = MediaSender(log=lambda m: None)
    drops = []
    s2.on_congestion = lambda why: drops.append(why)
    cap_us = s2.LATENCY_CAP_US

    fresh = FrameTask(STREAM_VIDEO, CODEC_H265, 1, 0, b"x" * 128, False,
                      lag_us=0)
    check(s2.put(fresh, drop_if_full=True), "新鲜的普通帧正常入队")

    stale = FrameTask(STREAM_VIDEO, CODEC_H265, 2, 0, b"x" * 128, False,
                      lag_us=cap_us + 50_000)
    check(not s2.put(stale, drop_if_full=True),
          "滞留 %.0fms（> 上限 %.0fms）的普通帧被丢弃" % ((cap_us + 50_000) / 1000, cap_us / 1000))
    check(s2.stats.dropped_late == 1, "记到了 dropped_late 计数")

    after = FrameTask(STREAM_VIDEO, CODEC_H265, 3, 0, b"x" * 128, False, lag_us=0)
    check(not s2.put(after, drop_if_full=True),
          "丢弃后进入「丢到下一个关键帧」状态，后续普通帧继续丢（保解码链完整）")
    key = FrameTask(STREAM_VIDEO, CODEC_H265, 4, 0, b"x" * 128, True, lag_us=0)
    check(s2.put(key, drop_if_full=True), "关键帧照常放行 —— 接收端才有恢复的机会")
    check(not s2._drop_video_until_key, "关键帧到达后退出丢帧状态")
    check(len(drops) > 0, "拥塞/滞后被上报，可供自适应降档使用")

    print()
    print("=== 4) 音频队列丢旧保新（不是拒收新帧）===")
    s3 = MediaSender(log=lambda m: None)
    for i in range(MediaSender.QUEUE_MAX_AUDIO):
        s3.put(FrameTask(STREAM_AUDIO, CODEC_AAC, i + 1, 0, b"a" * 32), drop_if_full=True)
    newest = FrameTask(STREAM_AUDIO, CODEC_AAC, 999, 0, b"a" * 32)
    s3.put(newest, drop_if_full=True)
    ids = []
    while True:
        try:
            t = s3._qa.get_nowait()
        except Exception:
            break
        if t is not None:
            ids.append(t.frame_id)
    check(len(ids) == MediaSender.QUEUE_MAX_AUDIO and ids[-1] == 999,
          "队满时挤掉最老的一帧、最新一帧一定在队尾（旧实现会拒收新帧 → 声音越拖越晚）")

    print()
    print("=== 5) 配置默认值 ===")
    from castlink.config import SenderConfig  # noqa: E402
    cfg = SenderConfig()
    check(cfg.mute_local is True,
          "「投射系统声音时静音本机」默认开启（用户要的就是只在投影仪出声）")

    print()
    if ok:
        print("✅ 通过：延迟被钉在固定量级，且有回归保护")
        return 0
    print("❌ 失败：延迟预算被破坏")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
