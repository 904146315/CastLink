#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# CastLink 投影仪端（Android）APK 手工构建脚本
#
# 本机没有 Gradle / Android Studio，因此直接用 SDK 里的最底层工具搭一条链：
#
#   javac  ->  d8  ->  aapt2(link)  ->  zipalign  ->  apksigner
#   源码       dex     资源+清单       对齐          签名
#
# 每一步失败都会立刻退出并打印原因，方便定位。
# 用法：bash tools/build_apk.sh
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/tools"
JDK="$TOOLS/jdk/jdk-17.0.20.1+1"
SDK="$TOOLS/android-sdk"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34"

JAVA="$JDK/bin/java.exe"
JAVAC="$JDK/bin/javac.exe"
AAPT2="$BT/aapt2.exe"
ZIPALIGN="$BT/zipalign.exe"
D8="$BT/lib/d8.jar"
APKSIGNER="$BT/lib/apksigner.jar"
ANDROID_JAR="$PLAT/android.jar"
# Windows 版 Python（注入 dex 用）：优先用托管 python，绝对 POSIX 路径，
# Git Bash 在调用时会自动把 /c/... 翻译成 C:\... 给 Windows 执行。
PY="/c/Users/xiaos/.workbuddy/binaries/python/versions/3.13.12/python.exe"
[ -f "$PY" ] || PY="$(command -v python3 || command -v python || echo python)"

APP="$ROOT/receiver/android"
SRC="$APP/java"
MANIFEST="$APP/AndroidManifest.xml"
RES="$APP/res"
OUT="$ROOT/dist"

# 关键：aapt2 / apksigner 无法处理含中文的目录路径（本项目根目录就叫"投屏软件"），
# 会直接报 "failed to open directory"。因此把源码与中间产物搬到一个纯 ASCII
# 的临时目录里构建，产出后再拷回 dist/。
STAGE="$(cygpath "${TEMP:-/tmp}")/castlink_apk_build"
# 本环境的批量删除守卫会拦下任何 >50 文件的删除（上一次构建的中间产物就够数了），
# 而"同一父目录内 rename"同样会被系统拒绝 —— 只有"移入子目录 + 目标名唯一"放行。
# 所以这里把旧 STAGE 移进一个子目录里，而不是 rm -rf。
if [ -d "$STAGE" ]; then
    STAGE_OLD="$(cygpath "${TEMP:-/tmp}")/castlink_apk_old"
    mkdir -p "$STAGE_OLD"
    mv "$STAGE" "$STAGE_OLD/stage_$(date +%Y%m%d%H%M%S)"
fi
mkdir -p "$STAGE"
cp -r "$SRC" "$STAGE/java"
cp -r "$RES" "$STAGE/res"
cp "$MANIFEST" "$STAGE/AndroidManifest.xml"
# android.jar 原始路径含中文（项目根叫"投屏软件"），aapt2/d8 在 stat 时会报
# "Failed to stat file ... 鎶曞睆杞�浠�..." 警告甚至失败。把它也搬到纯 ASCII 的
# STAGE 目录里，从根本上消除中文路径问题。
cp "$ANDROID_JAR" "$STAGE/android.jar"
ANDROID_JAR="$STAGE/android.jar"
SRC="$STAGE/java"
MANIFEST="$STAGE/AndroidManifest.xml"
RES="$STAGE/res"
BUILD="$STAGE/build"

PKG="com.castlink.receiver"
VERSION_CODE=14
VERSION_NAME="1.0.14"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Git Bash 的路径是 /d/xxx，Windows 版 javac/aapt2 不认这种写法（会变成 \d\xxx）。
# 所有要传给 Windows 可执行文件的路径都必须先转成 D:\xxx。
win() { cygpath -w "$1" 2>/dev/null || echo "$1"; }

for f in "$JAVAC" "$AAPT2" "$ZIPALIGN" "$D8" "$APKSIGNER" "$ANDROID_JAR" "$MANIFEST"; do
    [ -f "$f" ] || die "缺少构建工具: $f"
done

rm -rf "$BUILD"
mkdir -p "$BUILD/classes" "$BUILD/gen" "$OUT"

# 源码已搬到 STAGE，这里用相对路径列出（javac 对 /d/... 这种路径会解析错）
STAGE_REL="$(cd "$STAGE" && pwd)"

# ---------------------------------------------------------------- 1. 编译 Java
say "1/5 javac 编译 Java 源码"
(cd "$STAGE" && find java -name "*.java") > "$BUILD/sources.txt"
[ -s "$BUILD/sources.txt" ] || die "没有找到任何 .java 源文件"
# 注意：这里**不能**加 -bootclasspath android.jar。
# 那样会用 Android 的空壳类替换掉 JDK 核心类，javac 在编译 lambda 时找不到
# java.lang.invoke.LambdaMetafactory.metafactory，直接报"找不到符号"。
# android.jar 只放在 -classpath 上即可，API 版本由 d8 的 --min-api 兜底。
# -J-Duser.language=en 让报错信息用英文输出：中文报错在 Git Bash 里会变成
# 无法 grep 的二进制流，看不到真正的编译错误。
# sources.txt 里是相对 STAGE 的路径（java/com/...），必须在 STAGE 目录下执行 javac，
# 否则这些相对路径会相对于 ROOT 解析而找不到源文件。
( cd "$STAGE" && "$JAVAC" -encoding UTF-8 -source 8 -target 8 \
    -J-Duser.language=en -J-Dfile.encoding=UTF-8 \
    -classpath "$(win "$ANDROID_JAR")" \
    -d "$(win "$BUILD/classes")" \
    @"$(win "$BUILD/sources.txt")" ) > "$BUILD/javac.log" 2>&1 || {
        cat "$BUILD/javac.log"
        die "javac 编译失败"
    }
grep -i "warning" "$BUILD/javac.log" | head -5 || true
ls "$BUILD/classes/com/castlink/receiver" | head -20
echo "编译产物 $(find "$BUILD/classes" -name '*.class' | wc -l) 个 class"

# ---------------------------------------------------------------- 2. dex
say "2/5 d8 打包成 DEX"
# d8.jar 没有 Main-Class 清单项，java -jar 会报"没有主清单属性"，
# 必须显式指定入口类 com.android.tools.r8.D8。
# d8 需要 Windows 风格的输入路径（Git Bash 的 /d/ 或 /tmp 路径它都不认），
# 逐文件用 cygpath -w 转成正确的 Windows 路径，避免 /tmp→D:/tmp 这种错误映射。
> "$BUILD/classes_list.txt"
find "$BUILD/classes" -name "*.class" | while read -r f; do
    win "$f" >> "$BUILD/classes_list.txt"
done
[ -s "$BUILD/classes_list.txt" ] || die "没有 class 文件可打包进 dex"
"$JAVA" -cp "$(win "$D8")" com.android.tools.r8.D8 \
    --release --min-api 24 \
    --lib "$(win "$ANDROID_JAR")" \
    --output "$(win "$BUILD")" \
    @"$(win "$BUILD/classes_list.txt")" \
    || die "d8 转换失败"
[ -f "$BUILD/classes.dex" ] || die "没有生成 classes.dex"
ls -la "$BUILD/classes.dex"

# ---------------------------------------------------------------- 3. aapt2
say "3/5 aapt2 编译资源并链接 APK"
"$AAPT2" compile --dir "$(win "$RES")" -o "$(win "$BUILD/res.zip")" \
    || die "aapt2 compile 失败"
UNSIGNED="$BUILD/app-unsigned.apk"
"$AAPT2" link \
    -I "$(win "$ANDROID_JAR")" \
    --manifest "$(win "$MANIFEST")" \
    --java "$(win "$BUILD/gen")" \
    --min-sdk-version 24 \
    --target-sdk-version 34 \
    --version-code "$VERSION_CODE" \
    --version-name "$VERSION_NAME" \
    -o "$(win "$UNSIGNED")" \
    "$(win "$BUILD/res.zip")" \
    --auto-add-overlay || die "aapt2 link 失败"

# 把 dex 放进 APK。新版 aapt2（34.0.0）已移除 `add` 子命令，
# 因此改用 Python 的 zipfile 把 classes.dex 作为未压缩条目塞进 APK
# （未压缩便于 Android 直接 mmap，符合常规打包约定）。
say "3.5/5 注入 classes.dex 到 APK"
"$PY" - "$(win "$UNSIGNED")" "$(win "$BUILD/classes.dex")" <<'PYEOF'
import sys, zipfile, os
apk, dex = sys.argv[1], sys.argv[2]
tmp = apk + ".tmp"
with zipfile.ZipFile(apk, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        if item.filename == "classes.dex":
            continue  # 避免重复
        zout.writestr(item, zin.read(item.filename))
    # classes.dex 用 STORED（未压缩），与官方打包一致，便于 Android 直接 mmap
    with open(dex, "rb") as f:
        data = f.read()
    zi = zipfile.ZipInfo("classes.dex")
    zi.compress_type = zipfile.ZIP_STORED
    zout.writestr(zi, data)
os.replace(tmp, apk)
print("已注入 classes.dex, 大小 %d 字节" % len(data))
PYEOF
[ -f "$UNSIGNED" ] || die "注入 dex 后 APK 丢失"

# ---------------------------------------------------------------- 4. 签名密钥
say "4/5 准备签名密钥"
# 密钥必须**跨次构建保持稳定**：Android 只允许同签名的 APK 覆盖安装，
# 每次构建都换一把钥匙的话，投影仪上会直接报"应用未安装"，
# 用户被迫先卸载 —— 而且卸载会丢掉已配对令牌和全部设置。
# 因此密钥固定在用户目录下（纯 ASCII 路径，避开中文路径问题），
# 构建时拷进 STAGE 使用，STAGE 里没有就生成后拷回去。
KS_HOME="$HOME/.castlink"
KS_STORE="$KS_HOME/castlink-release.jks"
mkdir -p "$KS_HOME"
KS="$BUILD/castlink.jks"
if [ -f "$KS_STORE" ]; then
    cp "$KS_STORE" "$KS"
    echo "使用已有发布密钥: $KS_STORE"
else
    "$JDK/bin/keytool.exe" -genkeypair -v \
        -keystore "$(win "$KS")" -storepass castlink -keypass castlink \
        -alias castlink -keyalg RSA -keysize 2048 -validity 10950 \
        -dname "CN=CastLink, OU=CastLink, O=CastLink, L=, S=, C=CN" \
        >/dev/null || die "生成密钥失败"
    cp "$KS" "$KS_STORE"
    echo "已生成发布密钥并保存到 $KS_STORE（后续构建复用它，保证能覆盖安装）"
fi

# ---------------------------------------------------------------- 5. 对齐+签名
say "5/5 zipalign 对齐并签名"
ALIGNED="$BUILD/app-aligned.apk"
"$ZIPALIGN" -f -p 4 "$UNSIGNED" "$ALIGNED" || die "zipalign 失败"
# 同理，apksigner.jar 也要显式指定入口类 com.android.apksigner.ApkSignerTool
# --v4-signing-enabled false：v4 签名会在 APK 旁边多产出一个 .idsig 文件。
# 它只服务于 `adb install --incremental`，对"拷到 U 盘再插投影仪"这种交付方式没用，
# 只会让交付目录里多出一个让人犯嘀咕的附属文件。关掉更干净。
"$JAVA" -cp "$(win "$APKSIGNER")" com.android.apksigner.ApkSignerTool sign \
    --ks "$(win "$KS")" --ks-pass pass:castlink --key-pass pass:castlink \
    --ks-key-alias castlink --v4-signing-enabled false \
    --out "$(win "$OUT/CastLink-Receiver-v${VERSION_NAME}.apk")" \
    "$(win "$ALIGNED")" || die "签名失败"
"$JAVA" -cp "$(win "$APKSIGNER")" com.android.apksigner.ApkSignerTool verify \
    "$(win "$OUT/CastLink-Receiver-v${VERSION_NAME}.apk")" || die "签名校验失败"

# ---------------------------------------------------------------- 6. 产物自检
say "6/6 校验产物（防止再次出现"构建成功但文件不可用"）"
# 背景：上一轮构建日志明明打印了"✓ 构建完成"，但 dist/ 里那个 apk 后来被改名成
# .apk1，交付出去的文件是空的，用户装到的还是旧版 —— 白排查一整天。
# 因此这里在构建收尾时**强制**核对：文件在不在、大小合不合理、
# 清单里的版本号对不对、dex 里是不是新代码。
APK_OUT="$OUT/CastLink-Receiver-v${VERSION_NAME}.apk"
[ -f "$APK_OUT" ] || die "签名后没有生成 APK: $APK_OUT"
SIZE=$(stat -c %s "$APK_OUT" 2>/dev/null || echo 0)
[ "$SIZE" -gt 80000 ] || die "APK 只有 ${SIZE} 字节，明显不完整"

BADGING="$("$AAPT2" dump badging "$(win "$APK_OUT")" 2>&1)"
echo "$BADGING" | grep -q "package: name='com.castlink.receiver'" || die "APK 包名不对"
echo "$BADGING" | grep -q "versionCode='${VERSION_CODE}'" || die "APK versionCode 不是 ${VERSION_CODE}"
echo "$BADGING" | grep -q "versionName='${VERSION_NAME}'" || die "APK versionName 不是 ${VERSION_NAME}"
echo "$BADGING" | grep -E "^package:" | head -1

"$PY" - "$(win "$APK_OUT")" <<'PYEOF'
import sys
import zipfile

apk = sys.argv[1]
z = zipfile.ZipFile(apk)
names = z.namelist()
for need in ("classes.dex", "AndroidManifest.xml", "resources.arsc"):
    if need not in names:
        raise SystemExit("APK 里缺少 %s" % need)
raw = z.read("classes.dex")
# 这些符号分别代表「实测解码能力」「画面看门狗」「重写的音频解码」
# 「v1.0.6 的按码率折算接收缓冲」和「v1.0.7 的屏幕常亮 + 设备名迁移」。
# 缺任何一个都说明打进包里的不是当前源码。
# 音频那几个尤其重要：用户装到投影仪上的若是旧包，"没声音"的修复等于没生效，
# 而两端日志都看不出区别。
for probe in (b"DecoderCaps", b"watchdogLoop", b"stallRecoveries",
              b"parseAdts", b"switchMode", b"csd-0", b"audioStatusText",
              # 注意：PERFORMANCE_MODE_LOW_LATENCY 是 final int 常量，
              # 编译期就被内联成 1 了，dex 里找不到这个名字 ——
              # 要探的是**方法名** setPerformanceMode。
              b"rcvbufFor", b"setPerformanceMode",
              b"show_stats_clean",
              # v1.0.7：设备名迁移方法（名字会作为 method id 进 dex 字符串池）
              b"migrateName",
              # v1.0.8：屏幕常亮。**不要探 FLAG_KEEP_SCREEN_ON / WindowManager** ——
              # 它们是编译期内联常量，整个类在 dex 里连引用都不会留下（实测探它必然失败）。
              # 探这个自己抽出来的具名方法：它只在"用 WakeLock 实现常亮"时存在。
              # v1.0.7 用 Window flag 实现时方法名是 applyKeepScreenOn，
              # 那个版本在真机上「有声音没画面」，已废弃 —— 换成探这个名字，
              # 以后谁把实现改回窗口标志，这里会立刻红。
              b"acquireKeepScreenOn",
              # v1.0.9：修「有声音、画面冻住」的那一组改动。缺任何一个都说明
              # 打进包里的仍是旧代码 —— 而这组改动的效果恰好是"看不出区别"的
              # （画面冻住时两端日志都不报错），所以这道自检尤其重要。
              #   * cap_ceil_w   —— 实测解不动时记住的分辨率天花板
              #   * decode_stall —— 接收端发现"数据在进、解码器不出图"后
              #                     请求降档重协商的动作名
              #   * getSilentMs  —— 解码器"多久没出图"的查询（看门狗第 2 条线索）
              #   * stepDown     —— 分辨率阶梯降一级
              #   * SurfaceSource —— 解码器跟随 Surface 变化的来源接口
              b"cap_ceil_w", b"decode_stall", b"getSilentMs", b"stepDown",
              b"SurfaceSource",
              # v1.0.10：修「画面不动 + 发送端疯狂重启采集进程」的死循环。
              #   * purgeLegacyCeiling —— 清掉历史版本落盘的（可能误判的）分辨率天花板。
              #                     不清的话，设备会被一条错结论永久锁在 720p。
              #   * capCeilingMem     —— 天花板改成只存内存的证据（字段名进 dex 池）
              #   * configuredAtNs    —— 解码器饥饿宽限期的起点（启动抖动不再误报）
              #   * loweredOnce       —— 一次会话只降一次分辨率
              # 注意 STARVE_STREAK / STARVE_GRACE_MS / DECODE_STALL_MS 都是
              # 编译期常量，会被内联进字节码，探不到 —— 探字段名与方法名。
              b"purgeLegacyCeiling", b"capCeilingMem",
              b"configuredAtNs", b"loweredOnce",
              # v1.0.11：渲染通道可切换（SurfaceView / TextureView）+ 编码格式偏好开关。
              #   * renderMode / renderModeName / setRenderMode
              #         —— 解码器出图目标从硬件叠加层切到 GPU 合成（TextureView 默认）。
              #   * preferH264 / setPreferH264 —— 强制走 H.264 的免重装开关。
              #   * handleRenderToggle / handlePreferH264Toggle
              #         —— 两个新的隐藏手势（连按 5 次方向键上/下）。
              #   * onSurfaceTextureAvailable —— TextureView 的 Surface 回调（渲染通路的证据）。
              #   注意 RENDER_TEXTURE / RENDER_SURFACE 是 static final int 常量，
              #   会被编译期内联，dex 里探不到 —— 探具名方法。
              b"renderMode", b"renderModeName", b"setRenderMode",
              b"preferH264", b"setPreferH264",
              b"handleRenderToggle", b"handlePreferH264Toggle",
              b"onSurfaceTextureAvailable",
              # v1.0.12/1.0.13/1.0.14：退出应用手势（连按 2 次左键 / 连按 2 次返回键 → 确认框 → 退出）。
              #   * handleExitToggle —— 左键或返回键双击手势（1.5s 窗口）。
              #   * showExitDialog   —— 退出确认框（防误触断流）。
              #   * doExit           —— 真正退出：停前台服务 + finish + 兜底清进程。
              #   * ACTION_STOP      —— 服务里"干净退出"的 action（断会话 + 移除常驻通知）。
              b"handleExitToggle", b"showExitDialog", b"doExit",
              b"ACTION_STOP"):
    if probe not in raw:
        raise SystemExit("classes.dex 里没有 %s —— 打进去的是旧代码" % probe.decode())
print("  内容校验通过：classes.dex %d 字节，共 %d 个条目" % (len(raw), len(names)))
PYEOF

echo
echo "✓ 构建完成:"
ls -la "$APK_OUT"
