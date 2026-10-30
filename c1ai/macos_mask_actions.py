"""Pure checks of saved Mac mask transitions; never calls or writes Capture One.

Inputs are the ``state`` returned by the qualified masks observation: the native
layer inventory plus one ``macos_masks.read_mask_file`` record per local UUID.
These checks prove saved structure and independently supplied live selection,
not rendered mask pixels. The
caller must separately bind the process, document, candidate and operation.
"""
from __future__ import annotations

from copy import deepcopy
import math
import struct

from .contracts import Refused


_MODES = {"add": 0, "subtract": 5, "intersect": 2}
_GENERATORS = {"subject": "AISubject", "background": "AIBackground"}
_NEUTRAL = [1, 1.0, 0, 0]


def _require(condition, message):
    if not condition:
        raise Refused("mask_transition_unverified", message)


def _stable_ref(ref):
    return {key: value for key, value in ref.items() if key != "name"}


def _inventory(state):
    _require(isinstance(state, dict), "A complete saved mask observation is required")
    layers, details, masks = (state.get(key) for key in ("layers", "details", "masks"))
    _require(isinstance(layers, list) and isinstance(details, list) and isinstance(masks, list)
             and len(layers) == len(details) and bool(layers), "Incomplete layer inventory")
    _require(all(isinstance(row, dict) for row in layers + details + masks), "Invalid layer records")
    local_ids = [row.get("native_id") for row in layers if row.get("is_image_layer") is False]
    ids = [row.get("uuid") for row in masks]
    _require(all(isinstance(uid, str) and uid for uid in local_ids + ids)
             and len(set(local_ids)) == len(local_ids) and len(set(ids)) == len(ids)
             and set(ids) == set(local_ids), "Saved masks must cover every local layer UUID exactly once")
    for mask in masks:
        _require(isinstance(mask.get("structure"), dict)
                 and mask.get("source") == "session_comask_readonly"
                 and mask.get("format_version") == 3
                 and mask.get("mask_type") == mask["structure"].get("mask_type")
                 and isinstance(mask.get("content_sha256"), str)
                 and mask["content_sha256"] == mask["structure"].get("content_sha256"),
                 "Missing qualified saved mask structure")
    return layers, details, dict(zip(ids, masks))


def _record_content(mask):
    # Global file bytes and allocation change when any sibling is saved.
    return {key: value for key, value in mask.items() if key not in {"file_sha256", "blocks_count"}}


def _bound(before, after, layer_ref, *, allow_rename=False, allow_luma_change=False, derived_kind=None):
    bl, bd, bm = _inventory(before)
    al, ad, am = _inventory(after)
    _require(isinstance(layer_ref, dict) and layer_ref.get("is_image_layer") is False
             and layer_ref.get("identity_source") == "session_mask_uuid_readonly",
             "Use an observed local layer UUID reference")
    _require(layer_ref in bl and before.get("selected_layer") == layer_ref,
             "The operation must start on its observed selected local layer")
    index = bl.index(layer_ref)
    _require(len(bl) == len(al), "A mask operation changed the layer count")
    for i, (old, new) in enumerate(zip(bl, al)):
        _require(_stable_ref(old) == _stable_ref(new), "Layer UUID, scope or order changed")
        old_detail, new_detail = deepcopy(bd[i]), deepcopy(ad[i])
        if i == index and derived_kind is not None:
            _require(new_detail.get("kind") == derived_kind,
                     "The collapsed mask kind does not match its sole surviving component")
            old_detail["kind"] = derived_kind
        if i == index and allow_luma_change:
            old_detail.pop("luma_range", None)
            new_detail.pop("luma_range", None)
        if i == index and allow_rename:
            old_detail.pop("name", None)
            new_detail.pop("name", None)
            _require(isinstance(new.get("name"), str) and new["name"] == ad[i].get("name"),
                     "The renamed native layer and reference disagree")
        else:
            _require(old == new, "An unrequested layer name changed")
        _require(old_detail == new_detail, "An unrequested layer adjustment or property changed")
    _require(after.get("selected_layer") == al[index], "The selected parent layer changed")
    uid = layer_ref["native_id"]
    for other in bm:
        if other != uid:
            _require(_record_content(bm[other]) == _record_content(am[other]),
                     "A non-target UUID's saved mask changed")
    old, new = bm[uid], am[uid]
    _require(all(old.get(key) == new.get(key) for key in
                 ("uuid", "width", "height", "format_version", "source")),
             "The target mask identity or image geometry changed")
    return old, new, al[index]


def _children(structure):
    children = structure.get("children")
    _require(structure.get("mask_type") == 255 and isinstance(children, list) and bool(children)
             and all(isinstance(child, dict) and isinstance(child.get("content_sha256"), str)
                     for child in children), "A saved composite mask is required")
    _require(structure.get("composition_raw") == _NEUTRAL
             and not any(key in structure for key in ("map", "constant_fill", "luma_range", "other_fields")),
             "The composite has unsupported root semantics")
    return children


def _selection(structure):
    metadata = structure.get("metadata")
    _require(isinstance(metadata, dict), "Saved mask metadata is unavailable")
    value = metadata.get("SelectedSubmaskIndex")
    _require(type(value) is int and value >= -1, "The selected submask was not explicitly saved")
    return value


def _evidence(action, layer, **extra):
    return {"action": action, "layer_ref": layer,
            "source": "session_comask_readonly", "saved_structure_verified": True,
            "pixels_verified": False, "render_verified": False, **extra}


def preflight_viewer_gradient(state, layer_ref, patch):
    """Qualify one linear Viewer drag on an explicitly empty local mask.

    The caller must separately bind the visible photo rectangle and mouse
    dispatch. Viewer points are never converted to claimed RAW coordinates here.
    """
    _require(isinstance(patch, dict)
             and set(patch) <= {"action", "coordinate_space", "start", "stop", "midpoint"}
             and patch.get("action") == "linear_gradient"
             and patch.get("coordinate_space") == "viewer",
             "Only an explicit Viewer-space linear gradient is qualified")
    midpoint = patch.get("midpoint", 0.5)
    _require(type(midpoint) in (int, float) and math.isfinite(midpoint) and midpoint == 0.5,
             "Viewer linear creation currently requires midpoint 0.5")
    for key in ("start", "stop"):
        point = patch.get(key)
        _require(isinstance(point, list) and len(point) == 2
                 and all(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
                         for value in point), "Viewer endpoints must be finite normalized pairs")
    _require(patch["start"] != patch["stop"], "A linear gradient needs distinct Viewer endpoints")
    _empty_gradient_mask(state, layer_ref)
    return {"action": "linear_gradient", "coordinate_space": "viewer",
            "start": list(patch["start"]), "stop": list(patch["stop"]), "midpoint": 0.5}


def _empty_gradient_mask(state, layer_ref):
    layers, details, masks = _inventory(state)
    _require(isinstance(layer_ref, dict) and layer_ref in layers
             and state.get("selected_layer") == layer_ref
             and layer_ref.get("is_image_layer") is False
             and layer_ref.get("identity_source") == "session_mask_uuid_readonly",
             "Select an observed local layer UUID before drawing")
    _require(details[layers.index(layer_ref)].get("kind") == "adjustment",
             "Viewer creation requires an empty adjustment layer")
    mask = masks[layer_ref["native_id"]]
    root, constant = mask["structure"], mask.get("constant", {})
    _require(root.get("mask_type") == 0 and root.get("composition_raw") == _NEUTRAL
             and set(root) <= {"mask_type", "composition_raw", "metadata", "map", "content_sha256"}
             and isinstance(root.get("metadata"), dict)
             and set(root["metadata"]) <= {"SelectedSubmaskIndex"}
             and ("SelectedSubmaskIndex" not in root["metadata"]
                  or type(root["metadata"]["SelectedSubmaskIndex"]) is int
                  and root["metadata"]["SelectedSubmaskIndex"] == -1)
             and isinstance(constant, dict) and constant.get("verified") is True
             and type(constant.get("value_8bit")) is int and constant["value_8bit"] == 0
             and constant.get("density") == 0,
             "Create an empty neutral raster layer before drawing; existing mask contents are not replaced")
    return mask


def native_gradient_parameters(state, layer_ref, patch):
    """Convert the shared normalized, top-left RAW contract to ImageCore floats."""
    from .contracts import validate_structured_patch
    validate_structured_patch({"module": "masks", "patch": patch})
    _require(patch.get("coordinate_space", "raw") == "raw", "Native gradients use RAW coordinates")
    layers, _, masks = _inventory(state)
    _require(layer_ref in layers and state.get("selected_layer") == layer_ref,
             "Select the observed gradient layer before editing")
    mask = masks[layer_ref["native_id"]]
    if mask["structure"].get("mask_type") in {3, 4}:
        _require(parameter_invert_kind(state, layer_ref) == patch["action"] + "_raw",
                 "Edit the existing gradient with the same gradient action")
        _require(mask["structure"].get("metadata", {}).get("SelectedSubmaskIndex", -1) == -1,
                 "Select the whole gradient mask before editing")
        _require(set(mask["structure"].get("metadata", {})) <= {"SelectedSubmaskIndex"},
                 "Existing gradient metadata is outside the qualified edit scope")
    else:
        mask = _empty_gradient_mask(state, layer_ref)
    width, height = mask["width"], mask["height"]
    if patch["action"] == "linear_gradient":
        values = [patch["start"][0] * width, patch["start"][1] * height,
                  patch["stop"][0] * width, patch["stop"][1] * height, patch["midpoint"]]
    else:
        _require(patch["action"] == "radial_gradient", "Unsupported native gradient kind")
        angle = math.radians(patch["angle"])
        rx, ry = patch["radius_x"] * width, patch["radius_y"] * height
        # Host operation clamps extremely thin ellipses; reject before dispatch
        # instead of silently changing an absolute requested radius.
        _require(min(rx, ry) >= 1 and min(rx, ry) / max(rx, ry) >= .01,
                 "Native ellipse axes must be at least one RAW pixel and ratio at least 0.01")
        values = [patch["center"][0] * width, patch["center"][1] * height,
                  rx * math.cos(angle), rx * math.sin(angle),
                  -ry * math.sin(angle), ry * math.cos(angle), patch["falloff"], int(patch["inverted"])]
    return [struct.unpack("<f", struct.pack("<f", value))[0] for value in values]


def verify_native_gradient(before, after, layer_ref, patch, parameters):
    _require(native_gradient_parameters(before, layer_ref, patch) == parameters,
             "Native gradient parameters differ from the request")
    _, new, layer = _bound(before, after, layer_ref)
    key = patch["action"] + "_raw"
    _require(parameter_invert_kind(after, layer) == key and new["structure"][key] == parameters
             and new["structure"]["composition_raw"] == _NEUTRAL,
             "Saved gradient parameters or composition differ from the native request")
    old = next(m for m in before["masks"] if m["uuid"] == layer_ref["native_id"])
    if old["structure"].get("mask_type") in {3, 4}:
        previous, current = deepcopy(old["structure"]), deepcopy(new["structure"])
        for root in (previous, current):
            root.pop(key, None); root.pop("content_sha256", None)
            # C1 regenerates its stochastic render seed on each geometry edit.
            for field in root.get("other_fields", []):
                if field.get("name") == "RandSeed":
                    field.pop("sha256", None)
        _require(previous == current, "Gradient editing changed unrelated mask structure or metadata")
    return _evidence(patch["action"], layer, native_geometry_readback=True,
                     raw_points_matched_to_request=True, actual_raw=parameters,
                     content_sha256=new["content_sha256"])


def verify_gradient_selection_point(point, kind, parameters):
    """C1 initializes an empty layer's selection marker at the gradient handle."""
    if kind == "linear_gradient":
        x, y, end_x, end_y, midpoint = parameters
        expected = [x + (end_x - x) * midpoint, y + (end_y - y) * midpoint]
    else:
        expected = parameters[:2]
    try:
        actual = [float(value) for value in point.split(";")]
    except (AttributeError, ValueError):
        actual = []
    _require(len(actual) == 2 and all(math.isfinite(a) and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-3)
                                    for a, b in zip(actual, expected)),
             "Native selection marker does not match the gradient handle")


def verify_viewer_gradient(before, after, layer_ref, patch):
    """Prove empty-to-linear structure and report native geometry, not RAW intent."""
    requested = preflight_viewer_gradient(before, layer_ref, patch)
    _, new, layer = _bound(before, after, layer_ref)
    root = new["structure"]
    _require(root.get("mask_type") == 3 and root.get("composition_raw") == _NEUTRAL
             and set(root) <= {"mask_type", "composition_raw", "metadata", "linear_gradient_raw",
                              "other_fields", "content_sha256"},
             "The result is not a simple neutral linear gradient")
    metadata = root.get("metadata")
    _require(isinstance(metadata, dict) and set(metadata) <= {"SelectedSubmaskIndex"}
             and ("SelectedSubmaskIndex" not in metadata or
                  type(metadata["SelectedSubmaskIndex"]) is int and metadata["SelectedSubmaskIndex"] == -1),
             "The new gradient has unqualified metadata or a selected child")
    extras = root.get("other_fields", [])
    _require(isinstance(extras, list) and (not extras or
             len(extras) == 1 and isinstance(extras[0], dict)
             and set(extras[0]) == {"name", "flags", "format", "sha256", "children"}
             and extras[0]["name"] == "RandSeed" and extras[0]["flags"] == 0
             and extras[0]["format"] == 34 and extras[0]["children"] == []
             and isinstance(extras[0]["sha256"], str) and len(extras[0]["sha256"]) == 64
             and all(char in "0123456789abcdef" for char in extras[0]["sha256"])),
             "The new gradient has unknown parameter fields")
    geometry = root.get("linear_gradient_raw")
    _require(isinstance(geometry, list) and len(geometry) == 5
             and all(type(value) in (int, float) and math.isfinite(value) for value in geometry)
             and geometry[:2] != geometry[2:4] and geometry[4] == 0.5,
             "Native linear endpoints or midpoint did not read back as qualified geometry")
    return _evidence("linear_gradient", layer, source_structure_verified=True,
                     native_geometry_readback=True, requested_viewer=requested,
                     actual_raw={"linear_gradient_raw": list(geometry), "width": new["width"], "height": new["height"]},
                     raw_points_matched_to_request=False, viewer_geometry_match_verified=False,
                     content_sha256=new["content_sha256"])


def verify_combine_ai(before, after, layer_ref, mode, kind):
    """Verify exactly one new AI component; this is not combine(source_layer)."""
    _require(mode in _MODES and kind in _GENERATORS, "Unsupported AI combination mode or kind")
    old, new, layer = _bound(before, after, layer_ref, allow_rename=True)
    old_root, new_root = old["structure"], new["structure"]
    previous = _children(old_root) if old_root.get("mask_type") == 255 else [old_root]
    children = _children(new_root)
    _require(len(children) == len(previous) + 1, "Expected exactly one appended component")
    _require([row["content_sha256"] for row in children[:-1]] ==
             [row["content_sha256"] for row in previous], "An existing component changed or moved")
    child = children[-1]
    _require(child.get("mask_type") == 0
             and child.get("composition_raw") == [1, 1.0, _MODES[mode], 0]
             and not any(key in child for key in ("children", "luma_range", "constant_fill", "other_fields"))
             and child.get("metadata", {}).get("MaskGenerator") == _GENERATORS[kind],
             "The new component lacks the requested saved AI generator and composition mode")
    return _evidence("combine_ai", layer, mode=mode, kind=kind,
                     appended_index=len(previous), generator_metadata_verified=True,
                     ai_segmentation_pixels_verified=False,
                     content_sha256=new["content_sha256"])


def _copy_component_content(node, *, omit_name=False):
    """Keep every saved field except the digest and explicit UI selection.

    Labels may be regenerated when a simple parent becomes its first child;
    callers must opt into that single exception. Generator metadata is content.
    """
    result = deepcopy(node)
    result.pop("content_sha256", None)
    metadata = result.get("metadata")
    _require(isinstance(metadata, dict), "Saved component metadata is required")
    selected = metadata.pop("SelectedSubmaskIndex", -1)
    _require(type(selected) is int and selected >= -1, "Invalid saved selection metadata")
    if omit_name:
        metadata.pop("SubmaskName", None)
    return result


def _simple_copy_component(node, *, neutral=True):
    kind = node.get("mask_type")
    _require(type(kind) is int and kind in (0, 3, 4)
             and not any(key in node for key in ("children", "luma_range", "constant_fill")),
             "Existing-mask copy requires a simple raster, linear or radial mask without Luma")
    composition = node.get("composition_raw")
    _require(composition == _NEUTRAL if neutral else
             composition in ([1, 1.0, mode, 0] for mode in _MODES.values()),
             "Unsupported component composition")
    payload = {0: "map", 3: "linear_gradient_raw", 4: "radial_gradient_raw"}[kind]
    _require(not any(key in node for key in
                     {"map", "linear_gradient_raw", "radial_gradient_raw"} - {payload})
             and (kind == 0 or payload in node), "Mask type and saved payload disagree")
    _copy_component_content(node)


def _combine_destination(root):
    if root.get("mask_type") != 255:
        _simple_copy_component(root)
        return [root]
    children = _children(root)
    _require(len(children) < 64, "The composite has no room for another component")
    for child in children:
        _simple_copy_component(child, neutral=False)
    return children


def preflight_combine_existing(state, layer_ref, source_ref, mode):
    """Qualify two observed local UUIDs before adding a temporary Brush child.

    This checks saved content, not live child selection. The caller must bind
    that selection independently before the native Copy Mask From menu action.
    """
    _require(mode in _MODES, "Unsupported existing-mask combination mode")
    layers, _, masks = _inventory(state)
    _require(isinstance(layer_ref, dict) and isinstance(source_ref, dict)
             and layer_ref in layers and source_ref in layers
             and state.get("selected_layer") == layer_ref
             and all(ref.get("is_image_layer") is False
                     and ref.get("identity_source") == "session_mask_uuid_readonly"
                     for ref in (layer_ref, source_ref))
             and layer_ref["native_id"] != source_ref["native_id"],
             "Select the observed local destination and a different local source UUID")
    target, source = (masks[ref["native_id"]] for ref in (layer_ref, source_ref))
    _require(all(target.get(key) == source.get(key) for key in ("width", "height")),
             "Source and destination mask geometry differ")
    _simple_copy_component(source["structure"])
    return len(_combine_destination(target["structure"]))


def verify_append_blank(before, after, layer_ref, mode):
    """Prove one empty Brush was appended, preserving every existing mask."""
    _require(mode in _MODES, "Unsupported existing-mask combination mode")
    old, new, layer = _bound(before, after, layer_ref, allow_rename=True)
    old_root, new_root = old["structure"], new["structure"]
    previous, children = _combine_destination(old_root), _children(new_root)
    _require(len(children) == len(previous) + 1, "Expected exactly one appended Brush component")
    wrapped = old_root.get("mask_type") != 255
    for original, current in zip(previous, children[:-1]):
        _require(original["content_sha256"] == current["content_sha256"]
                 and _copy_component_content(original, omit_name=wrapped) ==
                 _copy_component_content(current, omit_name=wrapped),
                 "Appending a Brush changed an existing component")
    wrapper = _copy_component_content(new_root)
    wrapper.pop("children")
    if wrapped:
        _require(wrapper == {"mask_type": 255, "composition_raw": _NEUTRAL, "metadata": {}},
                 "The new composite wrapper contains unexpected metadata or semantics")
    else:
        old_wrapper = _copy_component_content(old_root)
        old_wrapper.pop("children")
        _require(wrapper == old_wrapper, "Appending a Brush changed the composite wrapper")
    blank = children[-1]
    name = blank.get("metadata", {}).get("SubmaskName")
    _require(isinstance(name, str) and bool(name)
             and _copy_component_content(blank) == {
                 "mask_type": 0, "composition_raw": [1, 1.0, _MODES[mode], 0],
                 "metadata": {"SubmaskName": name}},
             "The appended component is not an empty named Brush with the requested mode")
    return _evidence("append_blank", layer, mode=mode, appended_index=len(previous),
                     content_sha256=new["content_sha256"])


def verify_combine_existing(before, middle, after, layer_ref, source_ref, mode):
    """Verify native copy replaced only the newly appended Brush component.

    Full source payload and metadata must survive, including opaque RandSeed
    hashes and AI generator metadata. This proves saved structure, not rendering.
    """
    preflight_combine_existing(before, layer_ref, source_ref, mode)
    appended = verify_append_blank(before, middle, layer_ref, mode)
    _, final, layer = _bound(before, after, layer_ref, allow_rename=True)
    mid, final, _ = _bound(middle, after, appended["layer_ref"])
    _, _, sources = _inventory(before)
    source = sources[source_ref["native_id"]]
    old_root, new_root = mid["structure"], final["structure"]
    old_children, new_children = _children(old_root), _children(new_root)
    _require(len(new_children) == len(old_children), "Copy changed the number of components")
    old_wrapper, new_wrapper = (_copy_component_content(root) for root in (old_root, new_root))
    old_wrapper.pop("children")
    new_wrapper.pop("children")
    _require(old_wrapper == new_wrapper, "Copy changed the composite wrapper")
    _require(old_children[:-1] == new_children[:-1], "Copy changed or moved an existing sibling")
    expected = _copy_component_content(source["structure"])
    expected["composition_raw"] = [1, 1.0, _MODES[mode], 0]
    expected["metadata"]["SubmaskName"] = old_children[-1]["metadata"]["SubmaskName"]
    _require(_copy_component_content(new_children[-1]) == expected,
             "The new component does not exactly preserve the source mask and requested composition")
    return _evidence("combine_existing", layer, source_ref=source_ref, mode=mode,
                     appended_index=appended["appended_index"],
                     source_structure_verified=True, source_metadata_verified=True,
                     content_sha256=final["content_sha256"])


def verify_select_submask(before, after, layer_ref, index, *, live_selection=None):
    """Verify live AX selection and unchanged saved content independently.

    The caller maps the complete native layer inventory to AX parent rows first;
    AX names alone are not UUID identity and COMASK selection is not live state.
    """
    _require(type(index) is int and index >= -1, "Invalid submask index")
    old, new, layer = _bound(before, after, layer_ref)
    if index >= 0:
        _require(index < len(_children(old["structure"])), "The observed submask index is absent")
    evidence = live_selection
    _require(isinstance(evidence, dict) and evidence.get("source") == "macos_accessibility"
             and evidence.get("layer_ref") == layer_ref and evidence.get("submask_index") == index,
             "Submask selection needs live AX evidence mapped to the native layer inventory")
    rows, row_index, parent_index = (evidence.get(key) for key in ("rows", "row_index", "parent_row_index"))
    _require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
             and type(row_index) is int and type(parent_index) is int
             and 0 <= parent_index <= row_index < len(rows), "Invalid live layer row evidence")
    _require([i for i, row in enumerate(rows) if row.get("AXSelected") is True] == [row_index]
             and rows[parent_index].get("AXDisclosureLevel") == 0,
             "Live AX must report exactly one selected row and its parent layer")
    if index == -1:
        _require(row_index == parent_index, "Whole-mask selection must select its parent row")
    else:
        count = len(_children(old["structure"]))
        _require(rows[parent_index].get("AXDisclosing") is True
                 and row_index == parent_index + count - index
                 and parent_index + count < len(rows)
                 and all(row.get("AXDisclosureLevel") == 1 for row in rows[parent_index + 1:parent_index + count + 1])
                 and (parent_index + count + 1 == len(rows)
                      or rows[parent_index + count + 1].get("AXDisclosureLevel") == 0),
                 "Live AX children do not match the saved flat composite in reverse UI order")
    old_root, new_root = deepcopy(old["structure"]), deepcopy(new["structure"])
    old_root["metadata"].pop("SelectedSubmaskIndex", None)
    new_root["metadata"].pop("SelectedSubmaskIndex", None)
    _require(old_root == new_root and old["content_sha256"] == new["content_sha256"],
             "Selecting a submask changed mask contents or other metadata")
    return _evidence("select_submask", layer, selected_index=index, selection_verified=True,
                     selection_source="live_ax_rows_with_native_inventory_mapping", saved_selection_is_live=False)


def verify_remove_submask(before, after, layer_ref, index):
    """Prove exact removal of a component, preserving its siblings and parent.

    A native one-child collapse is accepted only when the survivor's complete
    semantic digest is unchanged. The caller must independently prove the live
    selected child with AX before dispatch; saved SelectedSubmaskIndex is stale.
    """
    _require(type(index) is int and index >= 0, "Invalid submask index")
    _, _, before_masks = _inventory(before)
    _, _, after_masks = _inventory(after)
    _require(isinstance(layer_ref, dict) and isinstance(layer_ref.get("native_id"), str)
             and layer_ref["native_id"] in before_masks and layer_ref["native_id"] in after_masks,
             "Deletion requires an observed local layer UUID in both inventories")
    old = before_masks[layer_ref["native_id"]]
    new = after_masks[layer_ref["native_id"]]
    previous = _children(old["structure"])
    _require(len(previous) >= 2 and index < len(previous), "Deletion requires an existing observed component")
    survivors = previous[:index] + previous[index + 1:]
    derived_kind = None
    if len(survivors) == 1 and new["structure"].get("mask_type") != 255:
        survivor = survivors[0]
        generator = survivor.get("metadata", {}).get("MaskGenerator")
        if survivor.get("mask_type") == 0:
            derived_kind = {"AISubject": "subject mask", "AIBackground": "background mask"}.get(generator)
            if derived_kind is None and generator is None:
                constant = new.get("constant", {})
                derived_kind = "filled" if constant.get("verified") is True and constant.get("value_8bit") == 255 else "adjustment"
        elif survivor.get("mask_type") == 1 and survivor.get("constant_fill") == 1:
            derived_kind = "filled"
    old, new, layer = _bound(before, after, layer_ref, derived_kind=derived_kind)
    if new["structure"].get("mask_type") == 255:
        current = _children(new["structure"])
        _require([row["content_sha256"] for row in current] ==
                 [row["content_sha256"] for row in survivors], "Deletion changed a surviving component")
    else:
        _require(len(survivors) == 1 and new["content_sha256"] == survivors[0]["content_sha256"],
                 "The collapsed mask differs from the sole surviving component")
    return _evidence("remove_submask", layer, removed_index=index, selection_verified=False,
                     saved_selection_is_live=False,
                     content_sha256=new["content_sha256"])


def clear_luma_range_index(state, layer_ref):
    """Preflight for the official clear-luma command, not an arbitrary index API.

    This build removes the topmost Luma component. Only a flat composite with
    exactly one Luma component is unambiguous enough for this qualification.
    """
    layers, _, masks = _inventory(state)
    _require(layer_ref in layers and state.get("selected_layer") == layer_ref
             and layer_ref.get("is_image_layer") is False,
             "Read and select the target local layer first")
    children = _children(masks[layer_ref["native_id"]]["structure"])
    _require(len(children) >= 2 and all("children" not in child for child in children),
             "Clear Luma requires a flat composite with a surviving non-Luma component")
    matches = [i for i, child in enumerate(children) if child.get("mask_type") == 257]
    _require(len(matches) == 1 and "luma_range" in children[matches[0]],
             "Clear Luma requires exactly one direct Luma component")
    return matches[0]


def _semantic_tree(node):
    """Compare parsed content without names, selection or derived digests."""
    result = {key: deepcopy(value) for key, value in node.items()
              if key not in {"metadata", "content_sha256", "children"}}
    if "children" in node:
        result["children"] = [_semantic_tree(child) for child in node["children"]]
    return result


def verify_clear_luma_range(before, after, layer_ref):
    """Prove removal of the sole direct Luma child and preservation of siblings."""
    index = clear_luma_range_index(before, layer_ref)
    old, new, layer = _bound(before, after, layer_ref, allow_luma_change=True)
    previous = _children(old["structure"])
    survivors = previous[:index] + previous[index + 1:]
    if new["structure"].get("mask_type") == 255:
        current = _children(new["structure"])
        _require([child["content_sha256"] for child in current] ==
                 [child["content_sha256"] for child in survivors],
                 "Clearing Luma changed a surviving component")
    else:
        _require(len(survivors) == 1, "Clearing Luma unexpectedly flattened multiple components")
        old_child, new_root = _semantic_tree(survivors[0]), _semantic_tree(new["structure"])
        old_comp, new_comp = old_child.pop("composition_raw", None), new_root.pop("composition_raw", None)
        _require(new_comp == old_comp or new_comp == _NEUTRAL,
                 "The collapsed survivor has an unexpected root composition")
        _require(old_child == new_root, "Clearing Luma changed the collapsed survivor's content")
    return _evidence("clear_luma_range", layer, removed_index=index,
                     content_sha256=new["content_sha256"])


def parameter_invert_kind(state, layer_ref):
    """Preflight the three non-raster transforms checked in the native build."""
    layers, _, masks = _inventory(state)
    _require(layer_ref in layers and state.get("selected_layer") == layer_ref
             and layer_ref.get("is_image_layer") is False,
             "Read and select the target local layer first")
    root = masks[layer_ref["native_id"]]["structure"]
    _require(root.get("composition_raw") == _NEUTRAL,
             "Parameter inversion requires a neutral root composition")
    _require(not any(key in root for key in ("map", "children", "luma_range")),
             "Raster/composite inversion requires different coverage evidence")
    # C1 adds this opaque render seed when a dynamic mask is rendered. Keep
    # its complete checksum in the before/after comparison; do not discard it
    # or admit arbitrary unknown mask features.
    extras = root.get("other_fields", [])
    _require(not extras or (len(extras) == 1 and extras[0].get("name") == "RandSeed"
             and extras[0].get("flags") == 0 and extras[0].get("format") == 34
             and extras[0].get("children") == []), "Unknown parameter-mask fields are not qualified")
    kind = {1: "constant_fill", 3: "linear_gradient_raw", 4: "radial_gradient_raw"}.get(root.get("mask_type"))
    _require(kind is not None and kind in root, "No qualified native parameter inversion")
    return kind


def verify_parameter_invert(before, after, layer_ref):
    """Verify native parameter inversion without claiming pixel coverage.

    CDrawBuf::Invert swaps linear endpoints and complements its midpoint,
    toggles the radial final integer, or complements ConstantFill density.
    It does not toggle a CompParam flag. Raster/composite inputs are refused.
    """
    kind = parameter_invert_kind(before, layer_ref)
    old, new, layer = _bound(before, after, layer_ref)
    _require(parameter_invert_kind(after, layer) == kind, "Inversion changed the mask representation")
    expected = _semantic_tree(old["structure"])
    f32 = lambda value: struct.unpack("<f", struct.pack("<f", value))[0]
    if kind == "constant_fill":
        expected[kind] = f32(1.0 - expected[kind])
    elif kind == "linear_gradient_raw":
        values = expected[kind]
        expected[kind] = values[2:4] + values[0:2] + [f32(1.0 - values[4])]
    else:
        expected[kind][-1] = 1 - expected[kind][-1]
    _require(_semantic_tree(new["structure"]) == expected,
             "The saved parameters do not match the native inversion transform")
    return _evidence("invert", layer, parameter_transform_verified=True,
                     parameter_kind=kind, content_sha256=new["content_sha256"])


def raster_invert_evidence(state, layer_ref):
    """Preflight a selected simple raster whose complete valid pixels were read."""
    layers, _, masks = _inventory(state)
    _require(layer_ref in layers and state.get("selected_layer") == layer_ref
             and layer_ref.get("is_image_layer") is False,
             "Read and select the target local layer first")
    mask = masks[layer_ref["native_id"]]
    root, raster = mask["structure"], mask.get("raster")
    _require(root.get("mask_type") == 0 and root.get("composition_raw") == _NEUTRAL
             and not any(key in root for key in ("children", "luma_range", "other_fields",
                                                 "constant_fill", "linear_gradient_raw", "radial_gradient_raw")),
             "Raster inversion requires a neutral simple raster mask")
    _require(isinstance(raster, dict) and raster.get("decoded") is True
             and raster.get("decoder") == "c1_16.8.6_codec_0_2"
             and raster.get("order") == "64x64_tiles_row_order_valid_rows"
             and raster.get("width") == mask["width"] and raster.get("height") == mask["height"]
             and raster.get("pixel_count") == mask["width"] * mask["height"],
             "Complete bounded raster decoding is required before inversion")
    for key in ("pixels_sha256", "complement_sha256"):
        value = raster.get(key)
        _require(isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value),
                 "Invalid raster pixel fingerprint")
    return raster


def verify_raster_invert(before, after, layer_ref):
    """Prove every valid saved pixel changed to 255 minus its previous value."""
    previous = raster_invert_evidence(before, layer_ref)
    old, new, layer = _bound(before, after, layer_ref)
    current = raster_invert_evidence(after, layer)
    _require(previous["complement_sha256"] == current["pixels_sha256"]
             and previous["pixels_sha256"] == current["complement_sha256"],
             "Saved raster pixels do not match the exact 8-bit complement")
    evidence = _evidence("invert", layer, pixel_transform_verified=True,
                         pixel_count=current["pixel_count"], content_sha256=new["content_sha256"])
    evidence["pixels_verified"] = True
    return evidence


def verify_native_raster_filter(before, after, layer_ref, action, radius, *,
                                native_command_succeeded=False):
    """Validate filter scope and decodability after an acknowledged native call.

    No-op results are valid. Saved pixels do not encode the supplied amount,
    and this check does not reconstruct or prove the vendor's filter algorithm.
    The caller supplies True only after the official command returned success.
    """
    limit = {"feather": 100, "refine": 300}.get(action)
    _require(limit is not None and type(radius) in (int, float) and math.isfinite(radius)
             and 0 <= radius <= limit and (action != "refine" or radius > 0),
             "Unsupported native mask filter amount")
    _require(native_command_succeeded is True, "The official filter command must acknowledge success")
    previous = raster_invert_evidence(before, layer_ref)
    _, new, layer = _bound(before, after, layer_ref)
    current = raster_invert_evidence(after, layer)
    return _evidence(action, layer, native_amount_acknowledged=radius,
                     amount_readback_verified=False, algorithm_result_verified=False,
                     saved_raster_decoded=True, pixel_count=current["pixel_count"],
                     pixels_changed=previous["pixels_sha256"] != current["pixels_sha256"],
                     content_sha256=new["content_sha256"])
