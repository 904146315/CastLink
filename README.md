# CastLink 无线投屏

局域网内的无线投屏工具：把 Windows 电脑（发送端）的屏幕与系统声音，
投到坚果投影仪或任意 Android 设备（接收端）上。

## 组件

- `sender/` —— Windows 发送端（Python + ffmpeg/Qt），抓取屏幕与系统声音并推流。
- `receiver/` —— Android 接收端（Java），在投影仪 / 手机上解码并渲染画面与声音。
- `tools/` —— 打包脚本：APK 用 `build_apk.sh`，Windows 安装包用
  `build_sender_exe.sh` / `build_win_receiver_exe.sh`，另有若干诊断与回归测试脚本。

## 下载（预编译版本）

不想自己编译，直接到 [Releases](https://github.com/904146315/CastLink/releases) 页面下载即可：

| 文件 | 用途 | 适用 |
|------|------|------|
| `CastLink-Sender-Setup.exe` | 发送端 · Windows 安装版（双击安装，推荐） | Windows 10/11 |
| `CastLink-Sender.zip` | 发送端 · 免安装绿色版（解压后运行里面的 `CastLink-Sender.exe`） | Windows 10/11 |
| `CastLink-Receiver-v1.0.14.apk` | 接收端 · 安卓 / 投影仪安装包 | Android 电视 / 投影仪 / 手机 |

> 注：`CastLink-Sender.exe` 为 onedir 启动器，须与同目录依赖一起使用，单独下载无法运行；
> 免安装请下载上面的 `CastLink-Sender.zip`。

### 使用步骤
1. 投影仪 / 手机安装 `CastLink-Receiver-v1.0.14.apk` 并打开（待机界面显示本机设备名）。
2. Windows 端安装或解压发送端，打开后在同一局域网内即可发现设备，点击投屏。
3. 若画面不动，可在接收端待机界面连按【上】键切换渲染通道、连按【下】键切 H.264；
   连按 2 次【左】或【返回】键退出应用。

### 组件版本
- 发送端：v1.0.10
- 接收端（安卓）：v1.0.14

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
