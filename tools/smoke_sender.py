# -*- coding: utf-8 -*-
"""
发送端无头冒烟测试。

目的：在不开真实窗口的前提下，把「发送端启动 + 运行时」会走的代码路径全部跑一遍，
      把任何异常（含 QSS 解析错误、子进程失败、编码器不支持）原样打出来。

用法：
    tools/pyenv/Scripts/python.exe tools/smoke_sender.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback

try:  # 行缓冲，避免硬崩溃时丢失全部输出
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_LOGGING_RULES"] = "qt.qpa.*=false"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILS = []


def step(name, fn):
    t0 = time.time()
    try:
        r = fn()
        dt = (time.time() - t0) * 1000
        print(f"[ OK ] {name}  ({dt:.0f} ms)" + (f"  -> {r}" if r is not None else ""))
        return r
    except Exception:
        dt = (time.time() - t0) * 1000
        print(f"[FAIL] {name}  ({dt:.0f} ms)")
        traceback.print_exc()
        FAILS.append(name)
        return None


def main():
    print("=" * 76)
    print("CastLink 发送端 冒烟测试")
    print("=" * 76)

    # ---- 1) 包导入 ----
    pkg = step("import castlink_sender", lambda: __import__("castlink_sender"))
    if pkg is None:
        return 1

    from castlink_sender.util import find_ffmpeg, resource_path
    from castlink_sender import capture, encoders
    from castlink_sender.capture import (
        primary_resolution, detect_capture_method, make_plan, VideoPipeline,
    )
    from castlink_sender.encoders import (
        probe_capabilities, build_video_encode_args, estimate_bitrate_mbps,
        QUALITY_MODES,
    )

    ff = step("find_ffmpeg()", find_ffmpeg)
    if not ff:
        print("!! 找不到 ffmpeg，后续测试无意义")
        return 1

    step("resource_path(app_icon.ico) 存在性",
         lambda: os.path.exists(resource_path("resources", "app_icon.ico")))

    res = step("primary_resolution()", primary_resolution)
    method = step("detect_capture_method()", lambda: detect_capture_method(ff))

    # ---- 2) 各画质档位生成命令 ----
    def plan_matrix():
        out = []
        for q in QUALITY_MODES:
            for chroma in ("420", "444"):
                p = make_plan(res or (1920, 1080), 60, "native", False, q, chroma, 3840, 2160)
                out.append(f"{q}/{chroma}->{p.out_w}x{p.out_h}@{p.fps}")
        return ", ".join(out)
    step("make_plan 全档位", plan_matrix)

    def cmd_matrix():
        bad = []
        for q in QUALITY_MODES:
            for chroma in ("420", "444"):
                for enc in ("hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_mf", "libx265"):
                    plan = make_plan(res or (1920, 1080), 60, "1080p", False, q, chroma, 3840, 2160)
                    try:
                        cmd = VideoPipeline.build_command(ff, plan, enc, method or "gdigrab")
                        assert isinstance(cmd, list) and cmd
                    except Exception as e:
                        bad.append(f"{enc}/{q}/{chroma}: {e}")
        if bad:
            raise RuntimeError(" | ".join(bad))
        return "所有 encoder × quality × chroma 组合均可生成命令"
    step("build_command 全组合", cmd_matrix)

    # ---- 3) 真实能力探测（最耗时）----
    caps = step("probe_capabilities(deep=True)", lambda: probe_capabilities(ff, deep=True))
    if caps is not None:
        avail = {n: c.available for n, c in caps.encoders.items()}
        print("        可用性:", avail)
        hevc = [n for n, c in caps.encoders.items() if c.available and n.startswith("hevc")]
        if not hevc:
            print("        !! 没有可用的 HEVC 编码器（GUI 会退回 H.264）")

    # ---- 4) GUI 启动路径（offscreen）----
    def gui_boot():
        import faulthandler
        faulthandler.enable()
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from castlink_sender import ui_theme as T
        from castlink_sender.ui_main import MainWindow

        print("    - ui_theme.qss() ...", flush=True)
        qss = T.qss()
        assert isinstance(qss, str) and len(qss) > 100

        print("    - QApplication(offscreen) ...", flush=True)
        app = QApplication.instance() or QApplication(sys.argv)

        print("    - QSystemTrayIcon 单独测试 ...", flush=True)
        from PySide6.QtWidgets import QSystemTrayIcon
        from PySide6.QtGui import QIcon
        tray = QSystemTrayIcon(QIcon())
        tray.show()
        print("      tray ok, available =", QSystemTrayIcon.isSystemTrayAvailable(), flush=True)
        tray.hide()

        print("    - MainWindow(...) ...", flush=True)
        win = MainWindow(ff, app)
        print("    - win.show() ...", flush=True)
        win.show()
        box = {}

        def done():
            box["state"] = win.session.state
            box["caps_ok"] = win.caps is not None
            box["plan_text"] = win.lbl_plan.text()
            win.quit_app()

        print("    - app.exec() ...", flush=True)
        QTimer.singleShot(8000, done)
        app.exec()
        return box

    box = step("MainWindow 启动 + 事件循环(8s)", gui_boot)
    if box:
        print("        最终状态:", box.get("state"), "| caps:", box.get("caps_ok"))
        print("        计划文案:", box.get("plan_text"))

    print("=" * 76)
    if FAILS:
        print("失败项:", ", ".join(FAILS))
        return 2
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
