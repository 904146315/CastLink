#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# CastLink 安装包生成脚本（把 onedir 产物打成单文件 Setup.exe）
#
# 前置：先跑 tools/build_sender_exe.sh 与 tools/build_win_receiver_exe.sh，
#       产出 dist/sender_build/CastLink-Sender 与 dist/win_receiver_build/CastLink-Receiver-Win
#
# 用法：bash tools/build_installers.sh            # 两个都打
#       bash tools/build_installers.sh sender     # 只打发送端
#       bash tools/build_installers.sh receiver   # 只打 Windows 接收端
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="tools/pyenv/Scripts/python.exe"
APP="tools/installer_app.py"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# 构建安装包时会去 move 旧的 Setup.exe；如果程序正开着会被 Windows 锁住，
# 移动/删除都会失败并连带把脚本带走，所以先结束残留进程。
taskkill //F //IM CastLink-Sender.exe >/dev/null 2>&1 || true
taskkill //F //IM CastLink-Receiver-Win.exe >/dev/null 2>&1 || true
sleep 0.5

WHAT="${1:-all}"

if [ "$WHAT" = "all" ] || [ "$WHAT" = "sender" ]; then
    SRC="dist/sender_build/CastLink-Sender"
    [ -f "$SRC/CastLink-Sender.exe" ] || die "缺少 $SRC（先跑 tools/build_sender_exe.sh）"
    say "打包发送端安装包"
    "$PY" tools/make_installer2.py \
        --src "$SRC" \
        --out dist/CastLink-Sender-Setup.exe \
        --name "CastLink 投屏发送端" \
        --subdir Sender \
        --exe CastLink-Sender.exe \
        --app "$APP"
fi

if [ "$WHAT" = "all" ] || [ "$WHAT" = "receiver" ]; then
    SRC="dist/win_receiver_build/CastLink-Receiver-Win"
    [ -f "$SRC/CastLink-Receiver-Win.exe" ] || die "缺少 $SRC（先跑 tools/build_win_receiver_exe.sh）"
    say "打包 Windows 接收端安装包"
    "$PY" tools/make_installer2.py \
        --src "$SRC" \
        --out dist/CastLink-Receiver-Win-Setup.exe \
        --name "CastLink 投屏接收端" \
        --subdir Receiver \
        --exe CastLink-Receiver-Win.exe \
        --app "$APP"
fi

say "✓ 安装包完成"
ls -la dist/*Setup.exe
