# -*- coding: utf-8 -*-
"""
系统声音采集 -> AAC -> ADTS。

Windows 上没有统一的"抓系统声音"API，回环设备有三种形态，按可靠性依次尝试：

1. **外部回环设备**（DirectShow）：Realtek/Conexant 的「立体声混音 (Stereo Mix)」、
   虚拟声卡（VB-CABLE、Voicemeeter）等。
   优点：ffmpeg 直接采，代码简单；缺点：**现代笔记本 / 品牌机基本默认禁用或干脆不提供**，
   需要用户自己去声音设置里翻出来 —— 指望不上。

2. **WASAPI 回环**（`pyaudiowpatch`）：直接对**默认扬声器**开一个 loopback 捕获流，
   抓到的就是正在播放的系统声音。**不需要用户装任何东西、改任何设置**，
   是 v1.0.2 起的主力方案。

   ⚠️ 注意：本项目的 ffmpeg 是官方 gyan 构建，`ffmpeg -devices` 里**没有** wasapi
   输入设备（只有 dshow / gdigrab / lavfi / vfwcap）。所以早期版本里
   `-f wasapi -list_devices` 那段枚举永远不会命中，音频也就永远起不来 ——
   这正是"投屏有画面但没声音"的根因。现在改成由 Python 侧负责 WASAPI 捕获，
   把 PCM 从 stdin 喂给 ffmpeg 编码，绕开了 ffmpeg 的设备支持问题。

3. 全部失败时**静默关闭音频**，而不是让整条投屏链路挂掉。

PCM 数据流：
    [默认扬声器] --WASAPI loopback--> 16bit PCM --> ffmpeg stdin --> ADTS AAC --> 网络
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

AAC_BITRATE = "160k"
AAC_RATE = 48000

# PCM 每个读块：1024 帧 @48kHz ≈ 21ms，够小以保证音频延迟，又不至于把 CPU 打满
PCM_CHUNK_FRAMES = 1024

_LOOPBACK_KEYWORDS = [
    ("立体声混音", 100), ("Stereo Mix", 100), ("What U Hear", 95),
    ("CABLE Output", 90), ("Voicemeeter Output", 88), ("Virtual", 70),
    ("虚拟", 70), ("Loopback", 80),
]
_RENDER_KEYWORDS = [("扬声器", 60), ("Speaker", 55), ("耳机", 50), ("Headphone", 50)]


# ------------------------------------------------------------------ 音源描述

@dataclass
class AudioSource:
    """一个已确定可用的音源。"""

    kind: str                 # "device"（ffmpeg 采） | "loopback"（Python 采）
    name: str
    proto: str = "dshow"      # 仅 kind=="device" 有意义
    index: int = -1           # 仅 kind=="loopback"：PyAudio 设备索引
    rate: int = AAC_RATE
    channels: int = 2

    @property
    def label(self) -> str:
        if self.kind == "loopback":
            return f"{self.name}（WASAPI 系统声音回环）"
        return f"{self.name}（{self.proto}）"


def _hide():
    import subprocess as sp
    import sys
    if sys.platform != "win32":
        return {}
    si = sp.STARTUPINFO()
    si.dwFlags |= sp.STARTF_USESHOWWINDOW
    return {"startupinfo": si, "creationflags": getattr(sp, "CREATE_NO_WINDOW", 0)}


# ------------------------------------------------------------------ 设备枚举

def list_devices(ffmpeg: str) -> List[Tuple[str, str]]:
    """返回 [(协议, 设备名)]。只枚举 dshow —— 本 ffmpeg 无 wasapi 输入设备。"""
    out: List[Tuple[str, str]] = []
    seen = set()
    for proto, args in (
        ("dshow", ["-f", "dshow", "-list_devices", "true", "-i", "dummy"]),
    ):
        try:
            p = subprocess.run([ffmpeg, "-hide_banner", *args],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=8.0, **_hide())
        except Exception:
            continue
        txt = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "ignore")
        for line in txt.splitlines():
            mm = re.search(r'"([^"]+)"', line)
            if not mm:
                continue
            name = mm.group(1)
            if name in ("dummy", "audio", "video"):
                continue
            if "(audio)" not in line:
                continue
            key = (proto, name)
            if key in seen:
                continue
            seen.add(key)
            out.append((proto, name))
    return out


def pick_loopback(ffmpeg: str) -> Optional[Tuple[str, str]]:
    """按关键字打分挑一个最像回环设备的（外部设备路径）。"""
    devices = list_devices(ffmpeg)
    best = None
    best_score = 0
    for proto, name in devices:
        score = 0
        for kw, s in _LOOPBACK_KEYWORDS:
            if kw.lower() in name.lower():
                score = max(score, s)
        if score > best_score:
            best_score, best = score, (proto, name)
    return best if best_score >= 50 else None


def find_wasapi_loopback() -> Optional[AudioSource]:
    """
    用 WASAPI 回环找"当前默认扬声器"的捕获端点。

    这是零配置方案：不需要 Stereo Mix，也不需要装虚拟声卡。
    返回 None 表示不可用（非 Windows / 缺 pyaudiowpatch / 驱动不支持）。

    整段都套在 PA_LOCK 里：这里的 PyAudio() 与 terminate() 会动 PortAudio 的
    全局状态，和采集泵并发执行会直接把进程打崩（见 castlink/padev.py）。
    """
    from castlink.padev import PA_LOCK
    try:
        import pyaudiowpatch as pyaudio
    except Exception:
        return None
    with PA_LOCK:
        return _find_wasapi_loopback_locked(pyaudio)


def _find_wasapi_loopback_locked(pyaudio) -> Optional[AudioSource]:
    pa = None
    try:
        pa = pyaudio.PyAudio()
        try:
            api = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out = int(api.get("defaultOutputDevice", -1))
        except Exception:
            default_out = -1

        # 优先直接用"默认扬声器对应的 loopback 设备"
        candidates = []
        try:
            for lb in pa.get_loopback_device_info_generator():
                candidates.append(lb)
        except Exception:
            pass
        if not candidates:
            # 有些驱动只把 loopback 暴露成默认输出设备本身
            if default_out >= 0:
                try:
                    info = pa.get_device_info_by_index(default_out)
                    if info.get("isLoopbackDevice") or int(info.get("maxInputChannels", 0)) > 0:
                        candidates.append(info)
                except Exception:
                    pass
        if not candidates:
            return None

        # 优先与默认输出设备同名的那个
        best = None
        default_name = ""
        if default_out >= 0:
            try:
                default_name = str(pa.get_device_info_by_index(default_out).get("name", ""))
            except Exception:
                default_name = ""
        for info in candidates:
            name = str(info.get("name", "")) or "系统声音回环"
            if default_name and default_name in name:
                best = info
                break
            if best is None:
                best = info
        if best is None:
            return None

        ch = int(best.get("maxInputChannels") or 2)
        ch = max(1, min(2, ch))
        rate = int(best.get("defaultSampleRate") or AAC_RATE)
        return AudioSource(kind="loopback", name=str(best.get("name", "系统声音回环")),
                           index=int(best["index"]), rate=rate, channels=ch)
    except Exception:
        return None
    finally:
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass


def resolve_source(ffmpeg: str, device: Optional[str] = None,
                   proto: Optional[str] = None) -> Optional[AudioSource]:
    """
    按可靠性依次挑音源：用户指定 > 外部回环设备 > WASAPI 回环。
    """
    if device:
        return AudioSource(kind="device", name=device, proto=proto or "dshow")

    picked = pick_loopback(ffmpeg)
    if picked:
        return AudioSource(kind="device", name=picked[1], proto=picked[0])

    return find_wasapi_loopback()


# ------------------------------------------------------------------ ffmpeg 命令

def build_encode_tail() -> List[str]:
    """PCM/设备输入之后统一的 AAC 编码输出参数。"""
    return ["-ar", str(AAC_RATE), "-ac", "2",
            "-c:a", "aac", "-b:a", AAC_BITRATE, "-profile:a", "aac_low",
            "-f", "adts", "pipe:1"]


def build_command(ffmpeg: str, device: Optional[str] = None,
                  proto: Optional[str] = None) -> Optional[List[str]]:
    """外部设备路径的命令（保持向后兼容）。"""
    if not device:
        picked = pick_loopback(ffmpeg)
        if not picked:
            return None
        proto, device = picked
    proto = proto or "dshow"
    inp = ["-f", proto, "-audio_buffer_size", "30", "-i", f"audio={device}"]
    return [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
            *inp, *build_encode_tail()]


def build_loopback_command(ffmpeg: str, source: AudioSource) -> List[str]:
    """WASAPI 回环路径：PCM 从 stdin 进来，ADTS 从 stdout 出去。"""
    return [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
            "-f", "s16le", "-ar", str(source.rate), "-ac", str(source.channels),
            "-i", "pipe:0",
            *build_encode_tail()]


# ------------------------------------------------------------------ 流水线

class AudioPipeline:
    """独立的音频采集子进程；与主视频链路互不阻塞。"""

    def __init__(self, ffmpeg: str, log=print):
        self.ffmpeg = ffmpeg
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._pump: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_error = ""
        self._device: Optional[str] = None       # 用户在设置里手动指定
        self._device_proto: Optional[str] = None
        self.source: Optional[AudioSource] = None
        self._resolved = False
        # 每次 start() 递增：上一轮的回环泵 / ADTS 读取线程看到代号变了就收摊，
        # 免得把旧会话的 PCM 灌进新一轮的 ffmpeg、或把旧音频帧当新数据发出去。
        self._gen = 0
        # 保护 self.proc 的读写：泵线程要拿 stdin，stop() 会把 proc 置空。
        self._lock = threading.Lock()

    # ---- 源解析（缓存）----
    def resolve(self, force: bool = False) -> Optional[AudioSource]:
        if self._resolved and not force and self.source is not None:
            return self.source
        self.source = resolve_source(self.ffmpeg, self._device, self._device_proto)
        self._resolved = True
        return self.source

    def set_device(self, device: Optional[str], proto: Optional[str] = None):
        """用户在设置里指定外部设备；None 表示自动。"""
        self._device = device or None
        self._device_proto = proto
        self._resolved = False
        self.source = None

    def available(self, device: Optional[str] = None) -> bool:
        old = self.source
        old_resolved = self._resolved
        self._device = device or self._device
        self._resolved = False
        ok = self.resolve(force=True) is not None
        # 只是探测，不改变已缓存的状态
        self.source = old
        self._resolved = old_resolved
        return ok

    # ---- 生命周期 ----
    def start(self, frame_cb: Callable[[bytes, int], None],
              device: Optional[str] = None,
              on_exit: Optional[Callable[[int], None]] = None,
              progress: Optional[Callable[[str], None]] = None) -> bool:
        if device:
            self.set_device(device)
        src = self.resolve(force=not self._resolved)
        if src is None:
            self.log("未找到可用的系统声音采集方式（既无外部回环设备，"
                     "WASAPI 回环也不可用），已关闭音频")
            return False

        self.stop()
        # 代号先加、再清 _stop：保证上一轮线程在 _stop 被清掉的瞬间就已经"过期"
        self._gen += 1
        self._stop.clear()
        self._last_error = ""
        gen = self._gen

        if src.kind == "loopback":
            cmd = build_loopback_command(self.ffmpeg, src)
        else:
            cmd = build_command(self.ffmpeg, src.name, src.proto)
            if not cmd:
                return False

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if src.kind == "loopback" else subprocess.DEVNULL,
                bufsize=0, **_hide())
        except Exception as e:
            self.log(f"音频进程启动失败: {e}")
            return False

        threading.Thread(target=self._err_loop, args=(gen,), daemon=True).start()
        self._thread = threading.Thread(target=self._read_loop,
                                        args=(frame_cb, on_exit, gen), daemon=True)
        self._thread.start()

        started = True
        if src.kind == "loopback":
            self._pump = threading.Thread(target=self._pcm_pump, args=(src, gen),
                                          daemon=True)
            self._pump.start()
            self.log(f"音频已启用：{src.label} {src.rate}Hz×{src.channels}")
        else:
            self.log(f"音频已启用：{src.label}")

        # 给 ffmpeg 一点时间，如果它立刻死了（设备被占用等），这里就能发现
        time.sleep(0.35)
        if self.proc.poll() is not None:
            self.log(f"音频进程立即退出：{self._last_error or '未知原因'}")
            self.stop()
            return False
        if progress:
            try:
                progress(src.label)
            except Exception:
                pass
        return started

    # ---- WASAPI 回环 PCM 泵 ----
    def _pcm_pump(self, src: AudioSource, gen: int):
        """
        从 WASAPI loopback 读 16bit PCM 喂给 ffmpeg 的 stdin。

        注意：没有声音在播放时，部分驱动的 loopback 读会阻塞 —— 这是正常的，
        ffmpeg 侧不会因此报错，一有声音就立刻续上。
        """
        pa = None
        stream = None
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            self._last_error = f"pyaudiowpatch 不可用: {e}"
            self.log(self._last_error)
            self._terminate_proc()
            return
        from castlink.padev import PA_LOCK
        try:
            # PyAudio 的创建与流打开必须在 PA_LOCK 里 ——
            # PortAudio 的初始化是进程级全局状态，和另一个线程的
            # PyAudio()/terminate() 撞上会直接把进程打崩。
            with PA_LOCK:
                pa = pyaudio.PyAudio()
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=src.channels,
                    rate=src.rate,
                    input=True,
                    input_device_index=src.index,
                    frames_per_buffer=PCM_CHUNK_FRAMES,
                )
            # stdin 在**本线程启动时**就固定下来：绝不能在循环里重新读
            # self.proc.stdin —— 重启会话后 self.proc 指向新进程，
            # 老泵会把自己的 PCM 灌进新一轮的 ffmpeg。
            with self._lock:
                stdin = self.proc.stdin if self.proc else None
            if stdin is None:
                return
            while not self._stop.is_set() and gen == self._gen:
                try:
                    data = stream.read(PCM_CHUNK_FRAMES, exception_on_overflow=False)
                except Exception as e:
                    if self._stop.is_set():
                        break
                    self._last_error = f"回环读取失败: {e}"
                    self.log(self._last_error)
                    break
                if not data:
                    continue
                try:
                    stdin.write(data)
                except Exception:
                    break
        except Exception as e:
            if not self._stop.is_set():
                self._last_error = f"回环流打开失败: {e}"
                self.log(self._last_error)
                self._terminate_proc()
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    with PA_LOCK:
                        pa.terminate()
                except Exception:
                    pass

    def _terminate_proc(self):
        proc = self.proc
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    # ---- ADTS 读取 ----
    def _err_loop(self, gen: int):
        proc = self.proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in proc.stderr:
                if gen != self._gen:
                    break
                line = raw.decode("utf-8", "ignore").strip()
                if line:
                    self._last_error = line
                    self.log(f"[音频] {line}")
        except Exception:
            pass

    def _read_loop(self, frame_cb, on_exit, gen: int):
        from castlink.protocol import AdtsSplitter
        splitter = AdtsSplitter()
        start_ns = time.monotonic_ns()
        proc = self.proc
        if not proc or not proc.stdout:
            return
        while not self._stop.is_set() and gen == self._gen:
            try:
                chunk = proc.stdout.read(65536)
            except Exception:
                break
            if not chunk:
                break
            now_us = (time.monotonic_ns() - start_ns) // 1000
            for frame in splitter.feed(chunk):
                try:
                    frame_cb(frame, now_us)
                except Exception as e:
                    self.log(f"音频回调异常: {e}")
        # 只在本代有效时上报退出，避免上一轮的退出事件被算到新会话头上
        if on_exit and gen == self._gen:
            on_exit(proc.poll() if proc is not None else 0)

    def stop(self):
        self._stop.set()
        with self._lock:
            proc = self.proc
            self.proc = None
        if proc:
            stdin = getattr(proc, "stdin", None)
            if stdin:
                try:
                    stdin.close()
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
        # 等 ADTS 读取线程收尾，免得它还在往 frame_cb 里推上一轮的音频帧
        th = self._thread
        if th and th.is_alive() and th is not threading.current_thread():
            try:
                th.join(timeout=1.0)
            except Exception:
                pass
        self._thread = None
        self._pump = None

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def source_label(self) -> str:
        return self.source.label if self.source else "未启用"
