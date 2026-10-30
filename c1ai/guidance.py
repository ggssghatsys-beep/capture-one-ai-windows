"""Client-neutral editing guidance; advisory, never a second execution engine."""
from copy import deepcopy

from .contracts import MODULES

ORDER = ('white_balance', 'exposure', 'hdr', 'levels', 'curves', 'color_mixer', 'color_balance')


def workflow():
    return {
        'capabilities_first': '先看context的editing_ready和本平台capabilities；未验证或未实现功能不可从其他平台推断。Mac支持Image与局部层数值和颜色模块，完整Color Editor粘贴要求明确保存的来源且有九个显式Basic色区。蒙版原生接口支持能力表列出的创建、填满/清空、复制、反相、Luma和滤镜；首次Luma写入是新增Add子蒙版，复制后使用新引用。启用匹配桥时Basic/Advanced/Skin、图层复制/排序、蒙版组合和子项控制均走后台原生；未配置桥时可选AX兼容路线要求相关工具可见；read(module)的module_capability反映本次可用性，skin_availability=requires_module_read表示尚未检查。Mac启用版本匹配的原生桥后，native整图和mask整层灰度预览直接使用C1内部渲染，线性/径向渐变在空调整层创建或对同类型简单渐变再编辑，使用统一归一化RAW坐标，不抢鼠标；没有配置桥时仍可显式配置Pilot整图。RAW map_points尚未实现，不能把旋转后的预览坐标直接当RAW。显式viewer线性鼠标路线仍是partial实验入口，不自动降级。具体代表性实测范围看action_qualification，不把一个组合模式的验证扩大到所有模式。',
        'scope': '用户已经打开并选定的Session及照片集合；默认不切换文档、不导出。',
        'transport': '本机MCP stdio或JSON CLI；不绑定模型、账号或聊天。',
        'analysis_first': '先context、read(fields=[])、preview(kind=native)，实际看图后区分可见问题、用户风格要求和不确定判断。可使用客户端已有的照片分析技能；本接口不依赖某个专属技能。',
        'candidate': '确认照片身份；只编辑用户指定或已登记的候选副本，必要时create_candidate。保留原变体，避免重复创建副本。',
        'module_order': list(ORDER),
        'module_loop': '仅选择有明确目的的模块：read(module) → adjust(module,绝对目标值) → 检查回读 → native预览。一个模块的相关参数合并请求；同模块多次技术调用可合并到一次预览。无需遍历所有模块。',
        'plan': '每个模块先说明画面依据、要改善的区域、目标及预计副作用；不要为照片自动套入固定参数或把API上限当作建议值。',
        'local_after_global': '先完成全局预览复核。仅在剩余问题局限于主体或背景时加蒙版；在平台支持时检查灰度蒙版覆盖；Mac用保存态蒙版证据与native整图检查，再用同一read/adjust接口调当前层。',
        'review': '用相同kind和max_size比较已实际查看的前后预览，检查主体层次、肤色、明暗细节、色彩关系及局部边缘。变亮或饱和度更高不自动代表更好。',
        'raw_limits': 'RAW通常有调整空间；渲染预览中的纯白/纯黑不能单独证明RAW传感器已经剪切，也不能保证细节一定可恢复。',
        'image_metrics': 'analyze_image 返回当前完整渲染的 RGB、亮度、ROI 和横向分区数值。先检查 freshness、target、修订和色彩假设；窗口的快速样本编号需显式 precision=display 才能 action=get 读取；AI 默认 precision=exact 另取精确样本，不能把窗口粗样本升级冒充精确值。统计量辅助判断，不代替画面审美与 RAW 可恢复性判断。',
        'stop': '达到用户目标且没有明确剩余问题就停止。没有可解释的收益不继续试数值；方向不符时用新观察凭据恢复该模块已保存的值，或有意粘贴保存的完整模块快照。',
        'evidence': '参数回读成功只证明执行结果；必须实际看到新预览才能判断画面。图片回传不可用时说明缺口，不声称已看图或已改善。Windows原生预览缓存最多8张，重要的前态需客户端在收到时保留。',
        'errors': '目标或观察过期则重新读取。写入超时/outcome_unknown先查原request_id的operations，禁止换编号重复执行。不要自动改用预设、UIA、截图识色或键鼠。',
        'excluded': ['清晰度', '锐化', '降噪', '镜头与几何调整', '人像精修', '导出'],
    }


_MODULE_GUIDANCE = {
    'white_balance': {
        'units': {'temperature': 'K', 'tint': 'C1色调数值'},
        'use_when': '有整体偏色或用户指定色温倾向；保留有意的环境光色，不强行把所有中性物体调成灰。',
        'review': '同时看肤色、背景中性色和环境氛围，避免用白平衡修复单个物体的颜色。'},
    'exposure': {
        'units': {'exposure': 'EV', 'contrast': 'C1数值', 'brightness': 'C1数值', 'saturation': 'C1数值'},
        'use_when': '先判断主体明暗关系和全局亮度，保持与画面意图相符的反差。',
        'review': '高光细节、暗部层次和主体立体感；RAW宽容度不等于必须提亮阴影。'},
    'hdr': {
        'units': 'C1高光/阴影/白色/黑色数值；不是EV或输出亮度百分比。',
        'use_when': '全局曝光后仍有明确亮部或暗部层次问题。',
        'review': '避免把所有区域拉到相近亮度，注意高光灰化和阴影过亮。'},
    'levels': {
        'units': '输入/输出端点0..255；midtone为-1..1的C1数值，不是gamma。',
        'use_when': '需要调整整体黑白点或色彩通道端点。',
        'review': '不要仅为填满直方图剪掉细节；输入黑点必须小于白点。'},
    'curves': {
        'units': '每个点[x,y]均为0..1；提交的是所选通道的完整控制点列表。',
        'use_when': '需要定形影调或通道色彩；先读取并保留不需改变的控制点。',
        'review': '观察相邻影调过渡，避免无目的增加控制点或用大幅S曲线代替曝光判断。'},
    'color_mixer': {
        'units': 'Basic/Advanced/Skin量程不同；RGB为0..255的C1原生Device RGB，不能把预览sRGB采样视为精确原生拾色。',
        'use_when': '特定颜色需要改变。优先读取已有色区，使用Basic明确色区或按原生index修改Advanced/Skin。',
        'review': '检查肤色与附近相近颜色是否一起改变；均匀度和饱和度不应默认增加。新增色区需复核覆盖与实际效果。'},
    'color_balance': {
        'units': 'x/y为C1色轮坐标，半径≤50；亮度为-50..50，主轮不接受亮度。',
        'use_when': '全局影调和物体颜色合理后，按用户意图处理阴影、中间调或高光色彩关系。',
        'review': '保留中性色、肤色和已有光色，不默认做青橙分离。'},
    'layers': {
        'units': 'opacity为0..100；身份使用完整layer_ref。',
        'use_when': '只有局部问题或有意分开调整时创建图层；先检查是否已有合适图层。',
        'review': '切换图层后重新read相应模块，确认target.layer；只按创建记录删除本工具创建的层。'},
    'masks': {
        'units': '渐变坐标按RAW原始宽高归一化；预览坐标先map_points，不能直接除以预览宽高。',
        'use_when': '全局调整后问题仍局限于某一区域；主体/背景、渐变或亮度范围按实际目标选择。',
        'review': 'kind=mask查看整层灰度覆盖，随后kind=native检查局部效果和边缘；灰度蒙版不会替代照片预览。'},
}


def read_guidance(request, observation):
    module = request.get('module')
    if module:
        names = [module]
    elif request.get('modules'):
        names = request['modules']
    else:
        fields = set(observation.get('values', {}))
        names = [name for name, members in MODULES.items() if fields.intersection(members)]
    modules = {name: deepcopy(_MODULE_GUIDANCE[name]) for name in names if name in _MODULE_GUIDANCE}
    if observation.get('target', {}).get('instance', {}).get('platform') == 'macos':
        if 'layers' in modules:
            modules['layers']['units'] = 'opacity为整数1..100；关闭图层用enabled=false；身份使用完整layer_ref。'
        if 'masks' in modules:
            modules['masks']['units'] = '子项index来自本次submask_inventory；-1为完整父蒙版。主体/背景、反相、填满/清空在配置桥后共用该桥；invert/fill/clear要求整父选中(-1)，复合/Luma父的整层反相拒绝。原生桥线性/径向使用归一化RAW坐标和半径；空层创建与同类型简单渐变再编辑共用同一action及完整目标几何，保留图层UUID和调色；Mac不支持map_points，不直接使用旋转后预览坐标。'
            modules['masks']['review'] = '启用原生桥后可用kind=mask查看整层灰度覆盖，再用kind=native检查整图效果；同一照片、图层和调整修订绑定两者。显式viewer线性鼠标路线仍返回partial。桥的组合和子项控制在后台执行；未配置桥时可选AX子项控制要求Layers工具可见且子项已展开。'
        if 'color_mixer' in modules:
            modules['color_mixer']['availability'] = '启用匹配桥时Basic/Advanced/Skin共用后台原生对象读写，支持Skin单一区域增删、RGB/范围/七字段、完整色彩中性重置；无需可见工具。未配置桥时Basic/Advanced走官方脚本，Skin七字段更新要求显式AX及可见的已有唯一Skin区域。查看本次module_capability的skin_availability和资格。'
    return {
        'write_semantics': '参数patch是绝对目标值，不是相对增量；省略字段保持原值。曲线points替换完整通道，模块paste替换完整模块。',
        'limits': 'context.capabilities与adjust的校验范围决定可执行边界；说明不覆盖运行时资格或原图保护。',
        'modules': modules,
    }


MCP_INSTRUCTIONS = (
    'Capture One: six editing tools plus shared image analysis using one serialized local owner. '
    'Windows editing and the experimental macOS port have separate capabilities; '
    'never infer Mac writing or preview support from Windows tool descriptions. '
    'Mac supports qualified Image and local-layer numeric and color modules, collection navigation and bounded explicit-candidate batches. An explicitly configured version-qualified Mac bridge provides native PNG previews of the edited photograph and whole composite layer mask, without export or focus. With no bridge configured, explicitly authorized Capture Pilot remains an optional whole-image JPEG source. Current-collection mode follows adjacent photos, sharing only the selected RAW through a temporary album and restoring browser selection afterward. Mac reset uses measured neutral values, not user/camera custom defaults; it excludes White Balance. With the matching native bridge, Color Editor reset includes Basic/Advanced/Skin and removes Advanced/Skin ranges. Without the bridge it covers Basic/Advanced only and preserves Skin. Mac paste requires the same instance, Session and processing engine; complete Color Editor paste includes Skin Tone and requires explicit saved snapshots with nine explicit Basic sectors in the source. Check module_actions qualification. Mac layer read/select/rename/enabled/opacity and local numeric/structured adjustment writes use native MaskUUID identities aligned with saved Session records with official Apple Events setters. Always obtain a fresh layer_ref from read(layers), select it, then read the requested module; null local values are unset, never zero. Layer reads save the Session to synchronize identity. Do not concurrently create/delete/reorder layers manually. Mac mask reads expose saved per-UUID structure and content evidence. Qualified mask actions are listed in capabilities; native fill supports density=1 only. First Luma Range write adds an Add submask, not intersection with the existing subject. Use native preview to judge the whole edited photograph; grayscale coverage inspection is optional diagnostics. Local reset/paste use the same guarded setters; consult module_actions qualification. Numeric paste requires explicit source values and never turns null into zero. Reset writes neutral values, not unset state. Paste copies adjustment parameters, not masks. With the matching native bridge, Mac layers.duplicate/move and masks.combine/combine_ai/select_submask/delete_submask run in the background through C1 model commands, using UUID inventories and complete saved-state proofs. They require no visible tools, focus or pointer. Without a bridge, the explicitly enabled AX/mouse compatibility routes remain available. layers.duplicate copies complete settings and the mask and returns a new layer_ref. Use the creation operation_id with remove_created for that copied layer. Mac layers.move uses the shared zero-based local index (bottom to top) and a whole selected parent; with the bridge it operates on native UUID order in the background. With the native bridge, all Color Editor modes use the same native list-copy/setter route: Skin add/update/remove, RGB/ranges/seven values and flags, Advanced inversion and Basic sectors. Complete neutral reset includes Skin. Without the bridge, optional explicitly enabled macOS AX supports updates to seven fields of an existing unique Skin range, combine {source_layer,mode} for a uniquely named simple source without Luma into a whole selected parent, combine_ai {mode,kind}, and submask selection/deletion; consult per-action qualification rather than assuming every mode is live verified. Color Editor Skin controls or the Layers tool must be visible, and child selection requires an expanded parent. Public Apple Events modules do not need visible tools. With the Mac bridge enabled, create_subject, create_background, invert, fill and clear also use the same native bridge. AI generation keeps the C1 feature gate. Invert/fill/clear require whole-parent selection (-1). Whole composite/Luma inversion is refused, not redirected to a child or rasterized. Simple raster/constant/linear/radial masks share invert; full-parent fill accepts density=1. With the native bridge enabled, masks.linear_gradient and masks.radial_gradient create on an empty adjustment layer or update a same-type simple gradient using the same complete normalized RAW geometry patch, with native parameter/undo/readback checks and no mouse. Layer UUID and other adjustments are preserved. Linear midpoint and radial axes, angle, falloff and inverted use the shared contract. Mac RAW map_points remains unavailable. Explicit coordinate_space=viewer is a separate partial experimental mouse route and is never an automatic fallback. A read of color_mixer returns module_capability with current Skin availability; requires_module_read in context means availability has not been checked, not lack of implementation. '
    'Both platforms share analyze_image and the same analysis implementation. '
    'analyze_image returns direct numerical statistics from the rendered crop; check freshness, identity and color assumptions. '
    'Call context for the shared editing workflow, target and capabilities; read returns module guidance/units. '
    'Inspect a native preview before choosing adjustments. Edit one necessary module at a time, '
    'using absolute values and fresh observations. Suggested order: '+', '.join(ORDER)+'. '
    'Review the global result before adding local masks. Stop when the user goal is met. '
    'Stay in the selected Session; no export or document switching by default. Only registered copies are writable. '
    'Readback proves execution, not aesthetic improvement. '
    'Use an explicit unique request_id for each write so an uncertain result can be queried with operations; '
    'never repeat an uncertain write with a new ID. A client with vision/image-block support is required '
    'for visual editing decisions. Use native paths; do not silently fall back to UI/preset editing.'
)
