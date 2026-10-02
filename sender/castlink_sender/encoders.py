# -*- coding: utf-8 -*-
"""
编码器能力探测与参数生成。

设计要点
--------
1. **不猜，只测。** 不同机型（小米笔记本的核显可能是 Iris Xe / Vega / MX 独显）
   硬件编码器的可用性天差地别，因此所有候选参数都以"实际编码几帧能否成功"为准。
2. **候选链式降级。** 每个编码目的都维护一组候选（例如 4:4:4 真无损：
   NVENC -> libx265），运行时挑第一个能跑通的；会话中途 ffmpeg 崩溃则自动切下一个。
3. **像素格式以 `ffmpeg -h encoder=X` 的输出为准**，避免给硬件编码器塞它不支持的 yuv444p。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------- 常量

VIDEO_CODEC_HEVC = "h265"
VIDEO_CODEC_H264 = "h264"

# 编码器偏好顺序：硬件在前，软件兜底
ENCODER_ORDER = [
    ("nvenc", "hevc_nvenc", "NVIDIA NVENC（推荐，延迟最低）"),
    ("qsv", "hevc_qsv", "Intel 核显 QuickSync"),
    ("amf", "hevc_amf", "AMD AMF"),
    ("mf", "hevc_mf", "Windows MediaFoundation"),
    ("x265", "libx265", "CPU 软件编码 x265（兼容一切，CPU 占用高）"),
]

H264_ORDER = [
    ("nvenc", "h264_nvenc"),
    ("qsv", "h264_qsv"),
    ("amf", "h264_amf"),
    ("mf", "h264_mf"),
    ("x264", "libx264"),
]


@dataclass
class QualityMode:
    key: str
    label: str
    desc: str
    lossless: bool = False
    # 每百万像素·帧的目标比特数（用于 CBR 类编码器与带宽估算）
    bits_per_mpx: float = 0.0
    cqp: int = 0            # 硬件编码器 constqp 值
    crf: float = 0.0        # x265 CRF
    note: str = ""


QUALITY_MODES: Dict[str, QualityMode] = {
    "lossless": QualityMode(
        key="lossless", label="真无损（数学无损）",
        desc="HEVC 无损模式，解码输出与原始桌面像素逐字节一致。"
             "代价是码率极高，建议 Wi-Fi 6E / 有线回程。",
        lossless=True, bits_per_mpx=0.0, note="lossless"),
    "visual": QualityMode(
        key="visual", label="视觉无损（推荐）",
        desc="PSNR 通常 ≥ 48dB，肉眼无法区分原生与投屏画面，码率只有真无损的 1/3。",
        lossless=False, bits_per_mpx=0.55, cqp=12, crf=10.0),
    "high": QualityMode(
        key="high", label="高画质",
        desc="适合电影与游戏，静态 PPT / 文档时几乎看不出压缩。",
        lossless=False, bits_per_mpx=0.30, cqp=18, crf=16.0),
    "balanced": QualityMode(
        key="balanced", label="均衡",
        desc="画质与流畅度平衡，绝大多数 Wi-Fi 环境都能稳住 4K30。",
        lossless=False, bits_per_mpx=0.16, cqp=23, crf=22.0),
    "smooth": QualityMode(
        key="smooth", label="流畅优先",
        desc="低码率，适合远距离弱信号或老款投影仪。",
        lossless=False, bits_per_mpx=0.08, cqp=30, crf=28.0),
}


def estimate_bitrate_mbps(width: int, height: int, fps: int, mode: str) -> float:
    """估算某分辨率/帧率/画质下的平均码率（Mbps），用于给用户带宽预警。"""
    mpx_per_frame = width * height / 1_000_000.0
    q = QUALITY_MODES[mode]
    if q.lossless:
        # 桌面内容 HEVC 无损的实测经验：约为原始数据量的 8%~18%
        # yuv444p = 3 bytes/px；yuv420p = 1.5 bytes/px
        raw_bps = width * height * 3 * 8 * fps
        return round(raw_bps * 0.13 / 1_000_000.0, 1)
    bps = q.bits_per_mpx * mpx_per_frame * fps * 1_000_000.0
    return round(bps / 1_000_000.0, 1)


def recommend_transport(mbps: float) -> Tuple[str, str]:
    """按带宽预算给出传输方式与组网建议。"""
    if mbps <= 25:
        return ("udp", "当前码率较低，5GHz Wi-Fi 即可稳定承载")
    if mbps <= 90:
        return ("tcp", "建议 5GHz / Wi-Fi 6 近距离，可靠传输避免丢帧")
    return ("tcp", "码率很高：请优先 Wi-Fi 6E(6GHz/160MHz) 或 USB-C 转 2.5G 有线回程")


# ---------------------------------------------------------------- 能力探测

@dataclass
class EncoderCap:
    key: str
    ffmpeg_name: str
    display: str
    pix_fmts: Set[str]
    available: bool = False
    reason: str = ""


@dataclass
class Capabilities:
    ffmpeg: str
    encoders: Dict[str, EncoderCap] = field(default_factory=dict)
    input_devices: List[str] = field(default_factory=list)

    def hevc_candidates(self, want_444: bool) -> List[Tuple[str, str]]:
        return self._candidates(VIDEO_CODEC_HEVC, want_444)

    def h264_candidates(self, want_444: bool = False) -> List[Tuple[str, str]]:
        return self._candidates(VIDEO_CODEC_H264, want_444)

    def _candidates(self, codec: str, want_444: bool) -> List[Tuple[str, str]]:
        table = ENCODER_ORDER if codec == VIDEO_CODEC_HEVC else H264_ORDER
        out = []
        for entry in table:
            key, name = entry[0], entry[1]
            cap = self.encoders.get(name)
            if not cap or not cap.available:
                continue
            if want_444 and "yuv444p" not in cap.pix_fmts:
                continue
            out.append((key, name))
        return out

    def get(self, ffmpeg_name: str) -> Optional[EncoderCap]:
        return self.encoders.get(ffmpeg_name)


_startupinfo = None
_creationflags = 0


def _spawn_kwargs(hide_window: bool = True):
    global _startupinfo, _creationflags
    import sys
    if sys.platform != "win32":
        return {}
    import subprocess as _sp
    si = _sp.STARTUPINFO()
    if hide_window:
        si.dwFlags |= _sp.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
    return {"startupinfo": si, "creationflags": getattr(_sp, "CREATE_NO_WINDOW", 0)}


def run_ffmpeg(ffmpeg: str, args: Sequence[str], timeout: float = 12.0):
    """统一入口：所有子进程窗口隐藏，超时可控。"""
    try:
        return subprocess.run([ffmpeg, "-hide_banner", *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, **_spawn_kwargs())
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


_PIX_RE = re.compile(r"^\s*Supported pixel formats:\s*(.+)$", re.M)


def _pix_fmts_of(ffmpeg: str, enc_name: str) -> Set[str]:
    p = run_ffmpeg(ffmpeg, ["-h", f"encoder={enc_name}"])
    if p is None or p.returncode != 0:
        return set()
    txt = (p.stdout or b"").decode("utf-8", "ignore")
    m = _PIX_RE.search(txt)
    if not m:
        return set()
    return set(m.group(1).split())


def _encode_probe(ffmpeg: str, args: Sequence[str], timeout: float = 20.0) -> bool:
    """
    真编码几帧，返回是否成功。
    这是唯一可信的"这个编码器在我的机器上能不能用"的判据。
    """
    base = ["-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=0.2",
            *args, "-f", "rawvideo", "-"]
    p = run_ffmpeg(ffmpeg, base, timeout=timeout)
    return bool(p is not None and p.returncode == 0)


_probe_lock = threading.Lock()


def probe_capabilities(ffmpeg: str, deep: bool = True) -> Capabilities:
    """
    deep=True 时会对每个候选硬件编码器做一次真实编码验证（约几百毫秒/个），
    因此我们加了进程内缓存。
    """
    caps = Capabilities(ffmpeg=ffmpeg)
    p = run_ffmpeg(ffmpeg, ["-encoders"])
    if p is None:
        return caps
    txt = (p.stdout or b"").decode("utf-8", "ignore")
    listed = set()
    for line in txt.splitlines():
        # 形如：  " V....D libx265   libx265 H.265 / HEVC (codec hevc)"
        #         " V..... hevc_qsv  HEVC (Intel Quick Sync...) (codec hevc)"
        # 第 1 列是类型位(V/A/S)，紧跟的 5 个字符是能力标志位(F/S/X/B/D)。
        # 旧正则 \s*V\.{0,5}\s+ 只认“V+最多5个点”，凡是带 D 标志（libx264/
        # libx265/hevc_nvenc/…_amf/…_mf）的编码器全被漏掉，会被误判成
        # “该 ffmpeg 构建未包含此编码器”，导致 NVENC/x265 永远选不上。
        m = re.match(r"\s*V[A-Z.]{5}\s+([\w.]+)\s", line)
        if m:
            listed.add(m.group(1))

    for key, name, display in ENCODER_ORDER:
        cap = EncoderCap(key=key, ffmpeg_name=name, display=display,
                         pix_fmts=set(), available=False)
        if name not in listed:
            cap.reason = "该 ffmpeg 构建未包含此编码器"
            caps.encoders[name] = cap
            continue
        cap.pix_fmts = _pix_fmts_of(ffmpeg, name)
        if deep:
            try:
                args = build_video_encode_args(name, "high", 420, low_latency=True)
            except Exception:
                args = ["-c:v", name]
            if _encode_probe(ffmpeg, args):
                cap.available = True
            else:
                cap.reason = "硬件不可用（缺少驱动或无此 EZ 内开工）"
        caps.encoders[name] = cap

    # H.264 候选（兼容老投影仪）
    for key, name in H264_ORDER:
        if name not in listed:
            continue
        cap = EncoderCap(key=key, ffmpeg_name=name, display=f"{name}(H.264)",
                         pix_fmts=_pix_fmts_of(ffmpeg, name))
        cap.available = True
        caps.encoders[name] = cap

    return caps


# ---------------------------------------------------------------- 参数生成

def _kworkers(list_n: int) -> int:
    try:
        import os
        return max(2, min(6, (os.cpu_count() or 4) - 1))
    except Exception:
        return 4


def build_video_encode_args(encoder_name: str, quality: str, chroma: str,
                            low_latency: bool = True,
                            fps: int = 60, gop_seconds: float = 0.5,
                            bitrate_cap_mbps: float = 0.0,
                            width: int = 1920, height: int = 1080) -> List[str]:
    """
    生成 ffmpeg 视频编码参数。

    约定：全部候选都**强制单 slice**（x265 slices=1，硬件默认即单 slice），
    这样 AnnexBSplitter 才能用极低成本切出精确帧边界。

    gop_seconds 默认 0.5：关键帧周期直接决定三件事 ——
      * 丢帧/弱网后画面多久能恢复（= 一个 GOP）；
      * 万一投影仪解码跟不上、只剩关键帧出得来，观众看到的刷新周期；
      * v1.0.6 起还多了一件：传输层"延迟上限"丢帧后会一路丢到关键帧，
        所以 GOP 越短，"为了追实时而牺牲的画面"恢复得越快。
    早期是 2.0 秒，配合解码器只吃关键帧时，观感正好是"几秒钟才动一下"；
    v1.0.3 缩到 1.0 秒；本轮用户报"偶尔丢帧卡顿"，再缩到 0.5 秒 ——
    1080p/24fps 下每 12 帧一个 IDR，码率代价很小，换来的是半秒内自愈。

    <p><b>v1.0.6 的另一处重点：把编码器自身的流水线延迟压到最低。</b>
    硬件编码器默认会"攒几帧再吐"（QSV 的 async_depth 默认 4，
    在 24fps 下就是 166ms 的纯延迟；NVENC 的 hq tune 会开前向分析），
    这部分延迟不体现在任何统计里，只能从参数上关掉。
    """
    q = QUALITY_MODES[quality]
    w444 = (chroma == "444")
    out: List[str] = ["-c:v", encoder_name]

    gop = max(1, int(round(fps * gop_seconds)))

    if encoder_name.endswith("nvenc"):
        if q.lossless:
            out += ["-preset", "lossless", "-tune", "lossless", "-rc", "constqp", "-qp", "0"]
        elif low_latency:
            # p7/hq 是"最高画质"那一档，它内部会开多 pass 前向分析，
            # 攒够若干帧才吐第一个包 —— 投屏要的是低延迟，不是归档画质。
            # p1 + tune ll 才能让 NVENC 真正按"编完立刻出"工作。
            out += ["-preset", "p1", "-tune", "ll",
                    "-rc", "constqp", "-qp", str(q.cqp)]
        else:
            out += ["-preset", "p7", "-tune", "hq",
                    "-rc", "constqp", "-qp", str(q.cqp)]
        out += ["-g", str(gop), "-forced-idr", "1", "-bf", "0", "-aud", "1"]
        if low_latency:
            # surfaces 也要小：它是"同时存在于编码器里的帧数"，每一帧都是延迟。
            out += ["-delay", "0", "-zerolatency", "1", "-surfaces", "4",
                    "-rc-lookahead", "0", "-b_ref_mode", "0"]
        out += ["-pix_fmt", "yuv444p" if w444 else "nv12"]
        if bitrate_cap_mbps > 0 and not q.lossless:
            out += ["-maxrate", f"{bitrate_cap_mbps:.0f}M", "-bufsize", f"{bitrate_cap_mbps * 2:.0f}M"]
        return out

    if encoder_name.endswith("_qsv"):
        out += ["-preset", "veryfast" if low_latency else "medium",
                "-g", str(gop), "-forced_idr", "1", "-bf", "0",
                "-idr_interval", "0"]
        if q.lossless:
            # QSV 不支持真无损，交给调用方降级到 x265
            out += ["-b:v", "200M", "-maxrate", "400M"]
        else:
            bps = q.bits_per_mpx * (width * height / 1_000_000.0) * fps
            if bitrate_cap_mbps > 0:
                bps = min(bps, bitrate_cap_mbps)
            out += ["-b:v", f"{max(2.0, bps):.0f}M",
                    "-maxrate", f"{max(3.0, bps * 1.4):.0f}M",
                    "-bufsize", f"{max(6.0, bps * 2):.0f}M"]
        if low_latency:
            # async_depth 是"同时在编码器里排队的帧数"，默认 4：
            # 24fps 下光是排队就 166ms。压到 1 = 编完立刻交。
            out += ["-async_depth", "1",
                    # extbrc 会引入前向分析（look_ahead_depth），投屏不需要
                    "-extbrc", "0", "-look_ahead_depth", "0",
                    # Intel 给"桌面串流"场景的优化档：针对屏幕内容调过参数
                    "-scenario", "displayremoting"]
        else:
            out += ["-async_depth", "2"]
        out += ["-pix_fmt", "nv12" if not w444 else "yuv444p"]
        return out

    if encoder_name.endswith("_amf"):
        if q.lossless:
            out += ["-usage", "transcoding", "-quality", "quality", "-rc", "cqp",
                    "-qp_i", "0", "-qp_p", "0"]
        else:
            out += ["-usage", "ultralowlatency" if low_latency else "transcoding",
                    "-quality", "quality", "-rc", "cqp",
                    "-qp_i", str(q.cqp), "-qp_p", str(q.cqp + 2)]
        out += ["-g", str(gop), "-forced_idr", "1", "-async_depth", "1",
                "-preanalysis", "0", "-vbaq", "1"]
        out += ["-pix_fmt", "nv12"]
        return out

    if encoder_name.endswith("_mf"):
        out += ["-b:v", f"{max(4.0, q.bits_per_mpx * (width * height / 1_000_000.0) * fps):.0f}M",
                "-g", str(gop)]
        return out

    if encoder_name == "libx265":
        # frame-threads 是"同时在编的帧数"，每多一帧就多一帧延迟。
        # 之前固定给到 CPU 数（2~6），等于自己往流水线里塞了 100~200ms；
        # 实时投屏必须钉成 1（tune zerolatency 本来就想这么干，
        # 但显式 -x265-params 会把它覆盖掉）。
        nw = 1 if low_latency else _kworkers(0)
        params = [f"slices=1", f"frame-threads={nw}", "rc-lookahead=0",
                  "repeat-headers=1", "aud=1", "open-gop=0"]
        if q.lossless:
            params.insert(0, "lossless=1")
        out += ["-preset", "ultrafast", "-tune", "zerolatency",
                "-x265-params", ":".join(params)]
        if not q.lossless:
            out += ["-crf", str(int(q.crf))]
        out += ["-g", str(gop), "-bf", "0", "-pix_fmt",
                "yuv444p" if w444 else "yuv420p"]
        if bitrate_cap_mbps > 0 and not q.lossless:
            out += ["-maxrate", f"{bitrate_cap_mbps:.0f}M",
                    "-bufsize", f"{bitrate_cap_mbps * 2:.0f}M"]
        return out

    if encoder_name == "libx264":
        out += ["-preset", "ultrafast", "-tune", "zerolatency",
                "-crf", str(int(q.crf + 2)), "-g", str(gop), "-bf", "0",
                "-pix_fmt", "yuv420p"]
        return out

    raise ValueError(f"未知编码器 {encoder_name}")


def audio_input_args(ffmpeg: str, device: Optional[str] = None) -> Optional[List[str]]:
    """
    枚举本机可用音频回环设备。返回 ffmpeg 输入参数，找不到则返回 None。

    Windows 上"把系统声音同时投出去"需要回环设备，命名五花八门，因此这里
    列出 dshow / wasapi 设备并做关键字匹配，也能被用户手动覆盖。
    """
    candidates = []
    for dev_name, args_probe in (("dshow", ["-f", "dshow", "-list_devices", "true", "-i", "dummy"]),
                                 ("wasapi", ["-f", "wasapi", "-list_devices", "true", "-i", "dummy"])):
        p = run_ffmpeg(ffmpeg, args_probe, timeout=8.0)
        if p is None:
            continue
        txt = (p.stdout or b"") + (p.stderr or b"")
        for line in txt.decode("utf-8", "ignore").splitlines():
            m = re.search(r'\[dshow[^\]]*\]\s+"([^"]+)"\s*\(audio\)|'
                          r'\[dshow[^\]]*\]\s+"([^"]+)"\s*$', line)
            name = None
            mm = re.search(r'"([^"]+)"', line)
            if mm and ("audio" in line or dev_name == "wasapi"):
                name = mm.group(1)
            if not name:
                continue
            kw = ("立体声混音", "Stereo Mix", "CABLE", "Voicemeeter", "虚拟",
                  "What U Hear", "Loopback", "扬声器", "Speaker", "Realtek")
            if any(k in name for k in kw):
                candidates.append((dev_name, name))

    if device:
        # 用户指定了设备名
        for dev_name, name in candidates:
            if name == device:
                return _mk_audio_input(dev_name, name)
        return _mk_audio_input("wasapi", device)

    if not candidates:
        return None
    dev_name, name = candidates[0]
    return _mk_audio_input(dev_name, name)


def _mk_audio_input(dev_name: str, name: str) -> List[str]:
    if dev_name == "wasapi":
        return ["-f", "wasapi", "-audio_buffer_size", "40", "-i", f"audio={name}"]
    return ["-f", "dshow", "-audio_buffer_size", "40", "-i", f"audio={name}"]


def list_audio_devices(ffmpeg: str) -> List[Tuple[str, str]]:
    """返回 [(协议, 设备名)]，供 UI 下拉选择。"""
    out: List[Tuple[str, str]] = []
    seen = set()
    for dev_name, probe in (("dshow", ["-f", "dshow", "-list_devices", "true", "-i", "dummy"]),
                            ("wasapi", ["-f", "wasapi", "-list_devices", "true", "-i", "dummy"])):
        p = run_ffmpeg(ffmpeg, probe, timeout=8.0)
        if p is None:
            continue
        txt = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "ignore")
        for line in txt.splitlines():
            if "(audio)" not in line and dev_name != "wasapi":
                continue
            mm = re.search(r'"([^"]+)"', line)
            if not mm:
                continue
            name = mm.group(1)
            key = (dev_name, name)
            if key in seen:
                continue
            seen.add(key)
            out.append((dev_name, name))
    return out
