# -*- coding: utf-8 -*-
"""
用 PyInstaller 把 onedir 程序文件夹打包成一个真正的单文件安装程序（Setup.exe）。

原理：PyInstaller 的 onefile 模式会把 --add-data 的载荷（app 文件夹）随包，
运行时自动解压到临时目录（sys._MEIPASS）；我们的安装程序主体再把它们复制到
%LOCALAPPDATA%\\Programs\\CastLink\\<subdir>，并创建开始菜单/桌面快捷方式。

这不需要 NSIS / Inno / iexpress（本环境均不可用或受限），纯靠 PyInstaller。

用法：
  python tools/make_installer2.py \
      --src  dist/sender_build/CastLink-Sender \
      --out  dist/CastLink-Sender-Setup.exe \
      --name "CastLink 投屏发送端" --subdir Sender --exe CastLink-Sender.exe
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="PyInstaller onedir 产物目录")
    ap.add_argument("--out", required=True, help="输出的安装程序 exe 路径")
    ap.add_argument("--name", required=True, help="应用显示名（快捷方式名）")
    ap.add_argument("--subdir", required=True, help="安装到 Programs 下的子目录")
    ap.add_argument("--exe", required=True, help="主程序 exe 文件名")
    ap.add_argument("--app", required=True, help="installer_app.py 路径")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        print("源目录不存在:", src, file=sys.stderr)
        return 2

    venv_py = Path("tools/pyenv/Scripts/python.exe")
    pyi = Path("tools/pyenv/Scripts/pyinstaller.exe")
    if not pyi.exists():
        pyi = venv_py

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    def move_aside(path: Path, stamp: str) -> None:
        """
        把旧的产物/中间目录移到同级 .oldbuilds/ 下（**只 rename，绝不删除**）。

        注意：本环境里"删除或移动 >50 文件的目录"都会被 [safe-delete] 拦下
        （连 rename 都算删除，`scope=turn`），所以这里一律 best-effort——
        移不动就跳过。中间目录已经改成带时间戳的新目录，正常情况下压根不需要移。
        """
        if not path.exists():
            return
        ob = path.parent / ".oldbuilds"
        try:
            ob.mkdir(parents=True, exist_ok=True)
            dst = ob / f"{path.name}_{stamp}"
            shutil.move(str(path), str(dst))
            print(f"旧物已移走: {dst}")
        except Exception as e:      # noqa: BLE001 — 移不动不影响新产物生成
            print(f"（跳过清理 {path.name}：{str(e)[:80]}）")

    stamp = time.strftime("%Y%m%d%H%M%S")
    move_aside(out, stamp)

    # 写配置到临时文件，构建时作为数据加入。
    # 目录名带时间戳：每次都是全新目录，就不存在"上一轮的 work/dist 需要先清掉"
    # 这个问题（PyInstaller 的 --clean 会删 workpath，残留多了会被守卫拦死）。
    tmp = Path(tempfile.gettempdir()) / f"castlink_installer_build_{stamp}"
    tmp.mkdir(parents=True, exist_ok=True)
    move_aside(Path("build") / "CastLink-Setup", stamp)
    cfg = tmp / "installer_config.json"
    cfg.write_text(json.dumps({
        "app_name": args.name,
        "install_subdir": args.subdir,
        "exe_name": args.exe,
    }, ensure_ascii=False), encoding="utf-8")

    # app 载荷：把整个 onedir 产物打成单个 .zip 再作为数据嵌入。
    # 直接 --add-data 传目录会让 PyInstaller 的 Analysis 把 resources/app_icon.ico
    # 当成 Python 脚本来解析，报 SyntaxError。打成 zip 后只有一个二进制文件，
    # PyInstaller 不会去“分析”里面的 .ico/.dll/.pyd，安装时再解压即可。
    zip_path = tmp / "payload.zip"
    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src):
            for f in files:
                fp = Path(root) / f
                zf.write(fp, fp.relative_to(src))

    win_zip = str(zip_path)
    win_cfg = str(cfg)
    win_app = str(Path(args.app).resolve())
    win_out = str(out)
    win_work = str(tmp / "work")
    win_dist = str(tmp / "dist")
    icon_path = src / "resources" / "app_icon.ico"

    cmd = [
        str(venv_py), "-m", "PyInstaller",
        "--name", "CastLink-Setup",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--paths", str(Path(args.app).resolve().parent),
        "--add-data", f"{win_zip};payload.zip",
        "--add-data", f"{win_cfg};.",
        # 中间产物与输出都放进临时目录，别污染项目根，
        # 也避开"项目里残留旧中间目录 -> --clean 想删 -> 被守卫拦下"的连锁失败
        "--workpath", win_work,
        "--distpath", win_dist,
        "--specpath", str(tmp),
        win_app,
    ]
    if icon_path.exists():
        # 注意顺序：先把 "--icon" 插到脚本前，再把 ico 路径插到 "--icon" 之后，
        # 结果才是 "... --icon <ico> <script>"。
        # 原代码两条 insert(-1) 顺序反了，会变成 "<ico> --icon <script>"，
        # 于是 PyInstaller 把 .ico 当成入口脚本去解析（SyntaxError），
        # 而真正的 installer_app.py 被 --icon 吃掉。
        cmd.insert(-1, "--icon")
        cmd.insert(-1, str(icon_path.resolve()))

    print("构建安装程序（onefile，内嵌 %d 个文件）..." % sum(1 for _ in src.rglob("*") if _.is_file()))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("PyInstaller 失败：")
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        return 1

    # onefile 产物在 --distpath/CastLink-Setup.exe
    built = Path(win_dist) / "CastLink-Setup.exe"
    if not built.exists():
        # 兜底：PyInstaller 默认 dist 目录
        built = Path("dist") / "CastLink-Setup.exe"
    if not built.exists():
        print("找不到构建产物:", built, file=sys.stderr)
        return 1

    shutil.move(str(built), str(out))
    # 中间目录不再主动删除：本环境删 >50 文件的目录会被守卫拦下并打印一屏噪音，
    # 而这些目录都在系统临时目录里（带时间戳），交给系统清理即可。
    print(f"✓ 安装程序已生成: {out} ({out.stat().st_size/1024/1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
