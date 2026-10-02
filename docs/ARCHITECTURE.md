# CastLink 投屏软件 —— 架构与设计文档

> 小米笔记本电脑 → 坚果投影仪 的 **4K 无损无线投屏** 解决方案。
> 两个客户端：电脑端（Windows 发送端）+ 投影仪端（Android TV 接收端）。

---

## 1. 总体架构

```
┌──────────────────────┐                          ┌──────────────────────────┐
│   电脑端（小米笔记本） │                          │  投影仪端（坚果 Android） │
│   CastLink 发送端     │   UDP 发现 / TCP 控制    │  CastLink 接收端          │
│                      │ ───────────────────────▶ │                          │
│  ┌────────────────┐  │  CAST/1 控制通道 (JSON)  │  ┌────────────────────┐  │
│  │ 桌面采集        │  │                          │  │ 网络接收 + 重组     │  │
│  │ ddagrab/gdigrab│  │  ┌────────────────────┐  │  │ FrameReassembler   │  │
│  └───────┬────────┘  │  │  媒体通道（视频/音频）│  │  └─────────┬──────────┘  │
│  ┌────────▼────────┐  │  │  TCP（可靠，无损强制）│  │  ┌────────▼──────────┐  │
│  │ 硬件编码器       │  │  │  UDP（低延迟，乱序） │◀─┼─▶│ MediaCodec 解码   │  │
│  │ NVENC/QSV/AMF/  │  │  └────────────────────┘  │  │ + AudioTrack       │  │
│  │ MF/x265 (HEVC)  │  │                          │  │  + Surface 渲染     │  │
│  └────────┬────────┘  │                          │  └────────────────────┘  │
│  ┌────────▼────────┐  │                          │                          │
│  │ CAST/1 分帧封装  │  │                          │                          │
│  │ + HMAC 配对      │  │                          │                          │
│  └────────────────┘  │                          │                          │
└──────────────────────┘                          └──────────────────────────┘
```

两端共享同一份 **CAST/1 协议实现**（`castlink/` 包），从字节层面保证发送端封装与接收端重组完全一致。

### 三类产物
| 产物 | 平台 | 用途 |
|------|------|------|
| `CastLink-Sender-Setup.exe` | Windows | 电脑端主程序（投屏发送） |
| `CastLink-Receiver-v1.0.0.apk` | Android TV | 投影仪端主程序（投屏接收） |
| `CastLink-Receiver-Win-Setup.exe` | Windows | 备用接收端（联调/无投影仪时使用） |

---

## 2. CAST/1 协议

自研轻量二进制协议，定长 32 字节头 + 净荷。小端序，magic `CAST`。

### 2.1 帧头（32 字节，`<4sBBBBBIHHIHHQ`）

| 偏移 | 字段 | 说明 |
|------|------|------|
| 0 | magic | `CAST` (4 字节) |
| 4 | version | 协议版本，当前 1 |
| 5 | stream | 流类型：`STREAM_VIDEO=1` / `STREAM_AUDIO=2` |
| 6 | codec | 编码：`CODEC_H265=1` / `CODEC_H264=2` / `CODEC_AAC=10` |
| 7 | flags | `F_KEYFRAME`(1) / `F_LAST`(2) 等标志位 |
| 8 | frame_id | 帧序号（u32，用于分片重组） |
| 12 | frag_idx | 当前分片序号（u16） |
| 14 | frag_count | 总分片数（u16） |
| 16 | total_size | 整帧（所有分片拼装后）字节数（u32） |
| 20 | payload_len | 本分片净荷长度（u16） |
| 22 | _pad2 | 保留（u16） |
| 24 | pts | 呈现时间戳（微秒，u64） |
| — | — | 合计 **32 字节** |

> ⚠️ **这是与投影仪端（Java）的跨语言契约**，任何一端增删字节都会让另一端静默失效。
> 发送端 Python 曾在此多写一个填充字节（头变成 33 字节），导致 `frame_id` 之后的
> 每个字段都比 Java 端偏 1 字节 —— 接收端解析出的 `payload_len` / `frag_count` 全是
> 垃圾值，分片永远收不齐，**一帧都重组不出来，表现就是投影仪黑屏且无声**，
> 而发送端界面显示一切正常（Python 两端共用同一份实现，同机联调测不出来）。
> 现在 `tools/proto_layout_test.py` 会把 `FrameReassembler.java` 的解析逻辑逐行复刻，
> 拿真正的发送端数据对拍，防止回归。

### 2.2 分片与重组
- 单个报文净荷上限：TCP `32768` 字节 / UDP `1152` 字节（为 PPPoE/隧道预留余量）。
- 发送端 `pack_frame()` 按上限切片，带 `frame_id + frag_idx/frag_count`。
- 接收端 `FrameAssembler`：
  - **TCP 模式**：从字节流中按 32 字节头解析，失步时向前滑 1 字节重新同步。
  - **UDP 模式**：每个报文独立 `feed_packet()`，支持乱序到达。
  - 碎片超时（250ms）即丢弃半帧，遵循"宁丢勿错"原则，避免花屏扩散。

### 2.3 净荷语义（视频）
- 视频流净荷 = **一个完整的编码帧（Annex-B AU）**，而非裸分片。
- `AnnexBSplitter` 负责把 ffmpeg 输出的连续 Annex-B 流切成帧边界；采用 **slice header 的 `first_slice_segment_in_pic_flag`** 判定帧边界（兼容 NVENC/QSV/AMF 的多 slice 输出），而不仅是单 slice 假设。
- `ParameterSetCache` 保证每个关键帧前必带 VPS/SPS/PPS；接收端**首帧必须是关键帧**才喂给解码器，避免 "Could not find ref with POC 0"。

### 2.4 控制通道
行式 JSON over TCP（独立端口），命令示例：
- 握手：`hello` → `hello_ack+nonce` → `auth`(HMAC-SHA256) → `auth_ok(+长期令牌)`
- 媒体协商：`start`(分辨率/帧率/码率/传输方式) → `ready`(媒体端口)
- 运行期：`idr`(弱网请求关键帧)、`stats`(每 1s 回报帧率/丢帧/抖动)、`ping`

### 2.5 传输方式
| 模式 | 特点 | 适用 |
|------|------|------|
| TCP | 可靠、有序、不丢包 | **无损(HEVC 数学无损)强制走此模式** |
| UDP | 低延迟、允许乱序/丢包、接收端主动要帧 | 高实时性档位、弱网 |

---

## 3. 画质档位（QUALITY_MODES）

| 档位 | 编码目标 | 传输 | 编码参数要点 |
|------|----------|------|--------------|
| `lossless` | HEVC **数学无损** | 强制 TCP | `lossless=1`；不做缩放/插值（保持原生分辨率） |
| `visual` | 视觉无损（默认） | TCP/UDP | CRF 极低 + 高码率上限 |
| `high` | 高画质 | TCP/UDP | 高码率 |
| `balanced` | 均衡 | TCP/UDP | 中等码率 |
| `smooth` | 流畅优先 | UDP | 低延迟、可降帧 |

- 4K 信号：发送端以物理分辨率采集（如小米笔记本 2880×1800），可保持原生或放大铺满 3840×2160 画布（黑边补齐），让投影仪收到标准 4K 信号。
- 码率由 `estimate_bitrate_mbps()` 按分辨率×帧率×码率系数估算，`recommend_transport()` 决定默认传输方式。

---

## 4. 发送端（电脑端，Windows）

### 4.1 采集
- 优先 `ddagrab`（Desktop Duplication API，GPU 复制、低 CPU）；不可用则降级 `gdigrab`（GDI）。运行时实测择优，会话中失效再降级。
- `primary_resolution()` 用 `EnumDisplaySettingsW` 取物理分辨率。

### 4.2 编码
- 硬件编码器能力探测 `probe_capabilities()`：先从 `ffmpeg -h encoder=X` 取支持的像素格式，再**真实编码几帧**验证可用性。
- 候选链自动降级：**NVENC → QSV → AMF → MF → libx265**。
- `build_video_encode_args()` 按各后端生成参数，强制**单 slice** 并保持参数集内联（`repeat-headers`），确保弱网重同步后立刻可解码。

### 4.3 传输
`MediaSender`：TCP/UDP 双模；发送队列带背压与丢帧策略；`TxStats` 统计帧率/码率/丢包。

### 4.4 配对与安全
- 对称式 UDP 广播发现（端口 47010，明文 JSON 信标，2s 一次），替代常被裁剪的 mDNS/SSDP；枚举各网卡定向广播地址。
- `HMAC-SHA256` PIN 配对：PIN 只参与 HMAC 计算，**不上网**；首次配对下发长期令牌，之后免输 PIN。

### 4.5 UI（PySide6）
无边框主窗口：设备列表（自绘两行 + pill）、画质/分辨率/帧率/传输参数表单、实时统计栅格、托盘、反向投屏请求弹窗、单实例守护。

---

## 5. 接收端（投影仪端，Android）

- `MediaCodec` HEVC 硬解 + `releaseOutputBuffer(idx, true)` 即时渲染；拿不到输入缓冲时丢弃非关键帧。
- `AudioTrack` MODE_STREAM 播放 AAC。
- 无第三方依赖：仅用 `org.json`。
- 前台 Service + WakeLock + MulticastLock，保证投屏期间不休眠、能收组播。
- Leanback launcher 图标 + banner，适配 Android TV。
- `FrameReassembler`：与 Python 端严格对齐的常量与字节序，250ms TTL，缓冲上限。
- 弱网时主动向发送端发 `{"t":"idr"}` 请求关键帧。

---

## 6. 接收端（Windows，联调/备用）

- `FrameAssembler` 重组 → `FfmpegSdlRenderer`（`-f sdl2` 弹窗；`--render null` 时 `-f null -` 供自动化联调）。
- 关键修复：曾经用手工偏移 `buf[20:22]` 读 `payload_len` 导致整条 TCP 流失步刷"非法 magic"；改为统一 `FrameHeader.unpack()` 解析 + 失步滑字节重同步。

---

## 7. 构建与打包

### 7.1 工具链（本机无 Gradle/Android Studio，纯手工）
- JDK 17（`tools/jdk/`）、Android SDK（`tools/android-sdk/`，platform-tools / android-34 / build-tools 34.0.0）
- ffmpeg 6.1.1 静态版（`tools/ffmpeg.exe`，带 sdl2 输出与 hevc_qsv/libx265/nvenc/amf/mf）
- Python venv（`tools/pyenv/`：PySide6 6.11.2、PyInstaller 6.22.3、Pillow、psutil）

### 7.2 Android APK（手工链）
```
javac → d8 → aapt2(compile+link) → zipalign → apksigner
```
- `tools/build_apk.sh`：源码/资源/清单复制到纯 ASCII 临时目录（绕开项目根中文路径），依次编译、DEX、资源链接、**zipfile 注入 classes.dex**（新版 aapt2 已移除 `add` 子命令）、对齐、签名。
- 产物：`dist/CastLink-Receiver-v1.0.0.apk`（v2/v3 签名有效）。

### 7.3 Windows 安装包
- PyInstaller `--onedir` 生成程序文件夹（含 `ffmpeg.exe`、`resources/`），`--contents-directory .` 保持扁平布局，使运行时 `find()`/`resource_path()` 能直接定位。
- 安装程序本身也是 PyInstaller **onefile**：把 onedir 文件夹作为内嵌载荷，运行时解压并复制到 `%LOCALAPPDATA%\Programs\CastLink\<子目录>`，用 VBS 创建开始菜单/桌面快捷方式。
  - 注：本环境 NSIS/Inno Setup 未实际可用、iexpress/makecab 因沙箱拦截失效，故采用此自包含方案，无需外部安装器。
- 产物：`dist/CastLink-Sender-Setup.exe`、`dist/CastLink-Receiver-Win-Setup.exe`。

---

## 8. 验证结果

- **协议往返一致性**（`tools/protocol_roundtrip_test.py`）：73/73 帧 TCP 逐字节一致；UDP 完全乱序也逐字节一致。
- **端到端联调**（`tools/e2e_test.py`，同机收发）：4K 无损档 254 帧零丢帧、29.8fps；全档位（lossless/visual/high/balanced/smooth）零 "Could not find ref" 错误。
- **修复的关键问题**：管道单次 `write` 部分写入导致 1.97MB 首帧被截断 → 改为循环写满；`-fflags nobuffer` 让 ffmpeg 不等 AU 收齐就解码 → 移除后零错误；QSV 多 slice 帧边界误判 → `AnnexBSplitter` 改用 slice header 判定。
- **APK**：aapt2 资源链接正常、v2/v3 签名校验通过。

---

## 9. 目录结构（关键文件）

```
castlink/                  两端共用的协议/工具包
  protocol.py              CAST/1 头、分片、Annex-B 切割、ADTS 切割、参数集缓存
  control.py               行式 JSON 控制通道
  discovery.py             对称 UDP 广播发现
  identity.py / config.py / ffmpeg.py
sender/castlink_sender/   电脑端发送端（PySide6 GUI）
  encoders.py capture.py transport.py audio.py session.py ui_main.py
receiver/android/          投影仪端（Android，Java）
  java/com/castlink/receiver/*.java  AndroidManifest.xml  res/
receiver/castlink_receiver/  Windows 接收端（联调）
receiver-win/              单文件 Windows 接收端（命令行）
tools/                    构建脚本、工具链、e2e/协议测试
docs/ARCHITECTURE.md      本文档
dist/                     最终产物（APK + 两个 Setup.exe）
```
