# -*- coding: utf-8 -*-
"""
Windows 接收端服务：发现广播 + 会话监听。

它是「投影仪端」在 PC 上的等价实现，用途有两点：
  1. 没有投影仪实机时，用来验证从采集到渲染的整条链路；
  2. 直接把任意 Windows 电脑变成一台接收端（会议室内投大屏/一体机也适用）。
"""
from __future__ import annotations

import socket
import threading
from typing import Callable, Dict, List, Optional

from castlink.control import ControlChannel
from castlink.discovery import Discovery
from castlink.identity import load_identity, app_dir
from castlink.config import ReceiverConfig

from .frames import FrameAssembler
from .renderers.sdl import FfmpegSdlRenderer
from .session import ReceiverSession, TokenStore


class ReceiverServer:
    def __init__(self, ffmpeg: str, cfg: ReceiverConfig,
                 log: Callable[[str], None] = print,
                 on_state: Optional[Callable[[str, str], None]] = None,
                 on_stats: Optional[Callable[[dict], None]] = None,
                 enable_discovery: bool = True,
                 null_output: bool = False):
        self.ffmpeg = ffmpeg
        self.cfg = cfg
        self.log = log
        self.on_state = on_state or (lambda s, t: None)
        self.on_stats = on_stats or (lambda d: None)
        # 同一台机器上同时跑发送端与接收端时，两者都会抢 UDP 47010；
        # 联调模式下关掉广播，改用「手动 IP」直连。
        self.enable_discovery = enable_discovery
        self.null_output = null_output

        self.identity = load_identity("device-recv.json")
        self.tokens = TokenStore(f"{app_dir()}/receiver_tokens.json")
        self.renderer = FfmpegSdlRenderer(ffmpeg, log=log, null_output=null_output)

        self.caps = {
            "maxW": 3840, "maxH": 2160, "maxFps": 60,
            "codecs": ["h265", "h264", "aac"],
            "transports": ["tcp", "udp"],
        }
        self._stop = threading.Event()
        self._srv: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []
        self.sessions: List[ReceiverSession] = []
        self.discovery: Optional[Discovery] = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> bool:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", self.cfg.port))
        except Exception as e:
            self.log(f"控制端口 {self.cfg.port} 绑定失败: {e}")
            return False
        srv.listen(4)
        self._srv = srv

        if self.enable_discovery:
            self.discovery = Discovery(
                role="receiver",
                identity=self.identity,
                advertise={
                    "ctrl": self.cfg.port,
                    "maxW": self.caps["maxW"], "maxH": self.caps["maxH"],
                    "maxFps": self.caps["maxFps"],
                    "codecs": self.caps["codecs"],
                    "transports": self.caps["transports"],
                    "secure": False, "model": "Windows 接收端",
                },
                log=self.log,
            )
            self.discovery.start()

        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        self._threads.append(t)
        self.log(f"接收端已就绪，控制端口 {self.cfg.port}")
        self.on_state("idle", "")
        return True

    def stop(self):
        self._stop.set()
        for s in list(self.sessions):
            try:
                s.stop()
            except Exception:
                pass
        try:
            self.renderer.stop()
        except Exception:
            pass
        if self.discovery:
            self.discovery.stop()
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass
            self._srv = None

    # ---------------------------------------------------------------- 接受连接
    def _accept_loop(self):
        srv = self._srv
        while srv is not None and not self._stop.is_set():
            try:
                srv.settimeout(0.5)
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            ctrl = ControlChannel(conn, addr)
            self.log(f"来自 {addr[0]} 的连接")
            self._spawn_session(ctrl)

    def _spawn_session(self, ctrl: ControlChannel):
        # 一台接收端同一时刻只服务一路投屏
        self.sessions = [s for s in self.sessions if not s.stop_flag.is_set()]
        if self.sessions:
            ctrl.send({"t": "error", "code": 503, "msg": "正在被其他设备投屏中"})
            ctrl.close()
            self.log("已有会话进行中，拒绝新连接")
            return

        session = ReceiverSession(
            ctrl=ctrl, identity=self.identity, pin=self.cfg.pin,
            require_pin=False, tokens=self.tokens, caps=self.caps,
            renderer=self.renderer, log=self.log,
            on_state=self._relay_state,
        )
        self.sessions.append(session)
        t = threading.Thread(target=session.serve, daemon=True)
        self._threads.append(t)
        t.start()

    def _relay_state(self, state: str, info: str):
        self.on_state(state, info)

    def stop_session(self):
        for s in list(self.sessions):
            try:
                s.stop()
            except Exception:
                pass

    def forget_tokens(self):
        self.tokens.clear()
        self.log("已清除配对令牌")
