# 独立波形窗口与 AI 图像分析

这是 0.3.0a1 新增功能，旧 0.2.0a1 Release 不包含它。沿用现有 Windows owner、连接、目标绑定和原生渲染接口；未另建调色服务，也未改变旧 `preview` 工具的含义。Windows 实测 C1 **16.8.6.3784 / Session**；Mac 取图适配尚未在本分支实现。

最新窗口采用一行工具栏＋图表，可缩小到 320×160；400×220 已检查。照片身份、ROI、详细数值、色彩假设移到“更多 → 区域、数值与状态”，主界面只用状态点及悬停提示提示等待／过期／失败。图表随窗口伸缩，统计范围和分析尺寸不随窗口变化。

色彩空间下拉框提供 **sRGB、Adobe RGB (1998)、Display P3**，并记住选择。`analyze_image` 同步支持 `color_space: srgb | adobe_rgb | display_p3`，缺省保持 sRGB；读取保存编号时保留该编号的原色彩空间，不能重解释。空间切换经 ImageCms 实际转换，不修改 C1 工作色彩设置。ICC 文件直接转换到目标空间，避免先缩进 sRGB；无源 ICC 时仍假定源为 sRGB，选择宽色域不能恢复预览中缺失的颜色。各空间按自己的 RGB-to-XYZ Y 权重和传递函数统计，跨空间拒绝直接比较参考差值。

Adobe RGB 使用 563/256 gamma；Display P3 使用 D65 白点及 sRGB 传递函数，不是影院 DCI-P3 或 HDR P3。ICC 参数配置由 Qt QColorSpace 的标准命名空间生成，随源码安装于 `c1ai/profiles/`，运行转换无需 Qt。定义参照 [Adobe RGB 官方规格](https://www.adobe.com/digitalimag/pdfs/AdobeRGB1998.pdf) 和 [W3C CSS Color 4](https://www.w3.org/TR/css-color-4/#predefined-display-p3)。主波形图为相应编码 RGB 加权的 Y′近似，AI 元数据给出具体权重；线性光 Y 另外返回。

最新分析来源改为 C1 原生位图的 **BGRA8 无压缩缓冲**，通过 `ToBitmapSourceDeepCopy`／`CopyPixels` 取得完整渲染像素，按图片和修订校验后传给共享分析核心，不经 PNG 编解码。内部使用有上限的 `.bgra` 本机缓存传递，尚非跨进程零拷贝。旧 `preview(kind=native)` 继续返回 PNG，独立缓存保证分析取图不会使该 PNG 的坐标映射编号失效。下文 PNG 格式记录是初始路线证据；新路线也仍为每通道 8 位、没有已确认的源 ICC。

## 启动

在项目根目录的 PowerShell 7 中运行：

```powershell
# 已安装的项目，补充窗口和绘图依赖；开发安装确保载入当前源码
.\.venv\Scripts\python.exe -m pip install -e '.[scopes]'
# 若还没有安装项目，先依照 README 运行 Install.ps1
.\Start-CaptureOne.ps1
.\Scopes.ps1
# 离线文件输入，不需要开启 C1
.\Scopes.ps1 -File 'D:\Photos\example.jpg'
```

已通过桥接启动 C1 时，不要重复重启。代码更新后重开波形窗口；若 owner 已驻留，需在没有编辑操作进行时重新加载 owner，不能结束正在执行的写入。刷新 AI 客户端的 MCP 连接以发现新增工具。

NumPy、Pillow/ImageCms 处理数值及色彩；PySide6-Essentials 提供窗口；PyAV 17.1 在进程内调用 FFmpeg waveform 滤镜。无需额外安装 FFmpeg 命令行程序。依赖由 pip 安装，项目不捆绑 Qt、FFmpeg、C1 或私人图像。纯数值接口不依赖 PySide6/PyAV；线条直方图只需 Pillow/NumPy。

窗口提供亮度波形、RGB Parade、线条直方图、暂停、刷新、固定参考和归一化 ROI。窗口大小只改变显示比例，分析尺寸不变。白色波形表示亮度信号；直方图四条线共用纵轴，显示高度为 `log10(1+9*count/peak)`，其中 peak 是四通道最大 bin 计数。对数输入只跨 1～10，避免像素数量越大压缩越强；零计数仍在基线，纵轴标签和 AI 返回值均为原始像素计数。另提供圆形矢量图与肤色参考箭头，见下文。

工具栏的“置顶：开／关”控制窗口保持在其他普通窗口上方，初次使用默认开启，之后记住选择。置顶不持续激活窗口或抢走 C1 的键盘焦点。刷新由一个单次定时器调度，避免重复安排重试；旧图仍被保留时复用已载入图像，不反复解码同一张波形 PNG。

## 数据源及验证边界

当前源码已有 `preview(kind=native)`：取得 C1 内部原生处理内核的完整渲染，而非旧版本的查看器截图。新增分析直接复用这一条路线，严格绑定实例、Session、照片、变体和图层，并检查渲染完成时的处理修订。`preview(kind=viewer)` 继续表示显示区域采样，没有被悄悄改名。

原生取图调用已有 `GetICProcessSettings`、`VariantGeometryTransformer.CropRawConstrained` 和 `ProcessToBitmapScheduledAsync`；C# 增加处理修订读取和原生 BGRA 缓冲回传，旧 PNG 预览保持兼容。输出由 C1 应用曝光、白平衡、曲线、局部调整及真实裁剪。查看器缩放／平移不是输入参数。

本机取得 **PNG / RGBA / 每通道 8 位 / 1067×1600 / 无嵌入 ICC**。分析固定长边不超过 1600，不放大小图。原生图像仍是预览，不是 RAW 像素，也不承诺更高位深。没有 ICC 时明确标注“假定 sRGB，色彩未独立确认”；带 ICC 的文件用 ImageCms 相对色度意图转到 sRGB。用于返回给 AI 的规范化 PNG 带 sRGB ICC，但原始色彩假设仍保留在元数据，不能由此冒充源图色彩已确认。

Capture Pilot 是另一条候选路线：根据 LiveLegacy 实现了诊断探针，但本机探测端口实际连接被拒绝，未作为生产数据源，也不宣称 Windows 支持 Live for Studio。现有原生内核路线已满足直接取图，不要求用户打开 HTTP 服务或反复导出。

最小探针（只读）保存图片和格式、尺寸、ICC、身份、修订与哈希：

```powershell
.\.venv\Scripts\python.exe scripts\probe_image_source.py --output artifacts\scopes\probe
# 仅在确认本机已开启 Capture Pilot 并知道真实端口时诊断
.\.venv\Scripts\python.exe scripts\probe_image_source.py --source pilot --port 12345 --output artifacts\scopes\pilot
```

输出可能含私人照片与本地路径；`artifacts/` 已忽略，禁止上传。Pilot 的矩形请求参数不是实际裁剪；新拍摄通知不是桌面选图；参数变化通知也不等于渲染完成。

## 给 AI 的直接数据

### 启动模式与桌面入口

`Install.ps1 -WithScopes` 安装窗口依赖、打开联动偏好并运行 `Setup-Shortcuts.ps1`，创建两个桌面快捷方式。已装 MCP 时可加 `-EnvironmentOnly` 保留会话配置；只补快捷方式可单独运行 `Setup-Shortcuts.ps1`。给 AI 的完整可复制提示见 [AI_INSTALL_PROMPT](AI_INSTALL_PROMPT.md)。

| 方式 | 行为 |
|---|---|
| Capture One + AI / Start-CaptureOne.ps1 | 启动 C1；自动联动开关打开时同时打开分析窗口。C1 已运行时不自动重启 |
| Capture One 图像分析 / Scopes.ps1 | 单独打开分析窗口，重复运行复用已有窗口；不启动或重启 C1 |
| 窗口 ⋯ → 随 C1 配套启动器打开 | 保存开关；仅项目启动器读取，原厂快捷方式不变 |
| Scopes.ps1 -Mode histogram | 打开直方图并记住选择；waveform 为亮度，parade 为 RGB |
| Start-CaptureOne.ps1 -NoScopes | 本次只启动 C1，不改联动偏好 |
| analyze_image(action="open_window", view="histogram") | AI 明确请求打开给人看的窗口；返回 open_requested，不冒充最新图像 |

关闭窗口即停止该窗口的轮询，不影响已有 MCP/owner。窗口不必常驻；随时从桌面重开。未选图、C1 尚未就绪或断连时显示状态，读取恢复后会继续更新。视图、色彩空间、置顶和联动开关保存在当前用户设置中；图像来源重新启动默认跟随 C1。没有开机常驻监视进程，也不会因普通 MCP 数值请求突然弹出窗口。

AI 无窗口用法：照常请求 `analyze_image()`。若要和人查看的结果严格对应，使用详细面板中的 analysis_id 调用 get，检查同一色彩空间和 freshness。打开窗口与读取分析是两个明确动作，窗口复用消息只接受视图及图像文件，不接受编辑命令。

新增 MCP 工具 `analyze_image`；CLI 的同名命令或 `c1ai.client.call` 共用同一个分析服务。以下为 Python/JSON CLI 请求，MCP 去掉 `command`：

```json
{"command":"analyze_image","include_histograms":true,"include_image":"waveform"}
```

```json
{"command":"analyze_image","roi":[0.2,0.1,0.8,0.9],"reference_id":"先前返回的分析编号"}
```

```json
{"command":"analyze_image","action":"get","analysis_id":"窗口显示的分析编号","include_image":"preview"}
```

动作：`refresh` 默认获取当前结果；`status` 只检查身份和修订，不重新渲染；`get` 取指定快照；`reference` 用 `analysis_id` 固定参考；`clear_reference` 解除固定。`source=file` 时须传 `path`。`include_image` 可取 `none`、`preview`、`waveform`、`parade`、`histogram`、`vectorscope`；默认不发送大图。`target` 可传已有 read 返回的完整目标，错配照旧拒绝。

返回内容包括：

- `analysis_id`、完整 `target`、照片标识 `photo_key`、数据源、采集时间、修订和完成状态。
- 原始文件哈希 `content_sha256`、规范化像素哈希 `pixel_sha256`、尺寸、位深、ICC 状态和裁剪依据。
- RGB、编码亮度和线性光亮度的均值、P1/P5/P50/P95/P99，以及接近黑端／白端比例。
- `horizontal_distribution`：沿 ROI 从左到右八等份（窄图不超过列数），每份带图像横坐标、像素数、RGB/亮度均值、P5/P50/P95 和端点比例。AI 无需看波形截图即可判断横向明暗分布。
- 按需返回四组 256-bin 直方图整数计数；不默认塞入逐像素数组。
- 与 `reference_id` 的差值，单位为**百分点，当前减参考**。同一照片、来源、色彩口径、分析尺寸、裁剪、ROI 和像素数才比较；可以比较同一照片的不同变体。

窗口与 MCP 共享同一个 owner 中的 `Pixels` 缓冲、分析快照和图片哈希。新增指标也必须从这一缓冲计算，返回单位、口径及数值，并继承身份、新鲜度、ROI 和参考比较约束；不能只画图或让 AI 从界面抄值。

紧凑窗口默认使用内部 `include_statistics=false` 请求，只计算图表所需数据，快照标记 `statistics_state=not_requested`。打开“区域、数值与状态”或 AI 用 `get` 请求该 `analysis_id` 时，从保留的同一份像素计算完整统计并缓存，标记 `statistics_state=ready`；参考差值也会补齐所需快照统计。MCP 默认始终返回完整数值，原接口不需修改。计算结束后重新核对修订，期间发生编辑的旧快照仍标为过期。关闭详细窗口后恢复轻量刷新；固定参考本身不会强制每帧计算数值差。

已确认 C1 内部存在 L/R/G/B 直方图数组类型，但尚未确认其缓存与本项目渲染帧、裁剪、色彩空间及修订的严格对应，所以目前没有用这些数组替换同源像素统计。普通直方图也不能提供波形横向位置及任意 ROI 的数值。

## 数学口径

亮度波形使用编码后的近似亮度 **Y′ = 0.2126 R′ + 0.7152 G′ + 0.0722 B′**。RGB 在 0–1 范围内；另提供逆 sRGB 传递函数后计算的线性光亮度 Y，二者不能混用。刻度为 **0–100% 信号**，不是视频 IRE。

接近黑端阈值为 ≤1%，接近白端为 ≥99%。分位数使用线性插值；直方图为 `min(255, floor(signal*256))`，共 256 bins。波形按横坐标及信号高度累积，保留图像位置，不把直方图画成波形；RGB Parade 对每个通道分别保留横坐标。绘图密度仅用于显示，不是像素占比的替代。

ROI 为图像归一化边界 `[left,top,right,bottom]`，不接受屏幕坐标；左上向下取整、右下向上取整，像素区间右下不包含。图像已裁剪时 ROI 就基于裁剪后图像。透明像素不计入数值统计；带非完全不透明像素的 ROI 暂不绘制波形，明确返回原因。

8 位量化、降采样和 JPEG 损失会影响精度。接近白端比例只描述这份预览，不能断言 RAW 高光不可恢复。

## 新鲜度、缓存和延迟

`freshness.state` 为 `ready`、`waiting`、`stale` 或 `failed`。收到事件、参数回读成功、拿到图片都不单独构成最新渲染证据；取图前后必须核对目标与处理修订，生成新波形之后再检查。切图或修改期间到达的旧请求不会被发布为当前结果。旧图可保留供查看，但标为过期；绘图组件故障与数值是否可用分开报告。

窗口一次只有一个请求，快速变化合并为下一次最新请求；空闲每 100ms 检查修订，同一修订命中后仅回小状态，不重复渲染。连续调整导致旧渲染失效或忙碌时按原生错误详情快速重试；真正断线时降低检查频率。固定分析尺寸独立于窗口大小。首次导入组件会慢于后续更新；C1 忙碌和复杂蒙版仍会增加延迟。

内存最多 12 个快照，包括一个固定参考；磁盘仅清理本模块 UUID 命名的生成图，跨重启也有上限。参考和 `analysis_id` 在 owner 重启后失效；重新固定即可。原生渲染缓存沿用已有 8 张上限。自动分析轮询不逐条写入调色操作日志。

## 公开项目调研与复用

| 项目 | 核验结果 | 本项目使用方式 |
|---|---|---|
| [ScopeWalker](https://github.com/ScopeWalker/ScopeWalker) | CC0；Win32/GDI，输入来自 BitBlt 屏幕截图，绘图与窗口状态耦合 | 界面思路参考，无源码复制 |
| [FFmpeg waveform](https://ffmpeg.org/ffmpeg-filters.html#waveform) | 保留横向位置的 waveform/parade 滤镜 | 通过 [PyAV Graph](https://pyav.basswood.io/docs/stable/api/filter.html) 在进程内调用，免除每帧启动程序 |
| [FilmCraft scopes](https://github.com/storytold/filmcraft/tree/main/crates/scopes) | MIT OR Apache-2.0；独立 Rust 数值核心及 agent summary | 借鉴“数值与显示分离”，不引入 Rust 构建链 |
| [Scopes++](https://github.com/MindStudioOfficial/scopes_plusplus) | GPL-3.0；C++/GPU/NDI 依赖较重 | 未采用 |
| [LiveLegacy](https://github.com/AlexMorrissey-Smith/LiveLegacy) | MIT；公开 Pilot HTTP 握手、会话、图像与通知客户端 | 仅实现诊断探针；未把 Mac 声明当成 Windows 验证 |

许可证与具体核验提交见根目录 THIRD_PARTY_NOTICES.md。上表中的项目没有复制源码或提示词；新增矢量图另复用 OpenRV Web 的 MIT 组件，见 SCOPES_REUSE.md。

## 给 Mac 同步开发者

新增模块为 `image_analysis.py`、`analysis_sources.py`、`analysis_service.py`、`scopes_window.py`。共享改动涉及 `core.py`、`contracts.py`、`mcp_server.py`、`__main__.py`、`guidance.py`、依赖和发布检查。Windows C# 增加 context 的 `process_revision`、原生 BGRA 缓冲及独立预览任务缓存。现有原生读写、preview 的 kind 和保护契约不变。

Mac 可复用数值核心和窗口；需把 `NativeSource` 的 Windows 内部渲染对接替换为经过实测的 Mac 来源，保持返回身份、修订、真实裁剪和完成证据。当前 owner IPC 是 Windows 实现，不能因窗口使用 Qt 就宣称本分支已支持 Mac。

## 矢量图与肤色参考

在工具栏选择“矢量图”，或运行 Scopes.ps1 -Mode vectorscope。复用 OpenRV Web 的 BT.601 形式 Cb/Cr 坐标、颜色靶标及 123° 参考方向，移植到现有 NumPy/Pillow 核心；完整比较与固定提交见 [SCOPES_REUSE](SCOPES_REUSE.md)，MIT 声明随包保留。窗口保持正方形比例，缩小窗口不会把圆拉成椭圆或改变角度。

横轴 Cb 向右，纵轴 Cr 向上。基于所选工作空间的编码 RGB 做色差投影，色彩空间转换仍由已有 ICC 入口负责。这不是视频合法范围检查或感知均匀色度图。统计网格 513×513、两轴范围 ±0.6，灰色精确在中心；网格量化步长为 0.6/256。所有分析像素参与计数，不另行随机抽点；非完全不透明像素排除。白点亮度压缩只影响显示。

肤色参考箭头在 sRGB 为 123°；Adobe RGB/P3 将相应 sRGB 参考色转换到所选空间后重新计算角度。它不是人脸／肤色检测，不是所有肤色必须对齐的目标。对肤色作判断时应使用图像 ROI；橙色物体也会落在参考方向附近。

AI 可请求 analyze_image(include_vectorscope=true)，或 include_image="vectorscope" 同时取得图及其数值。响应包含计数总量、近中性比例（半径 ≤0.02）、平均 Cb/Cr、平均色度半径、36 组每组 10° 的色相计数、色度加权方向、参考方向偏差和参考 ±10° 扇区内的非中性色像素比例。方向在中性／抵消时返回 null；扇区比例不是肤色占比。只返回摘要，不把 513² 密度矩阵塞入上下文。get、ROI、参考差值及新鲜度检查沿用同一服务。
