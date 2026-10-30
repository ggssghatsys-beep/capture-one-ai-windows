# Capture One AI · Windows / macOS

**0.4.0a1：合并原生 Mac 开发预览与现有 Windows 客户端。** Windows 保留 16.8.6.3784 的现有功能；Mac 在 Apple Silicon / macOS 15.7.4 / C1 16.8.6.25 / Session 完成代表性实测，包含原生调色、图层／蒙版、RGB 波形和菜单栏常驻入口。Mac 是版本绑定的源码预览，尚非独立签名安装包；具体缺口见 [Mac 当前范围](docs/MAC_STATUS.md)。

**安装时让桌面 AI 先询问是否创建桌面波形图入口。** 同意后才创建，不同平台共用同一工具接口。[复制安装提示](docs/AI_INSTALL_PROMPT.md) · [Mac 安装](docs/INSTALL_MAC.md) · [Windows 安装](docs/INSTALL.md)。

**直接填 API 对话修图，也可以接入桌面 AI 的 MCP；配套独立波形图和蒙版肤色分析。** 让视觉 AI 通过平台原生接口读取和调整 Capture One，并取得 C1 实际渲染的预览。提供 **API 修图窗口 + MCP stdio + JSON CLI**，共用一套原生执行接口。

**0.3.0a3 新增 API 修图窗口：** 填写 API 地址和密钥 → 从服务读取模型 → 查询思考等级 → 输入需求，选择“仅分析”或“在副本修图”。无需另外安装聊天客户端。支持 **OpenAI Responses API** 及实现相同图片／工具调用协议的服务；仅有 Chat Completions 的接口暂不支持。模型和等级不使用固定清单，服务未提供的能力明确标为未知。

**0.3.0a3 预览版，实测 Capture One 16.8.6.3784 / Windows / Session。** 本机 C1 副本读写、预览和蒙版分析已实测；**新 API 客户端的真实云端模型联调尚未完成**，不能据此保证所有模型或第三方兼容服务可用。使用 C1 内部托管对象和原生处理内核，不是官方公开稳定 SDK。其他 C1 版本、Catalog 和多照片批量任务尚未完成兼容验证；启动器拒绝未验证版本。

Native Capture One editing for Windows with an optional direct API chat window, MCP and a JSON CLI. Enter a Responses API key, discover models and reasoning efforts from your provider, and edit protected variants using the existing native bridge. Includes scopes, mask-aware analysis and ROI statistics. Cloud model integration is pending live validation. Unofficial, version-specific, MIT-licensed.

## 选择使用方式

| 方式 | 怎么开始 |
|---|---|
| **直接填写 API** | 安装时加 `-WithAI -WithScopes`，打开桌面“Capture One AI 修图”，在“API 设置”填地址和密钥、读取模型、查询思考等级。见 [API 窗口说明](docs/AI_WINDOW.md) |
| **接入已有桌面 AI** | 将生成的 `config/mcp-server.local.json` 加入支持本地 MCP 和图片的客户端。见 [MCP 接入](docs/AI_CLIENTS.md) |
| **只看波形／直方图／矢量图** | 安装时加 `-WithScopes`，打开“Capture One 图像分析”，不需要 API 密钥 |

API 修图会向所填服务发送照片预览、统计和必要的文件标识，不上传 RAW 原文件，并消耗该服务的 API 额度。密钥默认仅本次使用，也可选择 Windows 本机加密保存。只在登记副本上修改，保留原版本。

## 功能

- **API 对话修图**：动态模型与思考等级查询、只读分析／副本编辑、停止后续操作、用量显示；原 MCP 接入方式保持兼容。
- 曝光、对比度、亮度、饱和度、白平衡、HDR。
- RGB / R / G / B 色阶；RGB / R / G / B / Luma **五通道原生曲线**。
- Basic、Advanced、Skin 混色器；主色轮和阴影／中间调／高光色彩平衡。
- 调整图层创建、选择、透明度、改名、开关、复制和排序。
- 主体／背景 AI 蒙版；反相、复制、组合、羽化、渐变和亮度范围。
- 模块重置与快照复用；当前集合内按 UUID 选图和翻页。
- C1 原生照片／灰度蒙版预览，长边 128～1600 像素；预览与 RAW 坐标映射。
- 独立波形窗口和 `analyze_image`：亮度波形、RGB Parade、线条直方图、带肤色参考箭头的矢量图，以及供 AI 读取的同源数值、ROI 和参考差值。见 [启动与接口说明](docs/SCOPES.md) 和 [验证记录](docs/SCOPES_VALIDATION.md)。0.3.0a1 新增；旧 0.2.0a1 ZIP 保持原样。
- **0.3.0a2：跟随当前图层蒙版分析＋预览中拖动框选。** 选背景层看整图，选调整层只统计其蒙版覆盖的最终画面；波形、直方图、矢量图和 AI 数值使用同一覆盖权重，保留羽化。

### 只看皮肤区域怎么用

1. 在 C1 选中覆盖皮肤的蒙版图层，在分析窗口切到“矢量图”。窗口默认开启“⋯ → 跟随当前图层蒙版”。
2. 要进一步缩小范围，选择“⋯ → 在预览中框选区域…”，拖动矩形后点击“应用”。统计范围为矩形与蒙版的交集，不修改 C1 裁剪或蒙版。
3. 选回 Background 即分析背景层范围；若之前设置了矩形框选，可在框选窗口点“恢复整个画面”并应用。取消“跟随当前图层蒙版”可忽略蒙版。

肤色参考线不会自动找脸或识别皮肤。主体蒙版可能包含衣服，分析纯肤色时需要相应的皮肤蒙版。空蒙版显示没有覆盖像素，绝不自动退回整图。

AI 分析当前图层时调用 `analyze_image(analysis_scope="active_layer", include_vectorscope=True)`；省略 analysis_scope 保持旧整图语义。获取窗口同一份快照时传 analysis_id，详见 [AI 安装与使用提示](docs/AI_INSTALL_PROMPT.md)。

全局和局部调整共用同一接口。正常流程不导入预设、不截图识色、不发送键鼠输入，不要求展开面板。清晰度、锐化、降噪、镜头／几何及人像精修不在当前范围；默认不切文档、不导出。

## 安装与启动

希望让桌面 AI 帮你安装、把波形图启动器放到桌面并与 C1 一起打开，可复制 [AI 安装提示](docs/AI_INSTALL_PROMPT.md)。人用窗口，AI 用 MCP；两者共用同一图像分析核心，窗口关闭不影响 AI 读取数值。

Mac 使用 Apple Silicon Python 3.11+：`python3.12 scripts/install_macos.py`。同意创建波形桌面入口后加 `--desktop-scopes`；原生桥本机构建与授权步骤见 [Mac 安装](docs/INSTALL_MAC.md)。以下保留 Windows 安装步骤。

需要 64 位 Windows、有效授权的 C1 **16.8.6.3784**、64 位 Python 3.11+、PowerShell **7.6+** 与 .NET 10 Windows Desktop Runtime。本机验收 Python 3.14.7 / PowerShell 7.6.5，其他 Python 版本未逐项实测。安装依赖需要联网，运行库和 C1 均不随包分发。

下载 [Releases](https://github.com/ggssghatsys-beep/capture-one-ai-windows/releases) 的 Windows ZIP，解压到固定目录。在该目录的 PowerShell 7 中执行：

```powershell
.\Install.ps1 -WithAI -WithScopes -SessionPath 'D:\Photos\MySession\MySession.cosessiondb'
.\Start-CaptureOne.ps1
.\AI.ps1
```

已有项目和 Session 配置时，执行 `Install.ps1 -EnvironmentOnly -WithAI -WithScopes` 补装 API 窗口。会增加“Capture One AI 修图”桌面入口；打开后填入自己的 API 配置，建议先点“仅分析”。不使用内置 API 窗口时可省略 `-WithAI`。

需要配套窗口和桌面入口时，安装命令加 `-WithScopes`。已配置项目可执行 `Install.ps1 -WithScopes -EnvironmentOnly`；它会创建 **Capture One + AI**、**Capture One 图像分析** 两个快捷方式。前者启动 C1 并按设置联动窗口，后者可随时单独重开窗口。窗口“⋯”菜单可关闭自动联动；原厂快捷方式保持原样。详见 [启动模式](docs/SCOPES.md#启动模式与桌面入口)。

在 C1 打开配置的 Session，选中照片。首次加载桥接时应先正常退出 C1，再用本项目启动器启动；仅打开普通 C1 快捷方式不会加载桥接。启动器默认不会关闭已运行的 C1；确需正常重启时显式传 `-Restart`。见 [安装说明](docs/INSTALL.md)。

使用外部桌面 AI 时，将安装器生成的 `config/mcp-server.local.json` 加入该客户端；内置 API 窗口无需另外导入 MCP 配置。核心字段是本机 Python 路径和 `-m c1ai mcp`。本版提供原六个工具及新增 `analyze_image`；旧 0.2.0a1 Release 为六个。外部客户端须支持图片块并使用视觉模型。见 [AI 接入](docs/AI_CLIENTS.md)。

## 一条编辑通道

```text
API 修图窗口 / 外部视觉 AI → 同一 MCP 工具 / CLI → 串行执行器与操作记录 → C1 内部命名管道
       → C1 原生参数／图层／蒙版对象 → 参数回读 + C1 原生预览
```

1. context 确认环境；read(fields=[]) 获取当前照片；preview(kind=native) 实际看图。
2. images(create_candidate) 创建并登记变体副本，保留原版本。
3. 按画面选择模块：read → adjust → preview，使用读取返回的目标和观察凭据。
4. 全局完成后再决定是否加局部蒙版；达到目标即停止。

参数使用绝对值，同模块相关参数合并提交。每次写入有唯一编号；超时先查询原编号，不重复派发。只有登记候选副本可写，参数回读不等于视觉效果合格。

## 包内文档

继续开发请克隆本仓库最新源码，先读 [AGENTS](AGENTS.md)。Windows 与 Mac 保留独立宿主实现，工具和分析核心共享。Mac 已实现与实机验证范围、复杂蒙版限制及版本绑定见 [MAC_STATUS](docs/MAC_STATUS.md)，安装入口见 [INSTALL_MAC](docs/INSTALL_MAC.md)。本次发布为双平台源码包；旧 Windows ZIP 不被替换。

| 文档 | 内容 |
|---|---|
| [INSTALL](docs/INSTALL.md) | 安装、启动、配置、升级和排错 |
| [AI_CLIENTS](docs/AI_CLIENTS.md) | 通用 MCP / CLI 接入与 AI 调色流程 |
| [AI_WINDOW](docs/AI_WINDOW.md) | 直接填写 API、动态模型／思考等级、对话修图和验证边界 |
| [API](docs/API.md) | 六个工具、请求示例、单位和返回值 |
| [COLOR_API](docs/COLOR_API.md) | 混色器、色阶、图层、蒙版和模块复用 |
| [NAVIGATION_PREVIEW](docs/NAVIGATION_PREVIEW.md) | 选图、原生预览和坐标映射 |
| [SUPPORT](docs/SUPPORT.md) | 已验证范围与未完成事项 |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | 原生调用与原图保护 |
| [DEVELOPMENT](docs/DEVELOPMENT.md) | 开发和发布包生成 |
| [AGENTS](AGENTS.md) | 开发 AI 接手顺序、原生优先和原图保护原则 |
| [MAC_PORT](docs/MAC_PORT.md) | Mac 迁移入口、可复用代码、已知缺口与验收顺序 |
| [MAC_STATUS](docs/MAC_STATUS.md) | Mac 原生开发入口、实机结果与剩余限制 |

运行状态在当前用户 `%LOCALAPPDATA%\CaptureOneAI`，不进入仓库。分发包不含照片、预览、Session、私人配置、连接令牌或历史操作数据库。Windows 原生预览只保留最近 8 张缓存，不是永久成片导出。

[MIT License](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)

Mac 快速窗口直接读取 C1 当前完整裁切的 RGB 缓冲，持续更新代表值约 0.14～0.16s；精确局部分析仍按需计算，尚不承诺所有请求低于 0.5s。校样、位深及其他边界见 [图像分析](docs/SCOPES.md)。
