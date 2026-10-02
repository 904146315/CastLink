# -*- coding: utf-8 -*-
"""通用工具：路径、外部依赖定位、单实例守护、本机地址探测。"""
from __future__ import annotations

import os
import sys
import socket
from pathlib import Path
from typing import Optional, List


def _base_dir() -> Path:
    # PyInstaller onefile 会把资源解压到 sys._MEIPASS
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)          # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()


def resource_path(*parts: str) -> str:
    """打包后资源仍在 BASE_DIR 下，开发期也在 sender/ 下，统一入口。"""
    p = BASE_DIR.joinpath(*parts)
    if p.exists():
        return str(p)
    return str(Path(__file__).resolve().parent.parent.joinpath(*parts))


def ffmpeg_search_paths() -> List[str]:
    from castlink import ffmpeg as ff
    return ff.search_paths()


def find_ffmpeg(explicit: Optional[str] = None) -> Optional[str]:
    """统一走共享实现，保证两端挑到同一个 ffmpeg。"""
    from castlink import ffmpeg as ff
    return ff.find(explicit)


def local_ip_for(target: str = "8.8.8.8") -> str:
    """探测去往 target 时本机使用的出口 IP（不发实际数据包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


class SingleInstanceGuard:
    """
    单实例守护。

    用本地 TCP 端口占位而不是文件锁：进程被强杀后端口会立即释放，
    不会留下需要手工清理的残留锁文件。
    """

    def __init__(self, port: int = 47831):
        self.port = port
        self._sock: Optional[socket.socket] = None

    def try_acquire(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind(("127.0.0.1", self.port))
            s.listen(1)
            self._sock = s
            return True
        except OSError:
            return False

    def release(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


def format_bitrate(mbps: float) -> str:
    if mbps >= 1000:
        return f"{mbps / 1000:.2f} Gbps"
    if mbps >= 1:
        return f"{mbps:.1f} Mbps"
    return f"{mbps * 1000:.0f} Kbps"


def format_bytes(n: int) -> str:
    step = 1024.0
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < step or unit == "TB":
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= step
    return f"{v:.1f} TB"
