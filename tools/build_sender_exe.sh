#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# CastLink 发送端（电脑端，Windows）PyInstaller 打包脚本
#
# 产物：dist/sender_build/CastLink-Sender/  —— 一个可直接运行的文件夹，
# 其中的 CastLink-Sender.exe 即电脑端主程序，ffmpeg.exe 与 resources/ 已随包。
# 之后由 NSIS 把该文件夹打成安装包（见 tools/installer_sender.nsi）。
#
# 用法：bash tools/build_sender_exe.sh
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 所有要传给 Windows 版 PyInstaller 的路径都必须先转成纯 Windows 路径，
# 否则 Git Bash 的 /d/... 会被二次翻译成 D:\d\... 导致找不到文件。
WIN_ROOT="$(cygpath -w "$ROOT")"
SENDER_DIR="$(cygpath -w "$ROOT/sender")"
VENV="$ROOT/tools/pyenv/Scripts/python.exe"
PYI="$ROOT/tools/pyenv/Scripts/pyinstaller.exe"
[ -x "$PYI" ] || PYI="$VENV -m PyInstaller"

OUT="$ROOT/dist/sender_build"
ENTRY="$(cygpath -w "$ROOT/sender/castlink_sender/main.py")"
FFMPEG="$(cygpath -w "$ROOT/tools/ffmpeg.exe")"
RES="$(cygpath -w "$ROOT/sender/resources")"
ICON="$(cygpath -w "$ROOT/sender/resources/app_icon.ico")"
# PyInstaller 输出到一个**全新的时间戳暂存目录**，之后由 overlay_dist.py 就地
# 覆盖到交付路径 OUT。原因见 tools/overlay_dist.py 开头的说明：本环境删除或
# 移动 >50 文件的目录都会被 [safe-delete] 拦下（连 rename 都算删除），
# 所以构建脚本必须做到"只新建、不清理"。暂存目录会累积，定期清一次即可。
STAGE="$ROOT/dist/.stage/sender_$(date +%Y%m%d%H%M%S)"
DIST="$(cygpath -w "$STAGE")"
WORK="$(cygpath -w "$STAGE/work")"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -f "$FFMPEG" ] || die "缺少 tools/ffmpeg.exe"
[ -f "$ENTRY" ] || die "找不到发送端入口: $ENTRY"

say "准备构建"
# 唯一必须做的"清理"就是结束上一次构建出来的进程：
# Windows 会锁住运行中进程的 exe 与全部 DLL，占着的话连覆盖写都会失败。
taskkill //F //IM CastLink-Sender.exe >/dev/null 2>&1 || true
sleep 0.5
mkdir -p "$STAGE"
echo "暂存输出目录: $STAGE"

say "运行 PyInstaller（onedir + 捆绑 ffmpeg 与资源）"
# 音频回环依赖 pyaudiowpatch（WASAPI loopback，带一个 PortAudio 二进制）。
# 它可能没装（此时程序仍能跑，只是没有系统声音），所以先探测再决定要不要收进包。
AUDIO_ARGS=()
if "$VENV" -c "import pyaudiowpatch" >/dev/null 2>&1; then
    echo "检测到 pyaudiowpatch，将随包携带（系统声音回环）"
    AUDIO_ARGS=(--collect-all pyaudiowpatch --hidden-import pyaudiowpatch)
else
    echo "未安装 pyaudiowpatch —— 安装包将不含系统声音回环能力"
fi

"$VENV" -m PyInstaller \
    --name CastLink-Sender \
    --onedir \
    --windowed \
    --noconfirm \
    --clean \
    --contents-directory . \
    --paths "$WIN_ROOT" \
    --paths "$SENDER_DIR" \
    --collect-submodules castlink_sender \
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
    --add-data "$RES;resources" \
    --icon "$ICON" \
    --distpath "$DIST" \
    --workpath "$WORK" \
    "$ENTRY" || die "PyInstaller 失败"

APP="$OUT/CastLink-Sender"
say "覆盖到交付路径"
"$VENV" "$ROOT/tools/overlay_dist.py" "$STAGE/CastLink-Sender" "$APP" || die "覆盖失败"
[ -f "$APP/CastLink-Sender.exe" ] || die "没有生成 CastLink-Sender.exe"
[ -f "$APP/ffmpeg.exe" ] || die "ffmpeg.exe 没有随包"
[ -d "$APP/resources" ] || die "resources 没有随包"

cat > "$APP/VERSION.txt" <<EOF
CastLink 投屏发送端
版本: 1.0.10
构建: $(date +%Y-%m-%d)
EOF

say "✓ 发送端构建完成: $APP"
ls -la "$APP/CastLink-Sender.exe"
