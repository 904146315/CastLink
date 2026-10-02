# -*- coding: utf-8 -*-
"""
发送端界面冒烟测试（offscreen，不弹窗、不改用户配置）。

只验两件事：
  1. 主窗口能正常构建（改了 ui_main.py 之后最容易在这里炸）
  2. 「音频」那一格能把"本机音源 + 投影仪实际状态"正确地显示出来 ——
     这一格是排查"没声音"时电脑端唯一的证据来源，格式错了等于白做。

刻意**不** close() 窗口：offscreen 下关闭会弹模态确认框并挂死；
最后用 os._exit() 硬退。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("CASTLINK_NO_TRAY", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "sender")):
    if p not in sys.path:
        sys.path.insert(0, p)

from PySide6.QtWidgets import QApplication          # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])
    from castlink_sender.ui_main import MainWindow
    from castlink_sender.util import find_ffmpeg

    win = MainWindow(find_ffmpeg(), app)
    print("✅ 主窗口构建成功")

    cases = [
        ({"audio": "扬声器 (Realtek(R) Audio) [Loopback]（WASAPI 系统声音回环）",
          "peer": {"audioStatus": "OK", "audioRecv": 1234, "audioDec": 1234}},
         "回环 → OK"),
        ({"audio": "扬声器 (Realtek(R) Audio) [Loopback]（WASAPI 系统声音回环）",
          "peer": {"audioStatus": "无数据", "audioRecv": 0, "audioDec": 0}},
         "回环 → 无数据"),
        ({"audio": "未找到可用的系统声音采集方式", "peer": {}}, "无音源 → —"),
        ({"audio": "已在设置中关闭", "peer": {}}, "已关 → —"),
        ({"audio": "扬声器回环",
          "peer": {"audioStatus": "投影仪系统音量为 0"}}, "回环 → 投影仪系统音量为 0"),
        ({"audio": "扬声器回环",
          "peer": {"audioStatus": "换喂法:ADTS 自同步"}}, "回环 → 换喂法:ADTS 自同步"),
    ]
    ok = True
    for data, expect in cases:
        win._on_stats(data)
        got = win.stat_labels["audio"].text()
        flag = "✔" if got == expect else "✘"
        if got != expect:
            ok = False
        print(f"  {flag} 输入 {data['peer'].get('audioStatus', '—')!r:24} -> 显示 {got!r}"
              f"（期望 {expect!r}）")

    print()
    if ok:
        print("✅ 通过：界面能正确区分「本机采到了」和「投影仪放出来了」")
    else:
        print("❌ 有不符合预期的显示结果")
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
