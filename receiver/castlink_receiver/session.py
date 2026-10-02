# -*- coding: utf-8 -*-
"""
接收端会话：握手 → 参数确认 → 媒体接収 → 呈现。

这一份实现是 Windows 接收端的实现，同时也是 **Android 接收端的行为基准**：
Android 端用 Java 复刻了完全相同的时序（见 `receiver/android/.../Session.java`），
所以只要 Windows 端能跑通，投影仪端的行为就是确定的。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import threading
import time
from typing import Callable, Dict, Optional

from castlink.control import ControlChannel
from castlink.protocol import CODEC_H265, CODEC_H264, STREAM_AUDIO, STREAM_VIDEO

AUTH_SALT = b"CASTLINK-AUTH-V1"


def expected_proof(pin: str, nonce_b64: str) -> str:
    """服务端侧重新计算一遍发送端应该给出的 HMAC。"""
    try:
        nonce = base64.b64decode(nonce_b64)
    except Exception:
        nonce = b""
    mac = hmac.new(pin.encode("utf-8"), AUTH_SALT + nonce, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def new_token() -> str:
    return base64.b64encode(os.urandom(24)).decode("ascii")


def _hide():
    import subprocess as sp
    import sys
    if sys.platform != "win32":
        return {}
    si = sp.STARTUPINFO()
    si.dwFlags |= sp.STARTF_USESHOWWINDOW
    return {"startupinfo": si, "creationflags": getattr(sp, "CREATE_NO_WINDOW", 0)}


class TokenStore:
    """已配对设备的长期令牌，避免每次都输 PIN。"""

    def __init__(self, path: str):
        self.path = path
        self.tokens: Dict[str, str] = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.tokens = json.load(f)
        except Exception:
            self.tokens = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.tokens, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, peer_id: str) -> str:
        return self.tokens.get(peer_id, "")

    def put(self, peer_id: str, token: str):
        self.tokens[peer_id] = token
        self.save()

    def clear(self):
        self.tokens = {}
        self.save()


class ReceiverSession:
    """一条完整的接收会话，负责到自己「跑完为止」。"""

    def __init__(self, ctrl: ControlChannel, identity: dict, pin: str,
                 require_pin: bool, tokens: TokenStore, caps: dict,
                 renderer, log: Callable[[str], None] = print,
                 on_state: Optional[Callable[[str, str], None]] = None):
        self.ctrl = ctrl
        self.identity = identity
        self.pin = pin
        self.require_pin = require_pin
        self.tokens = tokens
        self.caps = caps
        self.renderer = renderer
        self.log = log
        self.on_state = on_state or (lambda s, t: None)

        self.stop_flag = threading.Event()
        self.video_info: dict = {}
        self.stats: dict = {}
        self.peer_name = ""
        self.peer_id = ""
        self._assembler = None
        self._got_key = False
        self._skipped_before_key = 0
        self._frames = 0
        self._fps_window_t = 0.0
        self._fps_window_n = 0
        self._fps = 0.0

    # ------------------------------------------------------------ 入口
    def serve(self):
        try:
            if not self._handshake():
                return
            self._start()
        except Exception as e:
            self.log(f"会话异常: {e!r}")
        finally:
            self._cleanup()

    def stop(self, reason: str = ""):
        self.stop_flag.set()

    # ------------------------------------------------------------ 握手
    def _handshake(self) -> bool:
        hello = self.ctrl.recv(timeout=8.0)
        if not hello or hello.get("t") != "hello":
            self.ctrl.send({"t": "error", "code": 400, "msg": "缺少 hello"})
            return False
        self.peer_id = hello.get("id", "")
        self.peer_name = hello.get("name", hello.get("device", "未知设备"))

        import secrets
        nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        self._nonce = nonce
        self.ctrl.send({
            "t": "hello_ack", "id": self.identity["id"],
            "name": self.identity.get("name", "CastLink 接收端"),
            "caps": self.caps, "nonce": nonce,
        })

        auth = self.ctrl.recv(timeout=8.0)
        if not auth or auth.get("t") != "auth":
            self.ctrl.send({"t": "error", "code": 400, "msg": "缺少认证"})
            return False

        token = auth.get("token", "")
        proof = auth.get("resp", "")
        ok_by_token = bool(token) and token == self.tokens.get(self.peer_id)
        ok_by_pin = (not self.require_pin) or (proof == expected_proof(self.pin, self._nonce))
        if not (ok_by_token or ok_by_pin):
            self.ctrl.send({"t": "error", "code": 401, "msg": "配对 PIN 不正确"})
            self.log(f"配对失败: {self.peer_name}")
            return False

        token_out = new_token()
        self.tokens.put(self.peer_id, token_out)
        self.ctrl.send({"t": "auth_ok", "token": token_out})
        self.log(f"已授权「{self.peer_name}」"
                 + ("（长期令牌）" if ok_by_token else "（PIN 配对）"))
        return True

    # ------------------------------------------------------------ 开流
    def _start(self):
        start = self.ctrl.recv(timeout=12.0)
        if not start or start.get("t") != "start":
            self.ctrl.send({"t": "error", "code": 400, "msg": "缺少 start"})
            return
        self.video_info = start.get("video") or {}
        self.audio_info = start.get("audio")
        transport = (start.get("transport") or {}).get("mode", "tcp")

        codec_name = self.video_info.get("codec", "h265")
        codec_id = CODEC_H264 if codec_name in ("h264", "avc") else CODEC_H265
        w = int(self.video_info.get("w", 1920))
        h = int(self.video_info.get("h", 1080))
        fps = int(self.video_info.get("fps", 60))
        quality = start.get("quality", "")
        bitrate = start.get("bitrate", 0)

        from .frames import FrameAssembler
        self._assembler = FrameAssembler(self._on_frame, self._on_drop, self.log)
        self._assembler.reset()
        self._got_key = False
        self._skipped_before_key = 0

        # 1) 建立媒体端点
        if transport == "udp":
            media_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                media_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            except Exception:
                pass
            media_sock.bind(("", 0))
            media_port = media_sock.getsockname()[1]
            listener = None
        else:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            except Exception:
                pass
            listener.bind(("", 0))
            media_port = listener.getsockname()[1]
            listener.listen(1)
            media_sock = None

        self.ctrl.send({"t": "ready", "media": {"mode": transport, "port": media_port}})
        self.log(f"媒体端口 {media_port}（{transport}）· {w}x{h}@{fps} {codec_name}"
                 + (f" · {quality} ≈{bitrate}Mbps" if bitrate else ""))

        if transport == "tcp":
            listener.settimeout(10.0)
            try:
                media_conn, _ = listener.accept()
            except Exception as e:
                self.log(f"等待媒体连接超时: {e}")
                listener.close()
                return
            listener.close()
            try:
                media_conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
        else:
            media_conn = media_sock
            media_conn.settimeout(0.5)

        # 2) 起渲染
        title = f"CastLink · {self.peer_name}"
        if not self.renderer.start(title, codec_id):
            self.ctrl.send({"t": "error", "code": 500, "msg": "无法启动渲染"})
            return

        self.on_state("streaming", self.peer_name)
        self._start_ts = time.time()

        pump = threading.Thread(target=self._control_pump, daemon=True)
        pump.start()
        report = threading.Thread(target=self._report_loop, daemon=True)
        report.start()
        try:
            self._media_loop(media_conn, transport)
        finally:
            self.stop_flag.set()
            self.renderer.stop()
            self.log(f"会话结束（来自「{self.peer_name}」）")

    # ------------------------------------------------------------ 数据处理
    def _media_loop(self, conn, transport: str):
        if transport == "udp":
            while not self.stop_flag.is_set():
                try:
                    pkt, _ = conn.recvfrom(65536)
                except socket.timeout:
                    continue
                except Exception:
                    break
                try:
                    self._assembler.feed_packet(pkt)
                except Exception as e:
                    self.log(f"报文处理异常: {e}")
        else:
            conn.settimeout(0.5)
            while not self.stop_flag.is_set():
                try:
                    chunk = conn.recv(1 << 20)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not chunk:
                    break
                try:
                    self._assembler.feed_bytes(chunk)
                except Exception as e:
                    self.log(f"报文处理异常: {e}")

    def _on_frame(self, stream, codec_id, pts, data, keyframe):
        if stream != STREAM_VIDEO:
            self._audio_frames = getattr(self, "_audio_frames", 0) + 1
            return
        if not self._got_key:
            # 解码器必须先拿到一个完整关键帧（带 VPS/SPS/PPS）才能开工。
            # 中途接入的接收端若从 P 帧开始喂，解码器会报 "Could not find ref"
            # 并连续花屏若干帧，因此这里等到 IDR 再放行。
            if not keyframe:
                self._skipped_before_key += 1
                return
            self._got_key = True
            self.log(f"首帧就位（IDR，{len(data)} 字节，此前跳过 {self._skipped_before_key} 帧）")
        self._frames += 1
        self._fps_window_n += 1
        now = time.perf_counter()
        if self._fps_window_t == 0.0:
            self._fps_window_t = now
        elif now - self._fps_window_t >= 0.5:
            self._fps = self._fps_window_n / (now - self._fps_window_t)
            self._fps_window_n = 0
            self._fps_window_t = now
        self.renderer.write_video(data)

    def _on_drop(self, stream, frame_id):
        pass

    def _control_pump(self):
        while not self.stop_flag.is_set():
            msg = self.ctrl.recv(timeout=1.0)
            if msg is None:
                continue
            t = msg.get("t")
            if t == "ping":
                self.ctrl.send({"t": "pong", "ts": msg.get("ts", 0)})
            elif t == "stop":
                self.log("发送端结束投屏")
                self.stop_flag.set()
                return
            elif t == "bye":
                self.stop_flag.set()
                return

    def _report_loop(self):
        """周期性把解码侧状态回报给发送端（丢帧率 / 抖动），用于它的自适应。"""
        while not self.stop_flag.is_set():
            snap = self._assembler.snapshot() if self._assembler else {}
            self.ctrl.send({
                "t": "stats",
                "fps": round(self._fps, 2),
                "rendered": self._frames,
                "dropped": snap.get("dropped", 0),
                "assembled": snap.get("assembled", 0),
                "lost_packets": snap.get("lost_packets", 0),
                "queue": snap.get("partial", 0),
                "jitter": round(getattr(self._assembler, "jitter_ms", 0.0), 2),
            })
            if self.stop_flag.wait(1.0):
                break

    def _cleanup(self):
        try:
            self.renderer.stop()
        except Exception:
            pass
        self.stop_flag.set()
        try:
            self.ctrl.close()
        except Exception:
            pass
        try:
            self.on_state("idle", "")
        except Exception:
            pass
