# CastLink 无线投屏

局域网内的无线投屏工具：把 Windows 电脑（发送端）的屏幕与系统声音，
投到坚果投影仪或任意 Android 设备（接收端）上。

## 组件

- `sender/` —— Windows 发送端（Python + ffmpeg/Qt），抓取屏幕与系统声音并推流。
- `receiver/` —— Android 接收端（Java），在投影仪 / 手机上解码并渲染画面与声音。
- `tools/` —— 打包脚本：APK 用 `build_apk.sh`，Windows 安装包用
  `build_sender_exe.sh` / `build_win_receiver_exe.sh`，另有若干诊断与回归测试脚本。

## 构建

### 接收端 APK
需要 Android SDK（build-tools 34、platform android-34）与 JDK 17，分别放到
`tools/android-sdk` 与 `tools/jdk`，然后：

```bash
bash tools/build_apk.sh
```

产物在 `dist/CastLink-Receiver-vX.Y.Z.apk`。

### 发送端（Windows 安装包）

```bash
bash tools/build_sender_exe.sh
```

> 第三方工具链（JDK / Android SDK / ffmpeg）体积较大，未纳入仓库。
> 请按 `tools/install-sdk.bat` 及脚本内的路径约定自行准备。

## 许可证

[MIT](LICENSE)
