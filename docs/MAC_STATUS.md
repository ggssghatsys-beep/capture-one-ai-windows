# Mac 开发记录

## 菜单栏常驻入口与持续使用检查（2026-10-30）

用户已明确首次连接耗时不计入持续响应要求。当前本机持续切图请求至窗口更新代表值仍为 0.140～0.162s，50ms 检查等待与 C1 自身生成最新画面的时间另计；精确局部统计代表值约 0.883s，仍按需执行，不把它标为低于 0.5s。

已生成本机“应用程序”及桌面的 **Capture One 图像分析.app**，固定使用项目 Python 3.12。打开后菜单栏保留波形入口，可找回窗口／切四视图／退出；关窗隐藏并停读，保留用户暂停状态。一次性启动器与既有单窗口通道复用同一窗口，避免 LaunchServices 仅发送 reopen 而不重新显示的问题。没有新增框架、开机自启或独立 Mac 安装包。

实机 Cocoa 完成：隐藏后图标保留、计时器停止；实际打开桌面 .app 后同一进程重新显示并取得 fresh 结果；在读取线程未结束时请求退出，正常等候后返回 0。四种图表使用当前原生 RGB 样本完成只读图表检查，当前样本缓存上的单项图表请求约 0.011～0.050s；这不是新照片的完整采集延迟。

检查并修复一个持续使用 P2：旧详情／ROI 请求排队后切换设置或暂停，可能按新的 generation 接收旧结果；待处理项现绑定 generation，切换、隐藏和退出丢弃旧项。Mac Quit 也沿待读取结束的退出路径，避免销毁仍在运行的 QThread。49 项相关检查通过，AU 静态复审无剩余明确 P1／P2；实机范围如上。照片、参数与选择未改变；其余未完成项仍见下方功能记录。

## 原生 RGB 快速分析与窗口响应（2026-10-30，后续增量）

本机 Apple Silicon / macOS 15.7.4 / C1 16.8.6.25 / Session 已加载 build `d1be0e036ad69272`。分析改读无压缩 BGRA8，不再先生成照片 PNG 再解码。公共 `preview(kind=native)` 的 PNG 返回方式保持兼容。

- **窗口快速样本**：`precision=display`，最长边 640。优先读取 C1 当前完整裁切画面的原生 Viewer 位图；核对照片／图层 UUID、三项修订、任务完成、overlay 为零、完整裁切及原生缓存新鲜度，再由 CoreGraphics 按原 ICC 转成 sRGB。不会读取缩略图、截屏、导出或移动鼠标。只有缓存资格不满足时才走同一桥的新位图任务；未配置桥时此分析路径明确拒绝，不启动 Pilot。
- **精确样本**：AI 默认 `precision=exact`，最长边 1600，仍使用独立 C1 原生渲染；统计按需计算，窗口持续刷新不计算分位数和横向区域统计。两种精度有独立缓存和编号，读取快速编号不会变成精确样本。两者均为 8 位调整后像素，不是传感器 RAW 或全尺寸数据。
- **校样口径明确**：Viewer 缓存包含当前显示／校样效果，转成 sRGB 不能恢复校样前像素。返回 `sample_source`、`sample_limitation`、`viewer_stamp`、稳定 `render_basis`；Viewer 与独立渲染、不同 proof/display profile 拒绝直接参考差值。正常调整修订不会因此禁止比较。独立渲染的源色彩仍未独立标定。
- **减少等待**：原生目录事件取代固定 150ms 轮询；Mac 窗口只读请求使用独立有界执行队列，数值写入和精确请求仍串行。两队列复用同一分析实现和宿主锁；显示读取不保存 Session、不写编辑日志、不发写入凭据。窗口空闲检查 50ms。
- **实测持续更新**：真实 Cocoa 窗口切换两张照片后，新 analysis_id 的请求至控件更新分别为 **0.162s / 0.140s**，包含取样、分析、图表及窗口更新；原生 RGB 取样约 0.058～0.088s。此时间不含下一次 50ms 检查之前的等待及 C1 自己生成新画面的时间，不冒充显示器实际呈现测量。先前候选副本连续曝光／局部渐变测试已恢复，普通 RGB 与旧 PNG 在相同修订逐像素一致。
- **未满足的性能边界**：新 owner 首次开窗／重载后首帧本轮为 **0.642～0.737s**，包含首次加载／重连；AI 精确整图代表值 0.380s，精确局部加权代表值 0.883s。缓存不可用、C1 繁忙、首次连接和精确局部统计尚不能保证全程低于 0.5s。持续窗口两次代表测量已低于 0.3s，不将其推广为所有操作的保证。
- **进程问题已定位**：误用应用名启动会触发本机旧 Intel Python 3.6 的 SIGILL；项目固定使用 Apple 芯片 Python 3.12，正确运行进程未见对应崩溃。后续启动必须使用项目环境的明确路径，不能让 LaunchServices 按“Python”名称选择版本。

46 项最小相关检查通过；AU 复审覆盖比较口径、蒙版缩放前校验、跨队列显示读取、原生桥目标绑定和文件并发。实机额外发现跨进程通道短暂占用被误判为连接故障，现已改为未派发的忙状态并在 50ms 后重读，不重放写入。没有新增框架、修改厂商程序或重新实测 Windows；没有生成安装包、提交或推送。其余功能与缺口仍以下节及能力返回为准。

## 渐变再编辑、完整色彩编辑器与后台结构操作（2026-10-30，本轮）

启用匹配的原生桥后，沿用相同的 `read → adjust → preview`。本轮实测环境仍为 Apple Silicon / macOS 15.7.4 / Capture One 16.8.6.25 / Session。以下是本机代表性闭环，不是所有版本、几何或参数组合的穷举保证；下方旧记录保留其当时范围。

- **已有渐变直接改形状通过。** 对简单线性／径向重新读取 masks，再提交同一 action 的完整归一化 RAW 几何；使用现有 ImageCoreCommandManager、临时变更和撤销提交更新，保持原图层 UUID、调色和其他图层，无需删除重建。实测线性连续两次修改端点／中点，径向修改中心、长短轴、角度、falloff 和 inverted，保存参数逐 float 回读并实际查看整图效果。C1 每次重新计算可更新 RandSeed；图层选择点随新几何更新，除此以外完整保存参数保持不变。首次已有线性写后因旧保护不允许选择点更新而留为 unknown，修复后仅查询原编号、完整哈希核对后结案，没有重放。
- **Basic／Advanced／Skin 共用后台色彩对象路线。** 从原生 ColorCorrectionList 深拷贝现有对象，使用宿主 setter 提交；Basic 九扇区索引核对本机官方脚本实现，保留其 RGB 和范围。Advanced／Skin 支持增删、浮点 RGB、范围、启用／反选；Skin 七字段可直接修改。完整中性 reset 清零 Basic 调色值并移除 Advanced／Skin，不使用相机自定义默认。原生 detached 计划在首个 setter 前核对全部 18 列和请求的最终范围；随后同时核对完整保存值、其他调色及蒙版。精确区分未保存的 NULL、空值和损坏数据；初始无显式色彩设置也可新建，不额外做初始化写入。
- **色彩代表性实机通过。** Advanced 浮点 RGB 创建与反选、删除；Skin 创建、浮点 RGB／范围／七字段更新、删除／重建；Basic Image 层橙色区及局部红色区读写；局部 Skin 与填满蒙版的整图效果；Image／局部完整色彩 reset。完整重置和移除测试层后，原生 PNG 与基线逐字节一致。Skin 启用／反选字段已接入，但本轮未独立穷举所有标志组合。
- **结构动作不再依赖前台。** `layers.duplicate` 使用 C1 完整复制命令，`move` 按实时 UUID 清单排序；`masks.combine` 直接追加源蒙版，`combine_ai` 使用官方 AI manager 的子蒙版生成入口，`select_submask/delete_submask` 使用实时原生选择及删除方法。共用原 MaskCommand 回执和原编号结案，未新增另一套 AI 工具或恢复框架。保留未配置桥时原有显式 UI 兼容路线；配置变更后的旧令牌必须重读。
- **结构代表性实机通过。** 简单线性图层完整复制／指定名称、两层排序及恢复；线性来源 subtract 组合、子项 0→1 反复选择和删除（栅格幸存）；背景 AI subtract 组合，保存结构回读与实际整图预览。其他 add/intersect 和主体 AI 走同一已接代码，但不把这一代表操作称为所有模式本轮实测。
- **其他复测与审核。** HDR 固定中性值 reset／快照 paste 后恢复原值和画面。66 项相关检查通过；AU 已关闭图层结案模块形状、色彩旧令牌切换路线、范围越界及旧客户端说明错误承诺保留 Skin 的问题，当前无剩余明确 P0／P1／P2。审核独立隔离检查 24 项通过，不继承为宿主实测。

最终本地 build `522fc220a7eba29d` 已加载，源码、配置与运行宿主匹配。已选回本轮开始的原照片及 Image 层，原有蒙版、图层清单和调色与开始时逐项相同；各测试候选的自建层均移除。原生未决操作为零，Pilot mobile／web 均关闭。未生成安装包、提交或推送。

仍未完成：预览 `map_points`、含局部层源照片克隆、复合／含 Luma 父蒙版整体反选、任意嵌套组合和渐变向复杂非空蒙版追加。独立色彩精度标定、真实模型 API、其他 C1 版本／Intel／Catalog 未验证；Mac 安装包按用户要求暂不做。没有修改或重新实测 Windows 宿主。

## 主体／背景与父蒙版动作统一到桥（2026-10-30）

启用版本匹配的桥后，`masks.create_subject`、`create_background`、`invert`、`fill(density=1)`、`clear` 与线性／径向创建共用同一原生桥。对外仍是 `read(module="masks") → adjust(module="masks", patch=...) → preview`，没有新增 AI 专用工具，也不要求调用方区分主体、背景或渐变的反相入口。未配置桥时保留已有官方脚本路径；桥已配置但不可用时拒绝这五个动作，不暗中换路线。

主体／背景使用官方 AppleScript 背后的 `segmentationMaskingJobManager.createSubjectMaskAsNewLayerFor:`／`createBackgroundMaskAsNewLayerFor:`，参数为单一已绑定 variant 数组。保留 `CapabilitiesManager.CMFeatureAIMasking` 资格检查；不会绕过 C1 功能许可。静态确认高层 job 同步完成模型应用，非主线程分支使用主队列 `sync`；其内部负责进度、保存及撤销，不另套渐变事务。正常返回不等于创建成功：必须确认恰好一个新 UUID、原层／蒙版和全部保存调色参数不变。取消返回且无新层时，原编号仍需完整未改变证据才能结案。

简单父蒙版反相直接使用其根对象 `maskByInvertingMask:`，不经过可能隐式选中某个子项的菜单分派。清空使用根对象 `maskByClearingMask:`；填满先在返回对象上清空、填充，再一次提交，避免把中间空态写入图层。沿 C1 temporal／dirty／undo 提交路径，不擅自移除 AI 元数据。当前这三个动作要求选中完整父蒙版（子项索引 -1）。

**代表性实机通过：** 主体与背景创建、两者反相／再次反相、完整有效像素的 255-x 回读、整图局部曝光 +1 的实际查看；简单蒙版填满／清空、含 Luma 的复合父层填满；线性与旋转径向经相同 `invert` 动作反相并取得灰度预览，径向反相后的整图效果也已查看。填满的保存态为精确 255；原生 8 位灰度预览受渲染量化影响可出现 254～255，不把显示位图当作更高精度模型值。自建测试层已按创建记录移除，恢复后 PNG 哈希与测试前一致。

**没有宣称所有结构均可整层反相。** 本机 ImageCore 的 `CDrawBuf::Invert` 明确拒绝 Composite；其复合构建同时拒绝嵌套 Composite，因此不能保结构地用“满蒙版减原复合根”包装。官方 Invert 在复合父层选择状态下可能改的是一个子项。当前对复合／含 Luma 父层的整层反相在派发前明确拒绝，已实机核验；没有猜测内部 flags、偷偷选子项、栅格化或添加包装层。常量、简单栅格（包括当前主体／背景生成结果）、线性、径向使用同一个反相动作。其他蒙版动作仍按能力表选择已验证的官方或 AX 路径，不以“经过桥”代替功能验证。

AU 增量审核发现并关闭两项 P2：均匀灰反相改为精确 U8 补数比较，避免 85→170 被浮点误差拒绝；桥配置改变后旧观察令牌拒绝切换执行路线。24 项相关检查通过，最终审核无剩余明确 P0／P1／P2。取消、释放回复丢失后的原编号结案及错误证据拒绝经过隔离核查；新增 AI 无新层取消分支未刻意在宿主触发，不继承为取消实机验证。

最终源码重新编译、正常保存重启并加载通过；源码、配置与宿主的 build_id 一致。重启后公共读取、五动作能力、原照片整图预览通过，PNG 仍与基线逐字节相同，未决桥写入为零。已选回原照片，Pilot mobile／web 均关闭。未生成安装包、提交或推送。


## 公共原生桥与共享图像分析（2026-10-30，本轮）

**已接公共接口并通过代表性实机闭环。** 环境为 Apple Silicon、macOS 15.7.4、Capture One 16.8.6.25、Session；不是其他 C1 版本或安装包的兼容保证。此前各节保留为历史研究记录，本节覆盖原“桥尚未接公共接口／RAW 渐变和蒙版回传未完成”的状态。

- 原六工具不变。`read/adjust(module="masks")` 的 `linear_gradient`、`radial_gradient` 使用统一归一化 RAW 坐标，默认 `coordinate_space="raw"`；仅在登记候选的空调整层创建。线性端点／中点、径向中心／椭圆轴／角度／falloff／inverted 由本机原生 ImageCoreCommandManager 创建，沿原生临时变更、撤销提交和参数回读链执行，不移动鼠标、不切换工具。
- `preview(kind="native")` 直接返回 C1 当前整图 PNG；`kind="mask"` 返回当前整层合成灰度蒙版。两者使用同一原生几何、裁切、版本和照片／图层绑定。灰度使用 C1 的 mask display mode 1（overlayFlags 32）及本次 settings filler 的 mask handle；彩色透明蒙版 mode 2 不作为覆盖图。客户端另核验灰度、不透明和尺寸。
- 实机完成线性非默认中点 `.35`、径向旋转椭圆 `25°/falloff .6` 的创建、精确 float 参数／完整其他层保存态回读、局部曝光 +1、实际整图查看；径向反相、清空后的纯黑 mask 与分析 `empty` 通过。径向创建不会初始化图层选择标记，线性会初始化为中点句柄位置，检查已按真实行为收紧。曾出现回读检查失败的操作按原 operation_id 核对，没有重放写入。
- 标准 MCP SDK 实测七工具列表、native／mask／vectorscope 图片块；MCP/CLI 共用同一 owner。原六工具之外的 `analyze_image` 来自同仓库最新上游，不另建 Mac 分析协议。
- 已合入上游 `ced6af5d49a5b35a487d858d3cf6d68fa1cd75df`（0.3.0a3）的波形、RGB Parade、直方图、矢量图、ROI、参考帧、色彩空间和 AI 窗口。Mac 复用同一分析算法，以原生 PNG 接入；Windows 的 BGRA 快速路径保留。四种图表、Display P3 ROI、局部蒙版加权、参数修改后旧分析过期及新帧对比已通过；真实 Qt 窗口四视图均显示当前原生结果。详见 SCOPES_VALIDATION。
- AI 窗口的通用代码已同步；Mac 可选记住密钥改用系统 Keychain，Windows DPAPI 保留。真实模型/API 调用未验证，不把 UI 或离线检查算成模型联调。

桥是**针对已实测版本的 C1 内部接口**，不是官方公开插件 SDK。Frida 仅将自编译桥加载进已正常运行的 C1，随即分离；原生回调独立存活。默认不启用，不修改 C1 应用、签名、libCon 或系统保护。首次系统调试授权仍由用户按 macOS 提示完成。代码变更后需正常重启 C1 才能加载新 build；程序不会为更新桥自行重启用户宿主。启用桥后若加载／校验失败明确拒绝，不暗中降级为 Pilot、导出或鼠标。关闭桥时，既有显式 Pilot 路线仍可用。

开发机启用方法（已有 Python 环境和本机编译器）：

```sh
python -m pip install '.[mac,scopes]'
python scripts/build_macos_bridge.py --enable
python -m c1ai.scopes_window
```

编译器可通过 `--compiler /path/to/zig` 指定；本轮使用本机已有 Zig。构建与用户配置保存在本机私有状态目录，不随仓库提交。当前 Release 仍是 Windows 包，未制作 Mac 安装包。

**仍有限制：** Mac `map_points` 未实现，不能把旋转后的预览坐标直接当 RAW；只能在空调整层原生创建渐变，不覆盖已有复杂蒙版；极薄径向轴在派发前拒绝（最小一 RAW 像素、轴比至少 .01）。含局部层的源变体克隆仍会在派发前拒绝，未缩减复制保护来绕开；应先从合格源建立候选，再新增局部层。任意镜头／透视组合、其他宿主版本、Intel Mac 和 Windows 的本轮改动未实机复测。已有 viewer 鼠标线性路线仍为显式 partial 实验入口。


## 运行中桥加载、原生预览及线性创建通过原型闭环（2026-10-30）

**此前的加载阻塞已通过另一条正常调试路线解决。** 用户确认系统调试授权后，Frida 17.24.3 成功附加正在运行的本机 C1，读出正确 PID／架构及 64,606 个 ObjC 类，再正常断开。通过 `Module.load` 加载本项目的只读 dylib，`loaded`、`ready` 和只读 `inspect` 均返回同一实际宿主 PID；重启后再次加载也通过。没有修改 C1、libCon、签名或系统保护。此结论仅覆盖本机安装，不能继承为其他安装或官方签名版本的附加资格。

**原生模型读取实测：** 使用官方 `frida-objc-bridge@8.0.6`，在 C1 主线程读取 `CaptureOneDocument.currentDocument → primaryVariant → localAdjustmentLayers → mask`。文档路径、变体行 ID／UUID、照片路径与官方 Apple Events 对齐；两个已有线性蒙版的 UUID 和全部五个 float 参数与保存文件逐值相同，前后完整观察状态不变。真实图层是 `NSKVONotifying_POLocalAdjLayer`，以 `isKindOfClass` 验证派生关系，不把 KVO 子类误判为错误对象。内部层数组与脚本层列表的顺序不同，继续用 UUID 定位。

**只读渲染原型实测：** [NativeBridge.m](../internal/macos/NativeBridge.m) 使用 `genericSettingsFiller`、`ICPackageBitmapJob` 和 `ICPackageOperation`。修复了两个实际问题：本机 `Job.result` 直接返回 `NSImage`；`cropScale` 与 `cropScaleView` 分别生效，按 C1 自身 ImageConverter／PrintPreview 的设置令前者为 1、后者为尺寸比例。已生成并实际查看横图 1200×800 和竖图 800×1200 PNG，检查为 sRGB。原型不经过 Pilot、导出配方或缩略图缓存。切回另一照片后，旧 preview ID 被目标／计数检查拒绝。

**原生线性创建原型实测：** [GradientBridge.js](../internal/macos/GradientBridge.js) 在同实例官方登记候选的简单空调整层上调用现有原生链：`beginTemporalChanges → imageCoreCommandManager.linearGradientMask → maskByUpdatingOrAddingComponent → maskByRemovingAIMetadata → setMask → commitTemporalChangesWithUndoAction`。完成回调按真实类型编码处理结构体并切回主线程，重新核对对象和临时蒙版；保留宿主撤销／持久化路径，不直接写内存字段或侧车。此次请求的 RAW 五参数保存后完全相同，原 MaskUUID 和图层清单保留。完整保存参数摘要差异精确定位为 C1 把 `ZLAYERSELECTPOINT` 从空值初始化为图像中心；仅在比较副本中还原该字段后，完整摘要与写前摘要相等，验证其余已保存真实图层参数不变。

随后用公共接口把该局部层曝光设为 +1，桥内整图 PNG 明确呈现渐变提亮。用原创建回执移除本次测试层后，重新渲染的 PNG 与修改前逐字节相同；已恢复原照片，Pilot mobile/web 均关闭。测试中没有鼠标输入。异步操作完成、撤销作用域关闭、原生 manager 空闲后才释放对象、block 和 Frida session；没有未决写入或自动重放。

**实现边界：上述是开发原型通过，尚未接入公共六工具、自动加载或发布包。** 公共 preview 仍使用已验证的 Pilot 路线，公共 Viewer 渐变仍保留实验／partial 标记；不得把本节原型资格自动赋给旧入口。径向创建、一般已有蒙版替换和其他 C1 安装均未实测。Frida／ObjC 运行库仅安装于私有研究目录，未新增产品依赖或复制厂商组件。AU 分别审查了渲染修复／原子构建及原生线性草案，未发现明确 P1/P2；审查不代替上述实测。

另发现一个既有克隆限制：含局部层的源变体在 `snapshot()` 枚举 `layer.adjustments.properties()` 时触及仅背景可读属性，会在真正 clone 派发前拒绝。此次改用无局部层原照片创建测试候选并继续桥验证；该克隆问题尚未修复，不能声称已有局部层变体克隆通过。

## 桥加载定位与现成工具（2026-10-30，后续实测）

**以下记录启动阶段加载的失败定位；后续运行中 Frida 加载的成功结果见上节。** 本轮把启动环境、参数、目录及应用／探针哈希固定，启动前确认所有对应可执行文件进程连续消失五秒。无库基线持续运行，官方脚本回读原 Session 和原照片通过；只加入不使用 Objective-C 的标记库后，constructor 确实执行，同一进程约 0.28 秒后以退出码 0 结束，没有其他 C1 进程接手。因此该失败不是“找不到库”，也不是渲染或渐变 setter 已被调用后失败。

另一轮使用公开 `NSWorkspace.OpenConfiguration`，记录实际回调 PID，而非把启动器 PID 当宿主。基线正常打开并回读；极简库轮同样写出 constructor PID 后退出，完成回调没有可用宿主 PID。此入口不能提供宿主退出码，不把回调失败当成相同退出码的证明。基线随后正常 quit 超时；已保存且进入退出流程的实例用 SIGTERM 结束，之后恢复正常启动。没有同时打开多个实例占用 Session。

私有启动探针确认过本机安装环境中的附加库会主动退出；这属于安装环境差异，不能推广为官方发行版或许可证结论。公开代码采用正常启动后加载自建桥，不修改应用、签名、授权组件或系统保护。个人安装包来源、签名细节及原始回溯不随仓库发布。

公开实现选择：

| 来源 | 核实结果及采用方式 |
| --- | --- |
| [Apple NSWorkspace 环境配置](https://developer.apple.com/documentation/appkit/nsworkspace/openconfiguration/environment)、[新实例选项](https://developer.apple.com/documentation/appkit/nsworkspace/openconfiguration/createsnewapplicationinstance) | 新实例才接收指定环境；用公开启动完成回调核对真实宿主。已用于上述私有 A/B，不新增产品启动器。 |
| [frida-objc-bridge 固定源码](https://github.com/frida/frida-objc-bridge/tree/d980532e50c3e4a1c12c737c76fe76647fb94c2c) | 已有 ObjC 调用、主线程调度和实例查找；LGPL-2.0 WITH WxWindows-exception-3.1。可作为后续调试基础，未复制到产品。Frida 17 自建脚本须显式引入该桥，不能把 `ObjC` 未定义当作宿主没有原生类。 |
| [frida-tools 追踪器](https://github.com/frida/frida-tools/blob/0bbe5a6ade6f7b7a0c1b00b9f69e716fe63a3aed/agents/tracer/agent.ts)、[frida-python RPC 示例](https://github.com/frida/frida-python/blob/2316a9f36d53298b1ce711ad3a34bad7340e24a6/examples/rpc.py) | 已有方法发现、跟踪及双向调用，wxWindows 许可；不需另造 socket 或调试框架。当前未发现 C1 专用、已经完成渐变桥的公开实现。 |
| [Frida 官方 macOS 示例](https://frida.re/docs/examples/macos/) | 附加需要系统进程调试授权。私有虚拟环境安装 17.24.3 后，仅尝试读取进程身份和原生类数量，十秒取消，未获得 session 或身份回复；原 C1 保持正常。系统已启动授权组件，电脑操作工具明确拒绝操作该凭据窗口。不能记作“权限已拒绝”或“附加已失败证明不可行”；已结束旧目标的诊断 helper，等待用户完成必要授权后才可另做绑定新 PID 的验证。 |

这些工具只用于本机诊断，未增加项目运行依赖或发布文件。普通 Apple Events、AX 和 Pilot 路线继续独立工作。测试新增层已移除并恢复原照片；收尾公共曝光读取为 ok，Pilot mobile/web 均关闭。相关蒙版／图层／模块动作检查 26 项通过，语法及 diff 检查通过。实际记录保留本机私有目录，不提交照片、Session 路径或原始日志。

## 渐变创建与公开 SDK 核查（2026-10-30）

**已实现实验入口，创建和整图效果已实测，几何对应验证尚未完成：** `masks.linear_gradient` 保持统一动作，新增显式 `coordinate_space="viewer"`。默认仍是 RAW，Windows 既有原生几何调用不变；Windows 明确拒绝尚未实现的 viewer 域。Mac 在已登记候选的空调整层上，按 Fit 单图照片矩形内部的归一化起止点生成中点 .5 的参数线性渐变。读取返回可选 `gradient_view`，窗口／照片几何进入观察令牌；只在用户开启鼠标后备、当前键盘配置中无修饰 L 唯一绑定线性渐变时执行。该路线会短暂激活 C1、移动鼠标。不是精确 RAW 坐标设置，不提供自动屏幕／RAW 反算，也不改镜头或裁切。

AU 后续发现两个 P2：固定虚拟键码未限制键盘布局，已补 US／ABC 底层布局限制；仅验证非空参数渐变仍不能证明请求的 Viewer 位置，已将返回收紧为 partial、requested_result_verified=false、model_verified=false。此前成功回执仅作为创建事实，不再用作完整几何确认。AU 修复复核已关闭这两项 P2，未发现新增明确 P0/P1/P2；复核限代码与隔离检查，不继承为几何或桥实机验证。用户随后允许近似精度，但主线已切换为桥；后续仍需证明至少近似位置对应后才能恢复成功状态。

本机公共请求从 Viewer (0.25,0.25) 到 (0.75,0.75) 成功；完整原生回读确认同一空层变为 simple linear type3、midpoint=.5，原始其他层、参数和蒙版保持。返回实际 RAW 五参数与 Viewer 请求分别列明。局部曝光 +1 后已取得并查看 Pilot 整图，并关闭该测试层取得对照图后重新启用；确认画面变化。未使用导出、缩略图或侧车写入。当前不支持径向创建、任意中点、已有蒙版直接替换或精确 RAW 端点；不能把此代表性验证扩张为所有裁切／变形／键盘布局。

用户要求优先核查原生替代，四路只读研究包括官方、现成仓库、桥及公开 SDK。发现 [BTx123/capture-one-gif-plugin 的 SDK 完整文档](https://github.com/BTx123/capture-one-gif-plugin/tree/fa8b4fba08d57f8a56119f7be347d40251976916/sdk/Docs/html)和33个框架头文件，可公开阅读而无需先注册。固定提交 `fa8b4fba08d57f8a56119f7be347d40251976916`；[更新日志](https://github.com/BTx123/capture-one-gif-plugin/blob/fa8b4fba08d57f8a56119f7be347d40251976916/sdk/Changelog.md)为1.0.1（2022-03-14），不是当前最新版的证明。实际 [COVariantProcessing](https://github.com/BTx123/capture-one-gif-plugin/blob/fa8b4fba08d57f8a56119f7be347d40251976916/sdk/Library/Frameworks/CaptureOnePlugins.framework/Versions/A/Headers/COVariantProcessing.h)仅提供导出配方设置；COEditingPlugin/COFileHandling 输入输出文件路径。文档明确插件由独立 COPluginHost 托管，没有蒙版／图层模型及几何 API，不能把 .coplugin 包装当作进入主进程 MOVariant/IPMask 的桥。仓库无开放许可，厂商保留 SDK 版权；只阅读，没有复制或分发厂商代码。

另外核对 [LrGeniusAI 的 Mac 插件](https://github.com/LrGenius/LrGeniusAI/tree/45fabb9ebb1be3052e407b94dff62b741e4c11b0/capture-one)（AGPL-3.0，项目自述实验中止未实测）、[Sortiq 插件](https://github.com/AmARRsch/Sortiq-Engine-Pro-macOS/blob/c639541eb89a851ffa5f345acdf9c4ead1dcd82e/CaptureOnePlugin/SorterProCaptureOnePlugin/SorterProCaptureOnePlugin.swift)（无许可）和 [Herramientas-Capture-One](https://github.com/falevian/Herramientas-Capture-One/blob/5c1868543fbab9261f17fb923122ed8ad1189356/applescript/zonas_capture_one.applescript)（MIT）：实际为外部文件交接或导出分析，未发现可复用渐变创建桥。本机旧桥记录证明库已加载后宿主就绪前退出，极简库也重现。后续退出调用栈已定位到包内 libCon.dylib，见上节；仍不能由此推定许可证状态是触发条件。

[官方自动化说明](https://support.captureone.com/hc/en-us/articles/360002681418-Capture-One-Workflow-Automation-with-AppleScript)和本机 SDEF 仍无渐变几何 setter；Tangent 两个相关 Action 只是选择线性／径向工具。[官方线性创建说明](https://support.captureone.com/hc/en-us/articles/360002618377-Creating-a-Linear-Gradient-Mask)支持从 Viewer 起拖到释放的路线。上述结论限定已读公开资料，不能据此证明厂商私有方法或未取得的新 SDK 永远不支持。

## 图层排序接通（2026-10-30）

本机发现 AXFrontmost=true 并不能保证物理鼠标命中 C1：调用之间前面的其它窗口遮挡了图层。将正常激活 C1、系统坐标命中校验和拖动放在同一次执行后，原生列表排序成功。没有隐藏、最小化或控制其它应用。

公共 `layers.move {layer_ref,index}` 已接入，沿用从底部局部层起算的零基索引。只在显式启用 `mouse_enabled=true` 时使用 CGEvent；默认不启用，不修改系统授权。限完整可见、子项折叠的父层行；拖动前核对原生照片／层和完整保存快照，确认源点与落点命中同一 C1 Session 窗口。只派发一次拖动，未知结果不重发。官方介绍的排序操作即拖动图层：[Working with multiple Layers](https://support.captureone.com/hc/en-us/articles/360002615097-Working-with-multiple-Layers)。

**代表性实机通过：** 两个已有简单线性渐变层下移探针、公共客户端移回原序，以及最终代码再次公共下移／上移往返均通过，只有两层 ZINDEX 对调；原生 UUID、所有真实层完整保存调色设置及整个 COMASK 文件不变。已取得并查看恢复排序后的 Pilot 整图预览。不是所有布局／所有复杂层种类的穷举；渐变按 RAW 坐标自动创建仍未接通。

## 固定源码研究、Basic 批量读取与已有蒙版组合（2026-10-30）

本轮按用户要求直接核对 GitHub 实现，而非只采信 README。以下均固定到所列提交，已读对应 MIT 许可证；没有安装、运行第三方工具或复制源码。第三方实现与声明不计作本项目实机验证。

| 仓库与固定提交 | 具体实现核查与取舍 |
| --- | --- |
| `iscca/capture-one-ai-macos` · `5f37bb83afb5e4978760e7a27489b50e3747d3df` · [MIT](https://github.com/iscca/capture-one-ai-macos/blob/5f37bb83afb5e4978760e7a27489b50e3747d3df/LICENSE) | [来源声明](https://github.com/iscca/capture-one-ai-macos/blob/5f37bb83afb5e4978760e7a27489b50e3747d3df/THIRD_PARTY_NOTICES.md)明确沿用本项目上游公共核心，不能因 GitHub 非 fork 标记把它视为独立验证。[`_validate_tool_patch`](https://github.com/iscca/capture-one-ai-macos/blob/5f37bb83afb5e4978760e7a27489b50e3747d3df/c1ai/macos_tools.py#L89)禁用失败的 move／duplicate；渐变、combine、Skin 逐字段无可用入口。预览为授权 JPEG 导出，不符合本轮不导出的约束。[`rawMixer`](https://github.com/iscca/capture-one-ai-macos/blob/5f37bb83afb5e4978760e7a27489b50e3747d3df/c1ai/native/macos_tools.js#L52)提供四列批量读取的轻量参考。 |
| `guillaume-czerw/capture-one-mcp` · `cfb4e42b573c9a5d0c4c0c583aaf9ce43c6ebb18` · [MIT](https://github.com/guillaume-czerw/capture-one-mcp/blob/cfb4e42b573c9a5d0c4c0c583aaf9ce43c6ebb18/LICENSE) | 是 `byjustinjones/capture-one-mcp` 的 fork。[`layers.ts`](https://github.com/guillaume-czerw/capture-one-mcp/blob/cfb4e42b573c9a5d0c4c0c583aaf9ce43c6ebb18/src/co/layers.ts)封装已知官方层／蒙版命令，蒙版返回 accepted、verified:false，没有排序、任意组合或渐变创建实现。[`renderPreview`](https://github.com/guillaume-czerw/capture-one-mcp/blob/cfb4e42b573c9a5d0c4c0c583aaf9ce43c6ebb18/src/co/preview.ts#L50)仍使用临时配方导出。 |
| `kiri11/c1cmd` · `94d8ba71cb2bfb9e24acbb9c5a0c0c6b2d5b62dd` · [MIT](https://github.com/kiri11/c1cmd/blob/94d8ba71cb2bfb9e24acbb9c5a0c0c6b2d5b62dd/LICENSE) | [`nativeAction`](https://github.com/kiri11/c1cmd/blob/94d8ba71cb2bfb9e24acbb9c5a0c0c6b2d5b62dd/Sources/CaptureOneCore/Resources/NativeActions.applescript#L2)没有新的排序／组合／渐变创建命令。[编译脚本缓存](https://github.com/kiri11/c1cmd/blob/94d8ba71cb2bfb9e24acbb9c5a0c0c6b2d5b62dd/Sources/CaptureOneCore/AppleScriptExecutor.swift#L114)和[浏览缓存](https://github.com/kiri11/c1cmd/blob/94d8ba71cb2bfb9e24acbb9c5a0c0c6b2d5b62dd/Sources/CaptureOneCore/ReadWorkflow.swift#L4)有实际实现，但需要更多架构改动，本轮不引入；浏览缓存本身也不用于写入或恢复。 |

**Basic 读取优化已实现并实机核对：** 仅将共享 `macos_adjustments.js` 的 Basic 九色读取从逐色属性请求改为 name／hue／saturation／lightness 四列 Apple Events，校验四列长度为九、扇区名称完整唯一、所有数值有限，保留 pink→magenta 映射和原单位。写入、Advanced、目标与完整旧值检查不变，Windows 未改。参考批量读取思路自行实现，未移植第三方的层名标识或 null 转零逻辑。语法及针对性离线检查通过；同一登记候选局部层的完整 color_mixer state 和 target 在旧、新读取间相等；收尾还核对了 Image 层完整读取结果等价。局部层单次对照旧版 2.5217 秒、新版 1.6132 秒。此数字不是多次基准，也不承诺所有照片或模块有相同比例的提速。

**已有蒙版组合新增受限产品路线：** 保持公共 `masks.combine {source_layer,mode}`，区别于生成新 AI 子项的 `combine_ai`。通过官方 Layer → Add／Subtract／Intersect → Brush 建立目标子项，再用 Copy Mask From 将已有来源复制到已验证的子项；仍是显式授权的 AX 官方菜单调用，不是新增脚本几何 setter。完整原生 UUID 清单、实时子项选择、原有蒙版、保存调色参数及来源选择点均参与核对。来源必须在当前变体内、名称唯一且是支持的简单栅格／线性／径向结构；不接受嵌套来源或 Luma 结构，也不把菜单文字当稳定身份。

公共 Add／Subtract／Intersect 三种路径均已在登记候选的独立临时复制层上通过客户端调用、完整原生结构与参数回读，并取得和查看 Pilot 整图预览；源线性渐变及旧子项保持，新增子项分别保存模式 0／5／2，测试复制层按各自创建记录删除。实测来源是简单线性渐变，不继承为栅格／径向等每一种来源均写测通过。此前 `combine_ai` 三种背景组合的验证不替代这项已有来源复制路线。另在同一复制父层上连续 Add→选子项 0→选整个父层→Subtract→再次选子项／父层均通过，旧组件完整保留，并已查看最终整图预览及删除测试层。上述三个公开仓库未提供可直接复用的成熟层排序或渐变创建方案，相关缺口继续保留；本节组合进展更新下方历史记录中的“已有蒙版组合尚未实现”，不扩大为任意复杂组合均受支持。

本轮最终 29 项相关测试通过。AU 两轮共发现四处保护问题（最终原生核对顺序、组合阶段 COMASK 基线、同窗口误命中、鼠标释放分配失败），均已修复并复核；最终无剩余明确 P0／P1／P2，审核含静态与隔离失败路径验证。测试候选完整保存调色参数、整个蒙版文件及原始层序均已与测试前逐项比较相同。临时复制层均删除，已恢复原照片 Image 层并确认 Pilot mobile／web 均关闭。

## 完整图层复制与本轮收尾（2026-10-30）

新增 Mac `layers.duplicate`，复用 Windows 已有的公共动作与六工具，不另建复制接口。使用 [C1 官方图层菜单](https://support.captureone.com/hc/en-us/articles/360002601658-Overview-of-Layers-and-Masks)的 Duplicate Selected Layer，经已获授权的直接 AX 调用；可选名称由官方 Apple Events 改名。复制的是 C1 完整层对象和动态蒙版，不是逐项重建公开参数，不用鼠标拖动、不写 Session／COMASK。能力要求显式启用 AX；菜单目前适配中英文。

**已实现且代表性实机通过：** 在 C1 16.8.6.25 登记候选上复制已有简单线性渐变层，完整保存调色设置、原生层属性及蒙版摘要匹配；只有新层名称、UUID、位置和必要元数据所属层引用变化，所有原层保持不变。指定名称和默认名称均通过。单独启用源层与复制层时 Pilot 整图 JPEG 逐字节相同且已查看；独立修改复制层曝光／透明度、回读和整图变化通过，源层曝光保持。使用复制返回的观察令牌直接修改透明度也通过。按各自创建 operation_id 删除复制层通过。此实测不扩张为所有蒙版类型或所有隐藏参数组合的穷举。

为保持旧观察保护，Mac layers 的观察令牌绑定完整保存参数和共享蒙版文件摘要；后续读取隐藏曲线／Skin 等变化不会覆盖旧令牌的基线。派发前仅生成一份完整快照并沿用到最后核对。AU 找到的两处 P2（令牌漏绑隐藏内容、二次读取静默替换基线）已修复，最终静态及隔离复核未发现剩余 P0／P1／P2。新增两个针对性测试，27 项相关图层／蒙版／模块动作／导航／批量测试通过。额外运行历史 Core／formatting 套件为 50 通过、31 失败；已对照仓库基线确认主要是 FakeBackend 缺少现行 set_values，以及旧用例仍预期已禁用 restore/recover。没有为旧测试恢复被禁用语义，也不声称全套回归通过；Windows 未实机复测。

**按用户要求暂放的项：** 自动渐变创建、图层排序、任意已有蒙版组合，仍没有通过本项目稳定性核对的可调用入口。渐变已在人工界面测试中创建成功，官方 readout 能提供屏幕点击到原生位置的准确证据，但无鼠标的进程事件尝试未改变蒙版，不能标为客户端创建已实现。样式研究确认局部 apply style 仅应用调色，完整样式的渐变还需原生坐标转换；没有现成可靠模板，停止手造。旧“删除后折叠为线性渐变”请求缺少隐藏 SQL 原值，只读证据不足以解释差异，仍保留未知，不重放、不放松检查。

本轮临时复制层和未使用空层已清理；保留可复用的测试渐变，局部曝光归零。未修改原 RAW、未导出、未安装、未打包或推送。最后恢复原照片 Image 层并核对 Pilot 关闭；私人记录与图片留在本机。


更新：2026-10-30。源码基线：`185f8cddc410980f3c821d88b5f39ac7c9f6c6af`，开发分支 `mac/native-backend`。现有发布包仍为 Windows 版。

**Mac 已实测官方候选创建、Image 层七个调色模块、当前集合分页／前后选图，以及两个候选的串行批量修改和恢复。Capture Pilot 已扩展到相邻不同 RAW，每次只共享当前 RAW 及其变体；曝光／曲线重置与快照粘贴、跨照片曝光粘贴及 MCP JPEG 图片闭环通过。局部七个调色模块、图层启用／透明度、定位／改名、受限蒙版命令和多层整图 Pilot 已接通；局部 reset/paste 已进一步接入，可选 AX 背景组合、子项定位／删除及局部 Skin 色相已通过代表性产品实机验证；渐变创建与图层重排仍未完成。** 原 RAW 和原版本未写，未导出，未用缩略图代替当前渲染。Windows 功能与六个统一工具保留。

## 已有线性渐变反相与剩余入口复核（2026-10-30）

已有简单线性渐变的官方 `invert mask` 已完成产品闭环：登记候选读取 → 反相 → 原生参数回读 → Pilot 整图 → 再次反相 → 回读和整图恢复。端点交换、中点互补、目标层 UUID、其他层与完整公开层态及蒙版结构保护均通过；恢复 JPEG 与本轮起始 JPEG 逐字节一致，反相图不同且已查看。调用仍为 `read(masks)` → `adjust(masks, {action: invert})`，没有新增工具或渐变专用写入通道。径向渐变反相仍待实机，不继承线性验证。

实测修复：C1 渲染渐变后会附加 `RandSeed`，旧预检把它当作未知结构拒绝。现在仅接受已观测的字段形状，并保留整个字段和摘要参与前后比较；随机种子变化或其他未知字段仍拒绝。两项相关测试通过，包含种子保持、种子变化及未知字段边界，不增加测试框架。

本轮通过官方 `copy mask` 将同 RAW 的已有渐变复制到独立登记候选用于验证；复制保留动态渐变，但这不是按 RAW 坐标创建渐变。此跨变体复制仅为受保护的本机测试准备，不扩大公共 copy 的范围。

剩余三项仍未开放：

- 渐变创建：[官方线性渐变说明](https://support.captureone.com/hc/en-us/articles/360002618377-Creating-a-Linear-Gradient-Mask)与[径向渐变说明](https://support.captureone.com/hc/en-us/articles/360002619037-Creating-a-Radial-Gradient-Mask)仍以查看器拖动为入口；脚本无几何 setter，屏幕坐标不能冒充现有 RAW 坐标契约。
- 图层排序：本机脚本对象未提供有效层重排实现，AX 图层行未暴露移动动作；拖放走进程内部排序方法，未建立稳定产品入口。
- 已有蒙版组合：[官方 Combine Masks](https://support.captureone.com/hc/en-us/articles/31021514753053-Combine-Masks)限定同一层。实机查看已有子项菜单并静态核查后，Add／Subtract／Intersect 都是创建新子项，不是改变现有子项模式；子项配置按钮仅打开 Luma Range。任意源层组合、既有子项模式修改仍不支持。现有 `combine_ai` 的主体／背景创建组合范围不变。

再次阅读 [c1cmd 原生编辑接口](https://github.com/kiri11/c1cmd/blob/main/docs/reference/native-editing.md)、[capture-one-mcp 图层说明](https://github.com/byjustinjones/capture-one-mcp/blob/main/docs/LAYERS.md)及 [Walter Rowe 脚本集](https://github.com/walterrowe/capture-one-scripts)，未找到这三项可直接复用的公开实现；没有安装依赖或复制第三方代码。没有引入注入桥、侧车写入或删除重建的替代方案。

AU 本轮最终静态／离线复核未发现剩余 P0／P1／P2。共享反相校验器补齐已有公共派发／回查路径的中性根约束；两次真实反相保存记录经最终校验器离线重核通过，无宿主重发。两项相关测试、编译、105 个公开文件隐私与本地链接检查通过，未在 Windows 实机复测。测试层曝光显式归零（不声称恢复原生 null），保留渐变供后续复用；选回原照片 Image 层，Pilot mobile/web 关闭且无活动共享记录。临时 AX 授权仍为上轮记录状态，关闭仍需本人系统认证。

## 直接 AX 实机与接口精简增量（2026-10-30）

继续保留六工具及官方 Apple Events 主路径。缺失的 Skin 数值和蒙版组合仅用可选 macOS Accessibility，不注入 C1，不写 Session／COMASK，不增加按 AI 厂商区分的服务。

- Skin Tone：现有单色域的七字段读取与保存值一致；局部 hue 修改、回读、Pilot 整图查看及恢复通过。恢复后的完整 Color Editor 保存摘要与 JPEG 都等于本轮基线。不是所有七字段／全局 Skin 写入穷举，也不包含自动取色建色域。
- 蒙版：主体层添加背景、减去背景、与背景相交，均由客户端单次操作官方菜单后核对保存结构、父层身份和非目标参数；子项 1→父层→0→1 的反复定位与背景子项删除通过。三种背景组合均取得并查看 Pilot 整图，添加背景表现为更大范围提亮，相交后的主体提亮显著减少。菜单临时打开以刷新 AppKit 状态，关闭后再原生核对，最后执行一次命令，避免菜单打开阻塞 Apple Events。
- 定位只读取当前工具区域的有限 AX 层级，兼容固定＋滚动工具区；真实行文本来自单个直接单元格，按完整原生 UUID 顺序交叉核对。仅适配已核实中英文菜单；工具须可见、子项须展开，其他工作区／语言不猜测点击。
- Pilot 预览清理新增官方 `tool tab.selected` 原工具页恢复：首次读取并记录稳定 ID，结束共享、清理临时相册和恢复浏览后按 ID 恢复并回读；本机从调整页预览后仍在调整页，mobile/web 均关闭，整图回传通过。旧清理记录无该字段时兼容跳过，不新增工具页或改工具展开状态。
- 精简中间重复观察和 AX 检查、同路径 COMASK 哈希及参数快照里不使用的文件读取。保留首次令牌、派发前目标／完整保存状态和写后验证，无缓存框架。实测一次 add 请求连同事前 read 为 16.48 秒，仅代表本机该照片与该操作。
- `read` 新增本次 `module_capability`，精简输出保留能力和 `submask_inventory`。Skin 未探测标 `requires_module_read`，不把 context 尚未检查当作不支持。MCP、CLI 和 AI 指引同步，Windows 请求语义保留。
- 早期一次组合派发后 AX 超时，原完整参数基线不足，保留原未知记录，未重放或伪造成功。保存并正常重启 C1 后，在新登记副本完成上述验证；新操作令牌绑定完整参数摘要，原请求的只读回查可以核对原基线。现存旧记录不补造证据。

补充边界：删除空刷子、让组合折叠为单个线性渐变时，保存结构验证通过但完整参数保护发现额外变化；原请求保留未知，未重放。现在删除后只剩非栅格参数蒙版会在派发前拒绝，已验证的主体／背景栅格存活路径保留。已有参数型渐变反相的代码和离线验证完成，但本轮未得到完整产品读写／预览闭环，继续标待实机；不把人工绘制探针或代码编译当作通过。

AU 最终静态与隔离审核未发现剩余明确 P0／P1／P2；未在 Windows 实机复测。

本轮验证与收尾：26 项相关现有测试通过（过期测试中 recover 名称按当前公开 reconcile 契约修正）；另有 7 项现有 Pilot 检查通过，编译、105 个公开文件清单／隐私及本地文档链接检查通过。恢复开始时选中的原照片 Image 层；未导出、未写原 RAW、未生成安装包或推送。本机临时为运行 Python 与负责启动它的桌面应用授予辅助功能；实测后尝试撤销时系统要求本人密码，已取消认证提示，因此这两项临时授权仍需本人关闭，未改动既有 Computer Use 权限。

渐变创建／RAW 坐标映射、图层重排仍无可靠产品入口：官方文档仍以查看器或图层列表拖动为入口，本机脚本字典不提供相应 setter；通用 move 不等于图层排序。已有参数型渐变的官方反相已接入，具体实机范围另记。不要把前台人工画出渐变的探针当作统一 RAW 坐标接口。

## 蒙版扩展与完整色彩编辑器增量（2026-10-30）

本轮继续使用官方脚本读写，在新登记的测试副本验证结构操作；前一轮副本的局部参数验证结束后已恢复。没有安装、导出或修改原 RAW。

- 局部 HDR／色阶／色彩平衡 reset，白平衡／HDR／色阶／色彩平衡 paste，Basic/Advanced reset 已完成代表性原生回读与 Pilot 整图检查。本轮重置后 JPEG 与本轮基线逐字节相同；色轮粘贴有原生浮点舍入，不把所有 paste 宣称为逐字节恢复。
- 已接入官方 `clear luma range`、`feather mask`、`refine mask` 和自建层删除。普通 simple raster 反相增加了只读有效像素解码和双向 `255-x` 哈希检查，未知编码拒绝写入；组合／参数型蒙版不能借此取得反相资格。羽化 0～100，精细化大于 0 且不超过 300。过滤命令成功、保存像素有效和其他层不变是证据边界，不声称独立重建了厂商算法或回读了不存在的 amount 属性。
- 完整 Color Editor paste 使用官方 `color editor settings` 赋值；完整保存原文用于保护 Basic／Advanced／Skin Tone。实测发现隐式 Basic 来源在粘贴后会生成显式中性记录，因此限制未确认的保存表示，首次结果保留未知而不重发。
- Skin Tone 的七个数值控件、保存字段及单位已核对；测试副本经 CUA 原生界面取色、色相／饱和度／均匀度修改可保存。它仍不等于产品 Skin Tone 自动写入接口实机通过。用户已同意采用可选直接 AX 后备；本项目运行程序尚无系统辅助功能权限，未修改系统授权。产品路径须单独完成实机验证，不将其冒充官方脚本 API。

本轮新增实机结果：官方自建层删除、移除唯一 Luma、普通主体栅格反相通过。反相往返的 Pilot JPEG 与本轮反相基线逐字节一致。主体层 feather=20、refine=20 经产品请求、完整像素有效性／非目标保护及整图 Pilot 查看通过；不是算法复算。完整 Color Editor paste 在 Image→Image、Image→局部层两条路径通过完整保存值回读与 Pilot 前后查看，包含 Skin Tone；来源仍须九个显式 Basic 扇区。首次隐式来源粘贴的未知记录保留，未重放；该测试层随后以明确的自建层删除操作移除，不伪造原请求成功。

已实现、待产品实机验证：直接 AX 的 Skin Tone 七字段更新、`combine_ai`（Subject/Background 的 add/subtract/intersect）、`select_submask`、`delete_submask`。UI 人工探针已经确认主体减背景、定位背景子项、删除该子项并保留父 UUID；读取 COMASK 发现 `SelectedSubmaskIndex` 不能代表实时选择，改为实时 AX 证据。保存结构的 CompParam 按本机 ABI 解码布尔字节，忽略其后三个填充字节，保留模式和 flags，避免把未初始化填充误当语义变化。实际项目 AX 尚因系统授权缺失未执行成功，因此能力表仍为 pending_live。

渐变与排序仍未完成产品接口：CUA 在测试副本前台绘制线性渐变，原生保存 type=3 和整图效果已确认；这不能证明 Windows 接口要求的 RAW 坐标。官方文档以查看器拖动为入口，裁切、镜头校正与缩放等变换尚无精确映射，未以屏幕比例代替 RAW 坐标。径向渐变创建、图层重排和任意源层组合也未开放。没有更改现有六工具或 Windows 坐标语义。

AU 增量审核发现并修复两项 P1：未决异步 AI 仍可能读取活动层，所以同实例／Session 内切图、切层与子项选择必须等待原操作解决；AX 控件查找后、实际写入前必须重新核对原生照片／图层和保存状态。复核无剩余明确 P0/P1/P2。10 项模块动作检查通过，AX/backend 相关离线检查及编译通过；未在 Windows 实机复测。105 个公开文件清单／隐私检查及本地文档链接检查通过；未生成发布包、提交或推送。已恢复本轮开始时所选原照片的 Image 层，Pilot mobile/web 均关闭，活动共享记录已清理。以下旧轮次记录保留其当时范围，以本节和当前能力表为准。

## 局部重置与模块粘贴增量（2026-10-30）

局部层 `reset`／`paste` 已复用现有官方参数写入路径，统一工具和 patch 格式不变。没有使用剪贴板、预设文件、整图重置或内部桥。

- 重置为既定固定中性值：曝光／HDR、曲线、色阶、色彩平衡及 Basic/Advanced；白平衡重置仍拒绝。归零不能恢复原生 null，色彩编辑器重置不处理 Skin Tone。
- 粘贴支持曝光、白平衡、HDR、曲线、色阶和色彩平衡，来源可以是此前读取的 Image 层或局部层，同一实例／Session／处理引擎限定保留。复制的是模块参数快照，不复制蒙版、图层透明度或启用状态，也不会重新读取来源后悄悄更换原快照。
- 数值来源存在 null 时，整个模块在首次写入前拒绝；不丢弃空字段，也不替换成 0。目标模块之外原有 null 可保持不变，回读不再把它传给数值比较函数。
- 结构重置／粘贴跳过已与快照相同的曲线／色阶通道，Basic 重置跳过中性扇区。完全无变化时仍执行一次现有原生完整状态核对，不能仅凭旧观察返回成功。
- 所有操作保留目标层身份和每一步完整模块回读；多参数动作非事务，未知结果不换编号重放。具体动作的代表性实机资格见能力表，不继承普通字段写入资格。

本轮实机：主体层曝光四字段和 RGB 曲线修改后，调用模块 reset，Pilot JPEG 与本轮基线逐字节一致；再用此前观察令牌 paste，两模块的回读确认通过，JPEG 与原修改图逐字节一致。另将主体层曲线快照粘贴到背景蒙版层，确认当前目的层参数、其他层不变及整图背景变化，再重置该层。最终主体和背景曲线、主体曝光均恢复，未复制或修改蒙版。该逐字节一致仅指本轮曝光／曲线闭环，不能扩大为上一轮白平衡／色轮的完整恢复。

能力表将局部 exposure／curves 的 reset 和 paste 标为代表性实测通过；其余局部模块动作已经接入，但仍标 implemented_pending_live，不继承普通参数写入的实测资格。白平衡 reset 和完整 Color Editor paste 仍不支持。23 项针对性检查通过（新增两项空值边界检查），99 文件源码／隐私与本地链接检查通过；AU 最终复核无剩余明确 P0/P1/P2。最后完整层清单／十字段状态与本轮基线一致，选回开始时的候选，临时共享与相册均清理；Windows 执行路径未改，未在 Windows 实机复测。

蒙版组合进一步核查了[官方菜单与快捷键说明](https://support.captureone.com/hc/en-us/articles/31021514753053-Combine-Masks)及本机菜单资源，找到 Layer → Add／Subtract／Intersect → Subject／Background。静态核查显示官方 set current layer 会清除子蒙版选择，组合挂到父层；但 AI 处理在挂载时仍读取 active layer，首次组合还可能自动改父层／原子蒙版名称。已知菜单模式到内部 composition 的静态映射不能当作实机标定。此路线必须明确为 UI 辅助后备、限制处理中切层，并核对所有层及 COMASK 内容；本轮未自动调用组合菜单或开放子蒙版组合，未把它冒充官方脚本写接口。

## 官方局部调色路线（2026-10-30）

再次核对本机 SDEF、[官方脚本说明](https://support.captureone.com/hc/en-us/articles/360002681418-Capture-One-Workflow-Automation-with-AppleScript)和公开工具后，选择继续使用官方 Apple Events。`layer.adjustments` 与 Image 层的 `variant.adjustments` 都是同一 `adjustment settings`；官方示例直接修改局部 contrast／brightness，也说明每层 Basic／Advanced 编辑器可用。因此局部调色不需要等待内部渲染桥。

- 参数和层属性通过官方 Apple Events 读写；同一小型 JXA 实现供全局／局部曲线、色阶、Basic／Advanced、色彩平衡复用。局部写入每一步仍核对整个层清单、真实 MaskUUID、已观察参数和目标模块。
- Session 只读用于补足官方缺失的持久层身份，COMASK 只读用于核查蒙版保存内容。二者是版本限定的内部读取，不冒充官方 API，也不写这些文件。实际整图仍使用已经授权的 Capture Pilot。
- 局部数值扩展到曝光四项、白平衡两项和 HDR 四项。HDR 使用公开 `highlight adjustment`。局部原生空值保持 null，结构模块在数学转换前拒绝不可读值，避免 null 被当作 0。
- 局部白平衡首次写入会初始化整组白平衡；这是本机原生实现的只读静态证据。实际 getter 可返回当前色温／色偏。两项必须一起观察；任一为空时派发前拒绝，不猜测初始化值。写入之后同时核对两项及其他层。
- 图层启用和透明度使用官方属性；透明度限定官方描述的整数 1～100，关闭用 enabled=false。该轮尚未开放局部模块 reset／paste，后续进展见上节。
- 官方字典没有子蒙版列表、稳定子蒙版标识或 Add／Subtract／Intersect 的结构化脚本接口。[官方 Combine Masks](https://support.captureone.com/hc/en-us/articles/31021514753053-Combine-Masks)提供界面与快捷键路线，但本轮没有把它包装成已验证的任意组合接口。[SDK 开发入口](https://www.captureone.com/en/partnerships/developer)也不足以证明存在更直接的图层编辑 API，故未为此引入插件或额外桥。

本轮在同一登记候选的主体蒙版层完成以下实际请求与原生回读，并逐模块查看 1200×800 Pilot 整图：

| 局部功能 | 实机范围 |
|---|---|
| 曝光／WB／HDR | 十字段代表值写入；WB 成对观察，初次色温写入出现约 0.000003 的色偏舍入变化，已按既定温度／色偏容差处理，仅允许组内该量级变化 |
| 曲线／色阶 | RGB 中间点新增及黑白点修改；其他通道读取通过，未逐通道写测 |
| Basic／Advanced | 红色 Basic H/S；Advanced 色域新增、同色域重复修改及移除；未逐个颜色和参数穷举 |
| 色彩平衡 | 默认布局中间调 x/y/lightness；其余轮读取通过，未逐轮写测 |
| 图层属性 | 透明度 100→50，启用→关闭；关闭后的整图 JPEG 与本轮初始图逐字节一致 |

整图预览反映多模块在主体层累积的变化。首次 WB 请求在色温已经生效后被旧的严格相等检查停止，未重复发送；显式恢复原观察值后，按原编号只读核对当前模块已回到原值，再进行新代表值验证。该 `not_applied` 对应当前状态，不否认中间曾发生修改。所有写入仍不承诺事务回滚。

本轮恢复通过：Advanced 新增色域已移除，Basic、RGB 曲线／色阶恢复先前值，WB 写回先前观察值，曝光／HDR 的未设置项显式归零，图层恢复启用和 100% 透明度。色轮回中心后仍有约 0.0017 的坐标半径残差，在既定 0.003 容差内；最终预览视觉回到基线，但 JPEG 非逐字节相同，RGB 平均像素差均低于 0.002／255。未把数值归零说成恢复 null，也未声称完整底层模型／像素完全恢复。其他三个层的完整可见参数及所有层身份与本轮初始记录一致。

AU 代码审核发现并修复一个 P2：局部结构模块需返回 helper 已校验结果，不能用额外未比较的重读替换后直接确认成功。修复后未发现剩余明确 P0／P1／P2；WB 组内容差和非目标层保护经独立离线核查。21 项现有针对性测试、Python/JXA 编译、99 文件公开清单与隐私检查通过；没有增加测试框架，未在 Windows 实机重测。 最终复核修正旧能力标签和 SUPPORT 的过期范围说明，跨层往返与 Image 层四个共享结构模块读取通过；选回开始时的候选，Pilot mobile/web 均关闭，临时相册和 active receipt 均已清理。未导出照片、生成安装包、提交或推送。

## 蒙版与多层整图增量（2026-10-29）

正常使用以 **选图层 → 调整 → 看整张合成照片** 为准。灰度蒙版是开发时核查覆盖的辅助步骤，不要求每次调色单独看蒙版，也不把界面截图当成产品预览。

- `read(masks)` 按真实 MaskUUID 返回保存结构、Luma 参数、引用压缩块内容摘要。只读内部 COMASK v3，不写数据库／侧车；不是官方公开读取 API。对空栅格、ConstantFill 端点和恒定栅格严格确认有效覆盖；仅有限解码边界块以排除图像外补齐像素，不能宣称一般蒙版位图已开放。
- `create_empty`、`create_subject`、`create_background`、`fill(density=1)`、`clear` 已通过产品请求、原生回读和实际覆盖检查。`invert` 只资格化已严格确认的空／满蒙版；空→满的首次结果因边界压缩块未解码而保留未知，补齐读取后按原编号只读 reconcile 确认，没有重放；再反相回空通过。
- 官方 `copy mask` 会替换目的 MaskUUID。现在核对原目的位置、名称及自身调整不变，其他层／源蒙版不变，新目的内容摘要等于原观察中的源内容，返回新引用；不宣称有稳定宿主对象 ID。首个主体复制按原请求只读核对完成，随后背景复制直接 `control_confirmed`。旧引用必须重读。
- Luma 七字段设置与重复修改通过；衰减边界先展开、范围端点再展开，之后设置目标值。旧顺序在重复修改时被 C1 拒绝，完整状态不变已按原编号只读确认为未应用，再修正顺序验证。**本机 16.8.6 首写会新增 Add 亮度子蒙版，不是自动与主体相交**；参数回读与结构、实际覆盖分别核查。全范围数值不等于移除 Luma。一般反相、任意组合、子蒙版定位、渐变、羽化／精细化仍未开放，不能从官方命令存在推断已经资格化。
- C1 在蒙版／Pilot 操作中重建 `ZVARIANTLAYER` 行，故层行号不是稳定身份。引用改用原生 MaskUUID 无损编码；Pilot 保存态摘要把层角色／UUID与实际参数、元数据内容绑定，排除存储行号重分配。Pilot 标识的 UUID 是 `tempUUID`，与持久变体 UUID 分开验证；没有把二者强行相等。
- 多层 Pilot 整图回传已通过。主体层曝光 +0.6 后实际整图仅主体提亮，再置 0，恢复 JPEG 与基线逐字节一致；其余三个局部曝光字段仍为 null。原本 exposure=null 最后为显式 0，这是视觉中性恢复，不是完整模型逐字节恢复。仍不导出、不使用缩略图，临时共享按现有范围启停。

早期采用层行号的空层创建请求保留未知记录；实际观察到新层已存在，没有重放或伪造旧引用恢复。后续验证另使用正常登记候选，私人过程记录不入仓库。

官方资料与公开工具核查：本机 SDEF 提供 fill／clear／invert／copy／feather／refine 和 Luma 七字段；[官方灰度查看说明](https://support.captureone.com/hc/en-us/articles/360002623258-Displaying-masked-areas)仅用于覆盖诊断，[官方 Luma 说明](https://support.captureone.com/hc/en-us/articles/360002622897-Editing-the-Luma-Range-Mask)解释边缘与组合；[Luma Layers 固定源码](https://github.com/MathieuBourlion/luma-layers/blob/b9eb48bdf32d59b18c05cab19a5f69091a91301a/LumaLayers/ScriptRunner.swift)参考端点写入顺序，未复制代码。官方另有 [PSD／PSB Alpha 蒙版导出](https://support.captureone.com/hc/en-us/articles/32398712990237-Export-masks-from-Capture-One-to-Photoshop)，本次未采用。两个研究代理分别负责原生格式／公开资料与 Pilot 绑定，实机写入由主代理统一执行。

## 环境与入口

- Apple Silicon / arm64，macOS 15.7.4 (24G517)。Capture One 16.8.6.25，安装于 `/Applications/Capture One.app`。
- 本机脚本字典 SHA-256：`ff9eb4287c01780cdb722e2a6e4a3f27797f27418a366cc864ae9b142a7c7f28`。
- `CLI/MCP → 当前用户 Unix socket → 同一 Core → MacOSBackend → 官方 Apple Events/JXA`。曲线点和 Advanced 色域的创建使用同一官方接口的 AppleScript 容器语法；没有预设导入、数据库／侧车写入或界面拖动调参。
- 目标绑定进程 PID／启动时间、Session 原生 ID／文件路径、image／variant 原生脚本 ID、RAW 路径及 Image 层。脚本 ID 是文档／进程作用域内的数字字符串，**不是 UUID**。Session 文件路径由官方 `folder` 加 `name` 取得。
- 写入仅限当前 C1 进程内登记的候选，重启后不自动继承旧登记。目录权限 0700、socket 0600，校验本机用户和服务端 PID，单 owner 串行执行。

## 实现与实机结果

| 功能 | 代码状态 | 本机验证范围 |
|---|---|---|
| 版本、字典、目标、参数读取 | 已实现 | 通过；Session、单选、Image 层 |
| 官方克隆、选择、状态核对、登记 | 已实现 | 修复后实测通过 |
| 曝光／对比度／亮度／饱和度 | 已实现 | 四字段代表值读写、回读、C1 查看器检查、恢复 |
| 白平衡 | 已实现 | 色温和色调代表值闭环通过 |
| HDR | 已实现 | 高光／阴影／白色／黑色代表值闭环通过 |
| 曲线 | 五通道已实现 | 五通道读取；RGB 增加中间点、查看画面、恢复通过；非 RGB 写入未逐个实测 |
| 色阶 | 四通道已实现 | 四通道读取；RGB 黑白点修改、查看画面、恢复通过；端点移动使用不交叉顺序 |
| Basic 色彩编辑器 | 九个颜色区域已实现 | 原生 pink 对应统一接口 magenta，代表性 H/S 修改、查看画面、完整恢复通过 |
| Advanced 色彩编辑器 | 新增／修改／移除、现有 RGB 起点、范围和启用状态已实现 | 现有色域 RGB 修改、完整公开状态回读、Pilot 画面检查及移除恢复通过；反选仍未开放 |
| 色彩平衡 | 四轮已实现，仅默认 red-east 布局 | 四轮读取；中间调 x/y/亮度代表值修改、查看画面、容差恢复通过；其余轮写入未逐轮实测 |
| 当前集合清单和分页 | 已实现 | 两页不重复、集合令牌一致；完整范围是当前集合，不是所有 Session 文件 |
| select／next／previous | 已实现，支持从局部层切图 | 官方 next 切到另一张 RAW、读取、Pilot 预览、previous 返回原候选通过；另实测选中主体层时 next/previous 往返和同编号不重放 |
| 批量调色 | 已实现，1～16 条显式候选请求 | 同一照片的两个登记变体分别改曝光、串行切换及恢复通过；不同 RAW 和满 16 条未实测 |
| MCP 六工具、CLI、本机通信 | 已实现 | MCP 握手、六工具发现、batch 参数发现、结构模块读取、操作查询通过；新增真实 MCP 曝光修改／恢复及图片块闭环 |
| 当前渲染图片回传 | Capture Pilot，须显式配置 | 已批准当前集合内相邻不同 RAW，逐次只共享所选 RAW；1200×800 JPEG、MCP 图片块、临时相册清理和浏览位置恢复通过 |
| 模块快照粘贴 | 七个调色模块已实现，含完整 Color Editor | 曝光跨 RAW、曲线及局部各模块代表性粘贴通过；完整编辑器 Image→Image／局部通过，来源需九个显式 Basic 扇区；要求同实例、Session、处理引擎 |
| 模块重置 | 曝光、HDR、曲线、色阶、色彩平衡及 Basic/Advanced 的固定中性值已实现 | 实测整图重置校准副本，确认上述默认值；曝光和曲线产品重置／粘贴恢复的 MCP 闭环通过；Basic/Advanced 的变更、重置、公开状态恢复与 Pilot 画面通过；局部 HDR／色阶／色彩平衡代表性重置通过，未穷举所有字段 |
| 图层与亮度范围只读清单 | 已接入 context | 单 Image 层、Image＋主体层读取通过；局部未设置值保留 null，位置仅显示，不生成伪 layer_ref |
| AI 主体／背景蒙版、空层创建 | 已接入统一产品接口 | 新层 UUID、类型、保存蒙版结构、主体／背景灰度覆盖均通过 |
| 填满／清空、复制、受限反相、亮度范围 | 已实现 | 全白／全黑有效覆盖严格回读；复制内容与源一致且返回新引用；普通栅格反相往返通过；Luma 七字段与 Add 子蒙版结构通过 |
| 多层清单／select／rename 与局部七模块 | 已实现受限 Session 接口 | 两个局部层 A→B→A、改成同名后按真实标识定位、曝光读写和恢复、C1 查看器检查通过；本轮七模块主体层代表值见上节，同编号不重放 |
| 完整图层 duplicate | 可选 AX 官方菜单，已实现 | 简单线性渐变层完整参数／蒙版复制、默认及指定名称、独立编辑、源／复制层整图等价和按创建记录删除通过 |
| AI 组合与子项控制 | 可选 AX 已实现 | 背景加／减／相交及子项反复选择、删除通过；不是任意源层组合 |
| Skin Tone 数值 | 可选 AX，已有单色域七字段 | 七字段读取、局部 hue 修改／回读／整图预览／恢复通过，其他字段未逐项写测 |
| 羽化／完善 | 官方命令已实现 | 普通主体栅格 amount=20、完整保存证据和整图预览通过 |
| 参数型渐变反相 | 官方命令与保存转换校验已接入 | 已有简单线性渐变代表性回读／Pilot 往返通过；径向及非端点 ConstantFill 待实机；不支持创建渐变 |
| 任意源层组合、渐变创建／RAW 坐标映射、图层排序 | 未实现 | 缺少可靠原生写入口，明确拒绝 |
| Catalog、多选、正式 Mac 安装器 | 未实现 | 开发入口使用 Python 虚拟环境 |

“通过”只指上述代表性操作，不能扩大为所有量程、相机格式、通道或任意编辑的像素完全恢复。七个调色模块早期画面检查使用 C1 查看器；本轮实际查看了 Pilot 返回的相邻照片、修改后和恢复后图片。曝光／曲线复用的最终 JPEG 与本轮基线逐字节一致，不把它写成所有模块重新实测。

## 数值和操作边界

- 参数为绝对目标值。曝光 EV、色温 K；HDR 高光使用 signed `highlight adjustment`，避免方向相反的旧 `highlight recovery`。
- 曲线点规范化到 0～1；色阶端点 0～255。结构模块每一步写入前后比较完整已观察模块，只更新该步请求字段，不吞掉未请求的并发修改。Apple Events 不提供多字段事务，使用时避免同时手动调参。
- Basic 直接按原生集合索引读取并核对唯一颜色名称；不能保留 JXA 的惰性对象数组后当作值快照。Advanced RGB 原点以官方 RGB 属性一次写入并回读；Advanced inverted 未开放；Skin Tone 七字段使用可选 AX，代表性资格见本页增量。
- Mac `reset` 明确为固定中性值，不冒充用户／相机自定义默认值：曝光和 HDR 为 0，五条曲线为 `(0,0)→(1,1)`，色阶输入／输出 0 和 255、midtone 0，色轮 x/y/lightness 0。白平衡重置拒绝；色彩编辑器仅将 Basic H/S/L 归零并移除 Advanced 色域，不处理 Skin Tone，响应明确标注 basic_advanced_only。`context.capabilities.module_actions` 给出各模块边界和实测状态；Windows 默认值重置语义不变。
- 重置入口核查：原生 UI 的 `resetToDefaults` 按钮已找到并在候选上完成曝光归零；这是 CUA 辅助实测，未接入产品 UI 自动点击。AppleScript 对参数、adjustments 对象、tool 对象调用 `reset` 均返回 -1708；JXA 工具调用也失败，没有采用整图重置代替产品模块重置。用户授权后另克隆校准副本，用官方 `reset adjustments` 整图重置并读取默认值，确认上述中性值，不将该样本的白平衡写成通用默认值。[官方重置说明](https://support.captureone.com/hc/en-us/articles/360002481077-Resetting-adjustments)
- Advanced 饱和度范围是相对原点的百分比偏移，必须使绝对饱和度仍在 0～100。早期过宽请求被 C1 截断后守卫停止，操作保留未知结果；已增加派发前验证并用合法范围复测。替换 RGB 时旧范围还必须在 RGB 分量变化的中间状态保持合法，否则派发前拒绝，须先收窄范围；不假设一个属性事件提供原子更新。新增／移除／重置未知结果会阻止同候选的后续结构动作，不能换编号重复。
- 色彩平衡将官方 H/S 转为半径 50 的 x/y。只接受已核实默认布局，其他布局拒绝。实测回到中心仍有约 0.0017 的坐标舍入残差，接口容差 0.003；这是规范坐标容差恢复，**不是原生 hue 字段或完整底层模型精确恢复**。
- 集合令牌包含实例、文档、集合、排序、筛选及变体清单；分页途中变化必须重读。列举顺序标为 `native_scripting_collection`，不冒充屏幕排序；next/previous 使用官方 change-selection 命令。仍要求单选照片，允许从局部层导航。导航目标 layer=null，观察令牌只准选图；局部曝光调色须在所选局部层重新读取；多层整图预览可保留局部层选择，克隆仍须回到 Image 层。
- 批量每项必须携带原先读取的 target/module/patch/observation_token；逐项切图后仍检查原旧值，不能重读后悄悄改用新基线。每项独立记录，失败即停，不自动重放或回滚，最终选中最后处理的候选。父请求保留子请求编号，未知结果查询原编号。
- `operations get/list` 可查询。数值及结构模块只读 reconcile 只能确认当前已暴露状态是否与原观察一致；没有通用自动恢复、批量断点续跑或跨进程候选重认。

## 保留的测试状态

早期保留的测试候选没有反复克隆或覆盖。旧 Advanced 失败候选仍保留部分测试色域；其间 C1 重启使旧写入资格失效，没有复制登记来绕过保护。新进程的 Pilot 候选继承了来源候选原有的色轮中心量化残差。本轮另正常建立一张相邻 RAW 的候选用于跨照片粘贴（曝光已恢复），以及一个整图重置校准副本（保留默认状态）。私人候选 ID、路径、图片和完整操作记录只保存在本机私有目录，不入仓库。

## 暂放的阻塞项

**完整局部图层／蒙版流程：** 本机官方脚本仍未公开稳定图层 ID，也没有完整蒙版覆盖回读；原生引用可为位置或名称形式，标准 ID 属性读取失败。下述新增接口通过只读 Session 原生记录与实时清单对齐，已接入选层／改名与七个局部调色模块；没有把名称或下标当成 ID。宿主内按 GUID 原子写入、一般蒙版位图和任意子蒙版组合仍待解决；已新增的受限蒙版命令与多层整图回传见下节。

### GitHub 实现与本机补查

2026-10-29 核查了下列公开实现的相关代码，没有复制第三方代码或安装这些项目：

| 来源 | 核查结果与采用边界 |
|---|---|
| [c1cmd，94d8ba7](https://github.com/kiri11/c1cmd/tree/94d8ba71cb2bfb9e24acbb9c5a0c0c6b2d5b62dd) | AppleScript 路线；Skin Tone 和完整蒙版状态仍不可读，不解决本项目身份缺口 |
| [byjustinjones/capture-one-mcp，cfb4e42](https://github.com/byjustinjones/capture-one-mcp/blob/cfb4e42b573c9a5d0c4c0c583aaf9ce43c6ebb18/src/co/layers.ts) | 按位置读写图层，明确区分蒙版命令接受与实际验证；其旧版“只能创建 adjustment”结论不直接沿用 |
| [Freedy001/capture-one-mcp，fee1daa](https://github.com/Freedy001/capture-one-mcp/blob/fee1daab628d26b565336c2f1eeadf3d8cab7085/capture-one-mcp/scripts/capture-one-bridge.js) | JXA 图层／亮度范围和 AI 创建命令；图层引用由位置与名称组成，没有新增原生稳定 ID |
| [iscca/capture-one-ai-macos，1091a2e](https://github.com/iscca/capture-one-ai-macos/blob/1091a2e431b9fb3c0a3b4bb194194032c89f7622/c1ai/native/macos_tools.js) | 上游派生项目，以名称标记管理新层，Skin Tone 仍拒绝；未移植其身份方案或 Windows Tangent 传输 |
| [vicape92/capture-one-mcp，b389579](https://github.com/vicape92/capture-one-mcp/blob/b389579b99484c738ea6b86798c58251900e3fff/src/index.ts) | AppleScript 和缓存预览路径，未发现补足上述图层／肤色缺口的入口 |
| [LoupedeckPlus，48736f7](https://github.com/vadimkozhin/LoupedeckPlus/tree/48736f7f0a32b07f06b94ddf70cf252bface92be) | 控制器方案仍借助键盘和 AppleScript；未找到新的 Skin Tone 参数接口 |
| [ComfyOne，027750a](https://github.com/Igrekess/ComfyOne/tree/027750ad0d6289099c81b5b590711a534fed18d5) | 对接 ComfyUI 生成流程，不是 C1 当前原生图层状态接口，未引入 |
| [Luma Layers，b9eb48b](https://github.com/MathieuBourlion/luma-layers/blob/b9eb48bdf32d59b18c05cab19a5f69091a91301a/LumaLayers/ScriptRunner.swift) | MIT；实际 Swift 入口生成 AppleScript，设置亮度范围并按区间安排端点写入顺序；按名称选层，没有原生稳定 ID。仅阅读源码，未安装、运行或复制代码 |

本机 Tangent `controls.xml` 有色彩平衡参数和图层／蒙版快捷动作，没有 Skin Tone 数值或图层稳定身份／蒙版像素接口。没有为此开启服务或安装驱动。

实际创建验证另用一个新候选：官方 `make new layer` 的 `kind=subject mask` 成功，C1 回读也为 subject mask；用 CUA 临时显示灰度蒙版，实际看到了人物覆盖，然后恢复普通照片显示。探针在创建后的采样中遇到 Image 层 luma range 为空，停止后只读核对，**没有重复创建**；产品只读清单已正确处理该空值。新候选保留主体层作后续开发样本，原版本及调色值未改。该结果证明本机创建入口可用，不证明稳定层身份、蒙版像素回读或完整工具链已经解决。

### 官方接口补查与下一步路线

2026-10-29 再核对[官方 AppleScript 文档](https://support.captureone.com/hc/en-us/articles/360002681418-Capture-One-Workflow-Automation-with-AppleScript)（页面更新于 2026-06-29）和本机 16.8.6.25 字典。**局部层并非没有原生写入口；尚未完成的是把它接入本项目的目标保护、回读和画面验证流程。** 官方示例直接使用 `make new layer` 或 `create people mask` 返回的图层设置局部参数。这为“在一次受保护操作中创建并立即调整新层”提供了研究方向，但不证明跨请求的稳定身份，也不提供多步事务。当前产品能力不变。

| 路线 | 本轮证据与开发结论 |
|---|---|
| 新建 AI 层后立即调色 | 官方示例确认；先考虑仅限本次新建层的受限流程。仍需验证返回引用的解析方式、单次操作中的目标检查、超时后的只读核对和实际局部效果，不能因同一脚本执行就假定引用稳定；尚未实现 |
| 亮度范围蒙版 | 本机字典公开七个可写字段和 `fill mask` 等命令；Luma Layers 提供端点写入顺序参考，但其独立脚本与 Swift 主入口的数值并不一致，不能只抄 README。官方说明亮度范围依赖基础蒙版；必须验证实际覆盖，不能仅凭七个参数回读宣称蒙版成功 |
| 已有图层持续编辑 | 本机只读返回 `layer 2 of variant id …`，读取该层 `ID` 返回 -1728，`properties` 只含已公开属性。位置可用于读取，不是稳定身份；没有据此生成伪 `layer_ref`。此次并未穷尽所有内部接口 |
| Skin Tone 与完整色彩编辑器粘贴 | 官方明确仅支持整个 `color editor settings` 赋值，未开放 Skin Tone 逐项参数；本机读取整个对象返回引用，读取 properties 仅返回类名，不能据此得到完整参数快照。整体赋值路线仍需解决不可见状态的验证，当前未开放 |
| Pilot 的额外属性入口 | 静态追踪本机构建 `getProperty` 的处理块看到相机与 `cameraProperty` 取值路径，尚未发现能补足图层 ID／蒙版像素的证据；不是通用模型读取接口的实测证明。本轮没有开启服务或尝试远程调参 |
| 官方插件 SDK | [开发者入口](https://www.captureone.com/en/partnerships/developer)提供注册入口并说明有文档、教程及两平台示例，但本轮未取得新的 SDK 文档。不能把入口存在或旧论坛讨论当作已经提供内部图层访问的证据 |

[官方亮度范围说明](https://support.captureone.com/hc/en-us/articles/360002622777-Luma-Range-Overview)和[创建说明](https://support.captureone.com/hc/en-us/articles/360002622857-Creating-a-Luma-Range-Mask)区分基础蒙版、范围和过渡，并说明新版组合蒙版的添加／相交／相减；这些界面功能不自动等于脚本支持全部组合结构。[Apple 的名称引用说明](https://developer.apple.com/documentation/foundation/nsnamespecifier)也明确按名称解析对象，不能把名称引用包装成唯一 ID。

本轮只作资料与源码核查及少量原生只读验证，没有修改照片、复制调整、安装外部工具或增加测试框架。结束时仍选中原 Pilot 测试候选，mobile／web 两种共享关闭。优先推进新建层的受限局部调色与亮度范围；已有层持续编辑、完整蒙版回读和 Skin Tone 不因这次研究被标为已完成。

### 多层反复定位与局部曝光增量

用户进一步要求反复操作多个层后，新增 `c1ai/native/macos_layers.js`，接入原有六工具中的 `read(layers)`、`adjust(layers: select/rename)` 和局部 `read/adjust(exposure)`。该阶段仅开放局部曝光 EV 写入，四个曝光模块字段可读；未设置值保持 null，未默认写成 0。该阶段层属性、其他局部调色、创建／删除／移动、蒙版写入和局部 reset/paste 均未开放；后续增量以本文顶部记录和当前能力表为准。

Windows 现有代码从进程内 `Mask.GUID` 定位层；Mac 内部也有 `POLocalAdjLayer.identifier`，但脚本层没有公开对应 ID。新路线由 C1 官方 `save` 保存后，以 SQLite `-readonly` 在单个读事务中读取 Session 的变体 UUID、层记录号、层顺序及 MaskUUID，逐层核对实时名称、启用状态、透明度和四个参数。它是内部持久化身份的兼容读法，**不是官方公开数据库 API，也没有证实 `identifier` 与 MaskUUID 在所有情况等价**。不直接写数据库／侧车、不修改应用、不需要加载内部桥。

首次实现曾让 `layer_ref` 绑定层记录号；后续发现 C1 会重建记录，现已改为真实 MaskUUID 的无损整数编码，绑定进程、变体及变体 UUID，层记录号仅用于当次对齐；当前层从原生 AppleEvent 位置描述符解析，避免按同名层猜测。实际 setter 仍按本次核对的位置派发，每次修改前后核查完整清单。仅限 16.8.6.25、当前进程登记候选和同一 Session；不支持同时手动创建／删除／重排层，不承诺原子 GUID 写入、任意删除重建的身份连续性或跨进程复用。数据库／实时状态不一致就拒绝，不按名称重新认领旧引用。

代表性实测使用新登记候选的两个 AI 层：A 为主体、B 为背景。通过 A 曝光 +0.6、B -0.6，A 改成与 B 同名后分别选中与回读，再 A 改回原名、调整至 +0.3，最后两层均恢复 0；未设置的其余三字段仍为 null。层记录号、蒙版 UUID 在切层／改名中保持不变，Image 层四字段保持原值。同请求编号返回已有结果、不重放。实际 C1 查看器看到了主体提亮与背景压暗，并检查了恢复画面；这不是多层 Pilot／MCP 图片回传验收。

重排研究发现重要宿主陷阱：原生 `move layer … to after layer …` 返回成功，却移除了测试候选的源层；随后仅通过 CUA 点击 C1 的“撤销移除图层”恢复，原生回读确认层记录号、UUID 和所有已观察参数恢复。未重放 move。一次界面拖动也未改变顺序，因此**重排后的定位仍未实测通过**，产品不开放 move。原始变体未改。

首个产品 select 的适配器回执漏了 requested_result_verified，导致 Core 正确保留 partial；修正后对原编号作完整状态只读 reconcile，确认当前选层结果，没有重放命令。AU 同时指出并修复 context 两次采样可能跨照片的问题。新增 3 项针对回执身份和空值边界的测试，加既有导航／批量共 7 项通过；AU 的隔离 Core 检查未调用 C1，当前未发现剩余明确 P0/P1/P2。实际同步与改名资格只来自上述本机构建的代表性操作，不能扩展到 Catalog 或其他版本。

**图片预览：** 用户明确不导出、不用缩略图，后续要求继续研究。没有找到直接返回当前渲染位图的官方脚本命令。参考 Windows 原生处理内核思路写了只读原型 `internal/macos/` 与构建脚本；本轮加载诊断修正了此前结论：

- 原构建没有显式链接 AppKit，在未预载 AppKit 的干净进程中报 `NSBitmapImageRep` 符号缺失。这是本项目构建缺陷；已声明 Foundation/AppKit 依赖并通过干净进程加载。Zig 使用仅包含系统 ABI 名称的最小导入声明，不复制厂商框架。
- 加入不含照片数据的 constructor 记录后，直接启动与 LaunchServices 启动都确认桥进入了 C1；但宿主随后以退出码 0 结束，未产生 `ready`，未调用渲染入口。constructor 记录不等于桥已可用。
- 同方式不带库启动，C1 持续运行；只写 PID、无 Objective-C／渲染代码的最小 C 库也能进入 C1，随后仍退出。这把问题缩小到当前宿主的外部库启动路线；尚无证据区分原版行为、安装包改动或其他启动检查，不能断言是许可证状态造成。
- 测试均先保存 Session，再正常退出；已恢复正常启动。没有修改应用签名、授权组件、系统保护或安全软件。

内部桥继续未接入产品、排除在发布清单之外。Pilot 探针验证成功后已收敛为 `c1ai/macos_pilot.py` 与 `c1ai/native/macos_pilot.js`，删除重复探针；其产品边界和实际结果如下。

### 已接入的 Capture Pilot 预览

- 旧配置 v1 继续支持明确 RAW 路径白名单和专用相册。用户后续要求多照片预览，本机已配置 v2：绑定当前 Session 和已批准集合的 ID／名称／种类／文件夹。每次创建随机唯一名称的临时相册，仅加入当前 RAW（C1 自动包含其全部变体），不共享整个源集合。相邻切换后重新读取目标即可继续预览。
- 用官方 Apple Events 启停 mobile server、设置密码并关闭评分／标记／调色／拍摄；web server 保持关闭。不借用用户已经运行的共享服务。HTTP 只连 `127.0.0.1`，先验证监听端口属于目标 C1 PID；不使用代理或重定向。
- 有密码的 mobile server 实测需要 HTTP Basic 的 `Guest` 用户加已配置密码，以及连接请求中的 SHA1 密码。凭据只存于当前用户 0600 配置，不放进命令行或操作日志；JXA 错误回显脱敏。
- Pilot 的变体 ID 在本机构建为 `原生脚本行 ID/UUID`。按原生集合成员、行 ID、变体位置与文件名核对，授权范围另外绑定完整 RAW 路径；不能仅用同名文件或可能复用的编号识别照片。
- 每次 `preview(kind=native, action=render)` 启动服务，取得指定尺寸 JPEG，并检查前后目标、集合、共享权限和公开调整快照；随后关闭服务、恢复非密码设置、删除自身临时相册，再核对目标及快照后交付图片。本机构建删除相册会清空浏览位置，已补上精确恢复原集合／选图并回读；不删除照片、变体或移动 RAW。随机密码保留，不尝试恢复无法读取的旧密码。缓存最多 8 张；JPEG 原样进入 MCP，不二次缩放／编码。
- 原生服务在修改曝光后报告该变体 `modified`，返回图像实际变亮；恢复后参数一致、JPEG 与修改前逐字节相同。统一工具和真实 MCP 各自完成验证，独立启停服务后的 MCP 修改／恢复闭环也通过。
- 当前限制：支持保存态稳定的多层整图，配置限定的 Session／源集合（或 v1 的 RAW 白名单）；不支持 mask／viewer／export／map_points。新增只读 Session 内容与 COS／COMASK 全文件摘要；仍没有原子宿主修订锁，避免与手动调参并发。返回的 Pilot JPEG 没有内嵌 ICC，本轮未独立核验其颜色管理，不能替代成片色彩验收。
- 正常完成及可处理的失败都会关闭共享。若 owner 被强制终止、宿主不可达或共享状态被外部改变，私有 active receipt 保留，后续预览拒绝自动重放；用下面的显式 cleanup 检查并停止本项目服务。日志写入失败也不能阻止尝试关闭服务，清理结果会保守报告为未知。

开发配置（先取得对应共享范围的明确授权，二选一；不是安装器）：

```sh
.venv/bin/python scripts/configure_macos_pilot.py --enable-pilot-sharing --collection-id 已批准集合ID
# 当前集合内相邻切换；每次只共享当时选中的 RAW：
.venv/bin/python scripts/configure_macos_pilot.py --enable-pilot-sharing --selected-in-current-collection
# 只在前次异常留下本项目 active receipt 时使用；不重放照片操作：
.venv/bin/python scripts/configure_macos_pilot.py --cleanup
```

配置脚本只创建私有授权配置，不立即开启服务；每次预览检查范围。现有配置不覆盖，扩大范围需重新明确配置。服务会同时监听局域网，即使本工具仅用本机连接。

### 公开方案核查（2026-10-29）

| 方案 | 源码／官方资料可确认的能力 | 对本项目的结论 |
|---|---|---|
| [c1cmd](https://github.com/kiri11/c1cmd/tree/94d8ba71cb2bfb9e24acbb9c5a0c0c6b2d5b62dd) | 官方脚本调色；联系表使用缓存缩略图，快速选片用 RAW 内嵌 JPEG，处理后图像可经配方导出 | 可参考调色映射；这些图像路径不满足本次要求 |
| [capture-one-mcp](https://github.com/byjustinjones/capture-one-mcp/blob/main/docs/PREVIEWS.md) | 为预览建立专用 JPEG 导出配方 | 仍是导出，不采用 |
| [LiveLegacy](https://github.com/AlexMorrissey-Smith/LiveLegacy/tree/c6b8422f22ebe712b1a5731d8acf96e84ed395ae) | 作者声明在 16.8.6 验证；通过 Capture Pilot／Live for Studio 的 `getImage` 请求指定尺寸和裁切画面，有变更通知 | 提供协议研究起点；本项目另完成上述本机身份／曝光／图片验证，并补上受保护 mobile server 的 Basic 认证 |
| [GIF 原生插件](https://github.com/BTx123/capture-one-gif-plugin/tree/fa8b4fba08d57f8a56119f7be347d40251976916) | `.coplugin` 与公开 SDK 用法；`COVariantProcessing` 定义的是处理后导出文件 | 不能据此认为插件能访问 C1 主进程或取得无导出位图；不复制其仓库里的厂商 SDK |
| [LrGeniusAI 的 C1 实验](https://github.com/LrGenius/LrGeniusAI/tree/55deb3b746cd7adff419e78eb162bace56224ae5/capture-one) | README 明确标为放弃的实验，未在 C1 实机运行 | 不作为可用方案或验证证据 |
| [C1 原生大预览缓存](https://support.captureone.com/hc/en-us/articles/30111725200413-The-Cache-folder-in-Capture-One) | 官方区分 `.cop` 较大预览和 `.cot` 缩略图；本机测试 RAW 的 `.cop` 是 JPEG XL 容器 | 同一 RAW 的多个变体只发现一个 `.cop`；未解码、未确认是否包含所选变体的完整调整，不能直接当成当前渲染交付 |

[官方 Capture Pilot 文档](https://support.captureone.com/hc/en-us/articles/360002574498-Capture-Pilot-overview)确认原生照片浏览、密码及远程权限设置。另核对了本机随 C1 附带的 `CapturePilotWebClient.js`：全屏预览与缩略图均调用 `getImage`，尺寸／裁切由请求指定；客户端区分 `modified` 与 `metadata` 变化并刷新图像。HTTP 端点细节来自附带客户端、第三方源码及本机方法清单，不能称为官方公开开发者 API。本项目的实机证据单独列于上一节，不继承第三方验证结论。

官方确认 16.8 开放 Basic/Advanced，16.8.2 开放若干 AI 蒙版；Skin Tone 仅确认可整体复制 color editor settings，不能由此宣称逐项编辑可用。[官方说明](https://support.captureone.com/hc/en-us/articles/360002681418-Capture-One-Workflow-Automation-with-AppleScript)

研究参考 c1cmd 的固定提交 `94d8ba71cb2bfb9e24acbb9c5a0c0c6b2d5b62dd`（MIT），仅借鉴官方容器创建／预览路线，未复制源码；第三方结果不计作本项目实测。

## 开发和检查

Python 3.11+，本机使用 3.12：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python scripts/check_macos.py --metadata-only
.venv/bin/python scripts/check_macos.py
.venv/bin/python -m c1ai context --compact
.venv/bin/python -m c1ai read --json '{"module":"curves"}' --compact
```

MCP 使用虚拟环境 Python 加 `-m c1ai mcp`。不运行 Windows 安装器；系统自动化权限仍由 macOS 管理。Python 后端改动需在空闲时重启本项目 owner，JXA 文件按调用读取，不需重启 C1。

本轮只运行针对通信、目标保护、批量停止和不重放的必要检查，加 Python/JXA 语法和发布清单／隐私检查，不生成发布包。历史完整 Core/formatting 抽查在纯基线复现 46 通过、32 失败（旧测试引用已移除接口）；不宣称全套测试通过，不扩展修复历史测试。Windows 宿主未在 Mac 重测。Windows `uia.ps1` 曾被安全软件隔离，已恢复为 Git 基线，未关闭扫描或添加排除。

## AU 审核

用户要求的 gpt-6-astra / ultra 审核已再次进行，覆盖结构模块、原生目标保护、集合、批量和共享通信。已关闭四项明确问题：批量父结果保留旧目标、结构模块返回未经核对的最后采样、Advanced 未知操作可重复新增／删除、批量日志持续失败被误报为拒绝。同时修复色阶与色相端点的安全写入顺序。最终复核未发现剩余明确 P0/P1/P2。

必要检查结果：9 项选定 pytest 通过（3 项真实本机 IPC、2 项批量保护、4 项共享 Core 保护），真实 JXA 脚本的 6 个隔离故障场景通过，另核对未知结构动作阻断及日志失败保守返回。Python/JXA 语法、88 文件发布清单、文档本地链接和凭据扫描通过。审核代理没有调用真实 C1。安全修复后另做一次 RGB 曲线代表性真实读写／恢复，完整曲线回读恢复一致；此时电脑已锁屏，未重复最终界面复看。其他模块保留此前实机结果，不把隔离检查写成全部模块重新实测。

内部桥原型继续排除于可交付能力，审核不能代替实机渲染证明。

Pilot 产品接入再次由同一 AU 代理审核。已关闭两项 P2：停止共享被日志写入失败阻挡、授权范围只绑定可能复用的脚本 ID；修复后同时绑定 RAW 路径，并保证停止日志无法创建时仍尝试受保护的清理、保留未知状态。另增加系统错误中的密码脱敏。最终复核未发现剩余明确 P0/P1/P2。

仅新增 5 项针对身份和共享失败边界的离线 pytest，均通过；加既有查看器图片交付检查共 6 项通过，Windows 命名管道测试按平台跳过。92 文件发布清单、隐私与文档本地链接检查通过，Windows UIA 文件与基线一致。修复后再次通过真实 MCP 取图，JPEG 与恢复后的基线逐字节一致；确认临时服务关闭、active receipt 移除、曝光恢复并保存 Session。审核代理没有调用 C1 或照片接口。没有生成或发布新的安装包。

### 相邻照片、模块复用与固定默认值增量复核

本轮 AU 审核发现并关闭一项 P2：Advanced RGB 原点先变化、范围后变化，会让最终合法请求出现越界中间状态。现于任何原生派发前检查新旧 RGB 分量组合与旧范围，不安全则拒绝并要求先收窄范围。处理引擎也在每次原生守卫检查；隔离脚本验证引擎变化返回 `engine_changed`，零写入。复核后未发现剩余明确 P0/P1/P2；Windows 原模块动作 fallback 与原结构阻断定义保留。

主流程 13 项针对性检查通过；AU 独立运行 Pilot／模块动作／批量的 13 项检查通过，两组有重叠，不累加为新的独立测试数量。Python/JXA 语法、93 文件发布清单、隐私和本地文档链接通过。代表性实机结果见上表，审核本身没有调用 C1。最后选回原 Pilot 测试候选、曝光恢复、临时服务关闭、临时相册为零、active receipt 移除，Session 已保存。校准副本保留整图重置状态。没有生成安装包、提交或推送；局部层／蒙版和未开放色彩参数继续列为未完成。

### 图层只读信息与局部层导航增量复核

同一 AU 代理增量审核未发现明确 P0/P1/P2。导航／批量 4 项针对性测试通过；审核另用隔离 Core 确认导航令牌不能用于调色、预览或克隆，且拒绝发生在后端派发前；相同请求编号不重放，Windows 默认 select 范围保留。没有增加测试框架或在审核中操作 C1。

本机通过两层只读清单、主体层选中时 next 到相邻 RAW、previous 返回并保留原层选择，以及相同编号查询不重放。之后经产品 select 返回原 Pilot 候选，重新读取曝光四字段一致，Session 保存完成、Pilot 两种服务关闭，active receipt 不存在。94 文件发布清单／Python 语法／凭据扫描及 JXA 编译通过。没有重新宣称全部模块实测，也没有生成安装包、提交或推送。

多层接口交付收尾：真实 MCP 保持六工具，read(layers) 返回三层及真实引用；最后两层曝光均为 0、原始未设置值保留 null，选回原 Pilot 开发候选并核对 Image 层四字段与本轮基线相同。Session 保存成功，mobile／web 共享关闭。96 文件的语法／隐私／本地文档链接检查、JXA 编译及 diff 空白检查通过；没有发布、提交或推送。

### 蒙版与多层整图增量复核

同一 AU 代理完成增量审核。修复了蒙版保存轮询可能接受新的选层／参数基线、派发前只读失败误记未知并锁住后续写入两项 P2；另外修正 Pilot 同名层按位置读取、反相端点范围、复制后的显式 UUID 转换和 Luma 端点写入顺序。最终静态及隔离复核未发现剩余明确 P0/P1/P2，审核代理未操作 C1／照片／服务。

16 项针对性测试通过；98 文件发布清单、Python 语法、私人路径扫描、本地文档链接、JXA 编译及 diff 空白检查通过。没有重新跑无关完整测试，没有生成安装器、提交或推送。

最终实机交付：真实 MCP 仍为六工具，四层候选的 Luma 重复修改后返回 `text + image`，实际查看了 1200×800 整图。局部曝光 0.4 恢复为 0 后，返回 JPEG 与该候选基线逐字节一致；主体层 0.6 的独立闭环也已确认恢复图一致。保留测试蒙版结构用于后续开发，原 RAW／原变体未写。

收尾已选回原开发基线候选，Image 层曝光四字段与本轮开始时一致；Session 已保存，Pilot mobile/web 均关闭、临时预览相册为零、active receipt 已移除。
