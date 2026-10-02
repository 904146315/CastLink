#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
重启风暴回归测试 —— 专治「画面不动 + 发送端疯狂重启采集进程」这个死循环。

真机日志（v1.0.9）里那条链条是这样的：

    投影仪请求关键帧（starved）
    采集进程退出 code=0          ← 我们自己刚杀掉的进程，被当成崩溃上报了
    尝试降级到编码器 libx265      ← 于是白白消耗一次编码器降级
    ... 20 秒内重复十几次 ...

三个缺陷叠加：
  1. `VideoPipeline.start()` 里 `_gen += 1` 排在 `self.stop()` **之后**，
     旧读取线程在"进程已死、代号未变"的窗口里完成退出上报；
  2. `_on_video_exit` 把这条过期上报当成故障，降级编码器；
  3. `_on_keyframe_request` 为了满足"要一个关键帧"，把整条流水线重建了一遍——
     而关键帧周期本来就只有 0.5 秒，**重启(1.5~2s)比等着还慢**。

本测试逐条钉住这三件事。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sender"))

from castlink_sender.capture import VideoPipeline          # noqa: E402
from castlink_sender.session import CastSession            # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [!!] %s %s" % (name, extra))


# --------------------------------------------------------------------------
# 一个只睡觉的子进程，用来冒充"正在跑、不会被自然结束"的 ffmpeg
def _sleeper(sec=30):
    return [sys.executable, "-c", "import time; time.sleep(%d)" % sec]


def test_pipeline_generation_isolation():
    print("\n== 1. 换进程时，旧代号的退出不能被上报 ==")
    exits = []
    p = VideoPipeline(sys.executable, log=lambda m: None)
    ok = p.start(_sleeper(), lambda *a: None,
                 on_exit=lambda c: exits.append(("gen1", c)))
    check("首次启动成功", ok)
    time.sleep(0.4)

    # 再启动一次 —— 内部会 terminate 掉上面那个进程
    ok2 = p.start(_sleeper(), lambda *a: None,
                  on_exit=lambda c: exits.append(("gen2", c)))
    check("二次启动成功", ok2)
    time.sleep(0.6)

    leaked = [e for e in exits if e[0] == "gen1"]
    check("旧代号的退出**没有**被上报（这就是那条 code=0 的来源）",
          not leaked, "实际收到: %r" % (exits,))
    check("此刻也不该有任何退出事件（新进程还活着）", not exits, repr(exits))

    p.stop()
    time.sleep(0.3)


def test_generation_bumped_before_stop():
    """
    确定性版本：直接看"调 stop() 的那一刻，代号已经加过了没有"。

    上面那条断言依赖线程调度，调度恰好偏向主线程时**即使代码是错的也会过**。
    这里不靠运气 —— 把 stop() 的时刻钉下来看代号，顺序错了一目了然。
    """
    print("\n== 1b. 代号必须**在 stop() 之前**自增（确定性检查）==")

    class Spy(VideoPipeline):
        def __init__(self, *a, **k):
            VideoPipeline.__init__(self, *a, **k)
            self.gen_when_stop_called = None

        def stop(self, wait=2.0):
            self.gen_when_stop_called = self._gen
            VideoPipeline.stop(self, wait)

    p = Spy(sys.executable, log=lambda m: None)
    p.start(_sleeper(), lambda *a: None, on_exit=lambda c: None)
    gen_after_first = p._gen
    time.sleep(0.2)

    p.gen_when_stop_called = None
    p.start(_sleeper(), lambda *a: None, on_exit=lambda c: None)
    check("第二次 start 调 stop() 时，代号已经加过了",
          p.gen_when_stop_called == gen_after_first + 1,
          "stop() 时代号=%r，应为 %d" % (p.gen_when_stop_called,
                                        gen_after_first + 1))
    p.stop()
    time.sleep(0.2)


def test_pipeline_real_exit_reported():
    print("\n== 2. 真实退出仍然要能上报（别把功能修没了）==")
    exits = []
    p = VideoPipeline(sys.executable, log=lambda m: None)
    # 这个进程会立刻自己结束 —— 属于"真·意外退出"
    p.start([sys.executable, "-c", "pass"], lambda *a: None,
            on_exit=lambda c: exits.append(c))
    for _ in range(40):
        if exits:
            break
        time.sleep(0.05)
    check("真实退出被上报", bool(exits), "实际: %r" % (exits,))
    p.stop()
    time.sleep(0.2)


# --------------------------------------------------------------------------
class _FakeSession(object):
    """只借用 CastSession 的两个方法，避免构造真实会话（会去探测 ffmpeg）。"""

    _gop_seconds = CastSession._gop_seconds
    _on_keyframe_request = CastSession._on_keyframe_request

    class _Plan(object):
        gop_seconds = 0.5

    def __init__(self, plan=None):
        self.plan = plan or _FakeSession._Plan()
        self.restarts = []
        self._video_wall0_ns = 0
        self._last_key_sent_ns = 0

    def log(self, m):
        pass

    def request_idr_pipe(self):
        self.restarts.append(time.monotonic_ns())


def test_keyframe_request_does_not_restart():
    print("\n== 3. 刚出过关键帧时，请求关键帧**不该**重启流水线 ==")
    s = _FakeSession()
    now = time.monotonic_ns()
    s._video_wall0_ns = now - 10_000_000_000          # 采集进程起了 10 秒
    s._last_key_sent_ns = now - 200_000_000           # 0.2 秒前刚出过 IDR
    s._on_keyframe_request("starved")
    check("0.2s 前刚有 IDR → 不重启（GOP 只有 0.5s，等到就行）",
          not s.restarts, repr(s.restarts))

    s2 = _FakeSession()
    s2._video_wall0_ns = now - 10_000_000_000
    s2._last_key_sent_ns = now - 900_000_000          # 0.9 秒前
    s2._on_keyframe_request("stall")
    check("0.9s 前有 IDR → 仍不重启（门槛是 max(2×GOP, 3s)）",
          not s2.restarts, repr(s2.restarts))


def test_keyframe_request_restarts_when_truly_stuck():
    print("\n== 4. 真的很久没出关键帧时，才允许重建 ==")
    s = _FakeSession()
    now = time.monotonic_ns()
    s._video_wall0_ns = now - 60_000_000_000
    s._last_key_sent_ns = now - 20_000_000_000        # 20 秒没出 IDR
    s._on_keyframe_request("stall")
    check("20s 没出 IDR → 重建（说明 -g 没生效，采集卡住了）",
          len(s.restarts) == 1, "实际 %d 次" % len(s.restarts))

    # 节流：3 秒内再请求一次不该再来一遍
    s._on_keyframe_request("stall")
    check("3 秒内重复请求被节流", len(s.restarts) == 1,
          "实际 %d 次" % len(s.restarts))


def test_startup_grace():
    print("\n== 5. 起流宽限期：刚起流就来要关键帧，不许动流水线 ==")
    s = _FakeSession()
    now = time.monotonic_ns()
    s._video_wall0_ns = now - 500_000_000             # 采集进程刚起 0.5 秒
    s._last_key_sent_ns = 0                           # 还没出过任何关键帧
    s._on_keyframe_request("starved")
    check("起流 0.5s、尚无 IDR → 不重启（编码器正在初始化）",
          not s.restarts, repr(s.restarts))

    s2 = _FakeSession()
    s2._video_wall0_ns = now - 12_000_000_000         # 起了 12 秒
    s2._last_key_sent_ns = 0
    s2._on_keyframe_request("starved")
    check("起流 12s、仍然一个 IDR 都没有 → 重建（真的卡住了）",
          len(s2.restarts) == 1, "实际 %d 次" % len(s2.restarts))


def test_gop_from_plan():
    print("\n== 6. GOP 周期从 plan 读取，取不到时兜底 0.5s ==")

    class NoGop(object):
        pass

    s = _FakeSession(plan=NoGop())
    check("plan 没有 gop_seconds → 兜底 0.5", abs(s._gop_seconds() - 0.5) < 1e-9)
    s2 = _FakeSession(plan=type("P", (), {"gop_seconds": 0.0})())
    check("gop_seconds=0 → 兜底 0.5", abs(s2._gop_seconds() - 0.5) < 1e-9)
    s3 = _FakeSession(plan=type("P", (), {"gop_seconds": 1.0})())
    check("gop_seconds=1.0 → 原样返回", abs(s3._gop_seconds() - 1.0) < 1e-9)


if __name__ == "__main__":
    test_pipeline_generation_isolation()
    test_generation_bumped_before_stop()
    test_pipeline_real_exit_reported()
    test_keyframe_request_does_not_restart()
    test_keyframe_request_restarts_when_truly_stuck()
    test_startup_grace()
    test_gop_from_plan()
    print("\n===== 通过 %d / 失败 %d =====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
