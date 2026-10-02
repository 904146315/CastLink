# -*- coding: utf-8 -*-
"""
CastLink 安装程序主体（被打成单文件 EXE）。

运行时会把内嵌的 app/ 文件夹解压（PyInstaller 自动完成）后复制到
%LOCALAPPDATA%\\Programs\\CastLink\\<subdir>，并在开始菜单与桌面创建快捷方式，
最后询问是否立即启动。

配置来自同包的 installer_config.json（由构建脚本生成）。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile


def _mb(title: str, text: str, flags: int = 0) -> int:
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags)


def load_config() -> dict:
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(base, "installer_config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def payload_dir() -> str:
    """返回解压后的程序目录。

    构建脚本（make_installer2.py）把整个 onedir 产物打成 payload.zip 嵌入安装包，
    这里在首次调用时解压到临时目录并返回其路径，避免 PyInstaller 把 .ico/.dll 当脚本解析。
    """
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    zp = os.path.join(base, "payload.zip")
    if os.path.isfile(zp):
        import zipfile
        extract = os.path.join(tempfile.gettempdir(), "castlink_payload_%d" % os.getpid())
        if not os.path.isdir(extract) or not os.listdir(extract):
            os.makedirs(extract, exist_ok=True)
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(extract)
        return extract
    # 兜底：开发期直接用 app/ 目录
    return os.path.join(base, "app")


def make_shortcuts(exe_path: str, name: str) -> None:
    vbs = (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        'name = "{name}"\n'
        'exe = "{exe}"\n'
        'dest = Left(exe, InStrRev(exe, "\\") - 1)\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\n'
        'sm = WshShell.SpecialFolders("StartMenu")\n'
        'Set smDir = fso.GetFolder(sm)\n'
        'If Not fso.FolderExists(sm & "\\CastLink") Then smDir.SubFolders.Add("CastLink")\n'
        'Set sc = WshShell.CreateShortcut(sm & "\\CastLink\\" & name & ".lnk")\n'
        'sc.TargetPath = exe\n'
        'sc.WorkingDirectory = dest\n'
        'sc.Description = name\n'
        'sc.IconLocation = exe & ",0"\n'
        'sc.Save\n'
        'desktop = WshShell.SpecialFolders("Desktop")\n'
        'Set dc = WshShell.CreateShortcut(desktop & "\\" & name & ".lnk")\n'
        'dc.TargetPath = exe\n'
        'dc.WorkingDirectory = dest\n'
        'dc.Description = name\n'
        'dc.IconLocation = exe & ",0"\n'
        'dc.Save\n'
    ).format(name=name, exe=exe_path.replace("/", "\\"))
    tf = os.path.join(tempfile.gettempdir(), "castlink_make_shortcut.vbs")
    with open(tf, "w", encoding="gbk") as f:
        f.write(vbs)
    try:
        subprocess.run(["cscript", "//nologo", tf], check=False, shell=True)
    finally:
        try:
            os.remove(tf)
        except OSError:
            pass


def main() -> int:
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        _mb("CastLink 安装", "读取安装配置失败：\n%s" % e, 0x10)
        return 1

    name = cfg.get("app_name", "CastLink")
    subdir = cfg.get("install_subdir", "App")
    exe_name = cfg.get("exe_name", "app.exe")

    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    dest = os.path.join(local, "Programs", "CastLink", subdir)

    payload = os.path.join(payload_dir(), "app")
    if not os.path.isdir(payload):
        _mb("CastLink 安装", "安装包内的程序文件缺失（app 目录不存在）。\n请重新下载完整的安装程序。", 0x10)
        return 1

    try:
        os.makedirs(dest, exist_ok=True)
        for item in os.listdir(payload):
            s = os.path.join(payload, item)
            d = os.path.join(dest, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    except Exception as e:  # noqa: BLE001
        _mb("CastLink 安装", "复制文件失败：\n%s" % e, 0x10)
        return 1

    exe = os.path.join(dest, exe_name)
    make_shortcuts(exe, name)

    _mb("CastLink 安装", "已成功安装 %s 到：\n%s\n\n可从「开始菜单 → CastLink」或桌面快捷方式启动。" % (name, dest), 0x40)
    # 询问是否现在启动
    r = _mb("CastLink 安装", "是否现在启动 %s？" % name, 0x24)  # MB_YESNO
    if r == 6:  # IDYES
        try:
            subprocess.Popen([exe], cwd=dest)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
