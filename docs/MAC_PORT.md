# macOS 开发交接

更新：2026-10-29。**当前只有 Windows 实现；本文没有把任何 Mac 功能标为已完成。** 在装有 Capture One 的 Mac 上克隆此仓库即可接续开发，不需要迁移原开发者的聊天、照片或 Windows 运行状态。

## 目标与启动方式

保留统一 MCP / CLI 工具和模块调色流程，在同一仓库增加 macOS 执行层。让桌面视觉 AI 能读取用户选中照片、在候选副本上调整并取得 C1 当前渲染结果。Windows 版继续可用。

```sh
git clone https://github.com/ggssghatsys-beep/capture-one-ai-windows.git
cd capture-one-ai-windows
```

已有克隆先检查工作区，再拉取最新源码，不覆盖未提交修改。开发者自行建立工作分支；未取得推送授权时，只在本地提交或提供改动，不自动发布。

此时**不要在 Mac 上执行 Install.ps1、Start-CaptureOne.ps1，也不要把 python -m c1ai 能安装视为已能控制 C1**。当前 owner、client、宿主后端和安装入口均有 Windows 依赖。先读根目录 [AGENTS.md](../AGENTS.md)，再完成下面的环境核查。

## 先取得 Mac 本机事实

记录 Apple Silicon／Intel、macOS 版本、Capture One 完整版本和构建号、应用安装位置、当前 Session。Mac 构建号单独记录，不能沿用 Windows 的 16.8.6.3784。

通过 Capture One 的 Scripts → Open Scripting Dictionary 或系统 Script Editor 读取**本机已安装版本**的脚本字典，保存所用属性／命令的名称、类型、单位和可写性摘要。先做只读连通性探测；控制 C1 所需的系统自动化权限由用户按 macOS 提示授予，不规避权限检查。不要以默认 activate 命令争抢焦点。

如果当前执行环境只有 Windows，可以审查平台依赖和准备代码，但不能填入 Mac 实测通过，也不能发布带“Mac 已适配”的安装包。

## 已核实的研究起点

以下于 2026-10-29 查阅，后续开发仍要对照本机版本：

- **官方文档确认**：macOS 支持 AppleScript 和 JXA；16.8 增加 Basic／Advanced 色彩编辑器的脚本读写；16.8.2 增加主体、背景和人物蒙版。**Skin Tone 的逐项参数仍未通过该脚本接口开放**，文档仅确认可以整体复制 color editor settings。[Capture One 官方说明](https://support.captureone.com/hc/en-us/articles/360002681418-Capture-One-Workflow-Automation-with-AppleScript)
- **第三方作者声明**：[c1cmd](https://github.com/kiri11/c1cmd) 提供 Mac CLI／MCP，扩展曲线、图层和蒙版仍标为实验性，作者验证环境为 Apple Silicon + C1 16.8.5.30。它可以作为原生连接、属性映射和预览实现的参考；不能继承为本项目测试结果。其相机内嵌 JPEG 预览明确不反映 C1 当前编辑。[README](https://github.com/kiri11/c1cmd/blob/main/README.md)

优先审查这些现成实现，避免重复摸索；实际复用前固定提交、核对许可证并保留版权说明。只借鉴思想时不复制代码或提示词。

## 哪些代码复用，哪些需要适配

| 位置 | 当前职责 | Mac 迁移要点 |
|---|---|---|
| c1ai/mcp_server.py、__main__.py | 六工具和 CLI 请求 | 保留对外格式；检查间接导入能否在 Mac 加载 |
| c1ai/contracts.py、guidance.py、formatting.py | 请求校验、单位、AI 流程、结果输出 | 优先复用；Windows 得出的数值范围要与 Mac 字典核对，不能直接当作 Mac 证据 |
| c1ai/core.py | 统一分派、目标检查、写入记录 | 保留流程；检查隐含的 Windows 身份和证据字段假设 |
| c1ai/journal.py | SQLite 操作与观察记录 | 可复用主体逻辑；状态目录和 scope 的反斜杠／casefold 规则需平台化，Mac 路径不能无条件套 Windows 大小写语义 |
| c1ai/client.py、owner.py | 本机通信、单实例、后台进程 | 当前依赖 Win32 命名管道、SID、互斥锁和进程标志，需要 Mac 实现；不要让公共导入直接触发 Windows 专用调用 |
| c1ai/windows_backend.py、internal_transport.py | Windows 宿主读写入口 | 保留 Windows 路线；新增 Mac 后端并按平台选择，禁止把两套宿主逻辑混在每个参数分支里 |
| internal/StartupHook.cs、NativeEngine.cs | Windows .NET 内部桥 | 不直接移植或在 Mac 加载该 DLL；Mac 先走官方 Apple Events 自动化 |
| c1ai/native_*.py | 模块编码、图层、兼容路径 | 逐文件审查后选择性复用；含 Windows／持久化格式假设，不能整目录当跨平台代码 |
| c1ai/model_preview.py | Windows 内部渲染与状态绑定 | 复用结果语义和过期判断，Mac 图像来源单独接通 |
| Install.ps1、Start-CaptureOne.ps1、scripts/configure.py | Windows 安装与本机配置 | 保留；Mac 新增自己的安装／权限说明，不修改原 Windows 资格档案来假装跨平台 |

建议仍为 `MCP / CLI → 同一 Core → 平台后端 → C1`。通信和运行目录只做必要平台分离，不先设计通用插件系统。可评估当前用户专属 Unix domain socket；它是设计候选，不是已存在的实现。无论采用什么方式，保留单个串行写入者、当前用户访问限制、请求编号及结果查询。

## 实施顺序与验收

### 1. 先跑通一个真实闭环

读取 C1 与当前照片身份 → 读取曝光 → 创建候选变体 → 取得修改前预览 → 调整一个小幅曝光值 → 原生回读 → 取得并查看修改后预览 → 按先前读值恢复副本。

这些步骤全部使用当前用户选定 Session，原版本不写。请求超时先查询原编号；克隆结果未知时不能新编号再克隆。没有 C1 修改后的预览，就记录缺口，不能用原片缩略图替代后宣称闭环通过。

### 2. 沿同一接口接入调色模块

建议顺序为白平衡／曝光／HDR → 色阶／曲线 → Basic／Advanced → 色彩平衡 → 图层及主体／背景蒙版。每个已实现模块做一轮代表性真实读写、回读和预览检查，失败时针对修复复测，不默认穷举或全套回归。

每项在能力表中明确 readable、writable、适用版本和证据层级。未通过的功能明确拒绝，不让 AI 从 Windows 能力表推断 Mac 可用。选图、层选择及单位变化必须在适配层处理，外层仍使用同一种 read/adjust 格式。

### 3. 单独处理两个关键缺口

**Skin Tone**：先确认本机脚本字典是否已经增加字段；未增加时再研究其他原生对象或可靠现成实现。若只允许整体复制，要明确它不能替代 hue／saturation／uniformity 的逐项调整；不自动改成预设导入或界面拖动。

**快速预览**：先核实能否获得 C1 当前调整后的原生渲染或有明确版本关联的缓存。RAW 内嵌 JPEG 不含 C1 修改，已有缩略图也可能陈旧。必须绑定当前文档、变体和调整版本，并让旧预览失效。若最终只能通过导出取得正确图像，报告这个限制，按用户授权明确启用该路径；不冒充无导出的原生预览，也不默认每调一次就全尺寸导出。

### 4. 最后完成分发

在指定 Mac 上验证安装、自动化授权、MCP 图片块和真实修图闭环，再为该平台写入独立版本支持表。保留 Windows 支持声明；新增 Mac 条目须带实测版本与已知缺口。更新 README、API／支持文档和打包清单，分别标明“代码已实现”和“实机已通过”。

不要求换语言或依赖 c1cmd 才能完成移植；路线由最小实测和维护成本决定。也不要求一次把全部功能伪装成可用：先交付真实闭环，再沿同一路径补齐用户需要的模块。

## 交接记录应包含什么

每项只记录必要信息：机器架构／系统／C1 构建号、具体原生入口、读写和预览结果、是否用到前台、限制和对应提交。原照片、路径、令牌和操作数据库留在本机，不上传。导出耗时或预览速度只报告实际测量条件，不能复用 Windows 的性能数字。

当前进度：Windows 基线已发布；Mac 研究与迁移说明已准备；Mac 后端、安装程序和实机验证均未开始。下一位开发者的第一项任务是 **Mac 版本／字典核查和只读连通性**。

## 可直接交给 Mac 上开发 AI 的任务

> 克隆这个仓库的最新 main，先阅读 AGENTS.md 和 docs/MAC_PORT.md。保留 Windows 功能与现有六个工具，在这台 Mac 上开发原生后端。先读取本机 macOS、芯片、Capture One 完整版本和脚本字典，以用户选定照片的候选副本跑通读取、曝光修改、回读和实际预览，再沿同一接口扩展模块。读写优先原生，不默认使用预设或界面模拟；不要堆框架和无关测试。分别记录已实现、实机通过和待解决项。没有原生入口的功能明确报告，不声称已支持。
