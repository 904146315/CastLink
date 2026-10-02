#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# CastLink 接收端（Windows 版，命令行/联调用）PyInstaller 打包脚本
#
# 这是一个纯 Python + ffmpeg 的接收端，用于在没有投影仪实机时做端到端联调，
# 也作为 Windows 平台上的备用接收端。不依赖 PySide6（命令行界面）。
#
# 产物：dist/win_receiver_build/CastLink-Receiver-Win/
# 用法：bash tools/build_win_receiver_exe.sh
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIN_ROOT="$(cygpath -w "$ROOT")"
VENV="$ROOT/tools/pyenv/Scripts/python.exe"

OUT="$ROOT/dist/win_receiver_build"
ENTRY="$(cygpath -w "$ROOT/receiver-win/castlink_receiver_win.py")"
FFMPEG="$(cygpath -w "$ROOT/tools/ffmpeg.exe")"
# 与发送端同一套策略：PyInstaller 输出到全新的时间戳暂存目录，再就地覆盖到
# 交付路径（原因见 tools/overlay_dist.py）。本环境删/移 >50 文件的目录都会被拦。
STAGE="$ROOT/dist/.stage/recv_$(date +%Y%m%d%H%M%S)"
DIST="$(cygpath -w "$STAGE")"
WORK="$(cygpath -w "$STAGE/work")"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -f "$FFMPEG" ] || die "缺少 tools/ffmpeg.exe"
[ -f "$ENTRY" ] || die "找不到接收端入口: $ENTRY"

say "准备构建"
# 唯一必须做的"清理"就是结束上一次构建出来的进程：Windows 会锁住运行中进程的
# exe 与全部 DLL，占着的话连覆盖写都会失败。
taskkill //F //IM CastLink-Receiver-Win.exe >/dev/null 2>&1 || true
sleep 0.5
mkdir -p "$STAGE"
echo "暂存输出目录: $STAGE"

say "运行 PyInstaller（console + 捆绑 ffmpeg）"
# 音频输出用 pyaudiowpatch（同发送端的 WASAPI/PortAudio 绑定）。
# 缺了它接收端只能静音 —— 而"没声音"正是本轮要修的问题之一，
# 所以这里必须有，不能像以前那样只带 ffmpeg。
AUDIO_ARGS=()
if "$VENV" -c "import pyaudiowpatch" >/dev/null 2>&1; then
    echo "检测到 pyaudiowpatch，将随包携带（音频输出）"
    AUDIO_ARGS=(--collect-all pyaudiowpatch --hidden-import pyaudiowpatch)
else
    die "未安装 pyaudiowpatch —— 接收端将无法播放声音"
fi

"$VENV" -m PyInstaller \
    --name CastLink-Receiver-Win \
    --onedir \
    --console \
    --noconfirm \
    --clean \
    --contents-directory . \
    --paths "$WIN_ROOT" \
    --hidden-import castlink \
    --hidden-import castlink.protocol \
    --hidden-import castlink.control \
    --hidden-import castlink.discovery \
    --hidden-import castlink.identity \
    --hidden-import castlink.config \
    --hidden-import castlink.ffmpeg \
    --hidden-import castlink.padev \
    --hidden-import castlink.winvol \
    "${AUDIO_ARGS[@]}" \
    --add-binary "$FFMPEG;." \
    --distpath "$DIST" \
    --workpath "$WORK" \
    "$ENTRY" || die "PyInstaller 失败"

APP="$OUT/CastLink-Receiver-Win"
say "覆盖到交付路径"
"$VENV" "$ROOT/tools/overlay_dist.py" "$STAGE/CastLink-Receiver-Win" "$APP" || die "覆盖失败"
[ -f "$APP/CastLink-Receiver-Win.exe" ] || die "没有生成 CastLink-Receiver-Win.exe"
[ -f "$APP/ffmpeg.exe" ] || die "ffmpeg.exe 没有随包"

cat > "$APP/VERSION.txt" <<EOF
CastLink 接收端（Windows）
版本: 1.0.10
构建: $(date +%Y-%m-%d)
EOF

say "✓ Windows 接收端构建完成: $APP"
ls -la "$APP/CastLink-Receiver-Win.exe"
