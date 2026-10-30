# AI 接入与调色流程

接口不绑定模型、账号或聊天。支持本机 MCP stdio 的客户端可以直接连接；能执行本机命令的 Agent 可以使用 JSON CLI。纯网页聊天需要自己的本地连接程序，本项目没有公网 HTTP 服务，MCP／owner 不保存云端模型密钥。

0.3.0a3 可选 [AI 修图窗口](AI_WINDOW.md) 是随项目提供的本地客户端，可自行填写 API、动态获取模型及查询思考等级，无需另一个聊天软件。该窗口可按用户选择加密保存自己的 API 密钥，仍复用同一 MCP 工具和 owner，不改变外部客户端接法。真实云端联调尚待有效密钥。

可直接复制 [给桌面 AI 的安装提示](AI_INSTALL_PROMPT.md)，其中包括申请安装窗口、桌面启动器和随 C1 联动的步骤。

## MCP

运行 Install.ps1 后使用 `config/mcp-server.local.json`。[示意配置](../config/mcp-server.example.json) 中的目录需要替换为安装目录。不同客户端外层配置格式可能不同，核心字段为 command、args、env。

上述安装入口用于 Windows。Mac 当前为源码开发版：使用已配置项目环境中的 Python 绝对路径作为 `command`，`args` 为 `["-m", "c1ai", "mcp"]`；CLI 使用同一个 Python 执行 `-m c1ai read --json '{"module":"exposure"}'`。不运行 Windows 安装脚本，正式 Mac 安装器尚未提供。

程序已安装到本地 Python 环境，可从其他工作目录启动，不依赖 cwd。七个工具为 context、images、read、adjust、preview、operations、analyze_image。视觉模型须收到 preview 的图片块；纯文本模型不能声称检查过画面。当前以标准 MCP SDK 客户端验证，没有逐个验收所有 AI 产品。

若工具仍显示旧定义（如 preview 只有 viewer/render、images 没有 variant_uuid），刷新 MCP 连接。默认使用 kind=native，无需为刷新工具重启 C1 或降级为截图。

## 给 AI 的操作说明

先读 context.workflow 和 read.guidance，再按以下流程操作：

1. 确认当前 Session、照片和未决操作，实际查看原生预览；依据画面和用户意图提出简短方案，不默认每张照片都需要提亮、增饱和或肤色均匀化。
2. 若目标不是登记候选，先创建副本。用户要求从头构思时从其选定原始版本复制，不叠加旧版风格；已有明确候选则继续使用，不为每次改参数再建副本。
3. 建议按白平衡、曝光、HDR、色阶、曲线、混色器、色轮处理，跳过无必要模块。read 后将 target、observation_token 原样用于 adjust。数值为绝对目标，曲线 points 替换完整通道。
4. 同模块多个技术调用可共用一次预览复核。保持 kind 和尺寸一致，检查人物层次、色相和明暗过渡；变亮或更饱和不自动等于更符合目标。
5. 全局后仅为明确局部问题加图层／蒙版。支持 mask 图片的平台先看覆盖，再看 native 效果。Mac 启用原生桥后也支持 mask 灰度回传和空层归一化 RAW 线性／径向创建；RAW map_points 未实现，不能直接把旋转后预览坐标当 RAW。选层后重新读取模块。
6. 超时／outcome_unknown 用原 request_id 查询 operations，禁止换编号重复。目标改变则重新读，不自动改用预设、UIA、截图识色或键鼠。
7. 达到目标后停止，说明实际结果和遗留问题，默认留在候选变体，不导出、不切文档。

这些原则也已内置到 MCP 初始化说明、context.workflow 和 read.guidance，无需另装专属技能。程序负责执行，审美由客户端视觉模型判断。

Mac 的普通调色、图层参数与原生蒙版命令优先走官方 Apple Events，不要求工具面板可见。Skin Tone 七字段、AI 组合、子蒙版控制和完整图层复制使用显式启用的可选 Accessibility 接口；数值控件／子项要求相关 Color Editor／Layers 工具可见，选择子项前需要展开父层。`layers.duplicate` 使用官方菜单，返回新引用后仍按普通局部层编辑；没有新增工具或专属 AI 调用方式。Pilot 预览结束后通过官方 tool tab.selected 恢复原工具页，已通过实机验证；不会新增工具页或寻找任意布局中的控件。中文与英文菜单是当前适配范围，具体操作仍以本机资格为准。

Mac 的 `read(module)` 同时返回 `module_capability`，MCP 与 CLI `--compact` 都保留它。`context` 中 `skin_availability=requires_module_read` 表示尚未检查：先 `read(color_mixer)`，查看本次 Skin 可用性及原因，再决定是否写入。已有 Skin 色区的保存态读取不等于当前 UI 控件可写。蒙版读取返回 `submask_inventory` 的原生子项索引，不按界面名称猜身份。`action_qualification` 区分已实现与代表性实机验证，不把一次减去背景验证扩大到所有组合。

Mac 完整 Color Editor 粘贴包括 Skin Tone，要求来源与目标均有完整保存快照，来源有九个显式 Basic 色区；它与只修改 Basic／Advanced 的模块重置不同。所有 AI 都使用同一套请求、观察凭据与操作记录，不需专属模型适配或另一个工具服务。

## CLI / Python

```powershell
.\Run.ps1 context --compact
.\Run.ps1 read --json '{"module":"exposure"}' --compact
.\Run.ps1 request --file request.json
```

完整 JSON 响应用于继续编辑；--compact 适合人查看，不保证保留所有后续调用字段。CLI 返回预览路径，Agent 需另行读取图片。Python 入口是 `from c1ai.client import call`，示例见 [API](API.md)。多个客户端共享一个 owner，但不应同时规划和编辑同一照片。

Mac 已有蒙版组合复用 `masks.combine {source_layer,mode}`：先选目标父层并重新 read(masks)，源必须来自同张候选、名称唯一、无 Luma 的支持简单蒙版；目标可为支持的平铺组合。`combine_ai` 是新生成 AI 子项，不能替代已有源层复制。两阶段非原子操作，结果未知时查询原 operation_id，不重发。Mac 图层排序复用 `layers.move {layer_ref,index}`，index 从底部局部层 0 起算；仅完整可见且子项折叠的图层列表，需显式 `mouse_enabled=true`，会激活 C1 并短暂移动鼠标。
