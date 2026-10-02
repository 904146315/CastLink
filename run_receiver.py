# -*- coding: utf-8 -*-
"""运行 Windows 接收端（开发模式，不经打包）。"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "receiver"))
sys.path.insert(0, ROOT)

from receiver.castlink_receiver import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
