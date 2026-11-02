# 原生预览与分析的 sRGB 输出

2026-11-01：照片预览和波形取样共用 `native-srgb-v1` 色彩约定。它确认渲染时做过转换；仅有 PNG 的 sRGB 标记不能证明像素已经是 sRGB。

## Windows

`GetICProcessSettings()` 的初始 ICC 链把输入相机配置同时放在输出位置。原先直接使用这份设置生成的 PNG 带有 sRGB 标记，却未明确转换到 sRGB，导致预览和分析偏离 C1 查看器。

现在只在临时渲染设置副本中替换第二个 ICC 为当前 C1 安装目录下的 `Color Profiles/Common/sRGB Color Space Profile.icm`。路径相对正在运行的 C1 程序解析，不使用开发者电脑路径、显示器 ICC、用户配方或相机型号硬编码。缺少有效 RGB ICC 或遇到尚未核验的 ICC 链时拒绝取图，不静默回退。

PNG 和 BGRA8 从同一转换后的位图产生。Python 检查原生色彩凭据，并为 PNG 补入 sRGB ICC；补标记不改变像素值。BGRA8 分析记录 `verified_native_srgb_output`，不再把原生照片缓冲视为“无 ICC、假定 sRGB”。

## macOS

原生位图任务原本已经请求系统 sRGB 校样／显示配置。本次增加最终核验：读取 CGImage 的源 ICC，使用 CoreGraphics 绘入明确的 sRGB 位图，PNG 和 BGRA8 都从这个结果产生。没有源 ICC 时拒绝取图，不猜测颜色。Viewer 快速缓存继续按其源 ICC 转为 sRGB，并补上相同色彩凭据；无法核验缓存 ICC 时走已有的独立渲染后备。

灰度蒙版是覆盖率，不应用照片色彩转换。Viewer 显示／校样效果仍可能已烘焙进像素，现有 `render_basis` 保护继续阻止将不同校样口径直接做差值比较。

## 更新已有安装

源码和 Python 包更新后还需要更新原生桥。只更新 Python、继续使用旧桥会明确报告 `native_color_unverified` / `NATIVE_COLOR_UNVERIFIED`，不会交付无法确认的原生照片。

- Windows：按 [安装与升级](INSTALL.md) 处理运行中的 owner，执行 `internal/build.ps1` 后重新连接。启动钩子未变，已加载桥接的 C1 不需要为本次修复重启。
- Mac：在原来已配置桥的安装环境中执行 `.venv/bin/python scripts/build_macos_bridge.py --enable`，然后重连 MCP／重新打开分析窗口。首次配置和权限按 [Mac 安装说明](INSTALL_MAC.md)。不迁移其他机器的连接令牌或候选登记。

这些步骤不修改照片、相机 ICC 选择、胶片曲线、显示器配置或系统校准。

## 验证范围

Windows 16.8.6.3784 已在本机真实照片核对：PNG 内嵌 sRGB ICC，PNG 与分析 BGRA8 逐像素一致，调整参数与选择未变。此前固定照片、参数和修订的诊断中，指定 sRGB 并将查看器样本按实际显示器 ICC 换算后，匹配区域平均通道差从 10.32 降至 0.27（8 位 0–255）。这是本机一致性检查，不是显示器校准或所有相机的独立认证。

Mac 修改通过共享 Python 色彩检查与 arm64 macOS 目标交叉编译；本次没有 Mac 宿主，新增转换与拒绝路径尚未实机验收，不继承之前版本的 Mac 实机结论。
