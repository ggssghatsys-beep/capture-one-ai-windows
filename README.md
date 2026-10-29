# Capture One AI Windows

让视觉 AI 在 Windows 上读取和调整 Capture One，并取得 C1 实际渲染的预览。提供 **MCP stdio + JSON CLI**，不绑定 AI 厂商、账号或聊天客户端。

**源码新增 0.3.0a3：可直接填写 API 的 AI 修图窗口。** 动态读取模型、查询思考等级，支持仅分析和副本修图，复用原有 MCP／原生接口。运行 `Install.ps1 -EnvironmentOnly -WithAI -WithScopes` 后打开 `AI.ps1`。本机 C1 调用已实测，真实云端模型联调待填入密钥；当前已发布基线仍为 0.3.0a2。详见 [AI 窗口使用与验证](docs/AI_WINDOW.md)。

**0.3.0a2 预览版，实测 Capture One 16.8.6.3784 / Windows / Session。** 使用 C1 内部托管对象和原生处理内核，不是官方公开稳定 SDK。其他 C1 版本、Catalog 和多照片批量任务尚未完成兼容验证；启动器拒绝未验证版本。

Native Capture One editing for Windows through MCP and a JSON CLI. Vision models plan edits; the local bridge executes them and returns real Capture One previews. Unofficial, version-specific, MIT-licensed.

## 功能

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

需要 64 位 Windows、有效授权的 C1 **16.8.6.3784**、64 位 Python 3.11+、PowerShell **7.6+** 与 .NET 10 Windows Desktop Runtime。本机验收 Python 3.14.7 / PowerShell 7.6.5，其他 Python 版本未逐项实测。安装依赖需要联网，运行库和 C1 均不随包分发。

下载 [Releases](https://github.com/ggssghatsys-beep/capture-one-ai-windows/releases) 的 Windows ZIP，解压到固定目录。在该目录的 PowerShell 7 中执行：

```powershell
.\Install.ps1 -SessionPath 'D:\Photos\MySession\MySession.cosessiondb'
.\Start-CaptureOne.ps1
.\Run.ps1 context --compact
```

需要配套窗口和桌面入口时，安装命令加 `-WithScopes`。已配置项目可执行 `Install.ps1 -WithScopes -EnvironmentOnly`；它会创建 **Capture One + AI**、**Capture One 图像分析** 两个快捷方式。前者启动 C1 并按设置联动窗口，后者可随时单独重开窗口。窗口“⋯”菜单可关闭自动联动；原厂快捷方式保持原样。详见 [启动模式](docs/SCOPES.md#启动模式与桌面入口)。

在 C1 打开配置的 Session，选中照片。首次加载桥接时应先正常退出 C1，再用本项目启动器启动；仅打开普通 C1 快捷方式不会加载桥接。启动器默认不会关闭已运行的 C1；确需正常重启时显式传 `-Restart`。见 [安装说明](docs/INSTALL.md)。

安装器生成 `config/mcp-server.local.json`，把它加入支持本地 MCP 的 AI 客户端。核心字段是本机 Python 路径和 `-m c1ai mcp`。本版提供原六个工具及新增 `analyze_image`；旧 0.2.0a1 Release 为六个。客户端须支持图片块并使用视觉模型。见 [AI 接入](docs/AI_CLIENTS.md)。

## 一条编辑通道

```text
视觉 AI → MCP / CLI → 串行执行器与操作记录 → C1 内部命名管道
       → C1 原生参数／图层／蒙版对象 → 参数回读 + C1 原生预览
```

1. context 确认环境；read(fields=[]) 获取当前照片；preview(kind=native) 实际看图。
2. images(create_candidate) 创建并登记变体副本，保留原版本。
3. 按画面选择模块：read → adjust → preview，使用读取返回的目标和观察凭据。
4. 全局完成后再决定是否加局部蒙版；达到目标即停止。

参数使用绝对值，同模块相关参数合并提交。每次写入有唯一编号；超时先查询原编号，不重复派发。只有登记候选副本可写，参数回读不等于视觉效果合格。

## 包内文档

继续开发可直接克隆本仓库最新源码，先读 [开发 AI 入口](AGENTS.md)。计划开发 Mac 版时阅读 [macOS 开发交接](docs/MAC_PORT.md)；**这是迁移说明，当前版本仍只支持已验证的 Windows 环境**。

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

运行状态在当前用户 `%LOCALAPPDATA%\CaptureOneAI`，不进入仓库。分发包不含照片、预览、Session、私人配置、连接令牌或历史操作数据库。预览只保留最近 8 张缓存，不是永久成片导出。

[MIT License](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
