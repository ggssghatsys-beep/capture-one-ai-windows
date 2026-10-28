"""Small UIA image operations; no legacy driver, workflow or disk configuration.

Browser IDs are UI observations, not proof of the active model variant. The
backend owns application/document identity and resolves a fresh observation
after selection or cloning. Every UI mutation passes through its action hook.
"""
from __future__ import annotations

import copy
import re
import uuid

BROWSER = {"automation_id": "MainItemsControl", "control_type": "List"}
SUMMARY = {"automation_id": "SummaryText", "control_type": "Text"}
VIEWER = {"automation_id": "tb_VariantTitle", "control_type": "Text"}
VARIANT_NAME = re.compile(r"^Variant: (.+) - VariantID: (\d+)$")
VIEWER_ORDINAL = re.compile(r" \[(\d+)\]$")
SINGLE_SELECTION_SUMMARY = re.compile(r"[ \t\u00a0\u3000]*([0-9]+)[ \t\u00a0\u3000]*个[ \t\u00a0\u3000]*，[ \t\u00a0\u3000]*共[ \t\u00a0\u3000]*([0-9]+)[ \t\u00a0\u3000]*个[ \t\u00a0\u3000]*")
FILTERED_SELECTION_SUMMARY = re.compile(r"([0-9]+)/([0-9]+) \(滤镜\)")


class NativeAdapterError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def fail(code, message, details=None):
    raise NativeAdapterError(code, message, details)


def checked_call(backend, observation, operation, **payload):
    result = backend.call(operation, **payload)
    expected = observation.get("raw_state", {}).get("window")
    actual = result.get("window") if isinstance(result, dict) else None
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        fail("WINDOW_UNVERIFIED", "原生适配器未取得完整窗口身份。")
    keys = ("pid", "hwnd", "title", "version")
    if any(key not in expected or actual.get(key) != expected[key] for key in keys):
        fail("TARGET_CHANGED", "原生操作期间应用或文档窗口改变。")
    return result


def inspect_nodes(backend, observation, selector, maximum=1200):
    result = checked_call(backend, observation, "inspect", selector=copy.deepcopy(selector),
                          max_nodes=maximum, tree_view="control")
    if (result.get("truncated") is not False or result.get("read_errors")
            or not isinstance(result.get("nodes"), list)
            or any(not isinstance(node, dict) for node in result["nodes"])):
        fail("INCOMPLETE_INSPECTION", "控件观察不完整；未推断缺失的项目。")
    return result["nodes"]


def read_node(backend, observation, selector):
    result = checked_call(backend, observation, "read", selector=copy.deepcopy(selector))
    if not isinstance(result.get("node"), dict):
        fail("CONTROL_UNREADABLE", "未取得控件读数。")
    return result["node"]


def invoke(backend, observation, selector, name, mode="invoke", extra_guard=None, intent=None):
    guard = copy.deepcopy(backend.guard(observation))
    if extra_guard:
        guard.setdefault("bindings", {}).update(extra_guard["bindings"])
        guard.setdefault("expected", {}).update(extra_guard["expected"])
    def dispatch():
        return checked_call(backend, observation, "invoke", selector=copy.deepcopy(selector),
                            mode=mode, guard=guard)
    result = backend.action(name, dispatch, intent=intent) if intent is not None else backend.action(name, dispatch)
    if not isinstance(result, dict) or result.get("accepted") is not True:
        fail("ACTION_OUTCOME_UNKNOWN", "原生动作未返回接受确认；不能自动重试。")
    return result


def parse_summary(text):
    # These are the only summary formats qualified by the old Chinese build.
    match = SINGLE_SELECTION_SUMMARY.fullmatch(text) if isinstance(text, str) else None
    filtered = False
    if match is None and isinstance(text, str):
        match = FILTERED_SELECTION_SUMMARY.fullmatch(text)
        filtered = match is not None
    if match is None and isinstance(text,str):
        match=re.fullmatch(r'([0-9]+)/([0-9]+)',text)
    if not match:
        fail("BROWSER_SUMMARY_UNSUPPORTED", "浏览器单选计数格式尚未验证。", {"observed": text})
    selected, total = map(int, match.groups())
    if total < 1 or selected > total:
        fail("BROWSER_SUMMARY_INVALID", "浏览器总数无效或选中数量大于总数。")
    return {"text": text, "selected_count": selected, "total_count": total, "filtered": filtered}


def _viewer_name(observation):
    name = observation.get("target", {}).get("variant", {}).get("label") if observation.get('evidence',{}).get('source')=='c1_internal_model' else observation.get("raw_state", {}).get("values", {}).get("selection", {}).get("name")
    if not isinstance(name, str) or not name:
        fail("TARGET_UNVERIFIED", "缺少当前查看器名称。")
    return name


def _selected(node):
    if "SelectionItem" in node.get("patterns", []) and type(node.get("selected")) is bool:
        return node["selected"]
    return None


def list_images(backend, observation=None):
    observation = observation or backend.observe([])
    summary = parse_summary(read_node(backend, observation, SUMMARY).get("name"))
    nodes = inspect_nodes(backend, observation, BROWSER, 2000)
    rows = []
    for node in nodes:
        if node.get("control_type") != "DataItem":
            continue
        match = VARIANT_NAME.fullmatch(node.get("name", ""))
        if not match:
            fail("BROWSER_ENUMERATION_INCOMPLETE", "浏览器中有无法识别的照片条目。")
        selector = node.get("selector")
        if (not isinstance(selector, dict) or selector.get("name") != node["name"]
                or selector.get("control_type") != "DataItem"):
            fail("PHOTO_SELECTOR_UNVERIFIED", "照片没有本次观察到的精确选择器。")
        rows.append({"filename": match[1], "browser_variant_id": int(match[2]),
                     "selected": _selected(node), "node": copy.deepcopy(node)})
    if len(rows) > summary["total_count"]:
        fail("BROWSER_ENUMERATION_INCOMPLETE", "可见照片条目数大于浏览器计数。")
    if len({row["browser_variant_id"] for row in rows}) != len(rows):
        fail("AMBIGUOUS_VARIANT", "浏览器包含重复 VariantID。")
    viewer = read_node(backend, observation, VIEWER).get("name")
    if viewer != _viewer_name(observation):
        fail("TARGET_CHANGED", "枚举期间查看器目标改变。")
    return {"items": rows, "summary": summary, "viewer_label": viewer,
            "observation": observation, "identity_basis": "browser_uia", "production_batch_qualified": False,
            "virtualized": len(rows) < summary["total_count"],
            "enumeration_complete": len(rows) == summary["total_count"],
            "scope": "current_browser_items_only"}


def _native_selected(inventory, requested_id):
    return (inventory["enumeration_complete"] and all(item["selected"] is not None for item in inventory["items"])
            and [item["browser_variant_id"] for item in inventory["items"] if item["selected"]] == [requested_id]
            and inventory["summary"]["selected_count"] == 1)


def select_image(backend, browser_variant_id, observation=None):
    """Use actual SelectionItem only. No keyboard, coordinates or ordinal guesses."""
    if type(browser_variant_id) is not int or browser_variant_id <= 0:
        fail("INVALID_VARIANT_ID", "需提供实际观察到的正整数浏览器 VariantID。")
    before = list_images(backend, observation)
    candidates = [item for item in before["items"] if item["browser_variant_id"] == browser_variant_id]
    if len(candidates) != 1:
        fail("PHOTO_NOT_FOUND", "未找到唯一浏览器目标。")
    candidate = candidates[0]
    if not before["enumeration_complete"] or not all(item["selected"] is not None for item in before["items"]):
        fail("NATIVE_SELECTION_UNSUPPORTED", "当前浏览器没有可核验的 SelectionItem；不退回方向键。")
    changed = not _native_selected(before, browser_variant_id)
    if changed:
        node = candidate["node"]
        if node.get("enabled") is not True or node.get("offscreen") is not False:
            fail("PHOTO_NOT_AVAILABLE", "目标条目不可见或不可用。")
        invoke(backend, before["observation"], node["selector"], "images.select", "select")
    after_observation = backend.observe([])
    after = list_images(backend, after_observation)
    old_observation = before["observation"]
    old_window = old_observation["raw_state"]["window"]
    new_window = after_observation["raw_state"]["window"]
    if (not _native_selected(after, browser_variant_id)
            or any(old_window.get(key) != new_window.get(key) for key in ("pid", "hwnd", "title", "version"))
            or any(old_observation["target"].get(key) != after_observation["target"].get(key)
                   for key in ("instance", "document"))
            or VIEWER_ORDINAL.sub("", after["viewer_label"]) != candidate["filename"]):
        fail("SELECTION_OUTCOME_UNKNOWN", "原生选择后没有读回唯一目标；不重复选择。")
    return {"changed": changed, "browser_variant_id": browser_variant_id,
            "selection_pattern_verified": True, "after": after_observation,
            "selected_variant_id_readback_verified": False,
            "note": "浏览器选择已核验；后端仍须建立应用模型变体身份。"}


def _source_variants(backend, observation):
    reader = getattr(backend, "variant_inventory", None)
    if not callable(reader):
        fail("VARIANT_INVENTORY_UNSUPPORTED", "后端尚未提供同源照片的只读变体清单。")
    rows = reader(observation)
    if not isinstance(rows, list) or not rows:
        fail("VARIANT_INVENTORY_UNVERIFIED", "同源变体清单为空或格式无效。")
    for row in rows:
        try:
            valid_uuid = isinstance(row.get("id"), str) and str(uuid.UUID(row["id"])) == row["id"]
        except (AttributeError, ValueError, TypeError):
            valid_uuid = False
        if (not valid_uuid or type(row.get("index")) is not int or row["index"] < 0
                or type(row.get("browser_variant_id")) is not int or row["browser_variant_id"] <= 0):
            fail("VARIANT_INVENTORY_UNVERIFIED", "同源变体缺少真实 UUID、序号或数据库标识。")
    if any(len({row[key] for row in rows}) != len(rows) for key in ("id", "index", "browser_variant_id")):
        fail("VARIANT_INVENTORY_UNVERIFIED", "同源变体 UUID、序号或数据库标识重复。")
    label = _viewer_name(observation)
    ordinal = VIEWER_ORDINAL.search(label)
    index = int(ordinal[1]) - 1 if ordinal else 0
    selected = [row for row in rows if row["id"] == observation["target"].get("variant", {}).get("id")]
    if len(selected) != 1 or selected[0]["index"] != index:
        fail("VARIANT_INVENTORY_UNVERIFIED", "已绑定变体身份与同源清单、查看器序号不一致。")
    return copy.deepcopy(rows)


def _clone_scope(observation):
    text = observation.get("raw_state", {}).get("values", {}).get("edit_scope", {}).get("name")
    summary = parse_summary(text)
    if summary["selected_count"] != 1:
        fail("SINGLE_SELECTION_REQUIRED", "创建候选要求当前恰好选中一张照片。")
    return summary


def clone_selected(backend, observation=None):
    obs = observation or backend.observe([])
    summary = _clone_scope(obs)
    before_label = _viewer_name(obs)
    filename = VIEWER_ORDINAL.sub("", before_label)
    old_inventory = _source_variants(backend, obs)
    old_rows = {row["id"]: row for row in old_inventory}
    # Support only observed localized menu labels; do not invent shortcuts.
    menu = {"name": "图像(I)", "control_type": "MenuItem"}
    scope_guard = {"bindings": {"clone_scope": SUMMARY},
                   "expected": {"clone_scope": {"name": summary["text"]}}}
    clone_baseline = {"before": obs, "source_variants": old_inventory}
    invoke(backend, obs, menu, "images.open_menu", "expand", scope_guard, intent=clone_baseline)
    # The full Image menu recursively exposes many unrelated submenus. Resolve
    # this exact qualified command instead; native resolution rejects duplicates.
    command = read_node(backend, obs, {"name": "复制变体", "control_type": "MenuItem"})
    if (command.get("name") != "复制变体" or command.get("control_type") != "MenuItem"
            or command.get("enabled") is not True or command.get("offscreen") is not False
            or "Invoke" not in command.get("patterns", [])
            or not isinstance(command.get("selector"), dict)):
        fail("CLONE_UNSUPPORTED", "未发现唯一的原生复制变体命令。")
    dispatch = invoke(backend, obs, command["selector"], "images.clone", extra_guard=scope_guard,
                      intent=clone_baseline)
    after_obs = backend.observe([])
    after_summary = _clone_scope(after_obs)
    after_label = _viewer_name(after_obs)
    new_inventory = _source_variants(backend, after_obs)
    new_rows = {row["id"]: row for row in new_inventory}
    added = set(new_rows) - set(old_rows)
    old_window = obs["raw_state"]["window"]
    new_window = after_obs["raw_state"]["window"]
    if (any(old_window.get(key) != new_window.get(key) for key in ("pid", "hwnd", "title", "version"))
            or any(obs["target"].get(key) != after_obs["target"].get(key)
                   for key in ("instance", "document", "image"))
            or after_summary["selected_count"] != 1
            or len(new_rows) != len(old_rows) + 1 or len(added) != 1
            or any(key not in new_rows or new_rows[key]["browser_variant_id"] != value["browser_variant_id"]
                   for key, value in old_rows.items())
            or next(iter(added)) != after_obs["target"]["variant"]["id"]
            or after_label == before_label or VIEWER_ORDINAL.sub("", after_label) != filename):
        fail("CLONE_OUTCOME_UNKNOWN", "同源变体差量或查看器身份不一致；不要重复创建。",
             {"before": old_rows, "after": new_rows, "after_observation": after_obs})
    candidate_id = next(iter(added))
    browser_id = new_rows[candidate_id]["browser_variant_id"]
    # The unique Session UUID delta and selected viewer already establish the
    # clone. A second walk of the entire virtualized browser adds no protection.
    browser,visible={"not_requested":True},False
    return {"candidate_browser_variant_id": browser_id, "candidate_variant_id": candidate_id,
            "before": obs, "after": after_obs, "source_variants_before": old_inventory,
            "source_variants_after": new_inventory, "browser_evidence": browser,
            "dispatch": dispatch, "new_browser_entry_verified": visible, "new_source_variant_verified": True,
            "selected_variant_id_readback_verified": False, "production_batch_qualified": False,
            "identity_basis": "same_source_session_uuid_delta_and_single_selected_ui_ordinal",
            "note": "唯一新增同源变体与重新绑定身份一致；范围限定为已核验 Session，不是原生选中 UUID 直接回读。"}


def recover_clone_navigation(backend, record):
    """Reconcile a completed menu-only action; never handles dispatched clones."""
    target = record.get("target")
    unknown = lambda reason: {"outcome": "unknown", "target": target,
                               "evidence": {"scope": "clone_navigation_only", "reason": reason,
                                            "model_verified": False, "replayed": False}}
    request = record.get("request", {})
    actions = record.get("actions", [])
    if (request.get("command") != "images" or request.get("action") not in ("clone", "create_candidate")
            or len(actions) != 1 or actions[0].get("name") != "images.open_menu"):
        return unknown("Not exactly one menu-only clone preparation action; clone recovery is not inferred")
    action = actions[0]
    if (action.get("status") != "completed" or action.get("result", {}).get("accepted") is not True
            or action.get("result", {}).get("after", {}).get("expand_state") != "Expanded"):
        return unknown("Menu action lacks a confirmed completed expansion")
    intent = action.get("intent", {})
    before, variants = intent.get("before"), intent.get("source_variants")
    if (not isinstance(before, dict) or not isinstance(variants, list) or not variants
            or before.get("target") != target or before.get("target") != record.get("before", {}).get("target")
            or before.get("values") != record.get("before", {}).get("values")):
        return unknown("The operation did not persist a complete source-variant baseline before menu expansion")
    current = backend.observe(list(before.get("values", {})))
    old_window, new_window = before.get("raw_state", {}).get("window", {}), current.get("raw_state", {}).get("window", {})
    if (current.get("target") != target or current.get("values") != before.get("values")
            or any(not old_window.get(key) or old_window[key] != new_window.get(key)
                   for key in ("pid", "hwnd", "title", "version"))
            or _clone_scope(current)["text"] != _clone_scope(before)["text"]):
        return unknown("Observed target, fields, application or single-selection scope changed")
    actual = _source_variants(backend, current)
    ordered = lambda rows: sorted(rows, key=lambda row: row["id"])
    if ordered(actual) != ordered(variants):
        return unknown("Same-source variant inventory changed")
    return {"outcome": "not_applied", "target": current["target"],
            "evidence": {"scope": "clone_not_dispatched_navigation_applied", "clone_dispatched": False,
                         "navigation_applied": True, "source_inventory_unchanged": True,
                         "source_variants_before": variants, "source_variants_after": actual,
                         "observation": current, "observed_fields": list(before.get("values", {})),
                         "unobserved_parameters_verified": False, "model_verified": False, "replayed": False}}
