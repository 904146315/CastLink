# -*- coding: utf-8 -*-
"""
回归测试：「投影仪解不动当前分辨率」这条自愈链路。

## 这条测试守的是什么

用户报的症状是**「手机端正常，投影仪上只有声音、画面冻住」**。同一个 APK，
差别在规格：投影仪面板是 4K，协商出来的就是 **3840x2160 HEVC**；
手机面板小，阶梯被压到 **1280x720** —— 两者解码负载差了 9 倍像素量。
投影仪 SoC 声称支持 4K HEVC、实际喂得进去出不来的情况很常见。

而旧的实现对此**毫无出路**，三处盲区叠在一起：

1. 接收端看门狗只盯"帧有没有收到"。这种故障下帧一直在正常到达，它永远不触发；
2. 解码器的渲染线程一遇异常就永久退出，且不会重建 —— 直接永久冻屏；
3. 发送端的自适应降档**只降帧率与画质、从不改分辨率**（怕中途改分辨率
   会让解码器黑屏）。于是"4K 解不动"这件事在它手里永远解决不了。

v1.0.9 加了一条能改分辨率的通道：接收端判定"数据在进、解码器不出图"两次之后，
把分辨率降一级写进设置（下次上报能力自然就低了），并发
`{"t":"idr","action":"lower"}` 请发送端按新规格重连。

## 判定标准

**A. 跨语言常量必须一致。**
接收端 `DecoderCaps.LADDER` 与发送端 `capture._RES_LADDER` 是两份独立的硬编码。
它们一旦不一致，降档就会要求一个发送端做不出来的尺寸。

**B. 两端的协议字面量必须一致。**
接收端发的是 `"action":"lower"`，发送端必须比的就是 `"lower"` ——
这类"一边改了另一边没改"的错最难查，因为它不报错，只是**永远不生效**。

**C. 关键顺序：先写天花板，再发消息。**
反过来的话，发送端收到消息立刻重连，而接收端的天花板还没落盘，
新会话里 `hello_ack` 报的还是 4K —— 重连一次毫无效果还白白卡两秒。

**D. 发送端收到 `action=lower` 必须真的重连，而不是当成普通的关键帧请求。**

**E. 重连必须节流**，否则两端会互相放大成重连风暴。

用法：python tools/decode_stall_test.py
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- A. 阶梯一致
def test_ladder_agreement():
    print("\nA. 分辨率阶梯：接收端与发送端必须一致")
    java = read("receiver/android/java/com/castlink/receiver/DecoderCaps.java")
    m = re.search(r"LADDER\s*=\s*\{(.*?)\};", java, re.S)
    check("能在 DecoderCaps.java 里找到 LADDER", m is not None)
    if not m:
        return
    jl = [(int(a), int(b)) for a, b in re.findall(r"\{(\d+),\s*(\d+)\}", m.group(1))]

    from castlink_sender.capture import _RES_LADDER
    py = [(int(a), int(b)) for a, b in _RES_LADDER]
    check("两侧阶梯完全相同", jl == py, f"java={jl}  py={py}")
    check("阶梯是降序（stepDown 依赖这个前提）",
          all(jl[i][0] >= jl[i + 1][0] for i in range(len(jl) - 1)), str(jl))


# ---------------------------------------------------------------- B/C. 协议与顺序
def test_protocol_and_order():
    print("\nB. 协议字面量：接收端发的与发送端收的必须是同一个词")
    java = read("receiver/android/java/com/castlink/receiver/StreamSession.java")
    py = read("sender/castlink_sender/session.py")
    check('接收端发送 "action" -> "lower"', '"action", "lower"' in java)
    check('发送端比较 action == "lower"', 'msg.get("action") == "lower"' in py)
    check('接收端发送 reason "decode_stall"', '"reason", "decode_stall"' in java)
    check("接收端带上了降档所需的动作字段",
          '"t", "idr", "reason", "decode_stall", "action", "lower"' in java)

    print("\nC. 顺序：先落下天花板，再发重连请求")
    i_ceil = java.find("Prefs.setCapCeiling(next[0], next[1]);")
    i_send = java.find('"action", "lower"')
    check("能找到写天花板的那一行", i_ceil > 0)
    check("能找到发重连请求的那一行", i_send > 0)
    check("写天花板在发消息之前",
          i_ceil > 0 and i_send > 0 and i_ceil < i_send,
          f"setCapCeiling@{i_ceil}  send@{i_send}")

    print("\nC2. 解码器必须跟着 Surface 走，且渲染线程不能因异常退出")
    vd = read("receiver/android/java/com/castlink/receiver/VideoDecoder.java")
    check("投喂前核对 Surface 身份", "cur != boundSurface" in vd)
    check("Surface 失效/更换时释放解码器重配",
          "releaseCodec(\"Surface 已更换\")" in vd and "releaseCodec(\"Surface 已失效\")" in vd)
    # 旧代码在这里是 break：渲染线程一死就再也没有人取输出，画面永久冻结
    body = vd[vd.find("private void renderLoop()"):]
    body = body[:body.find("\n    private static void sleepQuietly")]
    check("renderLoop 中不再出现 break（旧版正是它导致永久冻屏）",
          "break;" not in body, "见 renderLoop 注释")
    check("renderLoop 在 codec 为 null 时等待而不是退出", "sleepQuietly(5);" in body)


# ---------------------------------------------------------------- D/E. 发送端行为
class _FakeCfg:
    pin = "000000"
    transport = "tcp"
    quality = "smooth"
    fps = 24
    enable_audio = False
    encoder = "auto"
    chroma = "420"
    adaptive = False
    mute_local = True
    capture_mouse = False
    resolution_mode = "native"
    canvas_4k = False
    bitrate_cap_mbps = 0
    remembered_peers = {}

    def save(self):
        pass


class _FakeCtrl:
    """够用的假控制通道：按脚本吐消息，其余时候返回 None。"""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.connected = True
        self.sent = []

    def recv(self, timeout=1.0):
        if self._msgs:
            return self._msgs.pop(0)
        time.sleep(0.05)
        return None

    def send(self, obj):
        self.sent.append(obj)
        return True

    def close(self):
        self.connected = False


class _Plan:
    out_w = 3840
    out_h = 2160
    fps = 24


def _make_session():
    """构造一个不碰真实 socket / 子进程的 CastSession。"""
    import castlink_sender.session as S

    class _Stub:
        def __init__(self, *a, **k):
            self.last_error = ""

        def start(self, *a, **k):
            return True

        def stop(self, *a, **k):
            pass

        def close(self, *a, **k):
            pass

        def restore(self):
            pass

    S.detect_capture_method = lambda ffmpeg: "gdigrab"
    S.VideoPipeline = _Stub
    S.AudioPipeline = _Stub
    S.LocalAudioMuter = _Stub

    class _Caps:
        def hevc_candidates(self, want_444=False):
            return [(1, "hevc_qsv")]

        def h264_candidates(self):
            return [(1, "h264_qsv")]

    s = S.CastSession(_FakeCfg(), _Caps(), "ffmpeg", {"id": "x", "name": "PC"},
                      log=lambda *_: None)
    s.state = S.STATE_STREAMING
    s.host, s.port, s._label = "10.0.0.9", 47000, "N1S 4K"
    s.plan = _Plan()
    return s


def test_sender_renegotiate():
    print("\nD. 发送端：action=lower 必须触发重连（而不是只重出关键帧）")
    s = _make_session()
    calls = []
    s.connect_to = lambda host, port, label="", pin="": calls.append((host, port, label)) or True
    idr_calls = []
    s.request_idr_pipe = lambda: idr_calls.append(1)

    s.ctrl = _FakeCtrl([{"t": "idr", "reason": "decode_stall", "action": "lower"},
                        {"t": "idr", "reason": "decode_stall", "action": "lower"}])
    t = threading.Thread(target=s._control_rx, daemon=True)
    t.start()
    deadline = time.time() + 6.0
    while time.time() < deadline and not calls:
        time.sleep(0.05)
    check("收到了重连请求", bool(calls), f"calls={calls}")
    check("重连到原来的地址", calls and calls[0][0] == "10.0.0.9", str(calls[:1]))
    check("没有把它当成普通关键帧请求", idr_calls == [], f"idr_calls={idr_calls}")

    print("\nE. 重连必须节流（8 秒内只重连一次）")
    time.sleep(0.4)
    check("紧跟着的第二条消息没有再次重连", len(calls) == 1, f"calls={len(calls)}")

    print("\nF. 新会话不得被上一代控制线程误杀（_ctrl_gen 换代）")
    s2 = _make_session()
    s2.ctrl = _FakeCtrl([])
    t2 = threading.Thread(target=s2._control_rx, daemon=True)
    t2.start()
    time.sleep(0.3)
    check("旧接收线程在运行", t2.is_alive())
    s2._ctrl_gen += 1          # connect_to 换代
    t2.join(timeout=2.0)
    check("换代后旧线程自行退休", not t2.is_alive())


def main():
    print("=" * 72)
    print("解码停滞自愈链路回归")
    print("=" * 72)
    test_ladder_agreement()
    test_protocol_and_order()
    test_sender_renegotiate()
    print("\n" + "=" * 72)
    if FAILS:
        print("失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
