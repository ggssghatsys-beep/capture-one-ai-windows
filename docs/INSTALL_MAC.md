# macOS 源码预览版安装

0.4.0a1 发布包同时包含 Windows 与 Mac 源码。Mac 只在 **Apple Silicon / macOS 15.7.4 / Capture One 16.8.6.25 / Session** 完成代表性实测；不是已签名的独立应用安装包。Windows 继续使用 [INSTALL](INSTALL.md)，不要在 Mac 执行 Windows PowerShell 安装器。

## 安装环境与可选桌面入口

将源码解压／克隆到固定目录。使用原生 arm64 Python 3.11+，本机验证为 3.12。先核对 Python 的实际路径、版本与架构，避免按“Python”应用名打开系统旧版本。

```sh
python3.12 scripts/install_macos.py --with-scopes
```

脚本创建／复用项目 `.venv`，安装当前源码和 Mac／波形依赖，生成 `config/mcp-server.local.json`。不会启动／重启 C1、切换 Session、修改照片或改变系统权限；不覆盖既有桥、候选和操作记录。需要内置 API 窗口可额外加 `--with-ai`；真实云端模型/API 尚未联调。

**安装 AI 应先询问：“是否创建桌面波形图入口？”** 用户同意后使用：

```sh
python3.12 scripts/install_macos.py --desktop-scopes
# 只补入口，不重装依赖：
./.venv/bin/python scripts/create_macos_scopes_shortcut.py
```

`--desktop-scopes` 已包含波形依赖。用户拒绝桌面入口时不传此选项；是否安装窗口依赖可单独用 `--with-scopes` 决定。生成用户“应用程序”及桌面的 **Capture One 图像分析.app**，不覆盖其他安装目录的同名入口。程序文件夹移动后需更新入口与 MCP 路径。

入口固定使用项目 `.venv`。打开后菜单栏保留波形图标，关窗隐藏并停止读取，从桌面或菜单栏重开同一窗口；菜单“退出图像分析”会等当前只读线程结束。没有设置开机自启。

## 本机构建原生桥

普通数值读写优先使用官方 Apple Events；当前照片 RGB、灰度蒙版、渐变几何等使用版本绑定的内部原生桥。桥不是官方公开稳定 SDK，不包含厂商组件，不随包分发本机编译的 dylib。

1. 先运行 `./.venv/bin/python scripts/check_macos.py --metadata-only`，核对 C1 完整版本、架构与安装位置。其他版本不继承本机资格。
2. 使用已安装的 Apple 命令行开发工具，在本机运行：

   ```sh
   ./.venv/bin/python scripts/build_macos_bridge.py --enable
   ```

   可用 `--compiler /path/to/zig` 指定已安装的 Zig；不下载或打包厂商 SDK。缺少编译器时先安装官方开发工具，不修改 C1 程序、签名、授权组件或系统保护。
3. 在 C1 正常打开自己的 Session，单选照片。首次原生请求通过 Frida 在运行中的 C1 加载本项目自建库，随后断开调试连接。macOS 若要求自动化／调试授权，由用户完成；拒绝或缺权限时明确返回不可用，不尝试绕过。
4. 将生成的 MCP 配置加入桌面 AI，先只读核查 `context → read → preview → analyze_image`。写入只允许用 `images(create_candidate)` 正常登记的候选副本；不能复制其他电脑的候选或权限记录。

没有配置桥时，原生 RGB 波形读取不可用；不会为波形自动开启 Capture Pilot。AX／鼠标兼容路线需单独明确启用，不作为安装默认步骤。

## 升级与限制

更新源码前，结束本项目窗口及其未决操作；只在 owner 空闲时重新加载，不能结束其他 Python 或 C1 进程。若原生源码 build_id 改变，须先保存 C1 的工作、正常重启后再加载新桥，不在同一宿主覆盖旧库。

Windows 与 Mac 共用 MCP／CLI 和图像分析契约，平台仅适配宿主接口。Mac 快速窗口读取当前完整裁切的原生 RGB 缓存，保留校样效果；AI 默认 exact 另做原生渲染。精确局部统计、复杂复合蒙版反选、坐标映射及其他版本的边界见 [MAC_STATUS](MAC_STATUS.md)。安装验收不修改照片，不把安装成功等同于宿主功能通过。

状态、缓存和密钥只保存在本机，位于 `~/Library/Application Support/CaptureOneAI`；不要上传此目录或 Session／照片。
