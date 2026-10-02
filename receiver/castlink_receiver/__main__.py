# -*- coding: utf-8 -*-
"""接收端入口：`python -m castlink_receiver`（或仓库根目录的 run_receiver.py）。"""
from __future__ import annotations

import os
import sys


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName("CastLink 投屏接收端")
    app.setQuitOnLastWindowClosed(False)

    from castlink import ffmpeg as ff
    from castlink.config import ReceiverConfig
    from .server import ReceiverServer
    from .ui_main import ReceiverWindow
    from . import APP_NAME

    ffmpeg = ff.find()
    if not ffmpeg:
        QMessageBox.critical(None, APP_NAME,
                             "没有找到 ffmpeg。\n请重新安装 CastLink，"
                             "或在设置里指定 ffmpeg.exe 的路径。")
        return 1

    cfg = ReceiverConfig.load()
    server = ReceiverServer(ffmpeg, cfg)
    if not server.start():
        QMessageBox.critical(None, APP_NAME,
                             f"无法在端口 {cfg.port} 上启动接收服务。\n"
                             "可能被其他程序占用，或本程序已经运行。")
        return 2

    win = ReceiverWindow(server, cfg, app)
    win.show()
    code = app.exec()
    server.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
