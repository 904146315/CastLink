# -*- coding: utf-8 -*-
"""
Windows 接收端界面。

刻意做得极简：接收端在真实场景里是常驻后台的，
界面只要在出问题时能一眼看到「IP / 状态 / 丢帧」就够了。
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, QObject, Slot
from PySide6.QtGui import QIcon, QFont, QCloseEvent
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QFrame, QGridLayout, QMessageBox, QApplication, QSizeGrip,
)

from castlink.config import ReceiverConfig
from .server import ReceiverServer

QSS = """
* { font-family: "Microsoft YaHei UI","PingFang SC","Segoe UI",sans-serif; }
QWidget#Root { background: #f4f6fb; }
QFrame#Card {
    background: #ffffff; border: 1px solid #e3e8f2; border-radius: 12px;
}
QLabel#Title { color:#17203a; font-size:12px; font-weight:600; }
QLabel#Hint { color:#8a93a8; font-size:11px; }
QLabel#Big { color:#17203a; font-size:14px; font-weight:600; }
QLabel#Mono { color:#17203a; font-size:12px; font-weight:600; }
QPushButton {
    background:#fff; color:#17203a; border:1px solid #e3e8f2;
    border-radius:8px; padding:7px 14px; font-size:12px;
}
QPushButton:hover { border-color:#2f6bff; color:#2f6bff; }
QPushButton#Danger {
    background:#fdeaea; color:#d43b3b; border:1px solid #f6c9c9; font-weight:600;
}
QTextEdit#Log {
    background:#0f1626; color:#b9c6e0; border:none; border-radius:10px;
    font-family:Consolas,"Courier New",monospace; font-size:11px; padding:8px;
}
"""


class LogBus(QObject):
    line = Signal(str)
    state = Signal(str, str)


class ReceiverWindow(QMainWindow):
    def __init__(self, server: ReceiverServer, cfg: ReceiverConfig, app: QApplication):
        super().__init__()
        self.server = server
        self.cfg = cfg
        self.app = app
        self.setWindowTitle("CastLink 接收端（Windows）")
        self.setObjectName("Root")
        self.resize(760, 560)
        self.setMinimumSize(680, 480)
        self.setStyleSheet(QSS)

        root = QWidget()
        root.setObjectName("Root")
        v = QVBoxLayout(root)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(12)

        card = QFrame()
        card.setObjectName("Card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(16, 14, 16, 14)
        cv.setSpacing(8)

        t = QLabel("局域网接收状态")
        t.setObjectName("Title")
        cv.addWidget(t)

        self.lbl_state = QLabel("等待电脑连接…")
        self.lbl_state.setObjectName("Big")
        cv.addWidget(self.lbl_state)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        self.lbl_ip = QLabel("—")
        self.lbl_ip.setObjectName("Mono")
        self.lbl_frames = QLabel("0")
        self.lbl_frames.setObjectName("Mono")
        self.lbl_drop = QLabel("0")
        self.lbl_drop.setObjectName("Mono")
        self.lbl_lost = QLabel("0")
        self.lbl_lost.setObjectName("Mono")
        for i, (k, w) in enumerate((("本机地址", self.lbl_ip), ("已呈现帧", self.lbl_frames),
                                    ("丢帧", self.lbl_drop), ("丢包", self.lbl_lost))):
            lab = QLabel(k)
            lab.setObjectName("Hint")
            grid.addWidget(lab, 0, i * 2)
            grid.addWidget(w, 0, i * 2 + 1)
        cv.addLayout(grid)

        row = QHBoxLayout()
        self.btn_stop = QPushButton("结束当前投屏")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.clicked.connect(self._stop_session)
        self.btn_forget = QPushButton("清除配对记录")
        self.btn_forget.clicked.connect(self._forget)
        row.addWidget(self.btn_stop)
        row.addWidget(self.btn_forget)
        row.addStretch(1)
        cv.addLayout(row)
        v.addWidget(card)

        self.log = QTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        v.addWidget(self.log, 1)

        hint = QLabel("投屏画面会弹出独立窗口；关闭本窗口不会结束投屏，"
                      "请点击「结束当前投屏」或退出程序。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.setCentralWidget(root)
        grip = QSizeGrip(root)

        self.bus = LogBus()
        self.bus.line.connect(self._append)
        self.bus.state.connect(self._on_state)
        server.log = lambda m: self.bus.line.emit(str(m))
        server.on_state = lambda s, t: self.bus.state.emit(s, t)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        QTimer.singleShot(200, self._show_ip)

    @Slot(str)
    def _append(self, text: str):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f'<span style="color:#6a7a9c">[{ts}]</span> '
                        f'<span style="color:#b9c6e0">{text}</span>')

    @Slot(str, str)
    def _on_state(self, state: str, info: str):
        if state == "streaming":
            self.lbl_state.setText(f"正在接收「{info}」的画面")
        else:
            self.lbl_state.setText("等待电脑连接…")

    def _show_ip(self):
        try:
            from sender.castlink_sender.util import local_ip_for
            ip = local_ip_for()
        except Exception:
            ip = "127.0.0.1"
        self.lbl_ip.setText(f"{ip}:{self.cfg.port}")

    def _tick(self):
        snap = self.server.renderer and None
        sess = self.server.sessions
        if sess:
            s = sess[-1]
            asm = getattr(s, "_assembler", None)
            self.lbl_frames.setText(str(getattr(s, "_frames", 0)))
            if asm:
                snapd = asm.snapshot()
                self.lbl_drop.setText(str(snapd.get("dropped", 0)))
                self.lbl_lost.setText(str(snapd.get("lost_packets", 0)))

    def _stop_session(self):
        self.server.stop_session()
        self._append("已请求结束当前投屏")

    def _forget(self):
        self.server.forget_tokens()
        self._append("已清除配对令牌")

    def closeEvent(self, e: QCloseEvent):
        box = QMessageBox(self)
        box.setWindowTitle("CastLink 接收端")
        box.setText("退出接收端？")
        box.setInformativeText("选择「最小化到后台」可继续被电脑发现。")
        minimize = box.addButton("最小化到后台", QMessageBox.YesRole)
        quit_btn = box.addButton("退出", QMessageBox.NoRole)
        box.exec()
        if box.clickedButton() is minimize:
            self.hide()
            e.ignore()
        else:
            self.server.stop()
            self.app.quit()
            e.accept()
