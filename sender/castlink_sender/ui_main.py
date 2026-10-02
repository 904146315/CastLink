# -*- coding: utf-8 -*-
"""
发送端主界面。

架构约定：所有后台线程（发现、会话、统计）都不直接操作控件，
而是通过 Qt 信号投递到主线程，避免跨线程访问 QWidget 导致的随机崩溃。
"""
from __future__ import annotations

import os
import socket
import time
from typing import Dict, Optional

from PySide6.QtCore import (
    Qt, QObject, Signal, QTimer, QSize, QRect, QPoint, QEvent, Slot,
)
from PySide6.QtGui import (
    QFont, QIcon, QPainter, QColor, QPen, QPixmap, QBrush, QAction, QCloseEvent,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QStyledItemDelegate, QStyleOptionViewItem, QStyle,
    QComboBox, QSpinBox, QLineEdit, QCheckBox, QSplitter, QFrame, QMessageBox,
    QSizeGrip, QSystemTrayIcon, QMenu, QApplication, QInputDialog, QDialog,
    QFormLayout, QTextEdit, QDialogButtonBox, QAbstractItemView, QStatusBar,
)

from . import ui_theme as T
from castlink.config import SenderConfig
from castlink.identity import load_identity
from castlink.discovery import Discovery, Peer
from .session import (
    CastSession, STATE_IDLE, STATE_STREAMING, STATE_CONNECTING, STATE_STARTING,
    STATE_STOPPING, STATE_ERROR, STATE_TEXT, plan_for,
)
from .encoders import (
    Capabilities, probe_capabilities, QUALITY_MODES, estimate_bitrate_mbps,
    recommend_transport, VIDEO_CODEC_HEVC,
)
from .capture import primary_resolution
from .util import resource_path, find_ffmpeg, SingleInstanceGuard, format_bitrate
from . import __version__, APP_NAME


class Bus(QObject):
    """跨线程到 UI 线程的安全通道。"""

    peers = Signal()
    state = Signal(str, str)
    stats = Signal(dict)
    line = Signal(str)
    incoming = Signal(dict, object)


class DeviceDelegate(QStyledItemDelegate):
    """两行式设备条目：名称 + IP，右侧一枚状态 pill。"""

    def __init__(self, parent=None):
        super().__init__(parent)

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 54)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        peer: Peer = index.data(Qt.UserRole)
        r = option.rect.adjusted(6, 3, -6, -3)
        selected = option.state & QStyle.State_Selected
        hovered = option.state & QStyle.State_MouseOver

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        path_color = QColor(T.C_ACCENT) if selected else (
            QColor(T.C_ACCENT_SOFT) if hovered else QColor(T.C_SURFACE_ALT))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(path_color))
        painter.drawRoundedRect(r, 9, 9)

        text = T.C_TEXT if not selected else QColor("#ffffff")
        sub = QColor("#ffffff")
        sub.setAlpha(190)

        name_r = QRect(r.left() + 12, r.top() + 9, r.width() - 90, 18)
        painter.setPen(text)
        f = QFont("Microsoft YaHei UI", 10)
        f.setWeight(QFont.DemiBold)
        painter.setFont(f)
        painter.drawText(name_r, Qt.AlignLeft | Qt.AlignVCenter, peer.name)

        info_r = QRect(r.left() + 12, r.top() + 27, r.width() - 90, 16)
        painter.setPen(sub if selected else QColor(T.C_TEXT_FAINT))
        f2 = QFont("Microsoft YaHei UI", 8)
        painter.setFont(f2)
        caps = f"{peer.max_w}×{peer.max_h}@{peer.max_fps}"
        painter.drawText(info_r, Qt.AlignLeft | Qt.AlignVCenter,
                         f"{peer.ip} · {caps} · {peer.model or 'CAST/1'}")

        pill_text = "投屏中" if peer.streaming else ("需配对" if peer.secure else "就绪")
        pill_bg = QColor("#ffffff")
        pill_bg.setAlpha(45 if selected else 255)
        pill_w = 44
        pill_r = QRect(r.right() - pill_w - 6, r.top() + (r.height() - 20) // 2, pill_w, 20)
        painter.setPen(Qt.NoPen)
        if selected:
            painter.setBrush(QBrush(QColor(255, 255, 255, 60)))
        else:
            pill_bg_col = {"投屏中": T.C_OK_SOFT, "需配对": T.C_WARN_SOFT}.get(pill_text, "#eef1f7")
            painter.setBrush(QBrush(QColor(pill_bg_col)))
        painter.drawRoundedRect(pill_r, 7, 7)
        painter.setPen(QColor("#ffffff") if selected else
                       QColor({"投屏中": T.C_OK, "需配对": T.C_WARN}.get(pill_text, T.C_TEXT_DIM)))
        f3 = QFont("Microsoft YaHei UI", 8)
        f3.setBold(True)
        painter.setFont(f3)
        painter.drawText(pill_r, Qt.AlignCenter, pill_text)
        painter.restore()


class MainWindow(QMainWindow):
    def __init__(self, ffmpeg: str, app: QApplication):
        super().__init__()
        self.app = app
        self.ffmpeg = ffmpeg
        self.cfg = SenderConfig.load()
        self.identity = load_identity()
        self.bus = Bus()
        self.caps: Optional[Capabilities] = None
        self.peers: Dict[str, Peer] = {}
        self._selected_id: Optional[str] = None
        self._manual_target: Optional[tuple] = None
        self._drag_pos: Optional[QPoint] = None
        self._maximized = False
        self._latest_plan_note = ""
        self._estimated_mbps = 0.0
        # 收尾是否已开始 —— 用于让退出流程幂等。
        # 没有它的话：关闭窗口 -> 确认框 -> quit_app() -> app.quit() ->
        # 又触发一次 closeEvent -> 又弹确认框……永远退不出去（见 closeEvent 注释）。
        self._quitting = False

        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.setObjectName("MainWindow")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.resize(980, 690)
        self.setMinimumSize(880, 620)

        icon_path = resource_path("resources", "app_icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        else:
            png = resource_path("resources", "app_icon.png")
            if os.path.exists(png):
                self.setWindowIcon(QIcon(png))

        self.setStyleSheet(T.qss())
        self._build_ui()
        self._build_tray()
        self._wire_bus()

        self.discovery = Discovery(
            role="sender",
            identity=self.identity,
            advertise={"model": self.identity.get("name", "PC"), "ver": __version__},
            log=self._log,
        )
        self.session = CastSession(
            self.cfg, None, ffmpeg, self.identity,
            log=self._log,
            on_state=lambda s, t: self.bus.state.emit(s, t),
            on_stats=lambda d: self.bus.stats.emit(d),
            on_request=lambda req, resp: self.bus.incoming.emit(req, resp),
        )
        self.discovery.on_change = lambda: self.bus.peers.emit()
        self.discovery.start()
        self.session.start_request_server()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        QTimer.singleShot(50, lambda: self._log(
            f"CastLink 发送端 v{__version__} 就绪，等待 ffmpeg 能力探测…"))
        QTimer.singleShot(120, self._probe_async)

    # ---------------------------------------------------------------- UI 构建
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("MainWindow")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_titlebar())

        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(14, 12, 14, 10)
        bl.setSpacing(12)
        bl.addWidget(self._build_left(), 1)
        bl.addWidget(self._build_right(), 2)
        outer.addWidget(body, 1)

        self._build_statusbar()

        grip = QSizeGrip(root)
        grip.setFixedSize(16, 16)
        self.setCentralWidget(root)
        self.statusBar().addPermanentWidget(grip)

    def _build_titlebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(46)
        l = QHBoxLayout(bar)
        l.setContentsMargins(14, 0, 6, 0)
        l.setSpacing(10)

        logo = QLabel()
        p = resource_path("resources", "app_icon.png")
        if os.path.exists(p):
            logo.setPixmap(QPixmap(p).scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setFixedSize(24, 24)
        l.addWidget(logo)

        title = QLabel(APP_NAME)
        title.setObjectName("TitleText")
        sub = QLabel(f"v{__version__} · CAST/1")
        sub.setObjectName("SubTitleText")
        tv = QVBoxLayout()
        tv.setSpacing(0)
        tv.addWidget(title)
        tv.addWidget(sub)
        l.addLayout(tv)
        l.addStretch(1)

        self.btn_log = QPushButton("日志")
        self.btn_log.setObjectName("TitleBtn")
        self.btn_log.setFixedSize(52, 30)
        self.btn_log.clicked.connect(self._toggle_log)
        l.addWidget(self.btn_log)

        for name, obj_name, cb in (("—", "TitleBtn", self.showMinimized),
                                   ("□", "TitleBtn", self._toggle_max),
                                   ("✕", "CloseBtn", self.close)):
            b = QPushButton(name)
            b.setObjectName(obj_name)
            b.setFixedSize(34, 30)
            b.clicked.connect(cb)
            l.addWidget(b)
        return bar

    def _card(self, title: str, hint: str = "") -> tuple:
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("CardTitle")
        head.addWidget(t)
        head.addStretch(1)
        if hint:
            h = QLabel(hint)
            h.setObjectName("Hint")
            head.addWidget(h)
        v.addLayout(head)
        return card, v

    def _build_left(self) -> QWidget:
        card, v = self._card("局域网内的设备", "每 2 秒自动刷新")
        self.lst_devices = QListWidget()
        self.lst_devices.setObjectName("DeviceList")
        self.lst_devices.setItemDelegate(DeviceDelegate(self))
        self.lst_devices.setSelectionMode(QAbstractItemView.SingleSelection)
        self.lst_devices.itemDoubleClicked.connect(lambda _: self._connect_selected())
        v.addWidget(self.lst_devices, 1)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._tick)
        self.btn_manual = QPushButton("手动 IP")
        self.btn_manual.clicked.connect(self._manual_ip)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_manual)
        v.addLayout(row)

        self.lbl_empty = QLabel("正在搜索…确保电脑与投影仪处于同一 Wi-Fi，"
                                "且投影仪端的 CastLink 已打开。")
        self.lbl_empty.setObjectName("Hint")
        self.lbl_empty.setWordWrap(True)
        v.addWidget(self.lbl_empty)
        return card

    def _build_right(self) -> QWidget:
        col = QWidget()
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # ---- 会话卡 ----
        card, cv = self._card("会话")
        top = QHBoxLayout()
        self.lbl_state_icon = QLabel()
        self.lbl_state_icon.setFixedSize(14, 14)
        top.addWidget(self.lbl_state_icon)
        self.lbl_state = QLabel(STATE_TEXT[STATE_IDLE])
        f = QFont("Microsoft YaHei UI", 13)
        f.setWeight(QFont.DemiBold)
        self.lbl_state.setFont(f)
        top.addWidget(self.lbl_state)
        top.addStretch(1)
        self.btn_start = QPushButton("开始投屏")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self._toggle_start)
        self.btn_start.setEnabled(False)
        top.addWidget(self.btn_start)
        cv.addLayout(top)

        self.lbl_plan = QLabel("未选择设备")
        self.lbl_plan.setObjectName("Hint")
        self.lbl_plan.setWordWrap(True)
        cv.addWidget(self.lbl_plan)

        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        self.stat_labels: Dict[str, QLabel] = {}
        items = [("码率", "bitrate"), ("编码 FPS", "fps"), ("网络延迟", "rtt"),
                 ("发送队列", "queue"), ("丢帧", "dropped"), ("解码 FPS", "pfps"),
                 ("接收端收帧", "preceive"), ("接收端画面", "prender"),
                 ("接收端丢帧", "pdrop"), ("接收端丢包", "plost"),
                 # v1.0.9：光有"收帧 / 画面"两个数字还不够。数据在进、画面不动时，
                 # 必须能一眼分清是"等关键帧""解码器故障"还是"画面载体没了"——
                 # 这三者的处理方式完全不同，而投影仪那边默认不显示诊断。
                 ("接收端解码", "pvideo"),
                 ("音频", "audio")]
        for i, (label, key) in enumerate(items):
            lab = QLabel(label)
            lab.setObjectName("Hint")
            val = QLabel("—")
            val.setStyleSheet(f"color: {T.C_TEXT}; font-size: 12px; font-weight: 600;")
            grid.addWidget(lab, i // 3, (i % 3) * 2)
            grid.addWidget(val, i // 3, (i % 3) * 2 + 1)
            self.stat_labels[key] = val
        cv.addLayout(grid)
        v.addWidget(card)

        # ---- 设置卡 ----
        scard, sv = self._card("投屏参数", "改动即时生效于下一次投屏")
        form = QGridLayout()
        form.setContentsMargins(0, 2, 0, 2)
        form.setVerticalSpacing(9)
        form.setHorizontalSpacing(10)

        self.cb_quality = QComboBox()
        for key, q in QUALITY_MODES.items():
            self.cb_quality.addItem(q.label, key)
        self.cb_quality.setToolTip(QUALITY_MODES["lossless"].desc)

        self.cb_res = QComboBox()
        for label, val in (("原生桌面分辨率", "native"), ("1920×1080", "1080p"),
                           ("2560×1440", "1440p"), ("3840×2160", "4k")):
            self.cb_res.addItem(label, val)

        self.cb_fps = QComboBox()
        for val in (60, 30, 24):
            self.cb_fps.addItem(f"{val} fps", val)

        self.cb_chroma = QComboBox()
        self.cb_chroma.addItem("4:2:0（兼容性最好）", "420")
        self.cb_chroma.addItem("4:4:4（文字最锐利）", "444")

        self.cb_encoder = QComboBox()
        self.cb_encoder.addItem("自动择优", "auto")
        self.cb_encoder.addItem("NVIDIA NVENC", "nvenc")
        self.cb_encoder.addItem("Intel 核显 QSV", "qsv")
        self.cb_encoder.addItem("AMD AMF", "amf")
        self.cb_encoder.addItem("CPU 软件编码 x265", "x265")

        self.cb_transport = QComboBox()
        self.cb_transport.addItem("可靠 · TCP（丢包不花屏，推荐）", "tcp")
        self.cb_transport.addItem("低延迟 · UDP（最省延迟，弱网可能花屏）", "udp")
        self.cb_transport.setToolTip(
            "两种模式都是实时语义（队列有界、缓冲按时间折算、超时丢帧）。\n"
            "TCP：丢包靠重传，画面不会花，弱网时延迟略高。\n"
            "UDP：丢包直接表现为花屏，但链路抖动时延迟最低。")

        row = 0

        def add(lbl, widget, extra=None):
            nonlocal row
            l = QLabel(lbl)
            l.setObjectName("Hint")
            form.addWidget(l, row, 0)
            form.addWidget(widget, row, 1)
            if extra:
                form.addWidget(extra, row, 2)
            row += 1

        add("画质", self.cb_quality)
        add("输出分辨率", self.cb_res)
        add("帧率", self.cb_fps)
        add("色度抽样", self.cb_chroma)
        add("编码器", self.cb_encoder)
        add("传输方式", self.cb_transport)

        opts = QHBoxLayout()
        self.chk_audio = QCheckBox("投射系统声音")
        self.chk_mute = QCheckBox("静音本机")
        self.chk_mute.setToolTip("投屏期间把电脑扬声器静音，声音只从投影仪出。\n"
                                 "不静音的话同一段声音会两边同时响、相差几百毫秒，像回声。\n"
                                 "停止投屏后自动恢复。")
        self.chk_mouse = QCheckBox("包含鼠标指针")
        self.chk_canvas = QCheckBox("补齐 4K 画布")
        self.chk_canvas.setToolTip("桌面不足 4K 时，等比放大并补黑边，"
                                   "使投影仪收到标准 3840×2160 信号")
        for w in (self.chk_audio, self.chk_mute, self.chk_mouse, self.chk_canvas):
            opts.addWidget(w)
        opts.addStretch(1)
        sv.addLayout(form)
        sv.addLayout(opts)

        line = QHBoxLayout()
        line.setSpacing(8)
        lbl_pin = QLabel("配对 PIN")
        lbl_pin.setObjectName("Hint")
        self.ed_pin = QLineEdit()
        self.ed_pin.setEchoMode(QLineEdit.Password)
        self.ed_pin.setFixedWidth(120)
        self.chk_pin = QCheckBox("要求 PIN 配对")
        line.addWidget(lbl_pin)
        line.addWidget(self.ed_pin)
        line.addWidget(self.chk_pin)
        line.addStretch(1)
        self.btn_forget = QPushButton("清除配对记录")
        self.btn_forget.setObjectName("Ghost")
        self.btn_forget.clicked.connect(self._forget_peers)
        line.addWidget(self.btn_forget)
        sv.addLayout(line)
        v.addWidget(scard, 1)

        # ---- 日志 ----
        self.log = QTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setVisible(False)
        self.log.setFixedHeight(150)
        v.addWidget(self.log)
        return col

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.lbl_status = QLabel("就绪")
        sb.addWidget(self.lbl_status, 1)
        self.lbl_bw = QLabel("")
        self.lbl_bw.setObjectName("Hint")
        sb.addPermanentWidget(self.lbl_bw)

    def _build_tray(self):
        icon_path = resource_path("resources", "app_icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("显示主界面", self.show_normal_from_tray)
        menu.addSeparator()
        self.tray_act_start = QAction("开始投屏", self)
        self.tray_act_start.triggered.connect(self._toggle_start)
        menu.addAction(self.tray_act_start)
        act_stop = QAction("停止投屏", self)
        act_stop.triggered.connect(lambda: self.session.stop())
        menu.addAction(act_stop)
        menu.addSeparator()
        menu.addAction("退出", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_normal_from_tray()
                                    if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    # ---------------------------------------------------------------- 数据绑定
    def _load_cfg_to_ui(self):
        """
        把配置刷进各个控件。

        **必须屏蔽 `_cfg_changed`**：这些控件全都连着 `_cfg_changed`，逐个
        setChecked/setCurrentIndex 时信号会立刻触发，而 `_cfg_changed` 会把
        **所有**控件的当前值写回 cfg 并 save() —— 此时排在后面的控件还是出厂
        默认值，于是刚读出来的配置立刻被覆盖回默认、顺手落盘。

        症状非常隐蔽：音频开关、鼠标捕获、4K 画布、PIN 这些"排在后面"的设置，
        **每次启动都会被悄悄重置回默认**。用户明明在界面上勾了"投屏系统声音"，
        下次启动又变回没勾，投屏自然没声音 —— 这也是"没声音"的一个真实成因，
        而且和音频采集本身能不能用完全无关。
        """
        self._loading_cfg = True
        try:
            idx = self.cb_quality.findData(self.cfg.quality)
            self.cb_quality.setCurrentIndex(max(0, idx))
            idx = self.cb_res.findData(self.cfg.resolution_mode)
            self.cb_res.setCurrentIndex(max(0, idx))
            idx = self.cb_fps.findData(self.cfg.fps)
            self.cb_fps.setCurrentIndex(max(0, idx))
            idx = self.cb_chroma.findData(self.cfg.chroma)
            self.cb_chroma.setCurrentIndex(max(0, idx))
            idx = self.cb_encoder.findData(self.cfg.encoder)
            self.cb_encoder.setCurrentIndex(max(0, idx))
            idx = self.cb_transport.findData(self.cfg.transport)
            self.cb_transport.setCurrentIndex(max(0, idx))
            self.chk_audio.setChecked(self.cfg.enable_audio)
            self.chk_mute.setChecked(getattr(self.cfg, "mute_local", True))
            self.chk_mouse.setChecked(self.cfg.capture_mouse)
            self.chk_canvas.setChecked(self.cfg.canvas_4k)
            self.chk_pin.setChecked(self.cfg.require_pin)
            self.ed_pin.setText(self.cfg.pin)
        finally:
            self._loading_cfg = False
        self._sync_audio_deps()
        self._update_plan_note()

    def _sync_audio_deps(self):
        """「静音本机」只在投射系统声音时才有意义。"""
        try:
            self.chk_mute.setEnabled(self.chk_audio.isChecked())
        except Exception:
            pass

    def _wire_bus(self):
        self.cb_quality.currentIndexChanged.connect(self._cfg_changed)
        self.cb_res.currentIndexChanged.connect(self._cfg_changed)
        self.cb_fps.currentIndexChanged.connect(self._cfg_changed)
        self.cb_chroma.currentIndexChanged.connect(self._cfg_changed)
        self.cb_encoder.currentIndexChanged.connect(self._cfg_changed)
        self.cb_transport.currentIndexChanged.connect(self._cfg_changed)
        self.chk_audio.toggled.connect(self._cfg_changed)
        self.chk_mute.toggled.connect(self._cfg_changed)
        self.chk_mouse.toggled.connect(self._cfg_changed)
        self.chk_canvas.toggled.connect(self._cfg_changed)
        self.chk_pin.toggled.connect(self._cfg_changed)
        self.ed_pin.textChanged.connect(self._cfg_changed)

        self.bus.peers.connect(self._refresh_peers)
        self.bus.state.connect(self._on_state)
        self.bus.stats.connect(self._on_stats)
        self.bus.line.connect(self._append_log)
        self.bus.incoming.connect(self._on_incoming)

        self.lst_devices.itemSelectionChanged.connect(self._on_device_selected)
        self._load_cfg_to_ui()

    def _cfg_changed(self, *_):
        # 装载配置期间控件信号会连环触发，此时绝不能回写 cfg —— 否则会把
        # 尚未恢复的控件的默认值当成用户选择写进配置（见 _load_cfg_to_ui）。
        if getattr(self, "_loading_cfg", False):
            return
        self.cfg.quality = self.cb_quality.currentData()
        self.cfg.resolution_mode = self.cb_res.currentData()
        self.cfg.fps = self.cb_fps.currentData()
        self.cfg.chroma = self.cb_chroma.currentData()
        self.cfg.encoder = self.cb_encoder.currentData()
        self.cfg.transport = self.cb_transport.currentData()
        self.cfg.enable_audio = self.chk_audio.isChecked()
        self.cfg.mute_local = self.chk_mute.isChecked()
        self.cfg.capture_mouse = self.chk_mouse.isChecked()
        self.cfg.canvas_4k = self.chk_canvas.isChecked()
        self.cfg.require_pin = self.chk_pin.isChecked()
        self.cfg.pin = self.ed_pin.text() or "000000"
        self.cfg.save()
        self._sync_audio_deps()
        self._update_plan_note()

        q = QUALITY_MODES[self.cfg.quality]
        self.cb_quality.setToolTip(q.desc)

    # ---------------------------------------------------------------- 后台 -> UI
    def _log(self, msg: str):
        self.bus.line.emit(str(msg))

    def _append_log(self, text: str):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f'<span style="color:#6a7a9c">[{ts}]</span> '
                        f'<span style="color:#b9c6e0">{text}</span>')
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _probe_async(self):
        self.lbl_status.setText("正在探测本机编码器能力…")
        self.caps = probe_capabilities(self.ffmpeg, deep=True)
        self.session.caps = self.caps
        names = [c.display for c in self.caps.encoders.values() if c.available]
        available_hevc = [c.display for n, c in self.caps.encoders.items()
                          if c.available and n.startswith("hevc")]
        self._log("可用 HEVC 编码器: " + (", ".join(available_hevc) or "无（将退回 H.264）"))
        self.lbl_status.setText("能力探测完成")
        self._update_plan_note()

    def _refresh_peers(self):
        peers = self.discovery.peers_of("receiver")
        peers.sort(key=lambda p: p.name)
        old_ids = {p.id for p in peers}
        cur = self.lst_devices.currentItem()
        keep_id = cur.data(Qt.UserRole).id if cur else None

        self.lst_devices.blockSignals(True)
        self.lst_devices.clear()
        for p in peers:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, p)
            item.setSizeHint(QSize(100, 54))
            self.lst_devices.addItem(item)
        self.lst_devices.blockSignals(False)

        if keep_id and keep_id in old_ids:
            for i in range(self.lst_devices.count()):
                it = self.lst_devices.item(i)
                if it.data(Qt.UserRole).id == keep_id:
                    self.lst_devices.setCurrentItem(it)
                    break
        else:
            self.lst_devices.setCurrentRow(0)

        self.lbl_empty.setVisible(len(peers) == 0)
        self.btn_start.setEnabled(len(peers) > 0 or bool(self._manual_target))
        self._on_device_selected()

    def _on_device_selected(self):
        it = self.lst_devices.currentItem()
        if not it:
            self.lbl_plan.setText("未选择设备")
            self._update_plan_note()
            return
        p: Peer = it.data(Qt.UserRole)
        self._selected_id = p.id
        self.cfg.last_peer = p.id
        self._update_plan_note()

    def _update_plan_note(self):
        it = self.lst_devices.currentItem()
        peer: Optional[Peer] = it.data(Qt.UserRole) if it else None
        # 预览必须和真正开流用同一套算法（含带宽预算、帧率上限、编解码格式），
        # 否则界面上的 "预计 xx Mbps" 和实际下发的规格对不上。
        if peer is not None:
            caps = {"maxW": peer.max_w, "maxH": peer.max_h, "maxFps": peer.max_fps,
                    "codecs": list(peer.codecs or [])}
        else:
            caps = {}
        desktop = primary_resolution()
        plan = plan_for(self.cfg, desktop, caps)
        self._estimated_mbps = plan.bitrate_mbps
        q = QUALITY_MODES[self.cfg.quality]
        mode_txt = "真无损" if q.lossless else f"{plan.quality}"
        chroma_txt = {"420": "4:2:0", "444": "4:4:4"}.get(plan.chroma, plan.chroma)
        txt = (f"{plan.out_w}×{plan.out_h}@{plan.fps} · {chroma_txt} · "
               f"{q.label} · 预计 {format_bitrate(plan.bitrate_mbps)}")
        if plan.note:
            txt += f"（{plan.note}）"
        if self.cfg.hdr_tone_map:
            txt += " · HDR 色调映射"
        self.lbl_plan.setText(txt)

        title = peer.name if peer else "未选择设备"
        session = getattr(self, "session", None)
        if hasattr(self, "lbl_state") and session is not None \
                and session.state == STATE_IDLE:
            self.lbl_plan.setText(f"目标：{title} · " + txt)

        _, advice = recommend_transport(plan.bitrate_mbps)
        self.lbl_bw.setText(f"带宽建议：{advice}")
        if q.lossless and self.cfg.transport != "tcp":
            self.lbl_bw.setText("带宽建议：真无损必须配合 TCP 可靠传输，已自动切换")
            self.cb_transport.setCurrentIndex(0)

    def _on_state(self, state: str, msg: str):
        color = {STATE_STREAMING: T.C_OK, STATE_ERROR: T.C_ERR,
                 STATE_CONNECTING: T.C_WARN, STATE_STARTING: T.C_WARN,
                 STATE_STOPPING: T.C_WARN}.get(state, T.C_TEXT_DIM)
        self.lbl_state.setText(STATE_TEXT.get(state, state) + (f" · {msg}" if msg else ""))
        self.lbl_state_icon.setPixmap(self._dot_pixmap(color))
        streaming = state == STATE_STREAMING
        self.btn_start.setText("停止投屏" if streaming else "开始投屏")
        self.btn_start.setEnabled(True)
        self.btn_start.setProperty("class", "")
        self.btn_start.setObjectName("Danger" if streaming else "Primary")
        self.btn_start.style().polish(self.btn_start)
        self.tray_act_start.setText("开始投屏" if not streaming else "投屏中")
        self.lbl_status.setText(f"{STATE_TEXT.get(state, state)} {msg}".strip())
        self.tray.setToolTip(f"{APP_NAME} · {STATE_TEXT.get(state, state)}")

    def _dot_pixmap(self, color: str) -> QPixmap:
        pm = QPixmap(14, 14)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(2, 2, 10, 10)
        p.end()
        return pm

    def _update_audio_stat(self, d: dict):
        """
        「音频」这一格要同时回答两个问题：**本机采到没有** 和 **投影仪放出来没有**。

        只有一个本地音源名字是不够的 —— 之前"没声音"排查了好几轮，
        就是因为界面显示"扬声器(WASAPI 回环)"，看的人以为音频正常，
        其实是投影仪那侧压根解不出声。现在把接收端回报的状态并排显示出来：
        左边是本机音源，右边是投影仪的实际状态（OK / 无数据 / 解码失败…）。
        """
        note = str(d.get("audio") or "")
        if not note or note == "None":
            local = "—"
        elif "回环" in note:
            local = "回环"
        elif "未找到" in note or "不可用" in note or "失败" in note:
            local = "无音源"
        elif "关闭" in note:
            local = "已关"
        else:
            local = note.split("（")[0][:6]

        peer = d.get("peer") or {}
        remote = str(peer.get("audioStatus") or "").strip()
        if not remote:
            remote = "—"
        elif len(remote) > 16:
            remote = remote[:15] + "…"

        self.stat_labels["audio"].setText(f"{local} → {remote}")
        recv = peer.get("audioRecv")
        dec = peer.get("audioDec")
        if isinstance(recv, (int, float)) or isinstance(dec, (int, float)):
            self.stat_labels["audio"].setToolTip(
                f"本机音源：{note or '未启用'}\n"
                f"投影仪收到音频帧：{int(recv or 0)}\n"
                f"投影仪解出音频帧：{int(dec or 0)}")
        else:
            self.stat_labels["audio"].setToolTip(f"本机音源：{note or '未启用'}")

    def _on_stats(self, d: dict):
        drop_multiplier = 1
        self.stat_labels["bitrate"].setText(format_bitrate(d.get("bitrate", 0)))
        self.stat_labels["fps"].setText(f"{d.get('fps', 0):.1f}")
        self.stat_labels["rtt"].setText(f"{d.get('rtt', 0):.1f} ms")
        self.stat_labels["queue"].setText(f"{d.get('queued', 0)}")
        self.stat_labels["dropped"].setText(f"{d.get('dropped', 0)}")
        peer = d.get("peer") or {}
        pfps = peer.get("fps")
        self.stat_labels["pfps"].setText(f"{pfps:.1f}" if isinstance(pfps, (int, float)) else "—")
        # 接收端侧的计数 —— 排查"电脑在发、投影仪没画面/没声音"时最有用的证据：
        #   收帧为 0          -> 数据根本没到，或两边协议/端口对不上
        #   收帧在涨、画面为 0 -> 数据到了但解码器没出图（编码格式或参数集问题）
        #   丢帧在飞涨        -> 解码器跟不上，正在成片丢非关键帧（表现为几秒一帧）
        #   丢包在飞涨        -> 链路在丢数据
        for key, src in (("preceive", "assembled"), ("prender", "rendered"),
                         ("pdrop", "dropped"), ("plost", "lostPackets")):
            v = peer.get(src)
            self.stat_labels[key].setText(
                str(int(v)) if isinstance(v, (int, float)) else "—")
        self._update_video_stat(peer)
        self._update_audio_stat(d)

    def _update_video_stat(self, peer: dict):
        """
        「接收端解码」这一格：投影仪解码器的现场状态。

        为什么要专门给一格：诊断行在接收端默认是关的（用户要画面干净），
        而"有声音、画面不动"这类故障恰恰要看解码器内部状态。把它显示在电脑上，
        既保住了投影画面的干净，又不用再靠反复装包试探。

        三种状态对应的处置完全不同：
          * **等关键帧**     -> 缺 IDR（网络丢包 / 发送端还没出关键帧）
          * **解码器故障**    -> 解码器异常被重建，会自动重配，多半能自愈
          * **无画面载体**    -> 投影仪那侧 Surface 失效（界面被系统重建）
        """
        vstate = peer.get("videoState")
        if not (isinstance(vstate, str) and vstate):
            self.stat_labels["pvideo"].setText("—")
            self.stat_labels["pvideo"].setToolTip("等待投影仪回报解码状态")
            return
        rc = peer.get("videoReconfig")
        ds = peer.get("decodeStalls")
        marks = []
        if peer.get("surface") is False:
            marks.append("无画面载体")
        if isinstance(rc, (int, float)) and rc > 0:
            marks.append(f"重建{int(rc)}")
        if isinstance(ds, (int, float)) and ds > 0:
            marks.append(f"停滞{int(ds)}")
        txt = vstate if len(vstate) <= 10 else vstate[:9] + "…"
        if marks:
            txt += " " + "·".join(marks)
        self.stat_labels["pvideo"].setText(txt)
        out_ms = peer.get("videoOut")
        self.stat_labels["pvideo"].setToolTip(
            f"投影仪解码器状态：{vstate}\n"
            f"画面载体（Surface）有效：{'是' if peer.get('surface') else '否'}\n"
            f"解码器重建次数：{int(rc or 0)}\n"
            f"解码停滞次数：{int(ds or 0)}\n"
            f"距上次成功出图：{'从未出图' if out_ms == -1 else str(out_ms) + ' ms'}")

    # ---------------------------------------------------------------- 交互
    def _selected_peer(self) -> Optional[Peer]:
        it = self.lst_devices.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _toggle_start(self):
        if self.session.state == STATE_STREAMING:
            self.session.stop()
            return
        self.connect_selected()

    def connect_selected(self):
        peer = self._selected_peer()
        target = getattr(self, "_manual_target", None)
        if not peer and not target:
            QMessageBox.information(self, "没有可投屏的设备",
                                    "请先在左侧选择投影仪，或使用「手动 IP」输入其地址。")
            return
        if self.session.state in (STATE_CONNECTING, STATE_STARTING):
            return
        if self.caps is None:
            self._probe_async()
        self.session.caps = self.caps
        host = target[0] if target else peer.ip
        port = target[1] if target else peer.ctrl_port
        label = target[0] if target else peer.name
        self.cfg.transport = self.cb_transport.currentData()
        if self.cfg.quality == "lossless":
            self.cfg.transport = "tcp"
        self.session.connect_to(host, port, label)

    def _connect_selected(self):
        self.connect_selected()

    def _manual_ip(self):
        ip, ok = QInputDialog.getText(self, "手动连接投影仪",
                                      "输入投影仪 IP（例如 192.168.1.23）：")
        if not ok or not ip.strip():
            return
        ip = ip.strip()
        self._manual_target = (ip, 47000)
        self.btn_start.setEnabled(True)
        self._log(f"已设置手动目标 {ip}:47000")

    def _forget_peers(self):
        self.cfg.remembered_peers = {}
        self.cfg.save()
        self._log("已清除所有配对令牌，下次连接需重新输入 PIN")

    def _toggle_log(self):
        self.log.setVisible(not self.log.isVisible())

    def _toggle_max(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ---- 投影仪主动请求 ----
    def _on_incoming(self, req: dict, respond):
        box = QMessageBox(self)
        box.setWindowTitle("投屏请求")
        box.setText(f"投影仪「{req.get('name', req.get('ip'))}」请求接收你的屏幕画面。")
        box.setInformativeText("是否允许投屏？")
        yes = box.addButton("允许投屏", QMessageBox.YesRole)
        box.addButton("拒绝", QMessageBox.NoRole)
        box.exec()
        ok = box.clickedButton() is yes
        respond(ok, "" if ok else "用户拒绝")
        if ok:
            self._manual_target = (req.get("ip"), int(req.get("ctrl_port", 47000)))
            QTimer.singleShot(200, self.connect_selected)

    # ---- 窗口拖拽 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 46:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def show_normal_from_tray(self):
        self.show()
        self.raise_()
        self.activateWindow()

    # ---- 关闭 ----
    def closeEvent(self, e: QCloseEvent):
        # app.quit()/exit(0) 结束事件循环时，Qt 会把关闭事件再发给各窗口。
        # 若不短路，这里会**第二次**弹出确认框：用户点"是"→退出→又弹框，
        # 无限循环，进程永远关不掉（曾经"退出后进程还挂在任务管理器里"的根因）。
        if self._quitting:
            e.accept()
            return
        reply = QMessageBox.question(
            self, "退出 CastLink",
            "退出后将停止投屏。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            e.ignore()
            return
        self.quit_app()
        e.accept()

    def quit_app(self):
        """幂等收尾。重复调用直接返回，避免退出流程被重入。"""
        if self._quitting:
            return
        self._quitting = True
        try:
            self.session.shutdown()
        except Exception:
            pass
        try:
            self.discovery.stop()
        except Exception:
            pass
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass
        # 用 exit(0) 而不是 quit()：quit() 会把关闭事件发给所有窗口，
        # 把上面的 closeEvent 又拽进来一次 —— 那正是自锁的来源。
        self.app.exit(0)

    # ---- 定时刷新 ----
    def _tick(self):
        self.bus.peers.emit()
