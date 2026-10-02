#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 把接收端 APK 安装到投影仪（Android / Android TV）
#
# 覆盖两种连接方式：
#   1) 无线 ADB：投影仪已开启「网络调试 / ADB over WiFi」
#        bash tools/install_apk_adb.sh --wifi 192.168.1.50
#   2) USB ADB：投影仪用数据线连到本机，且已开启「USB 调试」
#        bash tools/install_apk_adb.sh --usb
#
# 不带参数时：先列出已连接设备，给出下一步提示。
#
# 为什么优先 ADB：投影仪自带的文件管理器经常不识别/不提供 APK 安装入口，
# 而 ADB 是系统级安装，能绕开这些限制，并直接返回真正的失败原因。
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADB="$ROOT/tools/android-sdk/platform-tools/adb.exe"
APK="$ROOT/dist/CastLink-Receiver-v1.0.0.apk"

win() { cygpath -w "$1" 2>/dev/null || echo "$1"; }

[ -x "$ADB" ] || { echo "找不到 adb: $ADB" >&2; exit 1; }
[ -f "$APK" ] || { echo "找不到 APK: $APK" >&2; exit 1; }

MODE="${1:-}"
case "$MODE" in
  --wifi)
    IP="${2:-}"
    [ -n "$IP" ] || { echo "用法: bash tools/install_apk_adb.sh --wifi <投影仪IP>" >&2; exit 2; }
    echo "==> 连接 $IP:5555 ..."
    "$ADB" connect "$IP:5555" || true
    TARGET="$IP:5555"
    ;;
  --usb)
    TARGET=""
    ;;
  *)
    echo "==> 当前已连接设备："
    "$ADB" devices -l || true
    echo
    echo "如果列表里没有投影仪："
    echo "  · 先在投影仪上开启开发者选项："
    echo "      设置 → 关于本机 → 连续点「版本号」7 次 → 返回 → 开发者选项"
    echo "      → 打开「USB 调试」；想无线装的话再打开「网络调试 / ADB over WiFi」"
    echo "  · USB 方式：用数据线把投影仪接到本机，再执行  bash tools/install_apk_adb.sh --usb"
    echo "  · 无线方式：先查投影仪的 IP，再执行    bash tools/install_apk_adb.sh --wifi <IP>"
    exit 0
    ;;
esac

echo "==> 安装 APK：$(basename "$APK")"
if [ -n "$TARGET" ]; then
    "$ADB" -s "$TARGET" install -r "$(win "$APK")"
else
    "$ADB" install -r "$(win "$APK")"
fi

echo
echo "==> 校验是否装上："
if [ -n "$TARGET" ]; then
    "$ADB" -s "$TARGET" shell pm list packages | grep castlink || echo "   没查到 com.castlink.receiver"
else
    "$ADB" shell pm list packages | grep castlink || echo "   没查到 com.castlink.receiver"
fi
