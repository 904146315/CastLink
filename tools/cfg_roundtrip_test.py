# -*- coding: utf-8 -*-
"""
配置往返回归测试：**界面装载配置时不得把配置改掉**。

守的是一个真实踩过的、极隐蔽的 bug
----------------------------------
`MainWindow._load_cfg_to_ui()` 逐个往控件里刷配置。所有控件都连着 `_cfg_changed`，
而 `_cfg_changed` 会把**全部**控件的当前值写回 cfg 并 `cfg.save()` 落盘。
于是在刷到第 3 个控件时，排在其后的控件还是出厂默认值，就被当成"用户选择"
写进了配置 —— 界面上看起来只是"某个开关每次启动都自己关掉了"，
实际上配置文件已经被改坏并保存。

受影响最致命的是 `enable_audio`：用户勾了"投屏系统声音"，下次启动又被重置回关，
投屏自然没声音，而且这跟音频采集链路能不能用完全无关，极难排查。

本测试：写入一组非默认值 → 起一次 MainWindow（offscreen）→ 回读配置文件，
断言这些值原封不动；同时断言控件确实读到了这些值。

用法：tools/pyenv/Scripts/python.exe tools/cfg_roundtrip_test.py
退出码 0 通过。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sender"))

# 这些都刻意取成"非默认值"，其中后面几项排在控件刷新顺序的末尾 ——
# 它们正是被启动过程覆盖掉的受害者。
NON_DEFAULT = {
    "enable_audio": True,      # 数据类默认 True，但界面复选框默认未勾选
    "mute_local": False,       # v1.0.6 默认 True，这里特意反着来
    "canvas_4k": True,         # 默认 False
    "capture_mouse": True,     # 默认 True，界面复选框默认未勾选
    "require_pin": True,       # 默认 False
    "pin": "246813",           # 默认 000000
    "quality": "high",
    "fps": 30,
    "transport": "udp",
}


def config_path() -> str:
    from castlink.config import SenderConfig
    return SenderConfig.path()


def main() -> int:
    """
    **全程不碰用户的真实配置**。

    早期版本是"备份真实配置 → 写测试值 → 还原"，看起来没问题，但一旦测试进程
    被超时杀掉（本测试就踩过），`finally` 里的还原根本不会执行，用户真实的
    `remembered_peers`（配对令牌）就被测试值覆盖丢了。
    所以现在直接把 `SenderConfig.path()` 指向临时目录，从源头上不可能污染。
    """
    import castlink.config as cfgmod
    from castlink.config import SenderConfig

    tmpdir = tempfile.mkdtemp(prefix="castlink_cfgtest_")
    fake = os.path.join(tmpdir, "sender_config.json")
    real = SenderConfig.path()
    print("真实配置（不会被改动）:", real)
    print("测试用配置:", fake)

    SenderConfig.path = staticmethod(lambda: fake)

    try:
        cfg = SenderConfig()
        for k, v in NON_DEFAULT.items():
            setattr(cfg, k, v)
        cfg.save()
        print("写入非默认配置:", json.dumps(NON_DEFAULT, ensure_ascii=False))
        return run_checks(fake)
    finally:
        SenderConfig.path = staticmethod(lambda: real)
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_checks(p: str) -> int:
    try:
        from PySide6.QtWidgets import QApplication
        from castlink_sender.ui_main import MainWindow
        from castlink_sender.util import find_ffmpeg

        if os.environ.get("CASTLINK_CFG_TEST_BREAK") == "1":
            # 人为让信号屏蔽失效，用来证明本测试真的抓得到这个 bug
            # （设了这个环境变量时**应当**报失败，退出码 1）。
            _orig = MainWindow._cfg_changed

            def _broken(self, *a, **kw):
                self._loading_cfg = False
                return _orig(self, *a, **kw)

            MainWindow._cfg_changed = _broken
            print("!! 已人为破坏信号屏蔽（用于自检本测试的有效性）")

        app = QApplication.instance() or QApplication([])
        win = MainWindow(find_ffmpeg(), app)

        # 1) 控件确实读到了配置
        ui_ok = True
        pairs = [
            ("chk_audio", "enable_audio"), ("chk_mute", "mute_local"),
            ("chk_canvas", "canvas_4k"),
            ("chk_mouse", "capture_mouse"), ("chk_pin", "require_pin"),
        ]
        for widget, key in pairs:
            got = getattr(win, widget).isChecked()
            want = NON_DEFAULT[key]
            mark = "✓" if got == want else "✗"
            if got != want:
                ui_ok = False
            print(f"  {mark} 控件 {widget:10s} = {got}（期望 {want}）")
        pin_txt = win.ed_pin.text()
        print(f"  {'✓' if pin_txt == NON_DEFAULT['pin'] else '✗'} 控件 ed_pin       = {pin_txt}"
              f"（期望 {NON_DEFAULT['pin']}）")
        ui_ok = ui_ok and pin_txt == NON_DEFAULT["pin"]

        # 注意：**不要** win.close()。offscreen 下关闭窗口会走 closeEvent 里的
        # 确认对话框，没有事件循环去应答它，进程会直接挂死到超时。
        # 我们只关心"构造窗口时有没有改写配置"，构造完读一遍就够。
        app.processEvents()

        code = 1
        # 2) 磁盘上的配置没被改写
        with open(p, encoding="utf-8") as f:
            after = json.load(f)
        disk_ok = True
        for k, v in NON_DEFAULT.items():
            got = after.get(k)
            mark = "✓" if got == v else "✗"
            if got != v:
                disk_ok = False
            print(f"  {mark} 磁盘 {k:16s} = {got!r}（期望 {v!r}）")

        print()
        if ui_ok and disk_ok:
            print("✅ 通过：配置能被界面正确装载，且不会被启动过程改写")
            return 0
        print("❌ 失败：配置在启动过程中被改写了"
              "（检查 _load_cfg_to_ui 有没有屏蔽 _cfg_changed）")
        return 1
    except Exception as e:      # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"❌ 测试异常: {e}")
        return 2


if __name__ == "__main__":
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Qt 起来之后正常 return 可能不退出（还有计时器等），直接硬退，保证退出码可靠
    os._exit(_code)
