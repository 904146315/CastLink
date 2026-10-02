# -*- coding: utf-8 -*-
"""二分定位发送端 GUI 退出崩溃：逐项禁用后台线程。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sender"))

mode = sys.argv[1] if len(sys.argv) > 1 else "none"

if mode in ("nodiscovery", "both"):
    from castlink.discovery import Discovery
    Discovery.start = lambda self: None
if mode in ("noreqserver", "both"):
    from castlink_sender.session import CastSession
    CastSession.start_request_server = lambda self: None

from PySide6.QtWidgets import QApplication            # noqa: E402
from PySide6.QtCore import QTimer                     # noqa: E402
from castlink_sender.ui_main import MainWindow        # noqa: E402
from castlink_sender.util import find_ffmpeg          # noqa: E402

app = QApplication(sys.argv)
win = MainWindow(find_ffmpeg(), app)
win.show()
print(f"mode={mode}: started, window shown", flush=True)
QTimer.singleShot(2500, app.quit)
app.exec()
print(f"SURVIVED mode={mode}", flush=True)
