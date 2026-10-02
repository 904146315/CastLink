# -*- coding: utf-8 -*-
"""CastLink 发送端入口。"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# 启动身份兼容（这里是 1.0.0 安装包一启动就报错的根因，勿删）
#
# 本文件有三种被加载的方式：
#   1) 开发期  python -m castlink_sender.main        —— 属于包，相对导入可用；
#   2) 开发期  python sender/castlink_sender/main.py —— 顶层脚本，无父包；
#   3) 打包后  PyInstaller 把本文件当作顶层脚本 __main__ 运行。
#
# 在后两种情况下 __package__ 为空，原先写的
#       from .ui_main import MainWindow
# 会抛：
#       ImportError: attempted relative import with no known parent package
# 而且因为该相对导入无法被静态解析，PyInstaller 连 castlink_sender 的子模块
# 都不会收进包，所以修好导入之后必须同时让构建脚本能解析到这个包。
#
# 统一方案：先把包所在的两级目录塞进 sys.path，之后一律用绝对导入。
# ---------------------------------------------------------------------------
if not getattr(sys, "frozen", False):
    _HERE = os.path.dirname(os.path.abspath(__file__))    # .../sender/castlink_sender
    _SENDER_DIR = os.path.dirname(_HERE)                  # .../sender
    _ROOT = os.path.dirname(_SENDER_DIR)                  # 项目根
    for _p in (_ROOT, _SENDER_DIR):
        if _p not in sys.path:
            sys.path.insert(0, _p)

# Windows：让 GetDpiHelper / EnumDisplaySettings 拿到真实物理分辨率
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _log_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "CastLink", "logs")


def _write_crash_log(detail: str) -> str:
    try:
        d = _log_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "sender_crash.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{detail}\n")
        return path
    except Exception:
        return "(无法写入日志文件)"


def _install_crash_handler():
    """
    把未捕获异常写进日志并弹窗。

    打包成 --windowed 之后没有控制台，缺了这一步用户只能看到一句
    “Failed to execute script”，排查全靠猜。
    """
    def hook(exc_type, exc, tb):
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        path = _write_crash_log(detail)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is None:
                QApplication(sys.argv)
            QMessageBox.critical(
                None, "CastLink 发送端 运行异常",
                f"程序遇到未处理的错误：\n\n{exc}\n\n详细日志已写入：\n{path}")
        except Exception:
            pass

    sys.excepthook = hook

    def thook(args):
        hook(args.exc_type, args.exc_value, args.exc_traceback)

    try:
        threading.excepthook = thook
    except Exception:
        pass


def _fatal(title: str, msg: str):
    _write_crash_log(f"[fatal] {title}: {msg}")
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication(sys.argv)
        QMessageBox.critical(None, title, msg)
    except Exception:
        try:
            print(f"{title}: {msg}")
        except Exception:
            pass
    sys.exit(1)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    _install_crash_handler()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName("CastLink 投屏发送端")
    app.setApplicationDisplayName("CastLink")
    app.setQuitOnLastWindowClosed(False)

    from castlink_sender.ui_main import MainWindow
    from castlink_sender.util import find_ffmpeg, SingleInstanceGuard, resource_path
    from castlink_sender import APP_NAME, __version__

    guard = SingleInstanceGuard()
    if not guard.try_acquire():
        QMessageBox.information(None, APP_NAME, "CastLink 已经在运行了。")
        sys.exit(0)

    ico = resource_path("resources", "app_icon.ico")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))

    ffmpeg = find_ffmpeg()
    if not ffmpeg or not os.path.exists(ffmpeg):
        _fatal(APP_NAME, "没有找到 ffmpeg 可执行文件。\n\n"
                         "请重新安装 CastLink，或手动把 ffmpeg.exe 放到安装目录下。")

    win = MainWindow(ffmpeg, app)
    win.show()
    code = app.exec()
    guard.release()
    return code


if __name__ == "__main__":
    sys.exit(main())
