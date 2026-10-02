# -*- coding: utf-8 -*-
"""
接收端的画面呈现。

本项目里 Windows 端用 ffmpeg 的 SDL2 输出设备显示，
Android 端用 MediaCodec + SurfaceView。两者共用同一个接口，
将来想换成 D3D / OpenGL 渲染只要替换这里的 Renderer。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Callable, Optional

try:
    from castlink.protocol import CODEC_H265, CODEC_H264
except Exception:  # pragma: no cover - 路径兜底
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from castlink.protocol import CODEC_H265, CODEC_H264


def _hide():
    import subprocess as sp
    if sys.platform != "win32":
        return {}
    si = sp.STARTUPINFO()
    si.dwFlags |= sp.STARTF_USESHOWWINDOW
    return {"startupinfo": si, "creationflags": getattr(sp, "CREATE_NO_WINDOW", 0)}


def raw_input_format(codec_id: int) -> str:
    return "hevc" if codec_id == CODEC_H265 else "h264"


PROBE_SIZE = 1 << 18   # 256KB：足够容纳 4K 关键帧与重复下发的参数集


class FfmpegSdlRenderer:
    """
    启动一个 ffmpeg 子进程：Annex-B 原始流 -> 解码 -> SDL 窗口。

    之所以直接用 ffmpeg 而不是 Python 解码：ffmpeg 的 HEVC 解码器有 SSE/AVX 汇编优化，
    4K60 软解在某些机器上也能跑；而 Python 侧逐个像素搬移根本喂不动。
    """

    def __init__(self, ffmpeg: str, log: Callable[[str], None] = print,
                 null_output: bool = False):
        self.ffmpeg = ffmpeg
        self.log = log
        # null_output=True 时只解码不显示，用于无人值守的自动化联调
        self.null_output = null_output
        self.proc: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self, title: str, codec_id: int) -> bool:
        if self.proc is not None:
            return True
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
            # 不要加 -fflags nobuffer：它会让 raw 流的 av_read_frame 在
            # 一个 AU 还没读全时就返回，解码器拿到半个 IDR 就报
            # "Could not find ref with POC 0"。实测同一段流用文件输入 0 错误、
            # 管道 + nobuffer 必有 1 次错误。去掉后首屏干净，延迟无可感增加。
            "-flags", "low_delay",
            "-analyzeduration", "0",
            # probesize 不能设太小：原始 Annex-B 流要靠 parser 从开头若干 KB 里
            # 解析出 VPS/SPS/PPS。设成 32 字节时参数集根本读不全，
            # 解码器会对开头每一帧报 "Could not find ref with POC 0"。
            "-probesize", str(PROBE_SIZE),
            "-f", raw_input_format(codec_id), "-i", "pipe:0",
        ]
        if self.null_output:
            cmd += ["-f", "null", "-"]
        else:
            cmd += ["-pix_fmt", "yuv420p", "-f", "sdl2", title]
        self.log("渲染命令: " + " ".join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, bufsize=0, **_hide())
        except Exception as e:
            self.log(f"启动播放器失败: {e}")
            self.proc = None
            return False
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()
        return True

    def write_video(self, data: bytes) -> bool:
        """
        把一整帧写进解码器。

        这里必须循环写：Windows 的匿名管道单次 write 只写到管道缓冲区的剩余空间，
        而 bufsize=0 时 stdin 是原始 FileIO，`write()` 会如实返回"实际写入了多少字节"。
        4K 无损的关键帧动辄 2MB，一次调用根本写不完 —— 只写一半等于给解码器喂
        截断的 NAL，表现就是首屏报 "Could not find ref with POC 0" 然后花屏。
        """
        if not self.proc or not self.proc.stdin:
            return False
        view = memoryview(data)
        try:
            while view:
                n = self.proc.stdin.write(view)
                if n is None or n <= 0:
                    break
                view = view[n:]
            return not view
        except Exception:
            return False

    def _stderr_loop(self):
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "ignore").strip()
                if line:
                    self.log(f"[播放器] {line}")
        except Exception:
            pass

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stop(self):
        proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
