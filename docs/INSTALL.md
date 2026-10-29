# 安装、启动与排错

## 环境与安装

使用 64 位 Windows、Capture One 16.8.6.3784 和一个 Session。需要 Python 3.11+（实测 3.14.7）、PowerShell 7.6+（实测 7.6.5）、.NET 10 Windows Desktop Runtime。脚本在 pwsh 中执行，不适用于 Windows PowerShell 5.1。Release ZIP 带本项目当前两份桥接 DLL；从 Git 下载源码时，同一启动脚本可在本机编译。

解压到固定目录，不要在压缩包中运行。安装通过 pip 联网取得依赖，Python、PowerShell、.NET 和 Capture One 不随包分发；C1 须独立安装并有效授权。移动目录后重新安装以更新 MCP 路径。

```powershell
.\Install.ps1 -SessionPath 'D:\Photos\MySession\MySession.cosessiondb'
# Python 不在 PATH 时
.\Install.ps1 -Python 'C:\Python314\python.exe' -SessionPath 'D:\Photos\MySession\MySession.cosessiondb'
```

安装器建立本目录 .venv，安装程序，生成 config/mcp-server.local.json，并配置 `%LOCALAPPDATA%\CaptureOneAI\config.json`。已有等效配置保持不动；变更时备份本机配置，不删除操作记录、候选登记或令牌，不修改 C1 安装文件。

仅安装程序：`Install.ps1 -EnvironmentOnly`。之后用 `.venv\Scripts\python.exe scripts\configure.py --session '实际路径'` 配置；加 --dry-run 只查看计划。

## 启动与连接

### 直接填写 API 修图（0.3.0a3）

首次安装可选完整入口：

```powershell
.\Install.ps1 -WithAI -WithScopes -SessionPath 'D:\Photos\MySession\MySession.cosessiondb'
.\Start-CaptureOne.ps1
.\AI.ps1
```

已有 Session 配置时用 `Install.ps1 -WithAI -WithScopes -EnvironmentOnly`。桌面会有“Capture One AI 修图”，打开后填写 API 地址、密钥，从服务读取模型并查询思考等级，然后输入需求。内置窗口不需要再配置另一个 MCP 聊天客户端。模型必须支持 Responses API 的图片输入与工具调用；仅有 Chat Completions 的服务不适用。详见 [AI_WINDOW](AI_WINDOW.md)，其中明确列出真实云端联调尚待完成。

`-WithAI` 安装 API 客户端与 Qt；`-WithScopes` 另外安装波形计算依赖。只使用外部 MCP 时不需要这两个可选组件。API 密钥只填在本地窗口中，不粘贴进仓库或发给负责安装的 AI。图片预览和相关工具数据会发送至你填写的服务，API 费用／活动额度以服务方规则为准。

### 原生桥接与外部 MCP

需要人看的波形／直方图窗口时，安装加 `-WithScopes`，例如 `Install.ps1 -WithScopes -SessionPath 'D:\Photos\MySession\MySession.cosessiondb'`。已有配置只补窗口可用 `Install.ps1 -WithScopes -EnvironmentOnly`。该选项会启用随项目启动器打开窗口，并创建两个桌面快捷方式；仅安装 MCP 不需要 Qt/PyAV，也不创建图标。复制给桌面 AI 的操作提示见 [AI_INSTALL_PROMPT](AI_INSTALL_PROMPT.md)。

首次先保存并正常退出 C1，再执行：

```powershell
.\Start-CaptureOne.ps1
# 非默认安装位置
.\Start-CaptureOne.ps1 -CaptureOneExe 'D:\Apps\Capture One\CaptureOne.exe'
.\Run.ps1 context --compact
```

启动器只为本次 C1 进程设置 DOTNET_STARTUP_HOOKS，不改系统环境。C1 打开后进入配置的 Session、单选照片。日常也通过此启动器启动；已经普通启动的 C1 不会被自动重启，可先正常退出，或完成当前操作后显式传 -Restart 正常重启，不会强杀 C1。

把 config/mcp-server.local.json 加入 AI 客户端并刷新连接。owner 在首次请求自动启动，各客户端共用一个，无需分别启动服务。**原始照片的 writable=false 属于原图保护**；创建登记候选后才可修改。

## 配置变更与升级

owner 驻留，关闭聊天不一定关闭 owner。Session 配置在 owner 启动时加载，因此配置工具在 owner 运行且配置需要变更时会拒绝写入，防止表面切换成功、实际仍用旧 Session。

需要更改配置或安装另一代码版本时：先完成所有操作，关闭 AI 修图窗口和分析窗口，并断开 MCP 客户端；在任务管理器“详细信息”中显示“命令行”，核对完整程序路径，仅结束本安装目录中参数为 `-m c1ai owner --serve` 的 `python.exe` 或 `pythonw.exe` 后台进程。无法准确识别时，退出应用后注销再登录 Windows。不要结束其他 Python 进程。随后配置／安装并重新连接。未决写入先用 operations 查询，不能直接终止 owner。

从 0.3.0a1 升到 0.3.0a2：把新版解压到固定安装目录，按上述步骤退出旧辅助进程，执行 `Install.ps1 -WithScopes -EnvironmentOnly`，然后重连 MCP 并打开桌面分析入口。已有用户配置、候选登记、照片和 Session 保留，不要从其他用户复制状态目录。该版本未修改 C1 启动钩子或原生引擎，已正常连接的 C1 无需为此升级重启。

升级到 0.3.0a3 并启用 API：按上述步骤安装新版，执行 `Install.ps1 -WithAI -WithScopes -EnvironmentOnly`，打开 `AI.ps1` 或桌面“Capture One AI 修图”。本版也未修改原生引擎或启动钩子，C1 无需仅为本次升级重启。只继续使用现有 MCP 时可以不加 `-WithAI`。

引擎源码更新通过 internal/build.ps1 发布并校验，通常可以热加载；启动钩子改变才需要重启 C1。日常调参、切工具和刷新 MCP 都不需要重启。

## 排错

| 现象 | 处理 |
|---|---|
| SESSION_REQUIRED | 核对 .cosessiondb 路径及 C1 打开的文档 |
| 内部管道未连接 | 通过本项目启动器加载桥接，且只运行一个 C1 |
| 未验证版本 | 使用 16.8.6.3784，不要修改 qualified_version 绕过 |
| 未选图／多选 | 单选一张，或 images(list) 后按 UUID 选择 |
| 原图不可写 | images(create_candidate) 创建登记变体 |
| 目标／观察过期 | 重新 read，使用完整的新 target、observation_token |
| outcome_unknown／超时 | 查询原 request_id，不重复执行 |
| preview 只有 viewer/render | 刷新旧 MCP 定义，新版默认 native |
| 图片看不到 | 客户端需传图片块；CLI 需读取返回的 PNG |
| C1 弹窗／卡住 | 先读 context，再处理实际弹窗；不盲点确定，不自动降级键鼠 |

## 卸载与数据

C1、MCP、owner 退出后可移除程序目录。状态在 `%LOCALAPPDATA%\CaptureOneAI`，含操作记录、候选登记、令牌及最多 8 张预览；不会自动删除。重新安装不要清除未决操作记录，不要把该目录上传公开仓库。照片及调整始终由 C1 在用户 Session 中管理。
