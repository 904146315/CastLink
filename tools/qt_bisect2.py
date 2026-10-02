# -*- coding: utf-8 -*-
"""
按功能开关二分 MainWindow 的 app.quit() 崩溃。

用法: python tools/qt_bisect2.py <comma,separated,disable,list>
可选开关: ui tray discovery reqserver timer session stylesheet show
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sender"))

opts = set((sys.argv[1] if len(sys.argv) > 1 else "").split(","))
opts.discard("")

from PySide6.QtWidgets import QApplication      # noqa: E402
from PySide6.QtCore import QTimer               # noqa: E402
from castlink_sender import ui_main             # noqa: E402

MW = ui_main.MainWindow

if "ui" in opts:
    MW._build_ui = lambda self: None
if "tray" in opts:
    MW._build_tray = lambda self: setattr(self, "tray", None)
if "stylesheet" in opts:
    MW.setStyleSheet = lambda self, *a, **k: None
if "timer" in opts:
    def _no_timer(self):
        self._timer = QTimer(self)
    MW._tick = lambda self: None
if "probe" in opts or "timer" in opts:
    MW._probe_async = lambda self: None
if "session" in opts:
    MW._build_ui = MW._build_ui  # 占位，session 由构造后置空
if "discovery" in opts:
    from castlink.discovery import Discovery
    Discovery.start = lambda self: None
if "reqserver" in opts:
    from castlink_sender.session import CastSession
    CastSession.start_request_server = lambda self: None

from castlink_sender.util import find_ffmpeg    # noqa: E402

app = QApplication(sys.argv)
win = MW(find_ffmpeg(), app)
if "show" not in opts:
    win.show()
print(f"opts={sorted(opts) or ['(none)']} -> constructed", flush=True)


def _q():
    print("   calling app.quit()", flush=True)
    app.quit()
    print("   app.quit() returned", flush=True)


QTimer.singleShot(2000, _q)
app.exec()
print("SURVIVED", flush=True)
