# -*- coding: utf-8 -*-
"""
CastLink 安装包生成器（Windows，无需 NSIS/Inno，使用系统自带的 iexpress）。

把 PyInstaller 产出的 onedir 文件夹打包成一个自解压安装程序（.exe）：
  - 运行后把程序释放到 %LOCALAPPDATA%\\Programs\\CastLink\\<dir>
  - 在开始菜单与桌面创建快捷方式
  - 无需管理员权限（装到当前用户目录）

用法（在 Git Bash 中）：
  python tools/make_installer.py \
      --src  dist/sender_build/CastLink-Sender \
      --out  dist/CastLink-Sender-Setup.exe \
      --name "CastLink 投屏发送端" \
      --dir  Sender \
      --exe  CastLink-Sender.exe

参数：
  --src    PyInstaller onedir 产物目录（含主程序 exe）
  --out    最终安装包输出路径（可含中文）
  --name   快捷方式与安装程序显示名
  --dir    安装到 Programs 下的子目录名（英文，无空格最佳）
  --exe    主程序 exe 文件名
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


INSTALL_BAT = r"""@echo off
set "DEST=%LOCALAPPDATA%\Programs\CastLink\{dir}"
if not exist "%DEST%" mkdir "%DEST%"
xcopy /E /I /Y /Q "." "%DEST%"
if exist "%DEST%\make_shortcut.vbs" (
    cscript //nologo "%DEST%\make_shortcut.vbs"
)
echo.
echo CastLink installed to: %DEST%
echo You can launch it from Start Menu -^> CastLink.
echo.
pause
"""

# 注意：VBS 以 GBK 编码保存，里面的中文快捷方式名才能被 cscript 正确读取。
# 安装路径用 %LOCALAPPDATA% 环境变量算出，不通过命令行传参（避免控制台编码乱码）。
SHORTCUT_VBS = r"""Set WshShell = CreateObject("WScript.Shell")
name = "{name}"
dest = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\CastLink\{dir}"
exe = dest & "\{exe}"
Set fso = CreateObject("Scripting.FileSystemObject")

sm = WshShell.SpecialFolders("StartMenu")
Set smDir = fso.GetFolder(sm)
If Not fso.FolderExists(sm & "\CastLink") Then smDir.SubFolders.Add("CastLink")
Set sc = WshShell.CreateShortcut(sm & "\CastLink\" & name & ".lnk")
sc.TargetPath = exe
sc.WorkingDirectory = dest
sc.Description = name
sc.IconLocation = exe & ",0"
sc.Save

desktop = WshShell.SpecialFolders("Desktop")
Set dc = WshShell.CreateShortcut(desktop & "\" & name & ".lnk")
dc.TargetPath = exe
dc.WorkingDirectory = dest
dc.Description = name
dc.IconLocation = exe & ",0"
dc.Save
"""


def build_sed(stage_dir: Path, out_exe: Path, name: str, exe_name: str) -> str:
    stage_win = str(stage_dir)
    # 枚举所有文件（相对路径，反斜杠）
    files: list[str] = []
    for root, _dirs, fnames in os.walk(stage_dir):
        for fn in fnames:
            full = Path(root) / fn
            rel = full.relative_to(stage_dir).as_posix().replace("/", "\\")
            files.append(rel)

    src_lines = ["[SourceFiles]", f"SourceFiles0={stage_win}", "[SourceFiles0]"]
    str_lines = [
        "InstallPrompt=",
        "DisplayLicense=",
        "FinishMessage=Installation complete.",
        f"TargetName={str(out_exe)}",
        "FriendlyName=CastLink Setup",
        "AppLaunched=install.bat",
        "PostInstallCmd=<None>",
    ]
    for i, rel in enumerate(files):
        src_lines.append(f"%FILE{i}%={rel}")
        str_lines.append(f"FILE{i}={rel}")

    sed = "[Version]\n"
    sed += "Class=IEXPRESS\n"
    sed += "SEDVersion=3\n"
    sed += "[Options]\n"
    sed += "PackagePurpose=InstallApp\n"
    sed += "ShowInstallProgramWindow=0\n"
    sed += "HideExtractAnimation=1\n"
    sed += "UseLongFileName=1\n"
    sed += "InsideCompressed=0\n"
    sed += "CAB_FixedSize=0\n"
    sed += "CAB_ResvCodeSigning=0\n"
    sed += "RebootMode=I\n"
    sed += "InstallPrompt=%InstallPrompt%\n"
    sed += "DisplayLicense=%DisplayLicense%\n"
    sed += "FinishMessage=%FinishMessage%\n"
    sed += "TargetName=%TargetName%\n"
    sed += "FriendlyName=%FriendlyName%\n"
    sed += "AppLaunched=%AppLaunched%\n"
    sed += "PostInstallCmd=%PostInstallCmd%\n"
    sed += "AdminTerritory=0\n"
    sed += "UserTerritory=1\n"
    sed += "SourceFiles=SourceFiles\n"
    sed += "\n".join(src_lines) + "\n"
    sed += "[Strings]\n"
    sed += "\n".join(str_lines) + "\n"
    return sed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--exe", required=True)
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        print(f"源目录不存在: {src}", file=sys.stderr)
        return 2

    # 把源目录复制到 ASCII 临时目录，避免中文/特殊路径让 iexpress 出错
    tmp = Path(tempfile.gettempdir()) / "castlink_installer_stage"
    stage = tmp / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    # 复制目录内容（保留结构）
    for item in src.iterdir():
        if item.is_dir():
            shutil.copytree(item, stage / item.name)
        else:
            shutil.copy2(item, stage / item.name)

    # 写入安装脚本
    (stage / "install.bat").write_text(
        INSTALL_BAT.format(dir=args.dir, name=args.name, exe=args.exe),
        encoding="utf-8", newline="\r\n")
    # VBS 内含中文快捷方式名，必须以 GBK 编码保存，cscript 才能正确解析
    (stage / "make_shortcut.vbs").write_text(
        SHORTCUT_VBS.format(name=args.name, dir=args.dir, exe=args.exe),
        encoding="gbk", newline="\r\n")

    # iexpress 输出先放 ASCII 临时目录，再拷到最终路径
    out_tmp = tmp / "setup_tmp.exe"
    sed_text = build_sed(stage, out_tmp, args.name, args.exe)
    sed_path = tmp / "install.sed"
    sed_path.write_text(sed_text, encoding="utf-8")

    iexpress = r"C:\Windows\System32\iexpress.exe"
    print(f"运行 iexpress，文件数约 {len(list(stage.rglob('*')))} ...")
    r = subprocess.run([iexpress, "/N", "/Q", str(sed_path)],
                       capture_output=True, text=True)
    if not out_tmp.exists():
        print("iexpress 失败：", r.stdout, r.stderr, file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(out_tmp), str(out))
    size = out.stat().st_size
    print(f"✓ 安装包已生成: {out} ({size/1024/1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
