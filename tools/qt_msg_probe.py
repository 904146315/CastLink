# -*- coding: utf-8 -*-
"""捕获 Qt 自身的消息（警告/致命），定位 app.quit() 崩溃的真正原因。"""
import os
import sys
import faulthandler

faulthandler.enable(all_threads=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sender"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import qInstallMessageHandler, QTimer   # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

LEVELS = {0: "DEBUG", 1: "INFO", 2: "WARN", 3: "CRIT", 4: "FATAL"}


def handler(mode, ctx, msg):
    print(f"  <Qt:{LEVELS.get(int(mode), mode)}> {msg}", flush=True)


qInstallMessageHandler(handler)

from castlink_sender.ui_main import MainWindow   # noqa: E402
from castlink_sender.util import find_ffmpeg     # noqa: E402

app = QApplication(sys.argv)
print("QApplication ok", flush=True)

win = MainWindow(find_ffmpeg(), app)
print("MainWindow ok", flush=True)
win.show()
print("shown ok", flush=True)


def _quit():
    print("---> about to call app.quit()", flush=True)
    app.quit()
    print("---> app.quit() RETURNED", flush=True)


QTimer.singleShot(2500, _quit)
print("entering exec()", flush=True)
app.exec()
print("app.exec() RETURNED NORMALLY", flush=True)
