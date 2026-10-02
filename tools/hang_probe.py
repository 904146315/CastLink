# -*- coding: utf-8 -*-
"""
发送端"退出卡死"的定位探针。

现象：MainWindow + app.exec() 之后，用 QTimer 调 quit_app() 收尾，
      事件循环却不返回，进程永远挂在后台（不是崩溃，没有 traceback）。

做法：开一个 20 秒的看门狗，到点把**所有线程的调用栈**打出来再退出，
      这样能直接看到卡在哪一行，而不是靠猜。

用法：tools/pyenv/Scripts/python.exe -u tools/hang_probe.py
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_LOGGING_RULES"] = "qt.qpa.*=false"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

# 20 秒后 dump 所有线程栈（exit=True 让进程直接退，避免探针自己也挂着）
faulthandler.dump_traceback_later(20, exit=True)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from castlink_sender.util import find_ffmpeg  # noqa: E402
from castlink_sender.ui_main import MainWindow  # noqa: E402

T0 = time.time()


def mark(msg):
    print(f"[{time.time() - T0:6.2f}s] {msg}", flush=True)


app = QApplication.instance() or QApplication(sys.argv)
mark("QApplication 就绪")

win = MainWindow(find_ffmpeg(), app)
mark("MainWindow 构造完成")

win.show()
mark("win.show() 完成")

# 顺便验证新增的三格接收端指标不会抛异常
win._on_stats({
    "bitrate": 12.3, "fps": 30.0, "rtt": 4.5, "queued": 0, "dropped": 0,
    "peer": {"fps": 29.9, "rendered": 1234, "assembled": 1240, "lostPackets": 6},
})
mark("_on_stats（含接收端三项）正常")

def done():
    mark("QTimer 触发 -> quit_app()")
    t = time.time()
    try:
        win.quit_app()
    except Exception as e:
        mark(f"quit_app 异常: {e}")
    mark(f"quit_app() 返回（累计 {time.time() - t:.2f}s）")


QTimer.singleShot(3000, done)
mark("进入 app.exec()")
rc = app.exec()
mark(f"app.exec() 返回 rc={rc}")
print("结论: 事件循环正常退出，quit 路径没有阻塞 ✅")
