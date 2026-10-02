# -*- coding: utf-8 -*-
"""
一次投屏会话的完整生命周期编排。

握手流程（防止同一 Wi-Fi 里陌生人蹭投影仪）
------------------------------------------
    S --- hello(id, name, token?) ------------>   R
    S <-- hello_ack(caps, nonce) ------------    R      生成一次性随机数
    S --- auth(HMAC-SHA256(pin, nonce|salt)) ->   R     双方共享 PIN
    S <-- auth_ok(session_token) -----------     R      首次配对成功后下发长期令牌
    S --- start(video/audio/transport) ----->    R
    S <-- ready(media_port) ---------------      R
    S === 媒体帧 ==========================>      R

PIN 只参与 HMAC 运算、从不上网，抓包也还原不出。
首次配对成功下发长期令牌，之后自动连接无需再输入。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from castlink import protocol as P
from castlink.control import ControlChannel, connect_control
from castlink.winvol import LocalAudioMuter
from .transport import MediaSender, FrameTask, MODE_TCP, MODE_UDP
from .capture import StreamPlan, VideoPipeline, detect_capture_method, primary_resolution, make_plan
from .audio import AudioPipeline
from .encoders import Capabilities, VIDEO_CODEC_HEVC, VIDEO_CODEC_H264

STATE_IDLE = "idle"
STATE_CONNECTING = "connecting"
STATE_STARTING = "starting"
STATE_STREAMING = "streaming"
STATE_STOPPING = "stopping"
STATE_ERROR = "error"

STATE_TEXT = {
    STATE_IDLE: "未投屏",
    STATE_CONNECTING: "正在连接投影仪…",
    STATE_STARTING: "正在协商参数…",
    STATE_STREAMING: "投屏中",
    STATE_STOPPING: "正在停止…",
    STATE_ERROR: "异常",
}

AUTH_SALT = b"CASTLINK-AUTH-V1"

PREFER_TO_HEVC = {"nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "amf": "hevc_amf",
                  "mf": "hevc_mf", "x265": "libx265"}
PREFER_TO_H264 = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf",
                  "mf": "h264_mf", "x264": "libx264"}

# 未显式限流时按传输方式给的"自动带宽预算"（Mbps）。
#
# 依据：普通 5GHz Wi-Fi 的实际可用吞吐大致 30~80 Mbps，2.4GHz 只有 5~20 Mbps。
# 早期版本在这里是"不限流"，于是 2880x1800@60 + visual 画质算出来 171 Mbps 的
# 目标码率直接怼给投影仪 —— 静态内容看不出问题，一放视频就把链路和解码器打爆。
#
# v1.0.2 把 TCP 预算从 80 降到 45：TCP 是有背压的，一旦发送速率逼近链路容量，
# 内核发送缓冲填满 -> 这边写 socket 阻塞 -> ffmpeg stdout 管道填满 -> 编码器
# 卡住，帧率会断崖式下跌（正是"几秒一帧"的成因之一）。所以实时视频走 TCP 时
# 必须把目标码率压在链路容量的六成左右，留出重传和抖动的余量。
# UDP 没有背压（丢包只表现为花屏），但它更容易把链路推满并影响同网的音频，
# 故预算定得比 TCP 略高一点、仍在安全区内。
AUTO_BUDGET_TCP = 45.0
AUTO_BUDGET_UDP = 50.0

# 画质从高到低，自适应降档时按这个顺序往下走
_QUALITY_ORDER = ["lossless", "visual", "high", "balanced", "smooth"]


def auto_budget(cfg) -> float:
    """本次会话的码率预算（Mbps）。0 表示不限（只有无损模式会走到）。"""
    cap = float(getattr(cfg, "bitrate_cap_mbps", 0) or 0)
    if cap > 0:
        return cap
    if cfg.quality == "lossless":
        return 0.0     # 用户明确要像素级无损，不自动降档，只在日志里提示
    return AUTO_BUDGET_UDP if cfg.transport == MODE_UDP else AUTO_BUDGET_TCP


def plan_for(cfg, desktop: Tuple[int, int], peer_caps: Optional[dict],
             fps=None, quality=None, codec: str = "") -> StreamPlan:
    """
    把「用户设置 + 投影仪上报的能力」翻译成实际规格。

    UI 预览和真正开流都走这一个函数 —— 之前 UI 是各算各的且不套带宽预算，
    界面上显示"预计 79.6 Mbps"，真开流却按 45 Mbps 预算降到 30 帧，
    用户看到的数字和实际对不上，这类不一致最难排查。
    """
    caps = peer_caps or {}
    max_w = int(caps.get("maxW") or 3840)
    max_h = int(caps.get("maxH") or 2160)
    max_fps = int(caps.get("maxFps") or 60)
    # 投影仪上报的宽高对齐要求（HEVC 常见 2/8/64）
    align_w = int(caps.get("alignW") or 2)
    align_h = int(caps.get("alignH") or 2)

    if not codec:
        codec = str(caps.get("codec") or VIDEO_CODEC_HEVC)
    if codec not in (VIDEO_CODEC_HEVC, VIDEO_CODEC_H264):
        # 兼容 hello_ack 里 codecs 是列表的形态
        listed = caps.get("codecs") or []
        codec = (VIDEO_CODEC_H264
                 if isinstance(listed, list) and listed and VIDEO_CODEC_HEVC not in listed
                 else VIDEO_CODEC_HEVC)

    if codec == VIDEO_CODEC_H264:
        # 大多数 H.264 硬件解码器在 4K60 上吃力，保险起见把上限压到 1080p
        max_w, max_h = min(max_w, 1920), min(max_h, 1080)

    fps_use = int(fps or cfg.fps)
    fps_use = min(fps_use, max_fps)
    return make_plan(desktop, fps_use, cfg.resolution_mode, cfg.canvas_4k,
                     quality or cfg.quality, cfg.chroma, max_w, max_h, codec,
                     align_w, align_h, auto_budget(cfg))


def auth_token(pin: str, nonce: str) -> str:
    try:
        msg = base64.b64decode(nonce) if nonce else b""
    except Exception:
        msg = b""
    mac = hmac.new(pin.encode("utf-8"), AUTH_SALT + msg, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def _hide():
    import subprocess as sp
    import sys
    if sys.platform != "win32":
        return {}
    si = sp.STARTUPINFO()
    si.dwFlags |= sp.STARTF_USESHOWWINDOW
    return {"startupinfo": si, "creationflags": getattr(sp, "CREATE_NO_WINDOW", 0)}


class CastSession:
    """「电脑 -> 投影仪」的一次投屏会话。不含任何 UI 依赖，便于单独联调。"""

    def __init__(self, cfg, caps: Capabilities, ffmpeg: str, identity: dict,
                 log: Callable[[str], None] = print,
                 on_state: Optional[Callable[[str, str], None]] = None,
                 on_stats: Optional[Callable[[dict], None]] = None,
                 on_request: Optional[Callable[[dict, Callable[[bool, str], None]], None]] = None):
        self.cfg = cfg
        self.caps = caps
        self.ffmpeg = ffmpeg
        self.identity = identity
        self.log = log
        self.on_state = on_state or (lambda s, t: None)
        self.on_stats = on_stats or (lambda d: None)
        self.on_request = on_request

        self.state = STATE_IDLE
        self.message = ""
        self.host = ""
        self.port = 0
        self.peer_caps: dict = {}
        self.peer_id = ""
        self.plan: Optional[StreamPlan] = None
        self.encoder_name = ""
        self.capture_method = detect_capture_method(ffmpeg)

        self.ctrl: Optional[ControlChannel] = None
        self.media = MediaSender(log=log)
        # 队列积压 = 链路/解码真的吃不下。处理方式和"接收端报帧率不足"一致：
        # 重新出一个 IDR（否则丢帧后接收端只能等下一个 GOP）并下调一档。
        self.media.on_congestion = self._on_media_congestion
        self.video = VideoPipeline(ffmpeg, log=log)
        self.audio = AudioPipeline(ffmpeg, log=log)
        # 投射系统声音时把本机扬声器静音（可在设置里关）。
        # 本机扬声器和投影仪同时出声、又差着几百毫秒，听起来就是回声。
        self.muter = LocalAudioMuter(log=log)

        self.rtt_ms = 0.0
        self.peer_stats: dict = {}
        self._stop = threading.Event()
        self._rx_thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None
        self._frame_seq = 0
        self._audio_seq = 0
        # 控制通道代号：换代后旧接收线程必须退休（见 _control_rx / stop / connect_to）
        self._ctrl_gen = 0
        self._last_renegotiate = 0.0
        self._pin = getattr(cfg, "pin", "000000")
        self._label = ""
        self._encoder_fallbacks: List[str] = []
        self._request_server: Optional[socket.socket] = None
        self._req_thread: Optional[threading.Thread] = None
        self._stats_thread: Optional[threading.Thread] = None
        self._start_ns = 0
        # 采集进程的墙钟基准 —— 延迟上限的零点（见 _spawn_video）。
        self._video_wall0_ns = 0
        self.last_plan_note = ""

        # ---- 自适应降档（cfg.adaptive）----
        # 只调帧率与画质，**不动分辨率**：分辨率一变，投影仪就得重配解码器，
        # 中途改分辨率在部分解码器上会直接黑屏。分辨率由首帧协商一次定死。
        self._adapt_rungs: List[dict] = []
        self._adapt_idx = 0
        self._adapt_bad = 0
        self._adapt_override: dict = {}
        self.audio_note = ""

    # ================================================== 状态
    def _set_state(self, state: str, msg: str = ""):
        self.state = state
        self.message = msg
        self.log(f"[状态] {STATE_TEXT.get(state, state)} {msg}".strip())
        try:
            self.on_state(state, msg or STATE_TEXT.get(state, ""))
        except Exception as e:
            self.log(f"状态回调异常: {e}")

    # ================================================== 主动连接
    def connect_to(self, host: str, port: int, label: str = "", pin: str = "") -> bool:
        self.stop(silent=True)
        self._stop.clear()
        # 新的一代控制通道：让上一代残留的接收线程无论如何都退休（见 _control_rx）。
        self._ctrl_gen = getattr(self, "_ctrl_gen", 0) + 1
        self.host, self.port = host, port
        self._label = label or host
        self._pin = pin or getattr(self.cfg, "pin", "000000")
        self._set_state(STATE_CONNECTING, self._label)

        ctrl = connect_control(host, port, timeout=3.0)
        if not ctrl:
            self._set_state(STATE_ERROR,
                            f"连不上 {host}:{port}。请确认投影仪端 CastLink 已在运行、且两者在同一局域网")
            return False
        self.ctrl = ctrl
        if not self._handshake():
            return False
        return self._start_stream()

    def _remember_token(self, peer_id: str, token: str):
        try:
            peers = dict(getattr(self.cfg, "remembered_peers", {}) or {})
            rec = dict(peers.get(peer_id, {}))
            rec["token"] = token
            peers[peer_id] = rec
            self.cfg.remembered_peers = peers
            self.cfg.save()
        except Exception:
            pass

    def _stored_token(self, peer_id: str) -> str:
        try:
            return (self.cfg.remembered_peers or {}).get(peer_id, {}).get("token", "")
        except Exception:
            return ""

    def _handshake(self) -> bool:
        ctrl = self.ctrl
        if not ctrl:
            return False
        hello = {
            "t": "hello", "id": self.identity["id"], "name": self.identity["name"],
            "ver": "1.0.0", "token": self._stored_token(""),
            "device": self.identity.get("model", "PC"),
        }
        if not ctrl.send(hello):
            self._set_state(STATE_ERROR, "握手失败（连接被投影仪关闭）")
            return False
        ack = ctrl.recv(timeout=6.0)
        if not ack or ack.get("t") != "hello_ack":
            self._set_state(STATE_ERROR, "投影仪无响应，或版本不兼容")
            return False

        self.peer_caps = ack.get("caps", {}) or {}
        self.peer_id = ack.get("id", "")
        token = self._stored_token(self.peer_id)

        resp = {"t": "auth", "token": token}
        if "nonce" in ack:
            resp["resp"] = auth_token(self._pin, ack["nonce"])
        if not ctrl.send(resp):
            self._set_state(STATE_ERROR, "认证阶段连接中断")
            return False

        ok = ctrl.recv(timeout=6.0)
        if not ok or ok.get("t") != "auth_ok":
            code = (ok or {}).get("code", -1)
            reason = (ok or {}).get("msg", "未知原因")
            if code == 401:
                self._set_state(STATE_ERROR, f"配对被拒绝：{reason}（请检查 PIN）")
            else:
                self._set_state(STATE_ERROR, f"认证失败：{reason}")
            return False
        new_token = ok.get("token", "")
        if new_token and self.peer_id:
            self._remember_token(self.peer_id, new_token)
        return True

    # ================================================== 参数协商
    def _choose_codec_and_encoder(self) -> Tuple[str, List[str], str]:
        caps = self.peer_caps
        codecs = caps.get("codecs") or ["h265", "h264"]
        want_444 = (self.cfg.chroma == "444")
        lossless = (self.cfg.quality == "lossless")

        prefer = self.cfg.encoder
        if self.cfg.quality in ("smooth", "balanced") and prefer == "auto":
            pass

        # 1) 决定编码格式
        codec = VIDEO_CODEC_HEVC
        if isinstance(codecs, list) and codecs and VIDEO_CODEC_HEVC not in codecs:
            codec = VIDEO_CODEC_H264 if VIDEO_CODEC_H264 in codecs else codecs[0]

        # 2) 列出候选（按性能偏好排序）
        if codec == VIDEO_CODEC_HEVC:
            cands = [n for _, n in self.caps.hevc_candidates(want_444)]
            if lossless:
                # QSV / AMF / MF 无真无损能力，交给 x265 或 NVENC
                cands = [n for n in cands if n in ("hevc_nvenc", "libx265")] or ["libx265"]
        else:
            cands = [n for _, n in self.caps.h264_candidates()]

        if prefer != "auto":
            forced = (PREFER_TO_HEVC if codec == VIDEO_CODEC_HEVC else PREFER_TO_H264).get(prefer)
            if forced and forced in cands:
                cands = [forced] + [n for n in cands if n != forced]

        if not cands:
            cands = ["libx265"] if codec == VIDEO_CODEC_HEVC else ["libx264"]
        return codec, cands, ("" if not cands else cands[0])

    def _bitrate_budget(self) -> float:
        """本次会话的码率预算（Mbps）。0 表示不限（只有无损模式会走到）。"""
        return auto_budget(self.cfg)

    def _build_plan(self) -> StreamPlan:
        # 自适应覆盖只影响帧率与画质
        return plan_for(
            self.cfg, primary_resolution(), self.peer_caps,
            fps=self._adapt_override.get("fps"),
            quality=self._adapt_override.get("quality"),
            codec=self._choose_codec_and_encoder()[0],
        )

    # ================================================== 开流
    def _start_stream(self) -> bool:
        self._set_state(STATE_STARTING, "协商参数…")
        # 每次开流都从用户设置的原始档位开始，不把上一次会话的降档带过来
        self._adapt_override = {}
        self._adapt_rungs = []
        self._adapt_idx = 0
        self._adapt_bad = 0
        self._adapt_prev_assembled = 0
        self._adapt_prev_rendered = 0
        self._adapt_prev_drops = 0
        self._congestion_events = 0
        self._congestion_why = ""
        self._last_idr_ns = 0
        self._adapt_last_check = 0.0
        plan = self._build_plan()
        codec, cands, primary = self._choose_codec_and_encoder()
        self.plan = plan
        self._encoder_fallbacks = cands[1:] + []
        self.encoder_name = primary
        self.log(f"本次规格：{plan.out_w}x{plan.out_h}@{plan.fps} · {plan.quality} · "
                 f"{primary} · 估算码率 {plan.bitrate_mbps:.1f} Mbps")
        if plan.note:
            self.log(f"规格说明：{plan.note}")
        self.last_plan_note = plan.note

        transports = self.peer_caps.get("transports") or ["tcp", "udp"]
        mode = self.cfg.transport if self.cfg.transport in transports else transports[0]

        # 音频必须在**下发 start 之前**就确定到底能不能采到：
        # 早期版本无条件把 audio 写进 start，投影仪那边老老实实起了 AAC 解码器，
        # 而这边因为找不到回环设备一个字节都没发 —— 观众只听到"没有声音"，
        # 两端日志各自都正常，最难查。现在采不到就不报，让两端认知一致。
        audio_ok = False
        self.audio_note = ""
        if self.cfg.enable_audio:
            src = self.audio.resolve(force=True)
            if src is not None:
                audio_ok = True
                self.audio_note = src.label
                self.log(f"音频源：{self.audio_note}")
            else:
                self.audio_note = "未找到可用的系统声音采集方式"
                self.log(f"{self.audio_note}，本次投屏将不含音频")
        else:
            self.audio_note = "已在设置中关闭"

        start = {
            "t": "start",
            "video": {
                "codec": codec, "w": plan.out_w, "h": plan.out_h, "fps": plan.fps,
                "pix": "yuv444p" if plan.chroma == "444" else "yuv420p",
            },
            "audio": {"codec": "aac", "sr": 48000, "ch": 2, "br": 160000} if audio_ok else None,
            "transport": {"mode": mode},
            "quality": plan.quality,
            "bitrate": plan.bitrate_mbps,
        }
        if not self.ctrl or not self.ctrl.send(start):
            self._set_state(STATE_ERROR, "下发开始指令失败")
            return False

        ready = self.ctrl.recv(timeout=8.0)
        if not ready or ready.get("t") != "ready":
            self._set_state(STATE_ERROR, f"投影仪未就绪：{(ready or {}).get('msg', '无响应')}")
            return False

        media_port = int(ready.get("media", {}).get("port", self.port + 2))
        self.media_mode = mode
        # 把码率告诉传输层：发送缓冲按"0.25 秒的数据量"算，
        # 而不是写死一个字节数（见 MediaSender.BUFFER_SECONDS）。
        self.media.set_bitrate(plan.bitrate_mbps)
        if mode == MODE_UDP:
            self.media.attach_udp((self.host, media_port))
        else:
            sock = None
            try:
                sock = socket.create_connection((self.host, media_port), timeout=4.0)
            except Exception as e:
                self._set_state(STATE_ERROR, f"媒体通道建立失败: {e}")
                return False
            self.media.attach_tcp(sock)
        self.media.start()

        if not self._start_video():
            self._abort_start()
            return False

        audio_started = False
        if audio_ok:
            audio_started = self._start_audio()
        self._apply_local_mute(audio_started)

        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._control_rx, daemon=True)
        self._rx_thread.start()
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self._ping_thread.start()
        self._stats_thread = threading.Thread(target=self._stats_loop, daemon=True)
        self._stats_thread.start()

        note = plan.note or f"{plan.out_w}×{plan.out_h}@{plan.fps} · {primary}"
        self._set_state(STATE_STREAMING, note)
        return True

    def _abort_start(self):
        """
        开流中途失败的回滚。

        没有它的话，失败后 media/视频/音频三样都还开着：用户再点一次"开始投屏"
        会又叠一套上去（旧媒体线程还在，socket 还连着），接收端那边看到的
        混乱连接就再也理不清了 —— 只能重启程序。
        """
        try:
            self.video.stop()
        except Exception:
            pass
        try:
            self.audio.stop()
        except Exception:
            pass
        try:
            self.media.close()
        except Exception:
            pass
        # 开流中途失败也要把本机音量放回去，否则用户会遇到
        # "投屏没成功，但电脑从此没声音了"这种莫名其妙的状态。
        self.muter.restore()
        self.media_mode = None


    def _video_cmd(self, encoder_name: str):
        return VideoPipeline.build_command(
            self.ffmpeg, self.plan, encoder_name, self.capture_method,
            draw_mouse=self.cfg.capture_mouse,
            bitrate_cap_mbps=self.cfg.bitrate_cap_mbps,
            low_latency=True,
        )

    def _spawn_video(self, cmd: List[str]) -> bool:
        """
        启动采集进程，并记下"这一代"的墙钟基准。

        基准是延迟上限的零点：采集线程给的 ts_us 是相对它自己启动时刻的，
        两者相减得到的就是这一帧的滞留时间。**每次重启采集进程都必须重置**，
        否则新进程的 ts 从 0 重新开始，会被算成"已经滞后了几十秒"而被全部丢弃。
        """
        self._video_wall0_ns = time.monotonic_ns()
        return self.video.start(
            cmd, self._on_video_frame,
            on_exit=lambda code: self._on_video_exit(code),
            codec=self.plan.codec,
        )

    def _start_video(self) -> bool:
        cmd = self._video_cmd(self.encoder_name)
        self.log("ffmpeg: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        if not self._spawn_video(cmd):
            self._set_state(STATE_ERROR, "无法启动采集进程")
            return False
        return True

    def _on_video_exit(self, code: int):
        if self.state in (STATE_STOPPING, STATE_IDLE) or self._stop.is_set():
            return
        self.log(f"采集进程退出 code={code}")
        if self._encoder_fallbacks:
            nxt = self._encoder_fallbacks.pop(0)
            self.log(f"尝试降级到编码器 {nxt}")
            try:
                cmd = self._video_cmd(nxt)
                self.encoder_name = nxt
                if self._spawn_video(cmd):
                    self._set_state(STATE_STREAMING, f"已降级到 {nxt}")
                    return
            except Exception as e:
                self.log(f"降级失败: {e}")
        self._set_state(STATE_ERROR, f"采集进程意外退出（{self.video.last_error[:120]}）")

    def _start_audio(self):
        def on_audio_frame(payload: bytes, ts: int):
            # 与视频同样放宽到 STARTING：音频管道一建好就开始出帧，
            # 而状态要到 _start_stream 末尾才变 STREAMING。
            # 早期这里只认 STREAMING，开场那 0.3~0.5 秒的声音被整段吞掉。
            if self.state not in (STATE_STREAMING, STATE_STARTING):
                return
            self._audio_seq += 1
            # 音频只用小队列：积压的音频比丢一帧更难听。丢旧保新。
            self.media.put(FrameTask(P.STREAM_AUDIO, P.CODEC_AAC, self._audio_seq,
                                     ts, payload), drop_if_full=True)

        def on_audio_exit(code: int):
            # 音频中途没了，本机还静着的话用户会两头都听不到 —— 立刻放开。
            self.muter.restore()
            if self._stop.is_set() or self.state in (STATE_STOPPING, STATE_IDLE):
                return
            self.log(f"音频采集进程退出（code={code}），"
                     f"最后一次错误：{self.audio.last_error[:120] or '无'}")

        if not self.audio.start(on_audio_frame, on_exit=on_audio_exit):
            self.log("音频未能启动（画面不受影响）")
            self.audio_note = self.audio.last_error or "启动失败"
            return False
        self.audio_note = self.audio.source_label
        return True

    def _apply_local_mute(self, audio_ok: bool):
        """
        投射系统声音时静音本机扬声器。

        注意顺序：**必须确认音频真的起来了**再静音。反过来做的话，
        音频采集失败时用户会两头都听不到 —— 电脑静着、投影仪也没声音，
        这是比"有回声"严重得多的故障。
        """
        if not audio_ok or not getattr(self.cfg, "mute_local", True):
            return
        if not self.muter.mute():
            self.log("本机扬声器未做改动（不影响投屏）")

    # ================================================== 帧回调
    def _on_video_frame(self, payload: bytes, keyframe: bool, ts_us: int):
        if self.state not in (STATE_STREAMING, STATE_STARTING):
            return
        plan = self.plan
        if plan is None:
            return
        self._frame_seq += 1
        # 这一帧从编码器 stdout 被读走到现在等了多久。
        # ts_us 是采集线程的单调时钟读数，本机在启动采集进程时记了一个基准，
        # 两者相减就是"已经滞后了多少"。用它当延迟上限的判据（见 transport.py）。
        base = getattr(self, "_video_wall0_ns", 0)
        lag_us = 0
        if base:
            lag_us = (time.monotonic_ns() - base) // 1000 - int(ts_us)
            if lag_us < 0:
                lag_us = 0
        # 实时语义：任何模式下都允许丢帧。可靠模式那套"背压保比特无损"
        # 已经证明会把整条链路拖死（见 transport.py 的类注释）。
        if keyframe:
            # 供 _on_keyframe_request 判断"是不是真的很久没出过关键帧"。
            # 有了它，"投影仪要关键帧"这件事才能被正确区分为
            # 「正常，下一个 IDR 0.5 秒后自己就到」和「编码器真的没在出关键帧」。
            self._last_key_sent_ns = time.monotonic_ns()
        self.media.put(FrameTask(P.STREAM_VIDEO,
                                 P.CODEC_H265 if plan.codec == VIDEO_CODEC_HEVC else P.CODEC_H264,
                                 self._frame_seq, ts_us, payload, keyframe,
                                 lag_us=lag_us),
                       drop_if_full=True)

    # ================================================== 控制通道
    def _control_rx(self):
        ctrl = self.ctrl
        if not ctrl:
            return
        # 记下自己属于哪一代控制通道。
        # 降档重连会"停掉旧会话、再连一个新的"，而这里自己可能正跑在旧会话的线程里；
        # 只认 _stop 是不够的 —— connect_to 会把 _stop 清掉，于是旧线程被"复活"，
        # 两个线程读同一个控制 socket，消息交错（代码里警告过的"鬼影会话"）。
        # 用代号做到"旧线程必定退休"。
        gen = getattr(self, "_ctrl_gen", 0)
        while not self._stop.is_set() and getattr(self, "_ctrl_gen", 0) == gen:
            msg = ctrl.recv(timeout=1.0)
            if msg is None:
                if self._stop.is_set() or getattr(self, "_ctrl_gen", 0) != gen:
                    break
                if not getattr(ctrl, "connected", True):
                    # 投影仪那边已经收摊（关掉了 App / 会话被顶掉）。
                    # 不及时收尾的话，界面会一直停在"投屏中"却什么都没有。
                    self.log("与投影仪的连接已断开")
                    self._set_state(STATE_ERROR, "与投影仪的连接已断开（投影仪端已退出投屏）")
                    self.stop(silent=True)
                    break
                continue
            t = msg.get("t")
            if t == "pong":
                sent = float(msg.get("ts", 0))
                if sent:
                    self.rtt_ms = (time.time() - sent) * 1000.0
            elif t == "stats":
                self.peer_stats = msg
            elif t == "error":
                reason = msg.get("msg", "未知错误")
                self.log(f"投影仪返回错误：{reason}")
                self._set_state(STATE_ERROR, f"投影仪报错：{reason}")
                self.stop(silent=True)
                break
            elif t == "idr":
                reason = msg.get("reason", "lost")
                if msg.get("action") == "lower":
                    # 投影仪已经用掉一次"只求关键帧"的轻量机会，仍然"数据在进、
                    # 解码器不出图"—— 判定为本机解不动当前分辨率
                    # （投影仪 SoC 谎报 4K HEVC 解码能力是常事）。见 _renegotiate_lower。
                    self._renegotiate_lower(reason)
                    continue
                self._on_keyframe_request(reason)
            elif t == "stop":
                self.log("投影仪主动结束投屏")
                self.stop()
            elif t == "bye":
                break

    def _ping_loop(self):
        while not self._stop.is_set():
            if self.ctrl:
                self.ctrl.send({"t": "ping", "ts": time.time()})
            if self._stop.wait(1.0):
                break

    def _stats_loop(self):
        while not self._stop.is_set():
            data = self.media.stats.sample()
            data["rtt"] = round(self.rtt_ms, 1)
            data["encoder"] = self.encoder_name
            data["capture"] = self.capture_method
            data["state"] = self.state
            data["peer"] = self.peer_stats
            data["audio"] = self.audio_note
            if self.plan:
                data["res"] = f"{self.plan.out_w}x{self.plan.out_h}@{self.plan.fps}"
            try:
                self.on_stats(data)
            except Exception:
                pass
            try:
                self._maybe_adapt()
            except Exception as e:
                self.log(f"自适应异常: {e}")
            # 每 5 秒把接收端的解码现场写进**发送端日志**。
            #
            # 为什么值得专门做这件事：投影仪上没有 adb、没有 logcat，
            # 用户唯一能提供的就是这份发送端日志。而"有声音没画面"至少有四种
            # 完全不同的成因（数据没到 / 解码器没配置 / 配置了但不出图 / 出了图但画面载体没了），
            # 它们在投影仪屏上长得一模一样。把接收端的自述搬到这里，
            # 下一次出问题就不需要用户去按遥控器、拍屏幕了。
            self._peer_tick = getattr(self, "_peer_tick", 0) + 1
            if self._peer_tick % 10 == 0:
                try:
                    self._log_peer_state()
                except Exception:
                    pass
            if self._stop.wait(0.5):
                break

    def _log_peer_state(self):
        """一行式摘要接收端上报的解码现场（见 StreamSession.reportLoop）。"""
        peer = self.peer_stats or {}
        if not peer:
            return
        plan = self.plan
        res = f"{plan.out_w}x{plan.out_h}@{plan.fps}" if plan else "—"
        out_ms = peer.get("videoOut")
        try:
            out_ms = int(out_ms)
        except Exception:
            out_ms = None
        state = str(peer.get("videoState", "?"))
        # videoState 本身可能已经是"无画面载体"，那就别再重复一遍
        surface_note = "" if peer.get("surface", True) or "载体" in state else "  无画面载体"
        render = peer.get("render", "?")
        codec_pref = "H264" if peer.get("preferH264") else "自动"
        self.log("[接收端] %s %s %s%s%s · 收 %s / 出 %s 帧 %sfps · 队列 %s · 丢 %s · 音频 %s 收%s解%s · 通%s 编%s" % (
            getattr(self, "_label", "") or "设备",
            res,
            state,
            surface_note,
            "" if out_ms is None or out_ms < 0 else f"  {out_ms}ms 前出图",
            peer.get("assembled", "—"),
            peer.get("rendered", "—"),
            round(float(peer.get("fps") or 0), 1),
            peer.get("queue", "—"),
            peer.get("dropped", "—"),
            peer.get("audioStatus", "—"),
            peer.get("audioRecv", "—"),
            peer.get("audioDec", "—"),
            render,
            codec_pref))

    # ================================================== 自适应降档
    def _on_media_congestion(self, why: str):
        """
        发送队列积压（链路或投影仪吃不下）。

        这里**不**重启 ffmpeg：传输层已经是"丢到下一个关键帧"的实时语义，
        接收端最迟在一个 GOP（1 秒）内就能自己续上。真正需要做的是降档 ——
        交给 _maybe_adapt 用 dropped_video 的增量来判断，它比接收端回报更早、
        也不依赖投影仪是否"诚实"。
        """
        self._congestion_events = getattr(self, "_congestion_events", 0) + 1
        self._congestion_why = why
        self.log(f"链路拥塞（第 {self._congestion_events} 次）：{why}")

    def _maybe_adapt(self):
        """
        依据接收端回报的**实际解码帧率 / 画面更新**判断投影仪是不是扛不住，
        扛不住就往下走一档（降帧率 → 降画质），并重启采集进程立刻生效。

        为什么必须看接收端的数字而不是本地队列：发送端把数据写进 socket 就算"发出"，
        TCP 收了、对端丢了，发送端一无所知。只有接收端回报的 fps/rendered 才是真相。
        """
        if not getattr(self.cfg, "adaptive", True):
            return
        if self.state != STATE_STREAMING or not self.plan:
            return

        now = time.time()
        if now - getattr(self, "_adapt_last_check", 0.0) < 2.5:
            return
        self._adapt_last_check = now

        # ---- 1) 本地硬证据：发送队列真的在丢帧 ----
        # 比接收端回报更早、也更可信：TCP 收了、对端丢了，发送端一无所知，
        # 但"我自己的队列溢出"是确凿的。
        drops = int(self.media.stats.dropped_video)
        prev_drops = getattr(self, "_adapt_prev_drops", drops)
        self._adapt_prev_drops = drops
        dropped_now = max(0, drops - prev_drops)

        peer = self.peer_stats or {}
        assembled = int(peer.get("assembled") or 0)
        rendered = int(peer.get("rendered") or 0)
        pfps = peer.get("fps")
        target_fps = float(self.plan.fps)

        prev_a = getattr(self, "_adapt_prev_assembled", assembled)
        prev_r = getattr(self, "_adapt_prev_rendered", rendered)
        self._adapt_prev_assembled, self._adapt_prev_rendered = assembled, rendered

        data_flowing = assembled >= prev_a + 3
        reason = ""
        if dropped_now >= 5:
            reason = f"发送队列积压，本轮丢弃 {dropped_now} 帧"
        elif data_flowing and rendered <= prev_r:
            reason = "接收端收到了数据但画面完全不更新"
        elif data_flowing and isinstance(pfps, (int, float)) and 0 < pfps < target_fps * 0.6:
            reason = f"接收端解码只有 {pfps:.1f} fps（目标 {target_fps:.0f}）"
        if not reason:
            self._adapt_bad = 0
            return

        # 拥塞是即时事实，不必等两轮观察；接收端回报才需要防抖。
        self._adapt_bad += 2 if reason.startswith("发送队列积压") else 1
        if self._adapt_bad < 2:
            self.log(f"检测到帧率不足（{reason}），再观察一轮…")
            return
        self._adapt_bad = 0

        if not self._adapt_rungs:
            self._adapt_rungs = self._adapt_rungs_compute()
        if self._adapt_idx >= len(self._adapt_rungs):
            self.log(f"已到最低档位，无法再降（{reason}）")
            return

        rung = self._adapt_rungs[self._adapt_idx]
        self._adapt_idx += 1
        self._adapt_override = dict(rung)
        old = self.plan
        self.plan = self._build_plan()
        self.log(f"自适应降档：{reason} → "
                 f"{old.out_w}x{old.out_h}@{old.fps} {old.quality} 变为 "
                 f"{self.plan.out_w}x{self.plan.out_h}@{self.plan.fps} {self.plan.quality}")
        # 重启采集进程：换码率/帧率必须重开编码器（顺带得到一个全新 IDR）
        try:
            self.request_idr_pipe()
        except Exception as e:
            self.log(f"降档重启采集失败: {e}")
        self._set_state(STATE_STREAMING,
                        f"{self.plan.out_w}×{self.plan.out_h}@{self.plan.fps} · "
                        f"{self.plan.quality}（已自动降档）")

    def _adapt_rungs_compute(self) -> List[dict]:
        """
        生成"从当前设置往下"的降档阶梯。

        只调帧率与画质 —— 分辨率在会话中途变化会迫使投影仪重配解码器，
        部分机型直接黑屏，所以分辨率只在开流时协商一次。
        """
        q0 = self.cfg.quality
        qi = _QUALITY_ORDER.index(q0) if q0 in _QUALITY_ORDER else 1
        fps0 = int(self.cfg.fps)
        rungs: List[dict] = []
        for f in (30, 24):
            if f < fps0:
                rungs.append({"fps": f, "quality": q0})
        for q in _QUALITY_ORDER[qi + 1:]:
            rungs.append({"fps": min(fps0, 30), "quality": q})
        return rungs

    def _gop_seconds(self) -> float:
        """本次规格的关键帧周期（秒）。取不到时按 0.5s 兜底（encoders.py 的默认）。"""
        plan = self.plan
        try:
            v = float(getattr(plan, "gop_seconds", 0.5))
        except Exception:
            v = 0.5
        return v if v > 0.05 else 0.5

    def _on_keyframe_request(self, reason: str):
        """
        投影仪要一个关键帧。

        <p><b>默认什么都不做 —— 等下一个自然 IDR。</b>
        关键帧周期已经是 0.5 秒（`-g 15` @30fps，见 encoders.py），
        也就是说**最多等半个 GOP 就有一个新 IDR 自己送上门**；
        而"重建采集进程"要 1.5~2 秒，还要重新协商解码器。
        **重启比等着更慢**，所以旧版这条路是纯粹的负优化。

        <p>更糟的是它会自我放大：重启 → 旧进程退出事件被误判成崩溃
        （见 capture.py `start()` 里代号顺序那段）→ 降级到软编 libx265 →
        1080p 软编跟不上 → 解码器更饿 → 更频繁地请求关键帧 → 再重启。
        真机日志里 20 秒内重启了十几次，画面自然一直是断的。

        <p>只在一种情况下才值得动流水线：**确实很久没出过关键帧了**
        （距上个关键帧超过 `max(2 个 GOP 周期, 3 秒)`）—— 那说明 `-g` 没被编码器
        采纳，或者采集进程卡在了某个不产帧的状态里。那个 3 秒下限同时也是
        "起了流之后先别乱动"的宽限期：采集进程冷启动本来就要 1~2 秒。
        """
        now_ns = time.monotonic_ns()
        gop_ns = int(self._gop_seconds() * 1_000_000_000)
        last_key = getattr(self, "_last_key_sent_ns", 0)
        # 还没出过关键帧时，退回到"本代采集进程是什么时候起的"——
        # `_video_wall0_ns` 和 `_last_key_sent_ns` 都是 monotonic 读数，可以直接相减。
        base = last_key or getattr(self, "_video_wall0_ns", 0)
        waited = (now_ns - base) if base else 0
        self._keyframe_requests = getattr(self, "_keyframe_requests", 0) + 1

        # 日志节流 1.5 秒：接收端在看门狗里本来就 2 秒一次，不节流会把日志淹掉。
        if now_ns - getattr(self, "_last_idr_log_ns", 0) >= 1_500_000_000:
            self._last_idr_log_ns = now_ns
            self.log(f"投影仪请求关键帧（{reason}，距上个关键帧 "
                     f"{waited / 1e6:.0f}ms，GOP {gop_ns / 1e6:.0f}ms）")

        # 起流后前 3 秒是宽限期：采集进程刚起来、编码器正在初始化，
        # 这时候没有关键帧是正常的。在这里动手只会把启动过程打断。
        if waited <= max(2 * gop_ns, 3_000_000_000):
            return
        if now_ns - getattr(self, "_last_idr_ns", 0) < 3_000_000_000:
            return
        self._last_idr_ns = now_ns
        self.log(f"距上个关键帧已 {waited / 1e9:.1f}s（远超 GOP "
                 f"{gop_ns / 1e9:.2f}s），判定编码器没按 `-g` 出帧，重建采集进程")
        self.request_idr_pipe()

    def request_idr_pipe(self):
        """重启采集进程以获得一个新的 IDR —— 简单粗暴但 100% 有效。"""
        cmd = self._video_cmd(self.encoder_name)
        self._spawn_video(cmd)

    # ================================================== 降低分辨率重协商
    def _renegotiate_lower(self, reason: str = ""):
        """
        按更低的分辨率重连一次。

        <p><b>为什么需要一条能改分辨率的路径。</b>发送端原有的自适应降档
        （见 {@link #_adapt_rungs_compute}）刻意**只动帧率和画质、从不改分辨率**，
        理由是"中途改分辨率会让接收端重配解码器，部分机型直接黑屏"。
        但"投影仪解不动 4K"这件事，只有降分辨率才有用 —— 于是在那条逻辑下，
        链路会一直在同一个解不动的规格上打转，用户看到的就是画面永远冻着。

        <p><b>为什么是"重连"而不是"中途换规格"。</b>接收端的解码器是在收到第一个
        关键帧时按当时的宽高配置的，要它在中途自适应一个不同的尺寸，各机型行为
        不一 —— 这正是当初不敢改分辨率的原因。整条会话重来的代价约 1~2 秒，
        而且只会在"当前规格确实解不动"时发生，之后天花板就被接收端记住了
        （`Prefs.KEY_CAP_CEIL_W`），它下次上报的能力自然就低了，
        所以同一台设备不需要反复重连。
        """
        now = time.monotonic()
        if now - getattr(self, "_last_renegotiate", 0.0) < 8.0:
            return          # 重连有成本，别让两边互相放大成风暴
        self._last_renegotiate = now
        host, port, label = self.host, self.port, self._label
        if not host:
            return
        cur = f"{self.plan.out_w}x{self.plan.out_h}" if self.plan else "当前规格"
        self.log(f"投影仪报告解码器不出图（{reason}），判定 {cur} 超出本机解码能力，"
                 f"按更低分辨率重新协商…")
        threading.Thread(target=self._reconnect_lower, args=(host, port, label),
                         name="castlink-reneg", daemon=True).start()

    def _reconnect_lower(self, host: str, port: int, label: str):
        """
        在**新线程**里重连。

        <p>不能就地重连：本方法是被 {@code _control_rx} 那条线程调起来的，
        而 `connect_to` 会先 `stop()` 掉包括它在内的所有后台线程再重建一套。
        就地重连的结果是旧的控制接收线程被新会话"复活"，两个线程读同一个控制
        socket，消息交错 —— 那正是代码里警告过的"鬼影会话"。
        （`_ctrl_gen` 已经保证旧线程必定退休，这里再让重连跑在独立线程上，
        两重保险。）
        """
        try:
            self.stop(silent=True)
            # 给投影仪一点时间收尾旧会话再连，避免两边都是半开连接
            time.sleep(0.8)
            if not self.connect_to(host, port, label):
                self.log("降低分辨率重协商未成功，可手动重新连接")
        except Exception as e:
            self.log(f"降低分辨率重协商异常: {e}")
            try:
                self._set_state(STATE_ERROR, f"重协商失败: {e}")
            except Exception:
                pass

    # ================================================== 反向请求（投影仪主动拉流）
    def start_request_server(self):
        if self._request_server:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", P.SENDER_CTRL_PORT))
            s.listen(4)
        except Exception as e:
            self.log(f"反向请求服务未启动: {e}")
            try:
                s.close()
            except Exception:
                pass
            return
        self._request_server = s
        self._req_thread = threading.Thread(target=self._req_loop, daemon=True)
        self._req_thread.start()

    def _req_loop(self):
        s = self._request_server
        if not s:
            return
        while s and not self._stop.is_set():
            try:
                conn, addr = s.accept()
            except Exception:
                break
            threading.Thread(target=self._handle_request, args=(conn, addr),
                             daemon=True).start()

    def _handle_request(self, conn, addr):
        ch = ControlChannel(conn, addr)
        msg = ch.recv(timeout=4.0)
        if not msg or msg.get("t") != "request":
            ch.close()
            return
        result = {"accepted": False}

        def respond(ok: bool, reason: str = ""):
            result["accepted"] = ok
            ch.send({"t": "request_ack", "accepted": ok, "msg": reason})
            ch.close()

        if not self.on_request:
            respond(False, "发送端未就绪")
            return
        try:
            self.on_request({
                "id": msg.get("id", ""), "name": msg.get("name", addr[0]),
                "ip": addr[0], "ctrl_port": int(msg.get("ctrl", self.port or P.RECEIVER_CTRL_PORT)),
            }, respond)
        except Exception as e:
            self.log(f"处理投屏请求异常: {e}")
            respond(False, str(e))

    # ================================================== 停止
    def stop(self, silent: bool = False):
        if self.state == STATE_IDLE and silent:
            return
        self._stop.set()
        # 控制通道换代：即使紧接着 connect_to 把 _stop 清掉，
        # 上一代的接收线程也不会被"复活"（见 _control_rx）。
        self._ctrl_gen = getattr(self, "_ctrl_gen", 0) + 1
        if not silent:
            self._set_state(STATE_STOPPING, "")
        try:
            if self.ctrl:
                self.ctrl.send({"t": "stop"})
        except Exception:
            pass
        self.video.stop()
        self.audio.stop()
        self.media.close()
        # 恢复本机扬声器：必须在 stop 里做，而不是等析构 ——
        # 用户点"停止投屏"之后马上要听到电脑自己的声音。
        self.muter.restore()
        try:
            if self.ctrl:
                self.ctrl.close()
        except Exception:
            pass
        self.ctrl = None
        # 把三条后台线程收干净再返回。
        # 不收的话，下一次 connect_to() 会**再起一套**而旧的那一套还活着：
        # 旧的 ping 线程读的是 self.ctrl（已经指向新连接了），两边一起发心跳；
        # 旧的统计线程也在往界面推数据。表现就是"重启之后状态栏一会儿投屏中
        # 一会儿未投屏"这种鬼影。
        for th in (self._rx_thread, self._ping_thread, self._stats_thread):
            # stop() 自己可能是被 _control_rx 这条线程调用的（收到 stop / 断线），
            # 此时 join 自己会抛 RuntimeError，必须排除掉。
            if th and th.is_alive() and th is not threading.current_thread():
                try:
                    th.join(timeout=1.0)
                except Exception:
                    pass
        self._rx_thread = self._ping_thread = self._stats_thread = None
        self.peer_stats = {}
        self.plan = None
        self.media_mode = None
        if not silent:
            self._set_state(STATE_IDLE, "")


    def shutdown(self):
        self.stop(silent=True)
        if self._request_server:
            try:
                self._request_server.close()
            except Exception:
                pass
            self._request_server = None
