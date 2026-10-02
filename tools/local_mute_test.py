# -*- coding: utf-8 -*-
"""
「投屏时静音本机扬声器」的实机验证。

要验证的核心问题是**一个反直觉的点**：

    把默认播放设备静音之后，WASAPI 回环还能不能采到声音？

如果答案是"不能"，那么"静音本机"就会把要投出去的声音一起静掉，
这个功能就不能做 —— 所以必须实测，不能靠推理。

WASAPI 回环的 tap 点在音频引擎的混音输出上、端点音量/静音之前，
所以预期是**仍然能采到**。本脚本就是来证实/证伪它的。

测量方法：同一段测试音，分别在"未静音"和"已静音"两种状态下回环采集，
比较 PCM 的峰值与 RMS。两者接近 => 静音不影响回环。

用法：
    tools/pyenv/Scripts/python.exe -u tools/local_mute_test.py
"""
from __future__ import annotations

import os
import sys
import time
import winsound

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sender"))

from castlink import winvol                      # noqa: E402
from castlink.padev import PA_LOCK               # noqa: E402

TONE = os.path.join(ROOT, "tools", "_tone.wav")


def _pcm_stats(data: bytes):
    """返回 (峰值, RMS)，输入是 16bit 小端 PCM。"""
    if not data:
        return 0, 0.0
    n = len(data) // 2
    peak = 0
    total = 0
    for i in range(n):
        v = data[2 * i] | (data[2 * i + 1] << 8)
        if v >= 32768:
            v -= 65536
        a = abs(v)
        if a > peak:
            peak = a
        total += v * v
    return peak, (total / max(1, n)) ** 0.5


def _capture(seconds: float) -> bytes:
    """从默认扬声器的 WASAPI 回环采一段 PCM。"""
    import pyaudiowpatch as pyaudio
    from castlink_sender.audio import find_wasapi_loopback

    src = find_wasapi_loopback()
    if src is None:
        raise RuntimeError("找不到 WASAPI 回环设备")

    with PA_LOCK:
        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=src.channels,
                         rate=src.rate, input=True,
                         input_device_index=src.index, frames_per_buffer=1024)
    chunks = []
    try:
        deadline = time.time() + seconds
        while time.time() < deadline:
            chunks.append(stream.read(1024, exception_on_overflow=False))
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        with PA_LOCK:
            try:
                pa.terminate()
            except Exception:
                pass
    return b"".join(chunks)


def _sample_while_playing(seconds: float = 1.6) -> bytes:
    """播一段测试音，同时回环采集。"""
    winsound.PlaySound(TONE, winsound.SND_FILENAME | winsound.SND_ASYNC)
    data = _capture(seconds)
    try:
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass
    return data


def main() -> int:
    if sys.platform != "win32":
        print("仅支持 Windows")
        return 1
    if not os.path.exists(TONE):
        print(f"缺少测试音频 {TONE}")
        return 1
    for msg in _fmt_winvol():
        print(msg)

    original = winvol.get_default_mute()
    print(f"当前默认播放设备静音状态: {original}")
    if original is None:
        print("❌ 拿不到 IAudioEndpointVolume —— 静音功能在本机不可用")
        return 1

    # ---- 1) 未静音时采集 ----
    if original:
        print("(先解除静音，保证基线有效)")
        winvol.set_default_mute(False)
        time.sleep(0.3)
    print("\n[1/2] 未静音：播放测试音并回环采集…")
    a = _sample_while_playing()
    pa_peak, pa_rms = _pcm_stats(a)
    print(f"      采到 {len(a)} 字节  峰值 {pa_peak}  RMS {pa_rms:.0f}")

    # ---- 2) 静音后采集 ----
    print("\n[2/2] 已静音：播放测试音并回环采集…")
    if not winvol.set_default_mute(True):
        print("❌ 设置静音失败")
        return 1
    time.sleep(0.3)
    assert winvol.get_default_mute() is True, "静音没生效"
    b = _sample_while_playing()
    pb_peak, pb_rms = _pcm_stats(b)
    print(f"      采到 {len(b)} 字节  峰值 {pb_peak}  RMS {pb_rms:.0f}")

    # ---- 恢复 ----
    winvol.set_default_mute(bool(original))
    print(f"\n已恢复原静音状态: {winvol.get_default_mute()}")

    # ---- 判定 ----
    print("\n" + "=" * 58)
    if pa_rms < 50:
        print("⚠️  基线本身就几乎没采到声音（本机可能没有在播放，或被独占）")
        print("    结论不可用：请确认扬声器有声音再重跑")
        return 2
    ratio = pb_rms / pa_rms if pa_rms else 0
    print(f"静音前后回环 RMS 之比 = {ratio:.2f}")
    if ratio > 0.5:
        print("✅ 静音本机扬声器后，WASAPI 回环仍能采到正常音量的声音")
        print("   → 可以放心实现「投屏时静音本机」")
        return 0
    print("❌ 静音之后回环也变静音了 —— 这台机器的驱动在端点音量之后才 tap")
    print("   → 「静音本机」会把要投出去的声音一起静掉，不能这么做")
    return 1


def _fmt_winvol():
    out = []
    out.append(f"winvol.get_default_mute() = {winvol.get_default_mute()}")
    m = winvol.LocalAudioMuter(log=lambda s: out.append(f"  [muter] {s}"))
    if m.mute():
        out.append("  [muter] mute() 成功改变状态")
    m.restore()
    out.append(f"  [muter] restore 之后 = {winvol.get_default_mute()}")
    return out


if __name__ == "__main__":
    sys.exit(main())
