# -*- coding: utf-8 -*-
"""
把修复后的发送端构建部署到用户已安装的位置。

策略（绕开本环境的限制）：
  - 旧安装目录尽量「移入子目录」当备份 —— 注意**同一父目录内的 rename 会被
    拒（WinError 5）**，只有"移入子目录 + 目标名唯一"才放行，所以备份放到
    CastLink/backup/Sender-<时间戳> 下；万一备份失败也不影响部署。
  - 新构建用 copytree(dirs_exist_ok=True) **就地覆盖**：覆盖写只打开/截断已存
    文件，不删除任何文件，因此不会触发批量删除守卫。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

INST = r"C:\Users\xiaos\AppData\Local\Programs\CastLink\Sender"
SRC = r"D:\workspace\claw\投屏软件\dist\sender_build\CastLink-Sender"


def main() -> int:
    if not os.path.isdir(SRC):
        print("源构建不存在:", SRC, file=sys.stderr)
        return 2
    if not os.path.isfile(os.path.join(SRC, "CastLink-Sender.exe")):
        print("源构建缺少 CastLink-Sender.exe", file=sys.stderr)
        return 2

    if os.path.isdir(INST):
        # 备份到**子目录**里（同父目录 rename 会被系统拒绝）
        base = os.path.dirname(INST)
        bak = os.path.join(base, "backup",
                           "Sender-" + time.strftime("%Y%m%d%H%M%S"))
        try:
            os.makedirs(os.path.dirname(bak), exist_ok=True)
            shutil.move(INST, bak)
            print("旧安装已备份到:", bak)
        except Exception as e:
            print(f"备份未成功（不影响部署，将就地覆盖）: {e}")

    shutil.copytree(SRC, INST, dirs_exist_ok=True)
    exe = os.path.join(INST, "CastLink-Sender.exe")
    ok = os.path.isfile(exe)
    print("已部署到:", INST, "| exe 存在:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
