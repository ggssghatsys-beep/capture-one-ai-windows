"""Fixed-recipe Capture One export through UIA, with separate read-only recovery.

Only the old bridge's bounded recipe-row matching and export selectors are
adapted here. No legacy workflow, driver, mutable profile or reports are loaded.
An accepted export is not a verified render. Receipts must be persisted by Core
before/at dispatch; a timeout is recovered with read_export, never another submit.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from .native_images import checked_call, fail, inspect_nodes, invoke, read_node

EXPORT = {"automation_id": "export", "control_type": "Window"}
EXPORT_OPEN = {"automation_id": "MainButton", "control_type": "Button",
               "ancestors": [{"automation_id": "Export", "control_type": "Custom"}]}
SIMPLE = ("automation_id", "name", "help_text", "control_type", "class_name")
SRGB = "sRGB IEC61966-2.1"
MAX_PREVIEW_BYTES = 128 * 1024 * 1024


def _plain_path(value, *, directory=False):
    """Reject links/reparse points before resolving, including every ancestor."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        fail("PREVIEW_PATH_UNVERIFIED", "路径必须为不含上级跳转的绝对路径。")
    for part in (path, *path.parents):
        try:
            stat = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            fail("PREVIEW_PATH_UNVERIFIED", "预览路径及祖先不能包含符号链接或重解析点。")
    if directory and not path.is_dir():
        fail("PREVIEW_PATH_UNVERIFIED", "预览目录不存在。")
    return path.resolve()


def _bounded_output(value, root):
    output = _plain_path(value)
    _plain_path(output.parent, directory=True)
    if root is not None:
        expected = _plain_path(root, directory=True)
        if output == expected or not output.is_relative_to(expected):
            fail("PREVIEW_PATH_OUTSIDE_ROOT", "配方输出不在本次指定目录内。")
    return output


def _archive_root(backend):
    return _plain_path(_plain_path(backend.state_dir, directory=True) / "renders")


def _owned_path(backend):
    return _plain_path(_plain_path(backend.state_dir, directory=True) / "export-owned.json")


def _load_owned(backend):
    path = _owned_path(backend)
    if not path.exists():
        return {"schema": "c1ai-export-owned-v1", "files": {}}
    try:
        record = json.loads(path.read_text("utf-8"))
    except (ValueError, OSError) as exc:
        fail("EXPORT_OWNERSHIP_INVALID", "预览临时文件归属记录无法读取。", {"reason": str(exc)})
    if not isinstance(record, dict) or record.get("schema") != "c1ai-export-owned-v1" or not isinstance(record.get("files"), dict):
        fail("EXPORT_OWNERSHIP_INVALID", "预览临时文件归属记录格式不匹配。")
    return record


def _save_owned(backend, record):
    destination = _owned_path(backend)
    # A unique local temporary file avoids following a pre-existing .tmp link.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                     prefix=".export-owned-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(record, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _owned_path(backend)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _file_hash(path):
    path = _plain_path(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        fail("EXPORT_OWNERSHIP_INVALID", "预览归属仅适用于单一普通文件。")
    before = path.stat()
    if before.st_size > MAX_PREVIEW_BYTES:
        fail("EXPORT_OWNERSHIP_INVALID", "文件超过预览大小限制；保留文件并停止复用。")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        count = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(block)
            if count > MAX_PREVIEW_BYTES:
                fail("EXPORT_CHANGED", "哈希核对期间文件超过预览大小限制。")
            digest.update(block)
    after = path.stat()
    fingerprint = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if fingerprint(before) != fingerprint(after):
        fail("EXPORT_CHANGED", "核对哈希期间文件发生变化。")
    return digest.hexdigest(), fingerprint(after)


def _owned_pair(backend, output, root, entry):
    if root is None or not isinstance(entry, dict):
        fail("OUTPUT_EXISTS", "现有输出没有已核验的专用目录和归属凭据。")
    output = _bounded_output(output, root)
    expected = _plain_path(root, directory=True)
    digest = entry.get("sha256")
    if (entry.get("export_path") != str(output) or entry.get("output_root") != str(expected)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        fail("EXPORT_OWNERSHIP_INVALID", "输出路径、目录或归属哈希与本次计划不一致。")
    archive = _plain_path(entry.get("archive_path", ""))
    required_archive = _archive_root(backend) / (digest + ".jpg")
    if archive != required_archive or archive == output:
        fail("EXPORT_OWNERSHIP_INVALID", "归档不是独立的固定哈希预览文件。")
    archive_hash, _ = _file_hash(archive)
    output_hash, fingerprint = _file_hash(output)
    if archive_hash != digest or output_hash != digest:
        fail("EXPORT_OWNERSHIP_INVALID", "输出或归档已改变；保留文件并停止复用。")
    return fingerprint


def _release_owned_export(backend, output, output_root):
    """One journaled unlink, only after a content-addressed archive is verified."""
    output = _bounded_output(output, output_root)
    if not output.exists():
        return False
    record = _load_owned(backend)
    entry = record["files"].get(str(output))
    _owned_pair(backend, output, output_root, entry)
    def release():
        # Recheck after the durable action hook, immediately before this unlink.
        latest = _load_owned(backend)
        if latest["files"].get(str(output)) != entry:
            fail("EXPORT_OWNERSHIP_INVALID", "写前记录后临时文件归属发生变化。")
        fingerprint = _owned_pair(backend, output, output_root, entry)
        now = _plain_path(output).stat()
        if (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != fingerprint:
            fail("EXPORT_CHANGED", "清理前临时文件发生变化。")
        output.unlink()
        latest["files"].pop(str(output))
        _save_owned(backend, latest)
        return {"removed": str(output), "sha256": entry["sha256"], "archive_path": entry["archive_path"]}
    backend.action("preview.release_owned_export", release, intent={"owned_export": copy.deepcopy(entry)})
    return True


def _record_owned_export(backend, receipt, output, archive, digest):
    root = receipt.get("output_root")
    # Without an explicit root or independent archive, reading never grants deletion authority.
    if root is None or output == archive:
        return
    entry = {"export_path": str(output), "output_root": str(_plain_path(root, directory=True)),
             "sha256": digest, "archive_path": str(archive), "verified_at": time.time()}
    _owned_pair(backend, output, root, entry)
    record = _load_owned(backend)
    record["files"][str(output)] = entry
    _save_owned(backend, record)


def _rid(node):
    value = node.get("runtime_id")
    if not isinstance(value, list) or not value or any(type(item) is not int for item in value):
        fail("PREVIEW_RECIPE_UNVERIFIED", "配方控件缺少运行时身份。")
    return tuple(value)


def _unique(nodes):
    result = {}
    for node in nodes:
        key = _rid(node)
        if key in result and result[key] != node:
            fail("PREVIEW_RECIPE_UNVERIFIED", "同一配方控件出现冲突读数。")
        result[key] = node
    return list(result.values())


def _parents(node):
    return node.get("selector", {}).get("ancestors", [])


def _row(node):
    parents = _parents(node)
    return (node.get("control_type") == "ListItem" and parents
            and parents[0].get("automation_id") == "listViewControl"
            and any(parent.get("automation_id") == "ExportMultipleRecipeView" for parent in parents))


def _child(node):
    parents = _parents(node)
    return (len(parents) >= 2 and parents[0].get("control_type") == "ListItem"
            and parents[1].get("automation_id") == "listViewControl"
            and any(parent.get("automation_id") == "ExportMultipleRecipeView" for parent in parents))


def _checked(nodes):
    checks = _unique([node for node in nodes if node.get("automation_id") == "EnabledCheckbox"])
    if not checks or any(node.get("toggle") not in ("On", "Off") or not _child(node) for node in checks):
        fail("PREVIEW_RECIPE_UNVERIFIED", "无法核对全部配方勾选状态。")
    enabled = [node for node in checks if node["toggle"] == "On"]
    if len(enabled) != 1:
        fail("PREVIEW_RECIPE_REQUIRED", "预览要求恰好启用一个导出配方。")
    return enabled[0]


def _labels(nodes):
    labels = _unique([node for node in nodes if node.get("automation_id") == "NameLabel" and _child(node)])
    names = [node.get("name") for node in labels]
    if (not labels or len(labels) > 128 or any(not isinstance(name, str) or not name.strip() for name in names)
            or len(names) != len(set(names))):
        fail("PREVIEW_RECIPE_UNVERIFIED", "配方名称缺失、重复或超出范围。")
    return labels


def _simple(value):
    return {key: value[key] for key in SIMPLE if value.get(key)}


def _row_selector(label, nodes):
    parents = _parents(label)
    prefix = [_simple(parent) for parent in parents[1:]]
    row_fields = _simple(parents[0])
    possibilities = []
    for row in _unique([node for node in nodes if _row(node)]):
        ancestors = [_simple(parent) for parent in _parents(row)]
        if (_simple(row.get("selector", {})) == row_fields and ancestors[:len(prefix)] == prefix
                and ancestors not in possibilities):
            possibilities.append(ancestors)
    if (len(possibilities) != 1 or len(possibilities[0]) > 8
            or not any(parent.get("automation_id") == "export" and parent.get("control_type") == "Window"
                       for parent in possibilities[0])):
        fail("PREVIEW_RECIPE_UNVERIFIED", "配方行没有唯一、完整的导出窗口归属。")
    return {**row_fields, "ancestors": possibilities[0],
            "child": {"automation_id": "NameLabel", "name": label["name"], "control_type": "Text"}}


def _snapshot_recipe(nodes, expected_recipe):
    """Bind row/label/check from fresh native parent edges, without UI queries."""
    labels = _labels(nodes)
    checks = _unique([node for node in nodes if node.get("automation_id") == "EnabledCheckbox"])
    children = labels + checks
    if not any("parent_runtime_id" in node for node in children):
        return None  # Compatibility with older qualified native workers.
    rows = _unique([node for node in nodes if _row(node)])
    row_by_id = {_rid(row): row for row in rows}
    by_parent = {key: {"labels": [], "checks": []} for key in row_by_id}
    if not rows or len(labels) != len(rows) or len(checks) != len(rows):
        fail("PREVIEW_RECIPE_UNVERIFIED", "配方行、名称和勾选控件数量不一致。")
    for kind, controls in (("labels", labels), ("checks", checks)):
        for node in controls:
            parent = node.get("parent_runtime_id")
            if (not _child(node) or not isinstance(parent, list) or not parent
                    or any(type(value) is not int for value in parent) or tuple(parent) not in row_by_id):
                fail("PREVIEW_RECIPE_UNVERIFIED", "配方子控件缺少本次快照中的明确父行身份。")
            by_parent[tuple(parent)][kind].append(node)
    if any(len(group["labels"]) != 1 or len(group["checks"]) != 1 for group in by_parent.values()):
        fail("PREVIEW_RECIPE_UNVERIFIED", "配方父行与名称、勾选控件不是一一对应。")
    enabled = _checked(nodes)
    wanted = [label for label in labels if label.get("name") == expected_recipe]
    if len(wanted) != 1:
        fail("PREVIEW_RECIPE_REQUIRED", "没有找到指定的现有预览配方。")
    label = wanted[0]
    row = row_by_id[tuple(label["parent_runtime_id"])]
    if tuple(enabled["parent_runtime_id"]) != _rid(row):
        fail("PREVIEW_RECIPE_REQUIRED", "唯一启用配方不是指定的预览配方。")
    if any(type(item.get("selected")) is not bool for item in rows):
        fail("PREVIEW_RECIPE_UNVERIFIED", "配方设置页选择状态无法确认。")
    selected = [item for item in rows if item["selected"]]
    if len(selected) != 1:
        fail("PREVIEW_RECIPE_UNVERIFIED", "未发现唯一配方设置页。")
    return {"row": row, "label": label, "enabled": enabled,
            "selector": _row_selector(label, nodes), "selected": _rid(selected[0]) == _rid(row)}


def _inspect_export(backend, observation):
    result = checked_call(backend, observation, "inspect", selector=copy.deepcopy(EXPORT),
                          profile="export", max_nodes=2000, tree_view="control")
    nodes, visited = result.get("nodes"), result.get("visited_nodes")
    if (result.get("profile") != "export" or result.get("truncated") is not False
            or result.get("read_errors") != [] or not isinstance(nodes, list)
            or any(not isinstance(node, dict) for node in nodes)
            or type(visited) is not int or not len(nodes) <= visited <= 2000 or visited < 1):
        fail("INCOMPLETE_INSPECTION", "导出投影未证明已完整遍历；不能把返回条目数当作完整性。")
    return nodes


def verify_recipe(backend, observation, expected_recipe, *, allow_select=False):
    """Bind the one checked recipe to its actual settings page; never toggle it."""
    if not isinstance(expected_recipe, str) or not expected_recipe.strip():
        fail("PREVIEW_RECIPE_REQUIRED", "需明确配置预览配方名。")
    nodes = _inspect_export(backend, observation)
    snapshot = _snapshot_recipe(nodes, expected_recipe)
    if snapshot is not None:
        changed = not snapshot["selected"]
        if changed:
            if not allow_select:
                fail("PREVIEW_RECIPE_CHANGED", "设置页不再属于已核验的预览配方。")
            row = snapshot["row"]
            if row.get("enabled") is not True or row.get("offscreen") is not False or "SelectionItem" not in row.get("patterns", []):
                fail("PREVIEW_RECIPE_UNAVAILABLE", "无法用原生选择接口显示配方设置。")
            invoke(backend, observation, snapshot["selector"], "preview.select_recipe", "select")
            nodes = _inspect_export(backend, observation)
            after = _snapshot_recipe(nodes, expected_recipe)
            if (after is None or not after["selected"] or
                    any(_rid(after[key]) != _rid(snapshot[key]) for key in ("row", "label", "enabled"))):
                fail("PREVIEW_RECIPE_CHANGED", "选择配方后父行身份或设置页发生变化。")
        return {"nodes": nodes, "recipe_name": expected_recipe, "selection_changed": changed,
                "checked_runtime_id": list(_rid(snapshot["enabled"])), "membership_basis": "native_parent_runtime_id"}
    enabled = _checked(nodes)
    labels = _labels(nodes)
    wanted = [node for node in labels if node["name"] == expected_recipe]
    if len(wanted) != 1:
        fail("PREVIEW_RECIPE_REQUIRED", "没有找到指定的现有预览配方；不会自动更换或创建配方。")
    label = wanted[0]
    selector = _row_selector(label, nodes)
    scoped = inspect_nodes(backend, observation, selector, 64)
    rows = _unique([node for node in scoped if _row(node)])
    scoped_labels = _unique([node for node in scoped if node.get("automation_id") == "NameLabel" and _child(node)])
    checks = _unique([node for node in scoped if node.get("automation_id") == "EnabledCheckbox" and _child(node)])
    if (len(rows) != 1 or len(scoped_labels) != 1 or len(checks) != 1
            or _rid(scoped_labels[0]) != _rid(label) or scoped_labels[0].get("name") != expected_recipe
            or _rid(checks[0]) != _rid(enabled) or checks[0].get("toggle") != "On"
            or type(rows[0].get("selected")) is not bool):
        fail("PREVIEW_RECIPE_UNVERIFIED", "指定配方不是唯一启用配方，或行身份已改变。")
    row = rows[0]
    changed = not row["selected"]
    if changed:
        if not allow_select:
            fail("PREVIEW_RECIPE_CHANGED", "导出设置页不再属于已核验的预览配方。")
        if row.get("enabled") is not True or row.get("offscreen") is not False or "SelectionItem" not in row.get("patterns", []):
            fail("PREVIEW_RECIPE_UNAVAILABLE", "无法用原生选择接口显示配方设置。")
        invoke(backend, observation, selector, "preview.select_recipe", "select")
    nodes = _inspect_export(backend, observation)
    current = _checked(nodes)
    current_labels = _labels(nodes)
    current_rows = _unique([node for node in nodes if _row(node)])
    selected = [node for node in current_rows if node.get("selected") is True]
    if (_rid(current) != _rid(enabled) or len(selected) != 1 or _rid(selected[0]) != _rid(row)
            or any(type(node.get("selected")) is not bool for node in current_rows)
            or not any(_rid(node) == _rid(label) and node.get("name") == expected_recipe for node in current_labels)):
        fail("PREVIEW_RECIPE_CHANGED", "最终核验时启用配方或设置页发生变化。")
    return {"nodes": nodes, "recipe_name": expected_recipe, "selection_changed": changed,
            "checked_runtime_id": list(_rid(enabled))}


def _only(nodes, predicate, description):
    matches = [node for node in nodes if predicate(node)]
    if len(matches) != 1:
        fail("PREVIEW_RECIPE_UNVERIFIED", "无法唯一核对" + description + "。")
    return matches[0]


def export_settings(recipe, output_root=None, *, allow_existing=False):
    nodes = recipe["nodes"]
    fmt = _only(nodes, lambda n: n.get("control_type") == "ComboBox"
                and n.get("selector", {}).get("previous_sibling", {}).get("name") in ("格式", "Format")
                and any(p.get("class_name") == "ExportFormatAndSizeToolView" for p in _parents(n)), "导出格式")
    selection = fmt.get("selection")
    if not isinstance(selection, list) or len(selection) != 1 or selection[0].get("name") != "JPEG":
        fail("PREVIEW_RECIPE_REQUIRED", "已启用配方的实际导出格式不是 JPEG。")
    folder = _only(nodes, lambda n: n.get("control_type") == "Text" and
                   n.get("selector", {}).get("previous_sibling", {}).get("name") == "样例路径", "输出样例目录")["name"]
    filename = _only(nodes, lambda n: n.get("control_type") == "Text" and
                     n.get("selector", {}).get("previous_sibling", {}).get("name") == "采样", "输出样例文件名")["name"]
    if (not isinstance(folder, str) or not isinstance(filename, str) or not Path(folder).is_absolute()
            or not filename or Path(filename).name != filename or any(c in filename for c in '/\\:*?"<>|\x00\r\n')
            or Path(filename).suffix.lower() not in (".jpg", ".jpeg")):
        fail("PREVIEW_PATH_UNVERIFIED", "当前配方没有可核验的绝对 JPEG 输出路径。")
    output = _bounded_output(Path(folder) / filename, output_root)
    if output.exists() and not allow_existing:
        fail("OUTPUT_EXISTS", "输出路径已有内容；不会覆盖或猜测自动编号文件。")
    profiles = _unique([node for node in nodes if node.get("control_type") == "MenuItem" and
                        any(p.get("class_name") == "OutputColorProfileMenuView" for p in _parents(node))])
    if len(profiles) != 1 or profiles[0].get("name") != SRGB:
        fail("PREVIEW_PROFILE_REQUIRED", "预览配方必须实际选中 sRGB IEC61966-2.1。")
    action = _only(nodes, lambda n: n.get("automation_id") == "export" and n.get("control_type") == "Button"
                   and n.get("name") == "导出 1 个图像", "单张导出按钮")
    if not isinstance(action.get("selector"), dict):
        fail("PREVIEW_RECIPE_UNVERIFIED", "导出按钮缺少本次观察到的选择器。")
    return {"recipe_name": recipe["recipe_name"], "output_path": str(output), "format": "JPEG",
            "profile": SRGB, "enabled_recipes": 1, "image_count": 1,
            "checked_runtime_id": recipe["checked_runtime_id"], "export_selector": action["selector"]}


def _assert_observation(expected, current):
    if expected.get("target") != current.get("target") or expected.get("values", {}) != current.get("values", {}):
        fail("PREVIEW_TARGET_CHANGED", "导出关联的目标或已观察参数发生变化。")


def _open_verified_recipe(backend, observation, expected_recipe):
    # A cheap window read establishes presence. Do not enumerate every descendant
    # just to decide whether the same window must be opened.
    try:
        read_node(backend, observation, EXPORT)
    except Exception as exc:
        if getattr(exc, "code", None) != "CONTROL_NOT_FOUND":
            raise
        invoke(backend, observation, EXPORT_OPEN, "preview.open_export")
    return verify_recipe(backend, observation, expected_recipe, allow_select=True)


def _destination_metadata(backend):
    path = _plain_path(_plain_path(backend.state_dir, directory=True) / "export-destination.json")
    if not path.exists():
        return path, {"schema": "c1ai-export-destination-v1", "destinations": {}}
    try:
        value = json.loads(path.read_text("utf-8"))
    except (ValueError, OSError) as exc:
        fail("DESTINATION_METADATA_INVALID", "导出目录恢复记录无法读取。", {"reason": str(exc)})
    if (not isinstance(value, dict) or value.get("schema") != "c1ai-export-destination-v1"
            or not isinstance(value.get("destinations"), dict)):
        fail("DESTINATION_METADATA_INVALID", "导出目录恢复记录格式不匹配。")
    return path, value


def _save_destination_metadata(backend, metadata):
    path, _ = _destination_metadata(backend)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".export-destination-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(metadata, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _destination_metadata(backend)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sample_directory_binding(recipe):
    sample = _only(recipe["nodes"], lambda node: node.get("control_type") == "Text"
                   and node.get("selector", {}).get("previous_sibling", {}).get("name") == "样例路径",
                   "输出样例目录")
    selector = copy.deepcopy(sample.get("selector"))
    if (not isinstance(selector, dict) or selector.get("control_type") != "Text"
            or not any(parent.get("automation_id") == "export" and parent.get("control_type") == "Window"
                       for parent in selector.get("ancestors", []))):
        fail("DESTINATION_UNVERIFIED", "样例目录没有本次观察到的导出窗口绑定。")
    # Only the field's own dynamic path is removed. Its observed label and all
    # owning ancestors stay bound; subsequent reads can see a changed directory.
    selector.pop("name", None)
    selector.pop("help_text", None)
    return sample, selector


def _root_menu(recipe):
    menu = _only(recipe["nodes"], lambda node: node.get("control_type") == "MenuItem"
                 and (node.get("automation_id") == "RootFolderMenu" or
                      any(parent.get("class_name") == "ExportDestinationRootFolderSelector" for parent in _parents(node)))
                 and "ExpandCollapse" in node.get("patterns", []), "导出根目录菜单")
    if (not isinstance(menu.get("selector"), dict) or menu.get("enabled") is not True
            or menu.get("offscreen") is not False):
        fail("DESTINATION_UNVERIFIED", "实际导出根目录菜单不可用。")
    return menu


def _sample_directory_readback(backend, observation, selector):
    node = read_node(backend, observation, selector)
    if node.get("control_type") != "Text" or not isinstance(node.get("name"), str):
        fail("DESTINATION_UNVERIFIED", "未读回实际输出样例目录。")
    return _plain_path(node["name"], directory=True), node["name"]


def _sample_directory(backend, observation, selector):
    return _sample_directory_readback(backend, observation, selector)[0]


def _wait_sample_directory(backend, observation, selector, desired, previous, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        actual = _sample_directory(backend, observation, selector)
        if actual == desired:
            return actual
        if actual != previous:
            fail("DESTINATION_CONFLICT", "实际输出样例目录变为其他位置；不继续确认。", {"actual": str(actual)})
        if time.monotonic() >= deadline:
            fail("DESTINATION_PENDING", "目录选择尚未在 C1 样例路径生效；恢复同一操作，不重新选择。")
        time.sleep(0.15)


def _wait_folder_picker(backend, observation, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = checked_call(backend, observation, "inspect_folder_picker")
            if not isinstance(result.get("picker"), dict):
                fail("DESTINATION_UNVERIFIED", "原生目录对话框缺少所有者凭据。")
            return result
        except Exception as exc:
            if getattr(exc, "code", None) != "FOLDER_PICKER_UNAVAILABLE" or time.monotonic() >= deadline:
                raise
        time.sleep(0.15)


def _directory_action(backend, observation, operation, name, path, picker, intent, *, expected_edit_text=None):
    extra = {}
    if operation == "select_current_export_folder":
        if not isinstance(expected_edit_text, str) or not expected_edit_text:
            fail("DESTINATION_UNVERIFIED", "选择当前目录缺少本次观察的原生输入框原文。")
        if expected_edit_text != Path(path).name and _plain_path(expected_edit_text) != _plain_path(path):
            fail("DESTINATION_UNVERIFIED", "原生输入框不是目标目录路径或名称；未派发选择当前目录。")
        extra["expected_edit_text"] = expected_edit_text
    def dispatch():
        result = checked_call(backend, observation, operation, path=str(path),
                              expected_picker=copy.deepcopy(picker), guard=backend.guard(observation), **extra)
        if result.get("accepted") is not True:
            fail("DESTINATION_OUTCOME_UNKNOWN", "目录动作没有接受凭据；不能自动重试。")
        return result
    return backend.action(name, dispatch, intent=copy.deepcopy(intent))


def _start_destination_change(backend, observation, selector, name, intent, metadata, extra_guard=None):
    """Persist pending metadata only after Core has recorded this native action."""
    _, saved_metadata = _destination_metadata(backend)
    action_intent = copy.deepcopy(intent)
    action_intent["metadata_entry_before"] = copy.deepcopy(saved_metadata["destinations"].get(intent["metadata_key"]))
    guard = copy.deepcopy(backend.guard(observation))
    if extra_guard:
        guard.setdefault("bindings", {}).update(extra_guard["bindings"])
        guard.setdefault("expected", {}).update(extra_guard["expected"])
    def dispatch():
        _save_destination_metadata(backend, metadata)
        try:
            result = checked_call(backend, observation, "invoke", selector=copy.deepcopy(selector), mode="expand", guard=guard)
        except Exception as error:
            dispatched = getattr(error, "dispatched", None)
            no_dispatch = dispatched is False or (dispatched is not True and getattr(error, "not_dispatched", None) is True)
            if no_dispatch:
                try:
                    _save_destination_metadata(backend, saved_metadata)
                except Exception as rollback_error:
                    # The enclosing action includes local recovery metadata. If
                    # undoing that metadata fails, do not certify it as refused.
                    fail("DESTINATION_METADATA_RECOVERY_REQUIRED", "原生动作未派发，但目录恢复记录未能撤回；原操作仍需核对。",
                         {"native_error": str(error), "metadata_error": str(rollback_error)})
            raise
        if result.get("accepted") is not True:
            fail("DESTINATION_OUTCOME_UNKNOWN", "目录导航没有接受凭据；不能自动重试。")
        return result
    return backend.action(name, dispatch, intent=action_intent)


def ensure_owned_destination(backend, observation, recipe=None):
    """Set the fixed scratch directory once, with guarded navigation/confirmation.

    Read-only waits never repeat any UI action. If an action raises or the sample
    stays unchanged, Core must recover the original operation using its intents.
    """
    state = _plain_path(backend.state_dir, directory=True)
    root = _plain_path(state / "export-slot")
    root.mkdir(exist_ok=True)
    root = _plain_path(root, directory=True)
    if recipe is None:
        recipe = _open_verified_recipe(backend, observation, backend.config.get("preview_recipe"))
    # Read-only validation may allow an existing user file. This function neither
    # exports nor deletes that file; output reuse has its separate hash lease.
    original_settings = export_settings(recipe, allow_existing=True)
    sample, sample_selector = _sample_directory_binding(recipe)
    previous, previous_name = _sample_directory_readback(backend, observation, sample_selector)
    if previous != _plain_path(sample["name"], directory=True):
        fail("DESTINATION_CONFLICT", "配方观察之后样例目录已改变。")
    metadata_path, metadata = _destination_metadata(backend)
    key_data = {"document": observation["target"].get("document"), "recipe_name": recipe["recipe_name"]}
    key = hashlib.sha256(json.dumps(key_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    prior = metadata["destinations"].get(key)
    _assert_observation(observation, backend.observe(list(observation.get("values", {}))))
    menu, cycle = None, 1
    if prior is not None:
        if (not isinstance(prior, dict) or prior.get("document") != key_data["document"]
                or prior.get("recipe_name") != key_data["recipe_name"] or prior.get("owned_root") != str(root)):
            fail("DESTINATION_RECOVERY_REQUIRED", "已有目录记录的文档、配方或专用目录不匹配；不能重新接管。")
        status = prior.get("status")
        if status in ("pending", "restoring"):
            fail("DESTINATION_RECOVERY_REQUIRED", "已有未核验的目录动作；必须在原操作中恢复，不能由新请求更改状态。")
        if status == "restored":
            menu = _root_menu(recipe)
            if (previous != _plain_path(prior.get("previous_root", ""), directory=True)
                    or not prior.get("previous_menu_name") or menu.get("name") != prior["previous_menu_name"]):
                fail("DESTINATION_RECOVERY_REQUIRED", "上轮恢复后目录或菜单模式已改变；不能把新位置当作原目录重新接管。")
            previous_cycle = prior.get("cycle", 1)
            if type(previous_cycle) is not int or previous_cycle < 1:
                fail("DESTINATION_METADATA_INVALID", "目录接管轮次记录无效。")
            cycle = previous_cycle + 1
        elif status != "selected" or previous != root:
            fail("DESTINATION_RECOVERY_REQUIRED", "已有目录接管状态与当前样例目录不一致；先核对原操作。")
    if previous == root:
        return {"changed": False, "root": str(root), "settings": original_settings, "recipe": recipe,
                "sample_selector": sample_selector, "target": observation["target"],
                "native_sample_path_verified": True, "metadata_path": str(metadata_path),
                "previous_root": prior.get("previous_root") if isinstance(prior, dict) else None}
    menu = menu or _root_menu(recipe)
    entry = {**key_data, "previous_root": str(previous), "previous_menu_name": menu.get("name"),
             "previous_menu_selector": copy.deepcopy(menu["selector"]),
             "owned_root": str(root), "sample_selector": sample_selector, "status": "pending",
             "cycle": cycle, "created_at": time.time(), "before": copy.deepcopy(observation),
             "restoration_scope": "exact_native_menu_choice_and_directory_required"}
    # Retain fixed historical provenance without recursively nesting prior
    # cycles. The first journal action stores the complete immediate prior entry.
    if prior is not None:
        for field in ("source_evidence", "historical_menu_option_selector", "imported_at",
                      "evidence_scope", "current_adoption_observation"):
            if field in prior:
                entry[field] = copy.deepcopy(prior[field])
    metadata["destinations"][key] = entry
    intent = {"path": str(root), "oldroot": str(previous), "previous_root": str(previous), "picker": None,
              "recipe_name": recipe["recipe_name"], "sample_selector": sample_selector,
              "metadata_key": key, "cycle": cycle, "before": copy.deepcopy(observation)}
    extra_guard = {"bindings": {"export_destination": sample_selector},
                   "expected": {"export_destination": {"name": previous_name}}}
    _start_destination_change(backend, observation, menu["selector"], "preview.destination_menu", intent, metadata,
                              extra_guard=extra_guard)
    choice = read_node(backend, observation, {"name": "选择文件夹…", "control_type": "MenuItem", "class_name": "MenuItem"})
    if (choice.get("name") != "选择文件夹…" or choice.get("control_type") != "MenuItem"
            or choice.get("enabled") is not True or choice.get("offscreen") is not False
            or "Invoke" not in choice.get("patterns", []) or not isinstance(choice.get("selector"), dict)):
        fail("DESTINATION_UNVERIFIED", "没有可核验的原生选择文件夹命令。")
    invoke(backend, observation, choice["selector"], "preview.choose_directory", intent=intent)
    picker_report = _wait_folder_picker(backend, observation)
    picker = picker_report["picker"]
    intent["picker"] = copy.deepcopy(picker)
    selected = _directory_action(backend, observation, "choose_export_folder", "preview.select_owned_directory",
                                 root, picker, intent)
    actual = _sample_directory(backend, observation, sample_selector)
    confirmation = None
    if actual != root:
        if actual != previous:
            fail("DESTINATION_CONFLICT", "选择目录后样例路径变为其他位置；不继续确认。")
        try:
            navigated = checked_call(backend, observation, "inspect_folder_picker")
        except Exception as exc:
            if getattr(exc, "code", None) != "FOLDER_PICKER_UNAVAILABLE":
                raise
            # A closed picker can precede the application's sample refresh.
            _wait_sample_directory(backend, observation, sample_selector, root, previous)
        else:
            if navigated.get("picker") != picker:
                fail("DESTINATION_CONFLICT", "目录导航后对话框所有者凭据改变。")
            navigation = navigated.get("navigation", {})
            if (navigation.get("hierarchy_verified") is not True or
                    _plain_path(navigation.get("folder_path", ""), directory=True) != root):
                fail("DESTINATION_PENDING", "目录对话框未确认到达专用输出目录；不重复导航。")
            final_intent = {**copy.deepcopy(intent), "expected_edit_text": navigation.get("edit_text")}
            confirmation = _directory_action(backend, observation, "select_current_export_folder", "preview.select_current_directory",
                                              root, picker, final_intent, expected_edit_text=navigation.get("edit_text"))
            _wait_sample_directory(backend, observation, sample_selector, root, previous)
    # Bind settings to the same enabled recipe again before reporting setup.
    final_recipe = verify_recipe(backend, observation, recipe["recipe_name"], allow_select=False)
    settings = export_settings(final_recipe, output_root=root, allow_existing=True)
    if Path(settings["output_path"]).parent != root:
        fail("DESTINATION_UNVERIFIED", "最终导出样例目录不是专用输出目录。")
    _assert_observation(observation, backend.observe(list(observation.get("values", {}))))
    entry.update(status="selected", verified_at=time.time())
    _save_destination_metadata(backend, metadata)
    return {"changed": True, "root": str(root), "previous_root": str(previous), "settings": settings,
            "recipe": final_recipe, "sample_selector": sample_selector, "target": observation["target"],
            "native_sample_path_verified": True, "metadata_path": str(metadata_path),
            "selection": selected, "confirmation": confirmation}


def restore_destination(backend, observation, recipe=None):
    """Restore the exact recorded root-menu choice, never infer a prior folder.

    A custom root absent from the current menu is unsupported here. Falling back
    to its path could silently change symbolic Session/source-directory behavior.
    """
    expected_name = recipe["recipe_name"] if recipe is not None else backend.config.get("preview_recipe")
    path, metadata = _destination_metadata(backend)
    key_data = {"document": observation["target"].get("document"), "recipe_name": expected_name}
    key = hashlib.sha256(json.dumps(key_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    entry = metadata["destinations"].get(key)
    root = _plain_path(_plain_path(backend.state_dir, directory=True) / "export-slot", directory=True)
    if (not isinstance(entry, dict) or entry.get("owned_root") != str(root)
            or entry.get("document") != key_data["document"] or entry.get("recipe_name") != expected_name
            or not isinstance(entry.get("previous_menu_name"), str) or not entry["previous_menu_name"]
            or not isinstance(entry.get("previous_menu_selector"), dict)
            or entry["previous_menu_selector"].get("name") != entry["previous_menu_name"]
            or entry["previous_menu_selector"].get("control_type") != "MenuItem"):
        fail("DESTINATION_RESTORE_UNSUPPORTED", "没有确切的原目录和原菜单凭据；不会猜测恢复位置。")
    previous = _plain_path(entry.get("previous_root", ""), directory=True)
    if entry.get("status") in ("pending", "restoring"):
        fail("DESTINATION_RECOVERY_REQUIRED", "已有未核验的目录动作；必须在原操作中恢复。")
    if recipe is None:
        recipe = _open_verified_recipe(backend, observation, expected_name)
    _, sample_selector = _sample_directory_binding(recipe)
    current, current_name = _sample_directory_readback(backend, observation, sample_selector)
    menu = _root_menu(recipe)
    _assert_observation(observation, backend.observe(list(observation.get("values", {}))))
    if current == previous and menu.get("name") == entry["previous_menu_name"]:
        entry.update(status="restored", restored_at=time.time())
        _save_destination_metadata(backend, metadata)
        return {"changed": False, "restored": True, "previous_root": str(previous),
                "target": observation["target"], "native_sample_path_verified": True,
                "menu_choice_verified": True, "metadata_path": str(path)}
    if current != root or entry.get("status") != "selected":
        fail("DESTINATION_RECOVERY_REQUIRED", "当前目录或恢复状态不允许重新派发；先核对原操作。")
    intent = {"path": str(previous), "oldroot": str(root), "previous_root": str(previous),
              "previous_menu_name": entry["previous_menu_name"], "recipe_name": expected_name,
              "sample_selector": sample_selector, "metadata_key": key, "cycle": entry.get("cycle", 1),
              "before": copy.deepcopy(observation)}
    extra_guard = {"bindings": {"export_destination": sample_selector},
                   "expected": {"export_destination": {"name": current_name}}}
    entry.update(status="restoring", restore_started_at=time.time())
    _start_destination_change(backend, observation, menu["selector"], "preview.restore_destination_menu",
                              intent, metadata, extra_guard=extra_guard)
    parent = _simple(menu["selector"])
    option_selector = {"name": entry["previous_menu_name"], "control_type": "MenuItem", "class_name": "MenuItem",
                       "ancestors": [parent, *copy.deepcopy(menu["selector"].get("ancestors", []))[:7]]}
    option = read_node(backend, observation, option_selector)
    if (option.get("name") != entry["previous_menu_name"] or option.get("control_type") != "MenuItem"
            or option.get("enabled") is not True or option.get("offscreen") is not False
            or "Invoke" not in option.get("patterns", []) or not isinstance(option.get("selector"), dict)):
        fail("DESTINATION_RESTORE_UNSUPPORTED", "原菜单选项当前不可精确调用；不降级为其他目录模式。")
    invoke(backend, observation, option["selector"], "preview.restore_destination", extra_guard=extra_guard, intent=intent)
    _wait_sample_directory(backend, observation, sample_selector, previous, root)
    final_recipe = verify_recipe(backend, observation, expected_name, allow_select=False)
    final_sample, _ = _sample_directory_binding(final_recipe)
    if _root_menu(final_recipe).get("name") != entry["previous_menu_name"] or _plain_path(final_sample["name"], directory=True) != previous:
        fail("DESTINATION_RESTORE_UNVERIFIED", "原目录或原菜单模式没有同时回读成功。")
    _assert_observation(observation, backend.observe(list(observation.get("values", {}))))
    entry.update(status="restored", restored_at=time.time())
    _save_destination_metadata(backend, metadata)
    return {"changed": True, "restored": True, "previous_root": str(previous), "target": observation["target"],
            "native_sample_path_verified": True, "menu_choice_verified": True, "metadata_path": str(path)}


def prepare_export(backend, expected_recipe, observation=None, output_root=None):
    before = observation or backend.observe([])
    fixed_root = _plain_path(_plain_path(backend.state_dir, directory=True) / "export-slot")
    if output_root is not None and _plain_path(output_root) != fixed_root:
        fail("PREVIEW_PATH_OUTSIDE_ROOT", "导出只使用此 owner 的固定 export-slot；配置路径不能扩大写入范围。")
    recipe = _open_verified_recipe(backend, before, expected_recipe)
    destination = ensure_owned_destination(backend, before, recipe=recipe)
    settings = destination["settings"]
    released = _release_owned_export(backend, settings["output_path"], fixed_root)
    if Path(settings["output_path"]).exists():
        fail("OUTPUT_EXISTS", "配方输出路径必须为空。")
    return {"schema": "c1ai-export-plan-v1", "before": before, "settings": settings,
            "output_root": str(fixed_root), "destination": {key: value for key, value in destination.items() if key != "recipe"},
            "prepared_at": time.time(), "recipe_selection_changed": recipe["selection_changed"],
            "owned_output_released": released}


def submit_export(backend, plan):
    """Send once. Caller/Core owns write-ahead persistence and retry exclusion."""
    if not isinstance(plan, dict) or plan.get("schema") != "c1ai-export-plan-v1":
        fail("INVALID_EXPORT_PLAN", "需使用本次核验的导出计划。")
    before = plan["before"]
    current = backend.observe(list(before.get("values", {})))
    _assert_observation(before, current)
    recipe = verify_recipe(backend, current, plan["settings"]["recipe_name"])
    settings = export_settings(recipe, plan.get("output_root"))
    if settings != plan["settings"]:
        fail("PREVIEW_RECIPE_CHANGED", "配方、输出或导出按钮已改变；未提交导出。")
    receipt = {"schema": "c1ai-export-receipt-v1", "before": before, "settings": settings,
               "submitted_at": time.time(), "source": "capture_one_export", "render_verified": False,
               "output_root": plan.get("output_root")}
    guard = copy.deepcopy(backend.guard(current))
    def dispatch():
        if _bounded_output(settings["output_path"], plan.get("output_root")).exists():
            fail("OUTPUT_EXISTS", "写前记录后输出路径出现了文件；未提交导出。")
        result = checked_call(backend, current, "invoke", selector=copy.deepcopy(settings["export_selector"]),
                              mode="invoke", guard=guard)
        if not isinstance(result, dict) or result.get("accepted") is not True:
            fail("ACTION_OUTCOME_UNKNOWN", "原生动作未返回接受确认；不能自动重试。")
        return result
    receipt["dispatch"] = backend.action("preview.export", dispatch, intent={"receipt": copy.deepcopy(receipt)})
    return receipt


def decode_jpeg(path):
    from PIL import Image, ImageCms
    if Path(path).stat().st_size > MAX_PREVIEW_BYTES:
        fail("EXPORT_INCOMPLETE", "导出 JPEG 超过预览大小限制。")
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_PREVIEW_BYTES + 1)
    if not data or len(data) > MAX_PREVIEW_BYTES or data[-2:] != b"\xff\xd9":
        fail("EXPORT_INCOMPLETE", "导出 JPEG 不完整或超过预览大小限制。")
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "JPEG":
            fail("EXPORT_FORMAT_MISMATCH", "实际文件不是 JPEG。")
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        icc = image.info.get("icc_profile")
        profile = ImageCms.getProfileName(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip() if icc else None
        software = str(image.getexif().get(305, ""))
        if not profile or "srgb" not in profile.lower().replace(" ", ""):
            fail("EXPORT_PROFILE_MISMATCH", "实际 JPEG 未嵌入可识别的 sRGB 配置。")
        if "Capture One" not in software:
            fail("EXPORT_PROVENANCE_MISSING", "实际 JPEG 缺少 Capture One 软件标记。")
        return {"sha256": hashlib.sha256(data).hexdigest(), "pixel_sha256": hashlib.sha256(image.convert("RGB").tobytes()).hexdigest(),
                "width": image.width, "height": image.height, "icc_profile": profile, "software": software,
                "format": image.format, "bytes": len(data), "decoded": True}


def read_export(backend, receipt, timeout=120, *, archive_dir=None, poll_interval=0.25):
    """No C1 writes or deletion. May create a hash archive and local ownership record."""
    if not isinstance(receipt, dict) or receipt.get("schema") != "c1ai-export-receipt-v1":
        fail("INVALID_EXPORT_RECEIPT", "缺少已持久记录的导出凭据。")
    if (type(timeout) not in (int, float) or not 0 <= timeout <= 300
            or type(poll_interval) not in (int, float) or not math.isfinite(poll_interval) or poll_interval <= 0):
        fail("INVALID_TIMEOUT", "预览等待时间须在 0–300 秒内。")
    path = _bounded_output(receipt["settings"]["output_path"], receipt.get("output_root"))
    deadline = time.monotonic() + timeout
    previous, stable = None, 0
    while True:
        _bounded_output(path, receipt.get("output_root"))
        if path.is_file():
            stat = path.stat()
            fingerprint = (stat.st_size, stat.st_mtime_ns)
            stable = stable + 1 if fingerprint == previous and stat.st_size > 0 else 0
            previous = fingerprint
            if stable >= 2:
                info = decode_jpeg(path)
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) != fingerprint or hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
                    fail("EXPORT_CHANGED", "图像在解码期间发生变化。")
                current = backend.observe(list(receipt["before"].get("values", {})))
                _assert_observation(receipt["before"], current)
                result_path = path
                if archive_dir is not None:
                    directory = _plain_path(archive_dir)
                    if directory != _archive_root(backend):
                        fail("PREVIEW_ARCHIVE_OUTSIDE_ROOT", "归档目录必须是本地状态目录中的 renders。")
                    directory.mkdir(parents=True, exist_ok=True)
                    result_path = _plain_path(directory / (info["sha256"] + ".jpg"))
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != info["sha256"]:
                        fail("EXPORT_CHANGED", "图像在归档前发生变化。")
                    try:
                        with result_path.open("xb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except FileExistsError:
                        if _file_hash(result_path)[0] != info["sha256"]:
                            fail("ARCHIVE_CONFLICT", "已有预览归档内容不匹配。")
                    _record_owned_export(backend, receipt, path, result_path, info["sha256"])
                return {"source": "capture_one_export", "path": str(result_path.resolve()),
                        "export_path": str(path), "target": current["target"], "image": info,
                        "render_verified": True, "quality_approved": False,
                        "verification": "new guarded export; stable full JPEG decode; software/ICC; unchanged observed target/values",
                        "limits": "不是完整渲染状态指纹；未读参数变化和其他生产者写入输出目录不在本适配器证据范围。"}
        if time.monotonic() >= deadline:
            fail("EXPORT_PENDING", "尚未取得稳定导出；使用同一凭据继续回读，不要重新导出。",
                 {"receipt": copy.deepcopy(receipt)})
        time.sleep(poll_interval)


def recover_export(backend, record, timeout=5):
    """Read one durable pre-dispatch receipt. Never prepare, submit or clean up."""
    if type(timeout) not in (int, float) or not 0 <= timeout <= 5:
        fail("INVALID_TIMEOUT", "导出恢复单次回读最多等待 5 秒。")
    if record.get("status") not in ("dispatching", "partial", "outcome_unknown"):
        fail("EXPORT_RECOVERY_UNVERIFIED", "仅未解决的导出操作可恢复；已完成导出应读取其固定哈希归档。")
    actions = [action for action in record.get("actions", []) if action.get("name") == "preview.export"]
    if len(actions) != 1:
        fail("EXPORT_RECOVERY_UNVERIFIED", "恢复要求本操作恰好一条已记录的导出动作。")
    action = actions[0]
    receipt = action.get("intent", {}).get("receipt")
    if (not isinstance(receipt, dict) or receipt.get("schema") != "c1ai-export-receipt-v1"
            or receipt.get("before", {}).get("target") != record.get("target")
            or action.get("status") not in ("dispatching", "outcome_unknown", "completed")):
        fail("EXPORT_RECOVERY_UNVERIFIED", "导出凭据、目标或动作状态不可核验。")
    rendered = read_export(backend, receipt, timeout=timeout, archive_dir=_archive_root(backend))
    return {"outcome": "applied_not_restored", "target": rendered["target"],
            "evidence": {"scope": "capture_one_export", "render": rendered,
                         "replayed": False, "model_verified": False}}


def _recovery_unknown(record, scope, reason):
    return {"outcome": "unknown", "target": record.get("target"),
            "evidence": {"scope": scope, "reason": reason, "replayed": False, "model_verified": False}}


def recover_preview_navigation(backend, record):
    """One completed open plus certified non-dispatch; never retry navigation."""
    actions = record.get("actions", [])
    scope = "export_not_dispatched_navigation_applied"
    if (record.get("status") not in ("dispatching", "partial", "outcome_unknown")
            or record.get("request", {}).get("command") != "preview" or not actions
            or actions[0].get("name") != "preview.open_export" or actions[0].get("status") != "completed"
            or actions[0].get("result", {}).get("accepted") is not True):
        return _recovery_unknown(record, scope, "First action is not a known completed export-window navigation")
    before = record.get("before", {})
    if not isinstance(before, dict) or before.get("target") != record.get("target"):
        return _recovery_unknown(record, scope, "Navigation receipt does not match the operation baseline")
    refused_names = {"preview.select_recipe", "preview.destination_menu", "preview.restore_destination_menu"}
    metadata_checked = False
    for index, action in enumerate(actions):
        intent = action.get("intent", {})
        recorded_before = intent.get("before", {})
        if (not isinstance(recorded_before, dict) or recorded_before.get("target") != before.get("target")
                or recorded_before.get("values") != before.get("values")):
            return _recovery_unknown(record, scope, "An action receipt does not match the operation baseline")
        if index == 0:
            continue
        if (action.get("name") not in refused_names or action.get("status") != "not_dispatched"
                or action.get("error", {}).get("dispatched") is not False):
            return _recovery_unknown(record, scope, "Another preview action lacks trusted non-dispatch evidence")
        if action["name"] in ("preview.destination_menu", "preview.restore_destination_menu"):
            key = intent.get("metadata_key")
            if not isinstance(key, str) or not key or "metadata_entry_before" not in intent:
                return _recovery_unknown(record, scope, "The refused directory action lacks its metadata baseline")
            _, metadata = _destination_metadata(backend)
            previous_entry = intent["metadata_entry_before"]
            matches = (key not in metadata["destinations"] if previous_entry is None else
                       isinstance(previous_entry, dict) and metadata["destinations"].get(key) == previous_entry)
            if not matches:
                return _recovery_unknown(record, scope, "Directory metadata was not restored to its pre-action entry")
            metadata_checked = True
    current = backend.observe(list(before.get("values", {})))
    _assert_observation(before, current)
    if current.get("target") != record.get("target"):
        return _recovery_unknown(record, scope, "Operation target differs from its recorded observation")
    window = read_node(backend, current, EXPORT)
    if window.get("automation_id") != "export" or window.get("control_type") != "Window":
        return _recovery_unknown(record, scope, "Qualified export window is not currently present")
    after = backend.observe(list(before.get("values", {})))
    _assert_observation(before, after)
    return {"outcome": "not_applied", "target": after["target"],
            "evidence": {"scope": scope, "navigation_applied": True, "export_dispatched": False,
                         "not_dispatched_actions": [action["name"] for action in actions[1:]],
                         "metadata_rollback_verified": metadata_checked,
                         "observation": after, "observed_fields": list(before.get("values", {})),
                         "unobserved_parameters_verified": False, "replayed": False, "model_verified": False}}






def build_historical_destination_metadata(backend, observation, recipe, *, prepare_path, options_path,
                                          prepare_sha256, options_sha256):
    """Build (do not persist) a baseline from two explicitly hash-bound receipts.

    The caller supplies a fresh verified current recipe/observation. Historical
    receipts supply only the former menu choice and directory, never current UI.
    """
    reports, sources = [], []
    for value, digest in ((prepare_path, prepare_sha256), (options_path, options_sha256)):
        path = _plain_path(value)
        raw = path.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or actual_hash != digest:
            fail("DESTINATION_SOURCE_MISMATCH", "历史目录证据哈希不匹配。")
        report = json.loads(raw)
        reports.append(report)
        sources.append({"path": str(path), "sha256": digest, "operation_id": report.get("operation_id"),
                        "created_at": report.get("created_at")})
    prepare, options = reports
    allowed = ({"preview.open_export", "preview.select_recipe"}, {"preview.destination_menu"})
    for report, action_name, names in zip(reports, ("prepare", "destination_options"), allowed):
        target = report.get("target")
        if (report.get("status") != "completed" or report.get("request", {}).get("command") != "preview"
                or report["request"].get("action") != action_name or not isinstance(target, dict)
                or report.get("before", {}).get("target") != target or report.get("result", {}).get("target") != target
                or not isinstance(report.get("result", {}).get("nodes"), list)
                or any(action.get("name") not in names or action.get("status") != "completed"
                       or action.get("result", {}).get("accepted") is not True for action in report.get("actions", []))):
            fail("DESTINATION_SOURCE_UNVERIFIED", "历史记录不是已完成且目标一致的导出准备或目录菜单观察。")
    if prepare["target"] != options["target"] or prepare["target"].get("document") != observation["target"].get("document"):
        fail("DESTINATION_SOURCE_UNVERIFIED", "两份历史记录或当前观察不属于同一文档。")
    settings = prepare["result"].get("settings", {})
    if settings.get("recipe_name") != recipe.get("recipe_name"):
        fail("DESTINATION_SOURCE_UNVERIFIED", "历史配方与当前核验配方不一致。")
    old_recipe = {"nodes": prepare["result"]["nodes"], "recipe_name": settings["recipe_name"],
                  "checked_runtime_id": settings.get("checked_runtime_id")}
    if export_settings(old_recipe, allow_existing=True) != settings:
        fail("DESTINATION_SOURCE_UNVERIFIED", "历史设置回执与其控件快照不一致。")
    old_sample, _ = _sample_directory_binding(old_recipe)
    previous = _plain_path(old_sample["name"], directory=True)
    menu = _root_menu(old_recipe)
    options_nodes = options["result"]["nodes"]
    matching = _unique([node for node in options_nodes if node.get("control_type") == "MenuItem"
                        and node.get("name") == menu.get("name") and "Invoke" in node.get("patterns", [])
                        and _parents(node) and _parents(node)[0].get("name") == menu.get("name")
                        and any(parent.get("class_name") == "ExportDestinationRootFolderSelector" for parent in _parents(node))])
    if len(matching) != 1 or matching[0].get("enabled") is not True:
        fail("DESTINATION_SOURCE_UNVERIFIED", "历史菜单没有同名、唯一且可调用的原目录选项。")
    root = _plain_path(_plain_path(backend.state_dir, directory=True) / "export-slot", directory=True)
    current_settings = export_settings(recipe, output_root=root, allow_existing=True)
    current_sample, sample_selector = _sample_directory_binding(recipe)
    if Path(current_settings["output_path"]).parent != root or _plain_path(current_sample["name"], directory=True) != root:
        fail("DESTINATION_SOURCE_UNVERIFIED", "调用方尚未提供当前专用目录的已核验快照。")
    key_data = {"document": observation["target"].get("document"), "recipe_name": recipe["recipe_name"]}
    key = hashlib.sha256(json.dumps(key_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    entry = {**key_data, "previous_root": str(previous), "previous_menu_name": menu["name"],
             "previous_menu_selector": copy.deepcopy(menu["selector"]),
             "historical_menu_option_selector": copy.deepcopy(matching[0]["selector"]),
             "owned_root": str(root), "sample_selector": sample_selector, "status": "selected",
             "before": copy.deepcopy(prepare["before"]), "current_adoption_observation": copy.deepcopy(observation),
             "source_evidence": sources, "imported_at": time.time(),
             "evidence_scope": "historical_destination_receipts_plus_caller_current_verified_recipe; no historical parameter fingerprint",
             "restoration_scope": "exact_native_menu_choice_and_directory_required"}
    path, metadata = _destination_metadata(backend)
    if key in metadata["destinations"]:
        fail("DESTINATION_METADATA_EXISTS", "已有此文档配方的目录记录；不能覆盖。")
    metadata["destinations"][key] = entry
    return {"path": str(path), "metadata": metadata, "metadata_key": key, "entry": entry, "persisted": False}
