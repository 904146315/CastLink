# -*- coding: utf-8 -*-
"""
界面主题。

浅色为主：投屏软件通常在明亮的会议室里被打开，深色 UI 在投影仪光线下容易看不清，
所以整体走「浅色卡片 + 蓝色强调色」的路线，状态色语义清晰。
"""
from __future__ import annotations

C_BG = "#f4f6fb"
C_SURFACE = "#ffffff"
C_SURFACE_ALT = "#fafbfe"
C_BORDER = "#e3e8f2"
C_TEXT = "#17203a"
C_TEXT_DIM = "#5c6780"
C_TEXT_FAINT = "#8a93a8"

C_ACCENT = "#2f6bff"
C_ACCENT_DARK = "#1f4fd6"
C_ACCENT_SOFT = "#e8efff"

C_OK = "#17915a"
C_OK_SOFT = "#e3f7ee"
C_WARN = "#b8730a"
C_WARN_SOFT = "#fdf1dd"
C_ERR = "#d43b3b"
C_ERR_SOFT = "#fdeaea"

C_HEADER_0 = "#111a33"
C_HEADER_1 = "#1c2b52"
C_HEADER_2 = "#0d1428"

RADIUS = 10
RADIUS_LG = 14


def qss() -> str:
    return f"""
    * {{ font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif; }}
    QToolTip {{
        color: {C_TEXT}; background: #ffffff; border: 1px solid {C_BORDER};
        padding: 6px 9px; border-radius: 6px;
    }}
    QWidget#MainWindow {{
        background: {C_BG};
    }}
    QWidget#TitleBar {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {C_HEADER_0}, stop:0.55 {C_HEADER_1}, stop:1 {C_HEADER_2});
    }}
    QLabel#TitleText {{
        color: #ffffff; font-size: 13px; font-weight: 600; letter-spacing: .3px;
    }}
    QLabel#SubTitleText {{
        color: rgba(255,255,255,0.58); font-size: 11px;
    }}
    QPushButton#TitleBtn {{
        background: transparent; color: rgba(255,255,255,0.82);
        border: none; border-radius: 6px; font-size: 12px;
    }}
    QPushButton#TitleBtn:hover {{ background: rgba(255,255,255,0.14); color: #fff; }}
    QPushButton#CloseBtn:hover {{ background: #e04b4b; color: #fff; }}

    QFrame#Card {{
        background: {C_SURFACE}; border: 1px solid {C_BORDER}; border-radius: {RADIUS_LG}px;
    }}
    QLabel#CardTitle {{
        color: {C_TEXT}; font-size: 12px; font-weight: 600;
    }}
    QLabel#Hint {{
        color: {C_TEXT_FAINT}; font-size: 11px;
    }}
    QLabel#SectionLabel {{
        color: {C_TEXT_DIM}; font-size: 11px; font-weight: 600;
    }}

    QListWidget#DeviceList {{
        background: {C_SURFACE_ALT}; border: 1px solid {C_BORDER};
        border-radius: {RADIUS}px; outline: 0; padding: 4px;
    }}
    QListWidget#DeviceList::item {{
        border-radius: 8px; margin: 2px 1px; padding: 0px;
    }}
    QListWidget#DeviceList::item:hover {{ background: {C_ACCENT_SOFT}; }}
    QListWidget#DeviceList::item:selected {{
        background: {C_ACCENT}; color: #ffffff;
    }}

    QLabel#Pill {{
        background: {C_ACCENT_SOFT}; color: {C_ACCENT_DARK};
        border-radius: 8px; padding: 3px 8px; font-size: 10px; font-weight: 600;
    }}
    QLabel#PillIdle {{ background: #eef1f7; color: {C_TEXT_DIM}; }}

    QPushButton {{
        background: {C_SURFACE}; color: {C_TEXT};
        border: 1px solid {C_BORDER}; border-radius: 8px; padding: 7px 14px; font-size: 12px;
    }}
    QPushButton:hover {{ border-color: {C_ACCENT}; color: {C_ACCENT}; }}
    QPushButton:disabled {{ color: {C_TEXT_FAINT}; background: #f2f4f9; border-color: {C_BORDER}; }}

    QPushButton#Primary {{
        background: {C_ACCENT}; color: #ffffff; border: 1px solid {C_ACCENT};
        font-weight: 600; padding: 9px 22px;
    }}
    QPushButton#Primary:hover {{ background: {C_ACCENT_DARK}; border-color: {C_ACCENT_DARK}; color: #fff; }}
    QPushButton#Primary:disabled {{ background: #a9c0ff; border-color: #a9c0ff; color: #fff; }}

    QPushButton#Danger {{
        background: {C_ERR_SOFT}; color: {C_ERR}; border: 1px solid #f6c9c9; font-weight: 600;
        padding: 9px 22px;
    }}
    QPushButton#Danger:hover {{ background: #fbdcdc; color: {C_ERR}; }}

    QPushButton#Ghost {{ background: transparent; border: 1px solid transparent; color: {C_TEXT_DIM}; }}
    QPushButton#Ghost:hover {{ color: {C_ACCENT}; background: {C_ACCENT_SOFT}; }}

    QComboBox, QSpinBox, QLineEdit {{
        background: {C_SURFACE}; color: {C_TEXT};
        border: 1px solid {C_BORDER}; border-radius: 8px; padding: 6px 10px; font-size: 12px;
        min-height: 20px;
    }}
    QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: {C_ACCENT}; }}
    QComboBox:focus, QLineEdit:focus {{ border-color: {C_ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{
        background: #fff; border: 1px solid {C_BORDER}; border-radius: 8px;
        selection-background-color: {C_ACCENT_SOFT}; selection-color: {C_TEXT};
        outline: 0;
    }}

    QCheckBox {{ color: {C_TEXT}; font-size: 12px; spacing: 6px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 5px;
        border: 1px solid {C_BORDER}; background: #fff;
    }}
    QCheckBox::indicator:hover {{ border-color: {C_ACCENT}; }}
    QCheckBox::indicator:checked {{
        background: {C_ACCENT}; border-color: {C_ACCENT};
        image: url(none);
    }}

    QTextEdit#Log {{
        background: #0f1626; color: #b9c6e0; border: none; border-radius: {RADIUS}px;
        font-family: Consolas, "Courier New", monospace; font-size: 11px; padding: 8px;
    }}

    QProgressBar {{
        background: #eef1f7; border: none; border-radius: 4px; height: 6px;
    }}
    QProgressBar::chunk {{ background: {C_ACCENT}; border-radius: 4px; }}

    QStatusBar {{ background: {C_SURFACE_ALT}; border-top: 1px solid {C_BORDER}; color: {C_TEXT_DIM}; font-size: 11px; }}
    QSplitter::handle {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #d3dae8; border-radius: 4px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: #b9c3d6; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """
