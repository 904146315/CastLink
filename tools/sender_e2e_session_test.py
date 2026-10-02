# -*- coding: utf-8 -*-
"""
发送端会话链路端到端回归测试（不需要投影仪）。

用一个"假投影仪"（纯 Python，实现 CAST/1 的接收侧握手 + 媒体收帧）来驱动
真实的 CastSession，覆盖两件用户实测到的事：

  1. **停止后再开始还能不能投屏。** 用户原话："第一次启动发送端可以正常投屏画面，
     但是点击停止投屏之后再次点击开始投屏，则会无法正常传输画面，需要重启才行。"
     —— 本测试连续 connect -> stop -> connect 三轮，每轮都断言真的收到了视频帧。

  2. **音频有没有真的发出去。** 断言媒体通道上出现 STREAM_AUDIO 的帧，
     并打印音频帧率与码率。只有"发得出去"才谈得上"收得到"。

  3. **流水线延迟有没有被缓冲堆起来。** 发送端记下每帧交给队列的时刻，
     假投影仪记下每帧到达的时刻，两者相减就是"编码完成 → 到达接收端"的耗时。
     队列深度、socket 缓冲只要被写大，这个数字立刻变大。

⚠️ 音频这条断言有个前提：**机器上必须真的在放声音**。
WASAPI 回环在没有音频渲染时不产生数据包（不是产生静音包），
所以安静的机器上音频帧数会是 0 —— 那是环境，不是缺陷。
本测试因此会自己播放测试音，把这条断言变成确定的。

用法：
    python tools/sender_e2e_session_test.py
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import time

sys.stdout.reconfigure(line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

from castlink import protocol as P                          # noqa: E402
from castlink.config import SenderConfig                    # noqa: E402
from castlink.identity import load_identity                 # noqa: E402
from castlink_sender.encoders import probe_capabilities     # noqa: E402
from castlink_sender.session import CastSession, STATE_STREAMING   # noqa: E402

FFMPEG = os.path.join(ROOT, "tools", "ffmpeg.exe")


# --------------------------------------------------------------- 假投影仪

class FakeReceiver(threading.Thread):
    """CAST/1 接收侧的最小实现：握手 -> ready -> 收媒体帧并计数。"""

    def __init__(self, ctrl_port: int, media_port: int):
        super().__init__(daemon=True)
        self.ctrl_port = ctrl_port
        self.media_port = media_port
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("0.0.0.0", ctrl_port))
        self.srv.listen(2)
        self.stop_flag = threading.Event()
        self.video_frames = 0
        self.audio_frames = 0
        self.audio_bytes = 0
        self.first_video_ns = 0
        self.last_video_ns = 0
        # frame_id -> 该帧**首个分片**到达的单调时钟（用来算端到端延迟）
        self.video_arrival = {}
        self.error = ""
        self.ready_seen = False
        self.audio_requested = False

    def run(self):
        try:
            while not self.stop_flag.is_set():
                try:
                    self.srv.settimeout(0.4)
                    sock, addr = self.srv.accept()
                except socket.timeout:
                    continue
                except Exception:
                    return
                try:
                    self._serve(sock)
                except Exception as e:
                    self.error = f"{type(e).__name__}: {e}"
                finally:
                    try:
                        sock.close()
                    except Exception:
                        pass
        finally:
            try:
                self.srv.close()
            except Exception:
                pass

    # ---- 单条控制连接 ----
    def _serve(self, sock):
        f = sock.makefile("rwb")
        hello = self._read(f)
        if not hello or hello.get("t") != "hello":
            return
        nonce = "QUJDRUZHSElKS0xNTk9Q"
        f.write((json.dumps({
            "t": "hello_ack", "id": "FAKE0001", "name": "假投影仪",
            "nonce": nonce,
            "caps": {"maxW": 1920, "maxH": 1080, "maxFps": 30,
                     "alignW": 2, "alignH": 2, "hw": True,
                     "codecs": ["h265", "h264"],
                     "transports": ["tcp", "udp"],
                     "detail": "fake"},
        }) + "\n").encode())
        f.flush()

        auth = self._read(f)
        if not auth or auth.get("t") != "auth":
            return
        f.write((json.dumps({"t": "auth_ok", "token": "FAKETOKEN"}) + "\n").encode())
        f.flush()

        start = self._read(f)
        if not start or start.get("t") != "start":
            return
        self.audio_requested = bool(start.get("audio"))
        self.ready_seen = True
        f.write((json.dumps({"t": "ready",
                             "media": {"mode": "tcp", "port": self.media_port},
                             "video": start.get("video") or {}}) + "\n").encode())
        f.flush()

        # 媒体通道
        msrv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        msrv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        msrv.bind(("0.0.0.0", self.media_port))
        msrv.listen(1)
        msrv.settimeout(6.0)
        try:
            media, _ = msrv.accept()
        except Exception:
            msrv.close()
            return
        msrv.close()
        media.settimeout(0.5)

        reader = threading.Thread(target=self._read_media, args=(media,), daemon=True)
        reader.start()

        # 收控制消息（pong 必须有，否则发送端 RTT 一直是 0；stop 用来收尾）
        while not self.stop_flag.is_set():
            msg = self._read(f, timeout=3.0)
            if msg is None:
                if self.stop_flag.is_set():
                    break
                break
            t = msg.get("t")
            if t == "ping":
                f.write((json.dumps({"t": "pong", "ts": msg.get("ts")}) + "\n").encode())
                f.flush()
            elif t in ("stop", "bye"):
                break
        try:
            media.close()
        except Exception:
            pass

    def _read_media(self, media):
        buf = bytearray()
        try:
            while not self.stop_flag.is_set():
                try:
                    chunk = media.recv(65536)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= P.HEADER_SIZE:
                    payload_len = struct.unpack_from("<H", buf, 20)[0]
                    total = P.HEADER_SIZE + payload_len
                    if len(buf) < total:
                        break
                    stream = buf[5]
                    frame_id = struct.unpack_from("<I", buf, 8)[0]
                    now = time.monotonic_ns()
                    if stream == P.STREAM_VIDEO:
                        self.video_frames += 1
                        if not self.first_video_ns:
                            self.first_video_ns = now
                        self.last_video_ns = now
                        # 只记首片：一帧可能被切成多个报文，延迟按"第一个字节到"算
                        self.video_arrival.setdefault(frame_id, now)
                    elif stream == P.STREAM_AUDIO:
                        self.audio_frames += 1
                        self.audio_bytes += payload_len
                    del buf[:total]
        except Exception:
            pass

    @staticmethod
    def _read(f, timeout=6.0):
        sock = f.raw if hasattr(f, "raw") else None
        if sock is not None:
            sock.settimeout(timeout)
        line = f.readline()
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8"))
        except Exception:
            return None

    def close(self):
        self.stop_flag.set()
        try:
            self.srv.close()
        except Exception:
            pass


# --------------------------------------------------------------- 主流程

def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


TONE = os.path.join(ROOT, "tools", "_tone.wav")


class ToneLoop(threading.Thread):
    """
    观察窗口内反复播放一段测试音。

    没有它，`音频帧 > 0` 这条断言就取决于"跑测试时这台机器恰好在不在放声音" ——
    那样的测试迟早会在某次安静的运行里红掉，然后被人当成"偶发失败"忽略。
    """

    def __init__(self, seconds: float):
        super().__init__(daemon=True)
        self.seconds = seconds
        self._stop = threading.Event()

    def run(self):
        if sys.platform != "win32" or not os.path.exists(TONE):
            return
        try:
            import winsound
        except Exception:
            return
        deadline = time.time() + self.seconds
        while time.time() < deadline and not self._stop.is_set():
            try:
                winsound.PlaySound(TONE, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                return
            self._stop.wait(1.0)
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def halt(self):
        self._stop.set()


def install_latency_probe(session: CastSession, put_times: dict):
    """
    记下每一帧"被交给发送队列"的时刻。

    配合假投影仪记录的到达时刻，就能算出**编码器出帧 → 到达接收端**这一段
    在流水线里待了多久。这是唯一能直接观测"延迟"的办法 ——
    队列深度、socket 缓冲、编码器缓冲全都会体现在这一个数字上。
    """
    orig = session._on_video_frame

    def wrapper(payload, keyframe, ts_us):
        orig(payload, keyframe, ts_us)
        put_times[session._frame_seq] = time.monotonic_ns()

    session._on_video_frame = wrapper
    return orig


def one_round(idx: int, session: CastSession, ctrl_port: int, media_port: int,
              seconds: float = 4.0):
    print()
    print("=" * 70)
    print(f"第 {idx} 轮：connect_to -> 观测 {seconds:.0f} 秒 -> stop")
    print("=" * 70)
    put_times: dict = {}
    install_latency_probe(session, put_times)
    rx = FakeReceiver(ctrl_port, media_port)
    rx.start()
    time.sleep(0.2)

    ok = session.connect_to("127.0.0.1", ctrl_port, "假投影仪")
    print(f"connect_to() -> {ok}  状态={session.state}  {session.message}")

    # 让机器真的有声音在放，音频采集才有数据可采（见文件头的说明）
    tone = ToneLoop(seconds + 1.0)
    tone.start()
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.5)
    tone.halt()

    v, a = rx.video_frames, rx.audio_frames
    abytes = rx.audio_bytes
    stats = session.media.stats.sample()
    span = 0.0
    if rx.first_video_ns and rx.last_video_ns > rx.first_video_ns:
        span = (rx.last_video_ns - rx.first_video_ns) / 1e9

    print(f"  假投影仪收到：视频 {v} 帧 / 音频 {a} 帧（{abytes} 字节）")
    if span > 0:
        print(f"  视频到达速率 ≈ {v / max(span, 1e-6) / (1 if span > 1 else 1):.1f} 帧"
              f"（首帧到末帧 {span:.2f} 秒）")
    print(f"  音频请求：start 消息里含 audio = {rx.audio_requested}")
    print(f"  发送端统计：{stats}  音频源={session.audio_note}")

    lat = frame_latency_ms(put_times, rx.video_arrival)
    if lat:
        lat.sort()
        p50 = lat[len(lat) // 2]
        p95 = lat[int(len(lat) * 0.95)] if len(lat) > 4 else lat[-1]
        over = sum(1 for x in lat if x > 250.0)
        print(f"  流水线延迟：样本 {len(lat)} 帧  "
              f"中位 {p50:.0f}ms  95分位 {p95:.0f}ms  最大 {lat[-1]:.0f}ms  "
              f"（>250ms 的 {over} 帧）")
    else:
        print("  流水线延迟：样本不足")

    session.stop()
    time.sleep(0.4)
    rx.close()
    return v, a, abytes, lat


def frame_latency_ms(put_times: dict, arrival: dict):
    """两边的 frame_id 取交集，算出"交给队列 → 到达接收端"的毫秒数。"""
    out = []
    for fid, t_arrive in arrival.items():
        t_put = put_times.get(fid)
        if t_put is None:
            continue
        d = (t_arrive - t_put) / 1e6
        # 跨轮次的残留（frame_id 会重用）会算出负数或离谱的大数，直接丢掉
        if 0 <= d <= 5000:
            out.append(d)
    return out


def main() -> int:
    ctrl_port = free_port()
    media_port = free_port()

    cfg = SenderConfig()
    cfg.resolution_mode = "1080p"
    cfg.fps = 30
    cfg.quality = "smooth"      # 最低档，测试机负载小
    cfg.transport = "tcp"
    cfg.enable_audio = True
    cfg.adaptive = False
    cfg.remembered_peers = {}

    caps = probe_capabilities(FFMPEG, deep=True)
    identity = load_identity("测试机")
    logs = []
    session = CastSession(cfg, caps, FFMPEG, identity,
                          log=lambda m: (logs.append(str(m)), print("   [log]", m)))

    results = []
    for i in (1, 2, 3):
        results.append(one_round(i, session, ctrl_port, media_port))

    session.shutdown()

    print()
    print("=" * 70)
    vids = [r[0] for r in results]
    auds = [r[1] for r in results]
    all_lat = [x for r in results for x in r[3]]
    print(f"三轮视频帧数：{vids}")
    print(f"三轮音频帧数：{auds}")
    ok = True
    if not all(v > 0 for v in vids):
        print("❌ 有轮次一个视频帧都没收到（停止后无法重新投屏）")
        ok = False
    else:
        print("✅ 每轮都有视频帧 —— 可反复停止/开始")
    if not all(a > 0 for a in auds):
        print("❌ 有轮次没有音频帧（发送端压根没把音频发出去）")
        ok = False
    else:
        print("✅ 每轮都有音频帧 —— 音频确实发送了")

    # ---- 延迟上限 ----
    # 本机回环链路上，唯一能把延迟做大的就是"帧在队列/socket 缓冲里排队"。
    # 队列已经有界、socket 缓冲已按时间折算，所以这里必须钉住：
    # 中位数要远低于上限，且绝大多数帧不该触发 250ms 的丢帧闸门。
    if all_lat:
        all_lat.sort()
        p50 = all_lat[len(all_lat) // 2]
        p95 = all_lat[int(len(all_lat) * 0.95)]
        print(f"整体流水线延迟：中位 {p50:.0f}ms  95分位 {p95:.0f}ms  最大 {all_lat[-1]:.0f}ms")
        if p50 <= 200:
            print(f"✅ 中位延迟 {p50:.0f}ms ≤ 200ms（旧配置里光队列就有 1000ms）")
        else:
            print(f"❌ 中位延迟 {p50:.0f}ms 偏高 —— 队列或缓冲又被写大了")
            ok = False
        if p95 <= 500:
            print(f"✅ 95 分位 {p95:.0f}ms ≤ 500ms（抖动被兜住）")
        else:
            print(f"❌ 95 分位 {p95:.0f}ms 偏高 —— 存在偶发积压")
            ok = False
    else:
        print("⚠️  没采到延迟样本，跳过延迟判定")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
