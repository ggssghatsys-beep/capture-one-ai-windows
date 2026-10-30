# 接口说明

原六个命令保持兼容；开发分支新增第七个只读命令 `analyze_image`，详见 [图像分析接口](SCOPES.md)。MCP 省略 JSON 的 command 字段；Python c1ai.client.call 和 CLI request --file 使用完整 JSON。只写登记候选，不能伪造目标、凭据和图层引用。

先检查本平台 `context.editing_ready` 和 `capabilities`。Mac 16.8.6.25 的 Session / Image 层支持 exposure、white_balance、hdr、curves、levels、color_mixer（Basic/Advanced）、color_balance（默认色轮布局），字段和绝对值语义不变；仅写当前进程内登记的候选。`images(create_candidate)` 创建并选中官方克隆；`images(select, variant_id=整数)` 使用观察到的原生脚本 ID，不是 UUID。`images(list)` 在当前集合分页，后续页沿用 collection_token；next/previous 使用官方切图命令。Advanced 现有 RGB 起点已可写；局部层支持选层／改名／启用／透明度，以及相同七个调色模块；蒙版已有限支持原生创建、填满／清空和亮度范围；启用原生桥时 Basic／Advanced／Skin 共用后台原生 ColorCorrectionList 深拷贝与 setter，支持 Skin 单一区域的增删、RGB／范围／七字段／标志和 Advanced 反选；未配置桥保留可选 AX 后备。原生计划在写前逐18列核对，回读同时保护所有隐藏列、其他调色和蒙版。Mac 模块粘贴支持曝光、白平衡、HDR、曲线、色阶、色彩平衡以及完整色彩编辑器，要求同实例、Session 和处理引擎；完整色彩编辑器使用官方 color editor settings，来源必须含九个显式 Basic 扇区，完整保存快照同时保护 Skin Tone。来源可为已读取的 Image 或局部层，复制的是参数快照而非蒙版；数值来源含 null 时写入前拒绝，不暗中归零。Mac reset 使用实测固定中性值，白平衡不重置，启用原生桥时色彩编辑器完整重置 Basic/Advanced/Skin（未配置桥保留 Basic/Advanced 限定）；不代表用户／相机自定义默认值。具体实测资格见 capabilities.module_actions。

Mac `adjust(batch=[...], request_id=...)` 接受 1～16 个 `{target,module,patch,observation_token}`；不要同时传单项字段。每项均来自该候选先前的实际读取，支持调色，逐项检查旧值、独立记账，失败即停，不自动重放或回滚；结束选中最后处理候选。`operations get/list` 查询父子记录，未知结果只查原编号。数值及结构模块支持只读 reconcile，但没有通用恢复或批量续跑。

Mac `context.layer_inventory` 提供只读图层清单、当前层描述、透明度、已暴露局部数值及亮度范围。`display_index` 仅供显示，重名时当前层位置可能为空；它不是 `layer_ref`，也不含蒙版像素。局部值为 `null` 时原样保留，不能当成 0。选中局部层时仍可 `images(list)` 后 `next/previous/select`：导航目标的 `layer=null` 只绑定照片，返回的观察令牌仅供继续选图；调色前必须重新 `read` 获取非导航令牌；预览可保留局部层选择；克隆仍须选择 Image 层。Windows 的清单令牌语义不变。

Mac 多层接口使用 `read(module="layers")` 返回的 `state.layers` 和 `selected_layer`。`native_id` 是真实蒙版 UUID，`runtime_id` 使用 UUID 的四个无损整数片段并绑定进程和变体；Image 层使用独立标记。数据库层行号会被 C1 重建，不再用作稳定引用，也不再返回 `object_id`；旧引用必须重读。不是名称哈希或层序号。每次先由 C1 保存 Session，再只读数据库并核对实时清单；不会直接写数据库或侧车。仅资格化 16.8.6.25 Session，不支持并发手动创建／删除／重排层，也不承诺原子 GUID 写入。

启用匹配原生桥时，Mac `layers.duplicate` 直接调用宿主的完整复制方法，`move` 按 UUID 排列调用原生排序，`masks.combine`、`combine_ai`、`select_submask`、`delete_submask` 同样使用后台原生方法与统一保存态核对；均不需要可见工具、前台或鼠标。以下 AX／鼠标说明仅适用于未配置桥的显式兼容路径。

Mac `layers.duplicate` 复用公共 `{action:"duplicate", layer_ref:当前局部层引用, name?:名称}`：显式启用 AX 后，通过官方“复制已选图层”菜单复制完整调色和蒙版，取得新的原生 UUID；可选名称随后通过官方脚本设置。只支持同一候选内的局部层，选中新增层后继续用相同七个模块调整；删除使用该次复制的 `operation_id` 与新鲜引用调用 `remove_created`。复制前令牌绑定完整保存参数与蒙版文件，复制后核对新层完整参数／蒙版摘要及所有原层不变；不是按公开参数逐项拼装。已有简单线性渐变层的代表性复制、改名、独立修改和整图预览已通过，其他蒙版种类不继承实测。图层排序和已有蒙版组合的限定范围见下。

按返回的完整 `layer_ref` 调用 `adjust(module="layers", patch={"action":"select","layer_ref":...})`；选中后重新读取要调整的模块，例如 `read(module="exposure")`，再 `adjust(module="exposure", patch={"exposure":绝对EV})`。切到其他层后可以重复同样流程。`rename` 同样携带当前完整引用；再次读取后按原生标识找回改名的层，不复用过期观察令牌。已实测两个同名局部层各自保留不同曝光值，以及 A→B→A 往返、改名、修改与恢复。局部十个数值字段及曲线、色阶、Basic/Advanced、色彩平衡沿用全局格式，未设置数值为 null，不自动当作 0。局部 reset/paste 复用同一参数接口，实测范围见 capabilities.module_actions；删除仅支持携带创建记录的 remove_created，排序使用下述显式鼠标后备；`create_empty` 新建空层已接通。`set_enabled` 携带 boolean enabled；`set_opacity` 携带整数 opacity（1～100，关闭用 enabled=false），两者只作用于当前选中局部层，携带完整 layer_ref。局部 WB 必须能同时读取温度和色偏；首次写入会初始化整组并成对回读。具体代表性实测范围见 MAC_STATUS。图层 select/rename、限定蒙版 fill/clear/invert/copy 支持只读 reconcile；Luma 目前仅确认完整状态未变时的 not_applied，不重放原命令。蒙版未知结果不能换编号重做。多层整图预览优先使用显式配置的原生桥；未配置桥时可使用下述 Pilot 路线。

Mac 桥已启用时，主体／背景新层、简单父蒙版反相、整父填满／清空也走同一桥，动作与字段不变。`invert/fill/clear` 要求选中完整父蒙版（submask index -1），`fill` 暂限 density=1。反相支持已资格化常量、简单栅格、线性和径向；复合／含 Luma 父层整层反相会派发前拒绝，不能将某个子项反相当作整个父结果反相。AI 创建保留 C1 原生功能资格检查。具体实机范围见 MAC_STATUS。

Mac 原生桥启用且版本匹配时，`masks.linear_gradient {start,stop,midpoint}` 与 `masks.radial_gradient {center,radius_x,radius_y,falloff,angle,inverted}` 采用与 Windows 相同的归一化 RAW 契约，`coordinate_space` 默认 `raw`。支持登记候选的空调整层创建，以及同类型简单线性／径向蒙版的直接形状更新；对已有渐变重新 `read(masks)`，用同一个 action 提交完整目标几何即可，保留图层 UUID、曝光等参数和其他图层。未向任意复杂蒙版追加渐变。原生临时变更／撤销提交及完整保存态回读后返回 `control_confirmed`；超时只查询原 operation_id，不能重放。线性与旋转径向已通过本机代表性实测，参见 [MAC_STATUS](MAC_STATUS.md)。无需鼠标或前台工具。Mac `map_points` 仍不可用，不把旋转后的照片预览坐标直接当 RAW。

Mac 实验性渐变创建沿用 `masks.linear_gradient`，增加显式 `coordinate_space:"viewer"`；省略时仍为 RAW，Windows 现有语义不变，Windows 暂拒绝 viewer。该 viewer 后备仅支持空局部层、Fit 单图、`midpoint:0.5`，`start`／`stop` 是当前可见照片矩形内的归一化坐标（左上为 0,0，向右／下增加，端点须在内部且跨度至少 4 屏幕点）。仅在开发验证时，先 `layers.create_empty`，再 `read(masks)` 取得 `gradient_view.available=true` 与新令牌，然后调用 `{action:"linear_gradient",coordinate_space:"viewer",start:[0.25,0.25],stop:[0.75,0.75],midpoint:0.5}`。需要已有 `mouse_enabled=true`，当前键盘底层布局须为 US／ABC，快捷键配置须唯一把无修饰 L 绑定到线性渐变；不会自动改快捷键或缩放。令牌绑定照片窗口几何，派发前核对全部保存参数、空蒙版、当前照片和精确照片命中，再选择官方工具并拖动一次。返回 `requested_viewer` 和 `actual_raw`，确认参数渐变类型／中点及其他图层不变；`gradient_view.available` 仅表示前置条件可用，不证明请求位置。当前结果为 `status=partial`、`requested_result_verified=false`、`model_verified=false`，尚未验证与请求位置近似一致，也不宣称精确 RAW 点匹配。已有蒙版不会被此 viewer 操作替换，先创建空层；viewer 后备不支持任意中点和径向，原生桥不受该 viewer 限制。代表性测试已确认参数渐变创建及曝光改变后的 Pilot 整图效果，但几何对应闭环尚未完成，不应作为已验证的日常修图动作。

Mac `layers.move` 复用 `{action:"move", layer_ref:当前局部层引用, index:目标索引}`，索引从最底部局部层的 0 开始，不含 Image 层。需本地 `macos-ui.json` 同时设置 `enabled=true`、`mouse_enabled=true`，并已有系统辅助功能／事件权限；不会自行更改系统权限。只支持完整可见且子项折叠的图层列表，选中整个父层后调用。它正常激活 C1，核实鼠标两端确实命中当前 Session 窗口，再执行一次原生列表拖动，因此会短暂占用前台和鼠标。回读必须与请求排列完全一致，完整参数只允许 ZINDEX 变化，蒙版文件必须逐字节不变。两个已有线性渐变层代表性排序、恢复和整图预览已通过，未穷举所有工具布局。

Mac `masks.combine` 复用 `{action:"combine", source_layer:新鲜源层引用, mode:"add"|"subtract"|"intersect"}`。先选中目标局部层的整个父蒙版，再读取 masks 并取得同张候选内的另一源层引用。显式启用 AX 后，通过官方组合菜单新增空 Brush 子项，再以“复制蒙版自”把源蒙版复制到该子项，保留组合方式；不调用会替换整个父蒙版的 AppleScript copy mask。源层必须有唯一名称，支持无 Luma 的简单栅格／线性／径向蒙版；目标为同类简单蒙版或平铺组合，不接受嵌套组合与带 Luma 源。两步分别核对照片、父层、实时子项及完整保存参数，源层与已有子项须保持不变；非原子操作，第二步失败可能留下空子项，未知结果只查询原操作，不能重发。实机资格按 capabilities.action_qualification 返回。

Mac 可选 AX 后备直接使用 macOS 辅助功能接口，不注入 C1。需用户显式启用本地 `macos-ui.json` 的 `enabled=true` 并为实际运行程序授予系统辅助功能权限；默认关闭。当前 Skin Tone 只支持已存在的单一色域（`mode=skin, action=update, index=0`）的 hue、saturation、lightness、smoothness、uniformity_hue、uniformity_saturation、uniformity_lightness，七字段读取及局部 hue 修改／恢复已通过代表性实机验证，其余字段未逐项写测。`masks.combine_ai(mode=add/subtract/intersect, kind=subject/background)` 操作官方菜单；`select_submask(index=...)` 和 `delete_submask(index=...)` 依据完整实时 AX 行与保存结构定位，索引取保存子项顺序，不能把 COMASK 的保存选择标记当作实时选择。删除前须先选定同一子项；若删除后只剩非栅格参数蒙版，本机额外保存状态尚未资格化，派发前拒绝。背景 add/subtract/intersect、子项反复定位和删除已通过实机回读；整图预览资格见 MAC_STATUS。工具需位于当前可见页；Pilot 预览清理通过官方 tool tab.selected 恢复预览前工具页。所有 AX 写入在找到控件后重新核对原生照片、图层和旧状态；仍非原子操作，不支持并发手动改图。AI 创建或组合结果未知时，同一实例／Session 的切图、切层和子项选择都拒绝，先查询原操作，不重发。

Mac `preview(kind=native, action=render)` 优先使用已配置的原生桥，返回带修订、照片／层身份和 C1 几何的 PNG；`kind=mask` 返回整层灰度覆盖，同尺寸同几何，不导出、不抢焦点。桥失败明确拒绝；禁用桥后，使用显式配置的 Capture Pilot：临时开启有密码的原生图片服务、关闭远程修改／拍摄、仅从本机取得 C1 JPEG、关闭并恢复共享设置，直接返回 MCP 图片块。不导出、不使用 RAW 内嵌图或缩略图文件。仅限获批 Session／当前源集合，可包含多个局部层并保持所选层；v2 每次用临时相册只共享当前 RAW 及其变体，结束移除相册并恢复原浏览位置。旧 v1 继续按 RAW 路径白名单限制，配置见 [MAC_STATUS](MAC_STATUS.md)；Pilot 路线拒绝 `kind=render/viewer/mask`；Mac 两条路线均未实现 `map_points`。返回 `source=capture_one_pilot_render`、目标、原生调整与保存态摘要、图像哈希和验证范围；同时校验 Session 内容、COS／COMASK 文件、图层 UUID。Pilot 临时 UUID 与持久变体 UUID 分别绑定，不能直接比较。数据库重建行号按相同内容处理，实际参数和元数据变化仍拒绝；没有原子宿主修订锁，避免同时手动调参。以下结构模块示例须对照平台能力。`context.status=ok` 只表示返回诊断结果，可能同时包含 `native_connection=unavailable` 和 `connection_error`；`editing_ready` 只说明参数写入资格，不保证预览可用。

| 工具 | 用途和主要字段 |
|---|---|
| context | 环境、目标、能力、未决操作、workflow；include_dialogs 可显式查询弹窗 |
| images | action=list/create_candidate/select/next/previous；scope、offset、limit、collection_token；选图优先 variant_uuid |
| read | fields 或 module 或 modules，target 可传预期目标；返回 values/state、target、observation_token、evidence、guidance |
| adjust | module、patch、target、observation_token、request_id；绝对目标值 |
| preview | kind=native/mask、max_size；action=capture（默认）或 map_points；返回图片、preview_id 与几何 |
| operations | action=get/list/reconcile，按 request_id 或 operation_id 查询；reconcile 不重放写入 |

images 的 prepare_tool/prepare_adjustments、preview 的 viewer/render 是显式兼容入口，不用于正常原生流程。

## 最小闭环（会创建副本）

示例展示调用顺序，不是通用预设。必须先查看 before 图片，再决定曝光值。

```python
import uuid
from c1ai.client import call

def ok(r):
    if r.get('status') not in ('ok', 'completed', 'control_confirmed'):
        raise RuntimeError(r)  # 结果未知先查原编号，不能自动重发
    return r

print(ok(call({'command': 'context'})))
r = ok(call({'command': 'read', 'fields': []}))
before = ok(call({'command': 'preview', 'kind': 'native', 'max_size': 1200,
    'target': r['target'], 'observation_token': r['observation_token'],
    'request_id': uuid.uuid4().hex}))
print(before['result']['path'])  # 在客户端实际查看

clone = ok(call({'command': 'images', 'action': 'create_candidate',
    'target': r['target'], 'observation_token': r['observation_token'],
    'request_id': uuid.uuid4().hex}))
target = clone['after']['target']
r = ok(call({'command': 'read', 'module': 'exposure', 'target': target}))
request_id = uuid.uuid4().hex
edited = ok(call({'command': 'adjust', 'module': 'exposure',
    'patch': {'exposure': 0.2}, 'target': r['target'],
    'observation_token': r['observation_token'], 'request_id': request_id}))
r = ok(call({'command': 'read', 'fields': [], 'target': target}))
after = ok(call({'command': 'preview', 'kind': 'native', 'max_size': 1200,
    'target': r['target'], 'observation_token': r['observation_token'],
    'request_id': uuid.uuid4().hex}))
print(after['result']['path'])
```

结果未知时：`{"command":"operations","action":"get","request_id":"原编号"}`。变体沿用同一 RAW，不复制或覆盖原始像素；创建后仍须重新读模块。

## 模块与单位

| module | patch 参数及接口范围 |
|---|---|
| white_balance | temperature 800～14000 K，tint −50～50 |
| exposure | exposure −4～4 EV，contrast/brightness −50～50，saturation −100～100 |
| hdr | highlight_recovery/shadow_recovery/white_recovery/black_recovery −100～100 |
| levels | channel=rgb/red/green/blue；input/output_black/white 0～255；midtone −1～1 |
| curves | channel=rgb/red/green/blue/luma；完整 points 数组，2～64 个 [x,y]，均 0～1；x 严格递增，首尾 x 为 0、1 |
| color_mixer | mode=basic/advanced/skin，量程见 COLOR_API |
| color_balance | wheel=master/shadows/midtones/highlights；x/y −50～50 且半径≤50；lightness −50～50，master 无亮度 |
| layers | action 和本次返回的完整 layer_ref；透明度 0～100 |
| masks | create_subject/create_background 或细化动作，见 COLOR_API |

范围不是推荐幅度，省略字段保持原值。色轮 x/y 受 C1 色轮方向设置影响，不是色相角或饱和度百分比。

```json
{"module":"curves","patch":{"channel":"luma","points":[[0,0],[0.25,0.23],[0.7,0.73],[1,1]]}}
```

```json
{"module":"color_mixer","patch":{"mode":"basic","color":"yellow","hue":-8,"saturation":5,"lightness":-2}}
```

```json
{"module":"color_balance","patch":{"wheel":"highlights","x":2,"y":1,"lightness":0}}
```

以上片段仍需 target、observation_token、新 request_id 才能执行。不要为用遍工具增加无目的调整。

## 图层和蒙版

Mac `read(masks)` 额外返回各层原生 UUID 对应的已保存结构、Luma 参数、内容摘要和可严格确认的恒定覆盖；解析的是 C1 自己保存的 `.comask`，只读、不直接修改。它是特定版本的内部格式适配，不是官方公开蒙版读取 API。该结构读取不返回位图；启用桥后 `preview(kind=mask)` 单独返回 C1 当前渲染的灰度覆盖。

Mac `create_subject/create_background` 新建并选中对应 AI 层；`fill` 只接受 `density=1`，`clear` 清空所选层；`invert` 接受严格确认的空／满蒙版，以及可完整解码的中性 simple raster；后者核对有效像素的 255-x 补集。`copy` 核对原源内容与目的层自身调整，返回新的 MaskUUID／layer_ref，必须重新读取后继续操作。`luma_range` 七字段可写，但 16.8.6 首次写入会**添加 Add 亮度子蒙版**，不等于与现有主体相交；不要把全范围数值当成删除 Luma。`clear_luma_range` 移除唯一直接 Luma 子项并核对存活蒙版；`feather(radius=0..100)`、`refine(radius>0..300)` 使用官方命令，限定可完整核查的中性 simple raster。amount 是命令参数，不是可回读的宿主属性。已有中性参数型线性／径向渐变和非端点 ConstantFill 已接入官方反相及保存参数转换校验，尚不表示所有类型均实测；Viewer 线性创建仅为上述实验入口，默认 RAW 线性／径向改走原生桥；任意源层组合仍未开放。灰度蒙版只用于开发时检查覆盖，正常修图使用 `preview(kind=native)` 看整张照片的合成效果。

read(layers) 返回 state.layers；用其中完整 layer_ref 选择图层。选层后 target.layer 改变，重新读参数，再用相同 adjust。masks 的 create_subject/create_background 调用 C1 AI 蒙版，返回创建结果；结果未知不重复创建。用 mask 预览检查覆盖，再看 native 效果。

create_empty 新建空调整层。remove_created 还须提供 creation_operation_id，只移除工具创建的层。图层／蒙版操作后引用可能变化，以新 read 为准。渐变坐标属于 RAW，不能用预览宽高直接归一化替代 map_points。

## 返回值

- ok：读取成功；control_confirmed：写入回读一致；completed：该操作完成。都不自动证明审美质量。
- refused：条件不满足，检查 error 和派发标记；不能一概理解为未触碰模型。
- partial/outcome_unknown：部分或未知结果，查原编号并重新读取，不自动重放或承诺事务回滚。
- 浮点回读存在微小误差；多 setter 中途失败不等于已写值自动恢复。

预览只保留最近 8 张缓存，重要前态由客户端自行保存。原生预览不是全尺寸导出或独立 ICC 验收。详见 [COLOR_API](COLOR_API.md) 和 [NAVIGATION_PREVIEW](NAVIGATION_PREVIEW.md)。
