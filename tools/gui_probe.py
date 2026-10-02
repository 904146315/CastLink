# -*- coding: utf-8 -*-
"""
GUI 事件循环定位探针。

不用 app.exec()，而是手动 processEvents()：
这样崩溃时能精确看到"第几次泵、间隔多少毫秒"时挂掉，
也可以逐个禁用可疑回调来二分定位。

用法：
    tools/pyenv/Scripts/python.exe -u -X faulthandler tools/gui_probe.py [秒数]
"""
from __future__ import annotations

import os
import sys
import time
import faulthandler

faulthandler.enable()

try:
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

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
MODE = sys.argv[2] if len(sys.argv) > 2 else "pump"   # pump | exec


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def main():
    from PySide6.QtWidgets import QApplication
    from castlink_sender.ui_main import MainWindow
    from castlink_sender.util import find_ffmpeg

    ff = find_ffmpeg()
    log("ffmpeg =", ff)

    app = QApplication(sys.argv)
    log("QApplication created")

    win = MainWindow(ff, app)
    log("MainWindow constructed; state =", win.session.state)
    win.show()
    log("window shown")

    if MODE == "exec":
        from PySide6.QtCore import QTimer
        log("using app.exec() mode")

        def _quit():
            log("timer fired -> quitting (step-by-step)")
            steps = [
                ("session.shutdown()", lambda: win.session.shutdown()),
                ("discovery.stop()", lambda: win.discovery.stop()),
                ("tray.hide()", lambda: win.tray.hide()),
                ("app.quit()", lambda: app.quit()),
            ]
            for name, fn in steps:
                log("   ->", name)
                fn()
                log("      ok:", name)
            log("all quit steps done")

        QTimer.singleShot(int(SECONDS * 1000), _quit)
        app.exec()
        log("app.exec() returned normally")
        return 0

    t0 = time.time()
    i = 0
    while time.time() - t0 < SECONDS:
        app.processEvents()
        i += 1
        time.sleep(0.05)
        if i % 20 == 0:
            log(f"pump #{i}  t={time.time()-t0:.2f}s  "
                f"state={win.session.state}  caps={'y' if win.caps else 'n'}  "
                f"peers={len(win.discovery.peers_of('receiver'))}")
    log("loop done, no crash.  state =", win.session.state)
    log("plan text =", win.lbl_plan.text())
    win.quit_app()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(3)
