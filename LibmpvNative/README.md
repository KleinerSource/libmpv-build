# libmpv 独立预编译

mpv 固定为 **0.41.0**，配方版本 **libmpv-v0.41.0-r1**。`sources.lock.json` 固定源码提交；升级源码、补丁或工具链后必须增加 `rN`，已发布版本不得覆盖。

在本仓库 Actions 手动运行 `libmpv precompiled Release`。Linux 构建 Android 四 ABI，macOS 构建 iOS 设备 arm64、模拟器 arm64/x86_64。两端均成功后才发布同一个 Release；App 不调用该工作流，也不需要安装原生源码构建工具。

默认 `all` 构建并发布完整版本；可单独选择 `android` 或 `ios` 验证并保存该平台制品，单平台运行不会发布不完整 Release。Android 的 MbedTLS 使用便携 AES 路径，绕过其 ARM crypto intrinsic 与 NDK 27 的编译冲突；不影响视频硬解。

产物：

- `full-{arm64-v8a,armeabi-v7a,x86_64,x86}.jar`：libmpv 和 media-kit Android helper；保留完整 FFmpeg 解码器及 PGS。
- `libmpv-ios.tar.gz`：动态 `Mpv.xcframework`，保留 libmpv OpenGL render API、VideoToolbox 和 AudioUnit。FFmpeg、dav1d、libass 等静态链接进框架，只导出 `mpv_*`，避免与 KSPlayer 的静态依赖串用。
- `manifest.json`：版本、源码提交、每个产物的 SHA-256。消费端必须匹配固定版本及 SHA-256，缺包或错误不可退回旧内核。

两端均从 FFmpeg 官方 6.1.4 源码构建，所有源码提交见 sources.lock.json。iOS 使用独立的 ios_deps.py，完全不依赖 App 的 KSPlayer、FFmpegKit、私有 FFmpeg 分支或其二进制。构建输出位于 build/libmpv-native/。

首次发布顺序：提交组件配方 → 手动执行独立工作流并确认 Release 五个包齐全 → 更新 App 的固定清单 → 构建 App 并做真机播放验收。普通 App 更新只执行下载、校验和链接。依赖源码更改才重新构建组件，已有 Release 长期保留供旧 App 构建复现。

本地检查：`python3 -m unittest discover -s LibmpvNative -p 'test_*.py' -v`。原生构建必须在全新输出目录中运行 `python3 LibmpvNative/build.py android` 或 `ios`。编译器检查必需解码器、API 导出及 iOS 非系统动态依赖；最终仍须运行 App 的播放/seek/字幕/后台恢复验收。
