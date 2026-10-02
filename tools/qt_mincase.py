# -*- coding: utf-8 -*-
"""定位 app.quit() 崩溃的最小复现：逐项叠加 MainWindow 的窗口特征。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QStatusBar, QSizeGrip,
    QLabel, QPushButton, QListWidget,
)

case = sys.argv[1] if len(sys.argv) > 1 else "frameless"


def build(app):
    if case == "plain":
        w = QMainWindow()
        w.setCentralWidget(QWidget())
        return w
    if case == "frameless":
        w = QMainWindow()
        w.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint)
        w.setCentralWidget(QWidget())
        return w
    if case == "statusbar":
        w = QMainWindow()
        w.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint)
        root = QWidget()
        w.setCentralWidget(root)
        sb = QStatusBar()
        w.setStatusBar(sb)
        grip = QSizeGrip(root)
        grip.setFixedSize(16, 16)
        sb.addPermanentWidget(grip)
        return w
    if case == "listwidget":
        w = QMainWindow()
        w.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint)
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.addWidget(QListWidget())
        lay.addWidget(QLabel("hi"))
        lay.addWidget(QPushButton("x"))
        w.setCentralWidget(root)
        return w
    raise SystemExit("bad case")


app = QApplication(sys.argv)
w = build(app)
w.resize(600, 400)
w.show()
print(f"case={case} shown", flush=True)
QTimer.singleShot(1500, app.quit)
app.exec()
print(f"SURVIVED case={case}", flush=True)
