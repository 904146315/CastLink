# -*- coding: utf-8 -*-
"""
桌面抓取 -> HEVC 编码 -> Annex-B 帧 的流水线。

采集方式优先级
-------------
1. **ddagrab（Desktop Duplication API）**：走 GPU 复制，CPU 占用极低，支持 4K60。
   缺点：远程桌面 / 锁定屏幕 / 部分混合显卡笔记本上会失败。
2. **gdigrab**：GDI 抓屏，兼容性最好，CPU 占用高一些。

运行时会在首次启动时实测两者，择可用者并把结果写进配置。
若会话进行中 ddagrab 失效（例如切换到独显输出），会自动降级重启。
"""
from __future__ import annotations

import ctypes
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from castlink.protocol import AnnexBSplitter, ParameterSetCache, CODEC_H265, CODEC_H264
from .encoders import (
    QUALITY_MODES, build_video_encode_args, estimate_bitrate_mbps,
    run_ffmpeg, _spawn_kwargs, VIDEO_CODEC_HEVC, VIDEO_CODEC_H264,
)

# ---------------------------------------------------------------- 屏幕几何


class _DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("_pad0", ctypes.c_ubyte * 16),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("_pad1", ctypes.c_ulong * 10),
    ]


def primary_resolution() -> Tuple[int, int]:
    """返回主显示器的**物理**分辨率（不受缩放/DPI 虚拟化影响）。"""
    try:
        dm = _DEVMODEW()
        dm.dmSize = ctypes.sizeof(_DEVMODEW)
        if ctypes.windll.user32.EnumDisplaySettingsW(None, 0xFFFFFFFF, ctypes.byref(dm)):
            if dm.dmPelsWidth and dm.dmPelsHeight:
                return int(dm.dmPelsWidth), int(dm.dmPelsHeight)
    except Exception:
        pass
    try:
        return (ctypes.windll.user32.GetSystemMetrics(0),
                ctypes.windll.user32.GetSystemMetrics(1))
    except Exception:
        return 1920, 1080


def _even(v: int) -> int:
    """编码器要求宽高必须是偶数（部分甚至要求 4 的倍数）。"""
    v = max(2, int(v))
    return v - (v % 2)


def _align(v: int, a: int) -> int:
    """
    把尺寸向下对齐到 `a` 的倍数。

    为什么必须对齐：HEVC 的 CTU 是 64×64、H.264 的宏块是 16×16，
    硬件解码器对"宽高不是对齐倍数"的码流往往退化到慢路径甚至直接失败。
    小米笔记本常见的 2880×1800 就是个典型陷阱 —— 1800 既不是 16 的倍数
    也不是 64 的倍数，投影仪上很容易解不动（表现就是"几秒才出一帧"）。
    向下对齐最多裁掉十几行像素，肉眼无感。
    """
    a = max(2, min(64, int(a)))
    v = max(a, int(v))
    v -= v % a
    return v


# 标准分辨率阶梯：降档时优先落到这些尺寸上，而不是任意缩放
_RES_LADDER = [(3840, 2160), (2560, 1440), (1920, 1080), (1280, 720), (960, 540)]


def _fit_budget(w: int, h: int, fps: int, quality: str, chroma: str,
                budget_mbps: float):
    """
    在码率预算内挑一个可用的 (宽, 高, 帧率)。

    降档顺序在 v1.0.2 调整过 —— 老版本是"先把帧率一路砍到 24、再动分辨率"，
    结果用户在投影仪上看到的是 2880×1800@24，画面糊不说还一顿一顿的，
    与"几秒钟刷新一帧"的抱怨高度重合。投屏的用途大多包含视频，
    帧率掉到 30 以下比降一档分辨率难看得多，所以新顺序是：

      1. **保分辨率、保帧率下限**：同分辨率只把 60 帧降到 30 帧；
      2. 30 帧仍超预算，才沿标准分辨率阶梯往下走（每级先试目标帧率、再试 30）；
      3. 阶梯见底还压不下来，才允许低于 30 帧。

    返回 (w, h, fps, 是否降过档)。
    """
    if budget_mbps <= 0:
        return w, h, fps, False
    if estimate_bitrate_mbps(w, h, fps, quality) <= budget_mbps:
        return w, h, fps, False

    floor = 30 if fps >= 30 else fps      # 帧率下限，用户本来就选更低时尊重用户

    def fits(x, y, f):
        return estimate_bitrate_mbps(x, y, f, quality) <= budget_mbps

    # 1) 同分辨率降到帧率下限（60 → 30），观感损失最小的一档
    if fps > floor and fits(w, h, floor):
        return w, h, floor, True

    # 2) 沿标准阶梯降分辨率，逐级往下；每级优先保住目标帧率
    for lw, lh in _RES_LADDER:
        if lw >= w or lh >= h:
            continue
        for f in (fps, floor):
            if f > fps:
                continue
            if fits(lw, lh, f):
                return lw, lh, f, True

    # 3) 阶梯见底仍超标 —— 退到最低档并允许低于 30 帧
    lw, lh = _RES_LADDER[-1]
    for f in (floor, 24):
        if f < fps and fits(lw, lh, f):
            return lw, lh, f, True
    return lw, lh, min(24, fps), True


@dataclass
class StreamPlan:
    capture_w: int
    capture_h: int
    out_w: int
    out_h: int
    fps: int
    codec: str
    quality: str
    chroma: str
    bitrate_mbps: float
    note: str = ""


def make_plan(desktop: Tuple[int, int], fps: int, resolution_mode: str,
              canvas_4k: bool, quality: str, chroma: str,
              max_w: int = 3840, max_h: int = 2160, codec: str = VIDEO_CODEC_HEVC,
              align_w: int = 2, align_h: int = 2,
              bitrate_budget_mbps: float = 0.0) -> StreamPlan:
    """
    把用户选择翻译成实际的采集/输出规格。

    输出必须同时满足：
      * 宽高对齐到解码器要求（默认偶数，投影仪上报后按其对齐值走）
      * 不超过投影仪声明的最大解码能力
      * 估算码率不超过带宽预算（超了就自动降帧率 / 降分辨率，见 _fit_budget）
    """
    dw, dh = desktop
    lossless = (quality == "lossless")

    if resolution_mode == "1080p":
        target = (1920, 1080)
    elif resolution_mode == "1440p":
        target = (2560, 1440)
    elif resolution_mode == "4k":
        target = (3840, 2160)
    else:
        target = (dw, dh)

    if canvas_4k and target[0] < 3840:
        # 将桌面等比放大到 4K 画布，黑边补齐，保证投影仪接收到标准 4K 信号
        target = (3840, 2160)

    notes: List[str] = []

    tw, th = target
    if lossless and (tw > dw or th > dh):
        # 无损的立身之本是"解码输出 == 桌面原始像素"。
        # 放大是插值，不可逆，放大后再做无损编码是自欺欺人。
        # 需要标准 4K 信号请选「视觉无损」——它放大后依然是肉眼无差。
        tw, th = dw, dh
        notes.append(f"无损模式保持桌面原始分辨率 {tw}x{th}（放大插值会破坏像素级保真）")

    if (not lossless and resolution_mode == "native" and not canvas_4k
            and tw == dw and th == dh):
        # 「原生分辨率」听起来最保真，但笔记本面板尺寸往往不是解码器友好的
        # 标准值：小米本常见的 2880×1800，高 1800 既不是 16 也不是 64 的倍数，
        # 投影仪 SoC 遇上这种尺寸常常退到慢速路径甚至改用软解，表现就是"几秒一帧"。
        # 而投影仪面板基本都是标准尺寸（坚果 N1S 4K 特别版是 3840×2160），
        # 所以向上吸附到"刚好装得下桌面"的最小标准档：电脑侧用高质量缩放做一次
        # 重采样，换到标准尺寸码流 + 与面板 1:1 的像素映射，投影仪只解码不缩放。
        snap = None
        for lw, lh in _RES_LADDER:
            if lw >= dw and lh >= dh:
                snap = (lw, lh)      # 阶梯是降序，循环结束时留下的就是最小可用档
        if snap and (dw, dh) not in _RES_LADDER and snap[0] <= max_w and snap[1] <= max_h:
            tw, th = snap
            notes.append(f"桌面 {dw}×{dh} 不是解码器友好的标准尺寸，已升采样到 "
                         f"{snap[0]}×{snap[1]}，与投影仪面板 1:1（投影仪只解码不缩放）")

    if tw > max_w or th > max_h:
        # 投影仪吃不下目标分辨率时，不要按比例缩成"刚好装得下"的尺寸
        # （2880×1800 强行塞进 1920×1080 会得到 1728×1080 —— 又是个非标尺寸，
        # 白白把前面躲开的坑再踩一遍）。改成落到标准阶梯里最大的一档，
        # 宽高比不一致的部分由编码前的 pad 补黑边，投影仪收到的永远是标准尺寸。
        fit = None
        for lw, lh in _RES_LADDER:
            if lw <= max_w and lh <= max_h and lw <= tw and lh <= th:
                fit = (lw, lh)
                break
        if fit:
            tw, th = fit
            notes.append(f"投影仪最大支持 {max_w}×{max_h}，输出改为标准 {tw}×{th}"
                         f"（宽高比不一致处补黑边）")
            if lossless:
                notes.append("无损模式在此规格下已非像素级保真")
        else:
            k = min(max_w / tw, max_h / th)
            tw, th = int(tw * k), int(th * k)
            if lossless:
                notes.append(f"投影仪最大支持 {max_w}x{max_h}，无损模式被迫缩放至 {tw}x{th}"
                             f"（此时已非像素级保真）")
            else:
                notes.append(f"已按投影仪能力缩放至 {tw}x{th}")

    # 对齐到解码器要求的倍数 —— 但**标准尺寸不参与对齐**。
    # H.264 的宏块是 16×16，很多机型上报的 alignH 就是 16，而 1080 并不是 16 的
    # 倍数（16×67=1072）。真按上报值向下对齐，1920×1080 会被砍成 1920×1072：
    # 明明是个全世界都支持的尺寸，反而被弄得非标。SPS 本身就带裁剪信息，
    # 标准尺寸交给编码器正常处理即可，只有非标尺寸才需要对齐。
    if (tw, th) not in _RES_LADDER:
        tw = _align(tw, align_w)
        th = _align(th, align_h)
    tw, th = _even(tw), _even(th)

    # 码率预算：只有非无损模式才自动降档（无损是用户明确要的像素级保真）
    if not lossless and bitrate_budget_mbps > 0:
        fw, fh, ffps, reduced = _fit_budget(tw, th, int(fps), quality, chroma,
                                            bitrate_budget_mbps)
        if reduced:
            est0 = estimate_bitrate_mbps(tw, th, int(fps), quality)
            notes.append(f"为控制在 {bitrate_budget_mbps:.0f} Mbps 带宽预算内，"
                         f"已从 {tw}x{th}@{fps} 降为 {fw}x{fh}@{ffps}"
                         f"（原估算 {est0:.0f} Mbps）")
            tw, th, fps = fw, fh, ffps
    elif lossless:
        # 真无损的码率是"像素数×深度×帧率"级别，2880×1800@60 算下来接近 1 Gbps，
        # 任何 Wi-Fi 都扛不住 —— 与其让用户以为是软件坏了，不如提前说清楚。
        est = estimate_bitrate_mbps(tw, th, int(fps), quality)
        if est > 150:
            notes.append(f"无损模式估算码率约 {est:.0f} Mbps，普通 Wi-Fi 远扛不住，"
                         f"画面会严重卡顿；同等观感请改用「视觉无损」")
    note = "；".join(notes)

    return StreamPlan(
        capture_w=dw, capture_h=dh, out_w=tw, out_h=th,
        fps=int(fps), codec=codec, quality=quality, chroma=chroma,
        bitrate_mbps=estimate_bitrate_mbps(tw, th, fps, quality), note=note,
    )


# ---------------------------------------------------------------- 采集方式探测

def _probe_capture_method(ffmpeg: str, method: str, seconds: float = 1.0) -> bool:
    if method == "ddagrab":
        inp = ["-f", "lavfi", "-i", "ddagrab=draw_mouse=1"]
    else:
        inp = ["-f", "gdigrab", "-framerate", "30", "-i", "desktop"]
    args = ["-hide_banner", "-loglevel", "error", "-nostdin", "-y", *inp,
            "-frames:v", "5", "-c:v", "libx264", "-preset", "ultrafast",
            "-f", "rawvideo", "-"]
    try:
        p = subprocess.run([ffmpeg, *args], stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=6.0, **_spawn_kwargs())
        return p.returncode == 0
    except Exception:
        return False


def detect_capture_method(ffmpeg: str) -> str:
    if _probe_capture_method(ffmpeg, "ddagrab"):
        return "ddagrab"
    return "gdigrab"


# ---------------------------------------------------------------- 视频流水线

class VideoPipeline:
    """
    启动一个 ffmpeg 子进程完成「抓屏 + 编码」，把 stdout 里的 Annex-B 流
    切成一帧一帧交给回调。

    所有异常都会被记到 log 并通过 on_error 回调上报，由上层决定是否降级重启。
    """

    READ_SIZE = 1 << 20   # 1MB：兼顾系统调用次数与内存

    def __init__(self, ffmpeg: str, log=print):
        self.ffmpeg = ffmpeg
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_error = ""
        # 每次 start() 递增：上一轮残留的读取线程看到代号变了就自己收摊，
        # 不会把旧会话的帧喂进新会话（否则新投屏开头会插入几帧完全过时的画面）。
        self._gen = 0

    # -- 组装命令行 --
    @staticmethod
    def build_command(ffmpeg: str, plan: StreamPlan, encoder_name: str,
                      capture_method: str, draw_mouse: bool = True,
                      bitrate_cap_mbps: float = 0.0,
                      low_latency: bool = True) -> List[str]:
        if plan.codec == VIDEO_CODEC_H264 and not encoder_name.startswith("h264"):
            raise ValueError("H.264 输出必须搭配 H.264 编码器")
        if plan.codec == VIDEO_CODEC_HEVC and not encoder_name.startswith(("hevc", "libx265")):
            raise ValueError("HEVC 输出必须搭配 HEVC 编码器")

        cmd: List[str] = [ffmpeg, "-hide_banner", "-nostdin", "-y",
                          "-loglevel", "warning",
                          "-fflags", "nobuffer", "-analyzeduration", "0",
                          "-probesize", "32"]

        if capture_method == "ddagrab":
            filt = f"ddagrab=draw_mouse={1 if draw_mouse else 0}"
            cmd += ["-f", "lavfi", "-i", filt]
        else:
            cmd += ["-f", "gdigrab", "-draw_mouse", "1" if draw_mouse else "0",
                    "-framerate", str(plan.fps), "-i", "desktop"]

        vf = []
        use_passthrough = (plan.out_w == plan.capture_w and plan.out_h == plan.capture_h)
        if not use_passthrough:
            # 注意：libswscale 命令行里"最近邻"叫 neighbor，
            # 写 point（C 宏 SWS_POINT 的名字）会被滤镜解析器拒绝。
            flags = "neighbor" if plan.quality == "lossless" else "bicubic"
            # 保持宽高比并居中补黑边，避免画面被拉伸变形
            vf.append(f"scale={plan.out_w}:{plan.out_h}"
                      f":force_original_aspect_ratio=decrease:flags={flags}")
            vf.append(f"pad={plan.out_w}:{plan.out_h}:(ow-iw)/2:(oh-ih)/2:color=black")
        # ddagrab 默认按显示器刷新率输出，这里显式锁帧；gdigrab 已在输入端指定了
        vf.append(f"fps={plan.fps}")
        vf.append("setsar=1")
        cmd += ["-vf", ",".join(vf)]

        cmd += build_video_encode_args(
            encoder_name, plan.quality, plan.chroma,
            low_latency=low_latency, fps=plan.fps,
            bitrate_cap_mbps=bitrate_cap_mbps,
            width=plan.out_w, height=plan.out_h,
        )
        # 单 slice 已在 codec args 中处理，这里去掉重复项
        cmd += ["-an", "-sn", "-dn"]
        # flush_packets：**每一帧编完立刻推出管道**，不许在 ffmpeg 的输出缓冲里攒。
        #
        # ffmpeg 的 AVIO 写缓冲默认 32KB，够小？静态桌面下 P 帧往往只有几百字节到
        # 一两 KB，32KB 就是 20~30 帧、整整一秒的画面被扣在缓冲里 —— 用户看到的
        # "画面有 1 秒延迟"有它一份。实测：不加这个参数时，帧是**成簇**到达的
        # （每批 5~7ms 内连来好几帧），加上之后变成均匀的 41ms 一帧。
        cmd += ["-flush_packets", "1"]
        raw_fmt = "hevc" if plan.codec == VIDEO_CODEC_HEVC else "h264"
        cmd += ["-f", raw_fmt, "pipe:1"]
        return cmd

    # -- 生命周期 --
    def start(self, cmd: List[str], frame_cb: Callable[[bytes, bool, int], None],
              on_exit: Optional[Callable[[int], None]] = None,
              codec: str = VIDEO_CODEC_HEVC):
        # 代号必须在 stop() **之前**自增，顺序不能调。
        #
        # 旧写法是先 stop() 再 `_gen += 1`，注释写的是"代号先加、再清 _stop"，
        # 但位置在 stop() 之后 —— 而 stop() 正是那个会杀掉旧进程、让旧 _read_loop
        # 走到结尾并做 `gen == self._gen` 判断的地方。于是存在一个窗口：
        # 进程已经死了、代号还没加，判断成立，**我们自己主动杀掉的进程被当成崩溃上报**，
        # 上层 `_on_video_exit` 据此消耗掉一次编码器降级名额
        # （hevc_qsv → libx265，软编跟不上 → 更多丢帧 → 更多重启）。
        #
        # 真机日志里的铁证：每次"投影仪请求关键帧"后面必然跟着两条
        #   采集进程退出 code=0
        #   尝试降级到编码器 libx265
        # 而 code=0 恰好是 `proc.poll()` 在进程尚未被系统回收时返回 None 的兜底值，
        # 说明这条上报根本不知道进程是怎么结束的 —— 它只是一个过期的旧代号事件。
        self._gen += 1
        self.stop()
        self._stop.clear()
        self._last_error = ""
        self.plan_codec = codec
        gen = self._gen
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL,
                                         bufsize=0, **_spawn_kwargs())
        except Exception as e:
            self._last_error = f"无法启动 ffmpeg: {e}"
            self.log(self._last_error)
            if on_exit:
                on_exit(-1)
            return False

        self._on_exit = on_exit
        self._reader = threading.Thread(target=self._read_loop,
                                        args=(frame_cb, gen), daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()
        return True

    def _stderr_loop(self):
        if not self.proc or not self.proc.stderr:
            return
        gen = self._gen
        try:
            for raw in self.proc.stderr:
                if self._stop.is_set() or gen != self._gen:
                    break
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                self._last_error = line
                self.log(f"[ffmpeg] {line}")
        except Exception:
            pass

    def _read_loop(self, frame_cb, gen: int):
        codec_id = CODEC_H265 if self.plan_codec == VIDEO_CODEC_HEVC else CODEC_H264
        splitter = AnnexBSplitter(codec_id)
        psc = ParameterSetCache()
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        stream_start = time.monotonic_ns()
        while not self._stop.is_set() and gen == self._gen:
            try:
                chunk = proc.stdout.read(self.READ_SIZE)
            except Exception:
                break
            if not chunk:
                break
            now_us = (time.monotonic_ns() - stream_start) // 1000
            try:
                frames = splitter.feed(chunk)
            except Exception as e:
                self.log(f"帧切分异常: {e}")
                continue
            for au, is_key in frames:
                if is_key:
                    psc.scan(au, codec_id)
                    au = psc.prefix(True) + au
                try:
                    frame_cb(au, is_key, now_us)
                except Exception as e:
                    self.log(f"帧回调异常: {e}")
        for au, is_key in splitter.flush():
            if self._stop.is_set() or gen != self._gen:
                break
            try:
                frame_cb(au, is_key, (time.monotonic_ns() - stream_start) // 1000)
            except Exception:
                pass
        code = proc.poll()
        if code is None:
            code = 0
        # 只在"还是当前这一代"时才上报退出 —— 否则会把上一次的退出事件
        # 当成新会话的意外退出，触发一次莫名其妙的编码器降级。
        if self._on_exit and gen == self._gen:
            self._on_exit(code)

    def stop(self, wait: float = 2.0):
        self._stop.set()
        proc = self.proc
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=wait)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
        self.proc = None

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)
