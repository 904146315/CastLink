# -*- coding: utf-8 -*-
"""安装包（Setup.exe）结构与安装逻辑回归测试 —— 直接解剖 PyInstaller onefile 产物。

钉住两个曾把安装包打废的 bug（发送端/接收端共用同一套脚本，两边都中招）：
  A) make_installer2.py 里 `--add-data "<zip>;payload.zip"` 的第 2 段是**目标目录**，
     于是负载被塞进子目录，运行时变成 `< _MEIPASS>\\payload.zip\\payload.zip`。
     安装器 `os.path.isfile(< _MEIPASS>/payload.zip)` 因此为假，**根本不解压负载**，
     直接掉进开发兜底分支找 app → 报"app 目录不存在"。
     （正确写法是目标设为 "." ，让负载落在包根。）
  B) payload.zip 内条目必须以 "app/" 为前缀，否则解压目录下没有 app/。

测试流程（对每个 Setup.exe）：
  1) 用 PyInstaller 的 CArchiveReader 打开 exe，断言数据条目里恰有一个 `payload.zip`
     且名字就是 `payload.zip`（不是 `payload.zip\\payload.zip`）；
  2) 按运行时规则重建一个模拟 _MEIPASS（解出 payload.zip 与 installer_config.json）；
  3) 断言 payload.zip 内所有条目都在 app/ 之下、且含 config 指定的主程序；
  4) 打桩 MessageBox/快捷方式，以该模拟 _MEIPASS 调 installer_app.main()，
     断言程序文件被复制到 <LOCALAPPDATA>\\Programs\\CastLink\\<subdir>。

用法：
  python tools/installer_payload_test.py                    # 自动测 dist/ 下所有 *Setup.exe
  python tools/installer_payload_test.py <a.exe> <b.exe>    # 指定若干 Setup.exe
"""
from __future__ import annotations

import glob
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print("  ✓ %s" % msg)
    else:
        print("  ✗ %s" % msg)
        FAILS.append(msg)


def _norm(name: str) -> str:
    return name.replace("\\", "/")


def _extract_reader_bytes(reader, key: str) -> bytes:
    try:
        data = reader.extract(key)
    except TypeError:
        data = reader.extract(key, raw=False)
    if isinstance(data, tuple):
        data = data[0]
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("提取 %r 返回了非字节对象: %r" % (key, type(data)))
    return bytes(data)


def test_one_setup(exe: str) -> None:
    from PyInstaller.archive.readers import CArchiveReader

    print("=" * 72)
    print("Setup:", exe)
    reader = CArchiveReader(exe)
    toc = reader.toc
    names = list(toc.keys())
    norm = {_norm(k): k for k in names}

    # [1] payload.zip 必须是包根下的**文件**（而非 payload.zip\payload.zip）
    payload_hits = [k for k in names if _norm(k).endswith("payload.zip")]
    check(len(payload_hits) == 1,
          "数据条目里恰有一个 payload.zip（实得 %d 个: %r）" % (len(payload_hits), payload_hits))
    if not payload_hits:
        return
    payload_key = payload_hits[0]
    check(_norm(payload_key) == "payload.zip",
          "payload.zip 位于包根（实际条目名 %r，期望 'payload.zip'）" % payload_key)
    check("installer_config.json" in norm,
          "存在 installer_config.json")

    # [2] 重建模拟 _MEIPASS
    meipass = tempfile.mkdtemp(prefix="castlink_fake_meipass_")
    try:
        with open(os.path.join(meipass, "payload.zip"), "wb") as f:
            f.write(_extract_reader_bytes(reader, payload_key))
        cfg_key = norm.get("installer_config.json")
        with open(os.path.join(meipass, "installer_config.json"), "wb") as f:
            f.write(_extract_reader_bytes(reader, cfg_key))
        check(os.path.isfile(os.path.join(meipass, "payload.zip")),
              "模拟 _MEIPASS 下 payload.zip 是文件（安装器的 isfile 判断成立）")

        with open(os.path.join(meipass, "installer_config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)

        # [3] payload.zip 内结构
        with zipfile.ZipFile(os.path.join(meipass, "payload.zip")) as zf:
            zn = [n for n in zf.namelist() if not n.endswith("/")]
        app_pref = [n for n in zn if n.startswith("app/")]
        exe_name = cfg.get("exe_name", "app.exe")
        check(len(zn) > 0 and len(app_pref) == len(zn),
              "payload.zip 全部条目都在 app/ 之下（%d/%d）" % (len(app_pref), len(zn)))
        check(("app/" + exe_name) in zn, "payload.zip 内含主程序 app/%s" % exe_name)

        # [4] 端到端安装逻辑
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import installer_app  # noqa: E402

        msgs: list[str] = []
        installer_app._mb = lambda title, text, flags=0: (msgs.append(text), 7)[1]
        installer_app.make_shortcuts = lambda *a, **k: None

        fake_local = tempfile.mkdtemp(prefix="castlink_test_localappdata_")
        old_frozen = getattr(sys, "frozen", None)
        old_meipass = getattr(sys, "_MEIPASS", None)
        old_local = os.environ.get("LOCALAPPDATA")
        try:
            sys.frozen = True
            sys._MEIPASS = meipass
            os.environ["LOCALAPPDATA"] = fake_local
            rc = installer_app.main()
            subdir = cfg.get("install_subdir", "App")
            dest = os.path.join(fake_local, "Programs", "CastLink", subdir, exe_name)
            check(rc == 0, "installer main() 返回 0（实际 %s）" % rc)
            check(os.path.isfile(dest) and os.path.getsize(dest) > 0,
                  "主程序已安装: Programs/CastLink/%s/%s" % (subdir, exe_name))
            check(any("成功安装" in m for m in msgs),
                  "出现『已成功安装』提示（而非报错框）")
        finally:
            if old_frozen is None:
                sys.__dict__.pop("frozen", None)
            else:
                sys.frozen = old_frozen
            if old_meipass is None:
                sys.__dict__.pop("_MEIPASS", None)
            else:
                sys._MEIPASS = old_meipass
            if old_local is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old_local
            shutil.rmtree(fake_local, ignore_errors=True)
    finally:
        shutil.rmtree(meipass, ignore_errors=True)


def main() -> int:
    exes = sys.argv[1:]
    if not exes:
        exes = sorted(glob.glob(os.path.join(ROOT, "dist", "*Setup.exe")))
    if not exes:
        print("未找到 Setup.exe；请先跑 tools/build_installers.sh")
        return 2
    for exe in exes:
        if not os.path.isfile(exe):
            print("跳过（不存在）:", exe)
            continue
        test_one_setup(exe)
    print()
    if FAILS:
        print("✗ installer_payload_test 失败 %d 项" % len(FAILS))
        return 1
    print("✓ installer_payload_test 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
