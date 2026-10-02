# -*- coding: utf-8 -*-
"""
CastLink 发送端 —— 小米笔记本 → 坚果投影仪的 4K 无线投屏。

这里做的唯一"魔法"是把项目根目录塞进 sys.path，
好让两端共用的 `castlink` 协议包无论从哪个工作目录启动都能被找到。
"""
from __future__ import annotations

import os
import sys

# 打包后（PyInstaller）模块被收进归档，sys.path 已由引导程序配置好，
# 这一堆路径推导在 frozen 下只会算出并不存在的目录，直接跳过更干净。
if not getattr(sys, "frozen", False):
    _PKG_DIR = os.path.dirname(os.path.abspath(__file__))          # .../sender/castlink_sender
    _APP_DIR = os.path.dirname(_PKG_DIR)                           # .../sender
    _ROOT = os.path.dirname(_APP_DIR)                              # 项目根
    for _p in (_ROOT, _APP_DIR):
        if _p not in sys.path:
            sys.path.insert(0, _p)

__version__ = "1.0.10"
APP_NAME = "CastLink 投屏发送端"
APP_PUBLISHER = "CastLink"
PROTOCOL_VERSION = "1.0"
