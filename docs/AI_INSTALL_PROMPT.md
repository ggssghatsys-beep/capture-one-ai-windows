# 交给桌面 AI 的安装提示

适用于能在本机读取文件、执行命令和配置本地 MCP 的桌面 AI。0.4.0a1 源码保留 Windows 16.8.6.3784，并新增实验性 Apple Silicon Mac 16.8.6.25 / Session；其他平台／构建不继承实测资格。旧 Windows Release 保持原样。

把下面文字和仓库地址或已解压目录一起交给 AI：

> 请帮我安装这个 Capture One AI 项目。先阅读 README、AGENTS.md，以及对应平台的 docs/INSTALL.md 或 docs/INSTALL_MAC.md；核对操作系统、Python 实际路径与架构、Capture One 完整版本和我的 Session，不编造路径或绕过版本检查。
>
> 安装前先问我：“是否创建桌面波形图入口（含直方图、RGB Parade 和矢量图）？”如果我已经明确说要或不要，就沿用我的选择，不重复询问。同意后安装所需窗口依赖并创建本平台入口；拒绝则不创建波形桌面图标。普通 MCP 安装不擅自增加云端账号或 API 客户端。
>
> 通过项目安装脚本生成 MCP 配置并接入当前桌面 AI，保留我的照片、现有配置和候选登记。安装验收只读 context、read、原生 preview 和 analyze_image，不为验证安装修改、克隆或导出照片，不强杀 C1。若系统要求调试授权或凭据，说明用途并让我完成；不要改变安全保护或授权组件。
>
> 如果我另外要求内置 API 修图窗口，再安装对应 AI 依赖。我会在本地窗口自行填写 API 地址和密钥，然后读取模型与思考等级；不要让我把密钥发到聊天里。真实云端模型/API 尚未联调，不能承诺所有兼容服务可用。
>
> 最后告诉我安装位置、MCP 接入结果、是否创建桌面入口，以及已验证和未验证的范围。升级时只处理本项目空闲窗口和 owner，不停止其他应用或 Python 进程。

## 安装 AI 的执行选择

| 平台／选择 | 执行方式 |
|---|---|
| Windows，MCP 基础安装 | `Install.ps1 -SessionPath '用户真实 Session 路径'`；已有配置用 `-EnvironmentOnly` |
| Windows，同意波形桌面入口 | 上述命令加 `-WithScopes`；只补入口用 `Setup-Shortcuts.ps1` |
| Windows，安装波形但不建波形图标 | `-WithScopes -NoScopesShortcut` |
| Windows，另要 API 窗口 | 加 `-WithAI`；只装 AI 不会创建缺少依赖的波形入口 |
| Mac，基础 MCP | 用已核对的 arm64 Python 3.11+ 执行 `scripts/install_macos.py` |
| Mac，同意波形桌面入口 | 加 `--desktop-scopes`；已含窗口依赖 |
| Mac，波形依赖但不建入口 | 加 `--with-scopes`，不加 `--desktop-scopes` |
| Mac，另要 API 窗口 | 加 `--with-ai` |

Mac 的桥须依据 [INSTALL_MAC](INSTALL_MAC.md) 本机构建／显式启用；环境安装器不会自动授权或启动 C1。桌面入口使用本项目 Python，不按系统“Python”应用名查找。Mac 打开后菜单栏常驻，关窗停读并隐藏，菜单内可退出；Windows 的联动开关仍只影响本项目启动器。两者均不增加开机常驻监控。

## AI 与人的共同分析入口

AI 用 `analyze_image()` 获取数值，无需先打开窗口。打开给人看的图表用 `analyze_image(action="open_window", view="histogram")`，它只表示已请求打开／重用窗口，不代表新图像已就绪。

窗口使用 `precision="display"` 快速样本。要分析窗口中的同一份快照，用详细面板的 analysis_id 调用 `analyze_image(action="get", analysis_id="...", precision="display")`；需要更细统计则另取默认 exact 样本，不能把快速编号升级为精确样本。检查 freshness、照片、色彩空间、裁剪和 render_basis 后再判断。

局部／肤色判断显式使用 `analysis_scope="active_layer"`，可叠加归一化 ROI；省略仍为整图。肤色参考线不是皮肤检测；空蒙版显示 empty，不回退整图。实际修改仍通过受候选保护的 read/adjust/preview。
