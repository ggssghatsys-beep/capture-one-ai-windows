"""Native Color Editor saved-state proofs for C1 Mac 16.8.6.25.

No host or database writes. The compiled bridge edits deep native copies; this
module checks the complete serialized plans and C1's subsequent saved values.
"""
from __future__ import annotations

import hashlib
import math
import struct
from .contracts import Refused
from .macos_color_editor import _color_rows, decode_saved_color_value

BASIC_SECTORS = ("magenta", "purple", "blue", "cyan", "green", "yellow", "orange", "red", "all")

COLUMNS = {"enabled": (0, 1), "hue": (3, 1), "saturation": (4, 1),
           "lightness": (5, 10), "hue_low": (9, 1), "hue_high": (10, 1),
           "saturation_low": (11, 1), "saturation_high": (12, 1),
           "smoothness": (13, 1), "uniformity_hue": (14, 1), "inverted": (15, 1),
           "uniformity_saturation": (16, 1), "uniformity_lightness": (17, 1)}


def _require(condition, message):
    if not condition:
        raise Refused("native_color_readback_mismatch", message)


def groups_rows(groups):
    _require(isinstance(groups, list) and len(groups) == 3, "Three native Color Editor groups required")
    result = []
    for kind, text in enumerate(groups):
        rows = [] if text == "" else _color_rows(text)[0]
        _require(all(row[2] == kind and row[0] in (0, 1) and row[1] in (0, 1)
                     and row[15] in (0, 1) for row in rows), "Unknown native correction type or flag")
        _require(len(rows) <= (1 if kind == 2 else 35), "Native correction count exceeds qualified scope")
        result.append(rows)
    return result


def saved_rows(snapshot):
    _require(snapshot.get("available") is True and snapshot.get("format") == "capture_one_color_corrections_18_column_text",
             "An explicit complete saved Color Editor snapshot is required")
    rows, encoded, storage_state = decode_saved_color_value(snapshot["raw"])
    _require(snapshot.get("storage_state", "explicit") == storage_state, "Saved Color Editor storage state changed")
    _require(rows == snapshot.get("rows") and hashlib.sha256(encoded).hexdigest() == snapshot.get("sha256"),
             "Saved Color Editor rows or digest disagree with their source")
    return rows


def verify_live_saved(snapshot, groups):
    """Reject live/saved disagreement; bind genuinely absent/empty storage.

    Native groups supply the actual default model. An absent SQL column never
    supplies fabricated rows, and absent Advanced/Skin must also be empty live.
    """
    rows, native = saved_rows(snapshot), groups_rows(groups)
    for kind in range(3):
        saved = [row for row in rows if row[2] == kind]
        if kind == 1 and not saved:
            continue
        _require(saved == native[kind], "Native Color Editor differs from C1's saved group")
    return native


def enrich_state(state, snapshot, groups):
    native = verify_live_saved(snapshot, groups)
    def ranges(rows, skin=False):
        result = []
        for index, row in enumerate(rows):
            fields = {name: bool(row[column]) if name in {"enabled", "inverted"} else row[column] * scale
                      for name, (column, scale) in COLUMNS.items()
                      if skin or not name.startswith("uniformity_")}
            result.append({"index": index, "rgb": row[6:9], **fields})
        return result
    _require(len(native[1]) == 9, "Basic native model must contain all nine known sectors")
    colors = {name: {"hue": row[3], "saturation": row[4], "lightness": row[5] * 10}
              for name, row in zip(BASIC_SECTORS, native[1])}
    return {**state, "colors": colors, "advanced": {**state["advanced"], "ranges": ranges(native[0])},
            "skin": {"available": True, "ranges": ranges(native[2], True),
                     "source": "capture_one_native_bridge", "persisted_only": False}}


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _near(actual, expected):
    return math.isclose(actual, expected, rel_tol=1.2e-7, abs_tol=1e-7)


def _validate_requested_extent(row, patch, *, adding=False, stored=False):
    """Check only requested geometry, preserving C1's existing/default extents."""
    saturation_changed = (bool({"saturation_low", "saturation_high"} & patch.keys())
                          or (not adding and "rgb" in patch))
    if saturation_changed:
        maximum, minimum = max(row["rgb"]), min(row["rgb"])
        saturation = 100 * (maximum - minimum) / maximum if maximum else 0
        low, high = row.get("saturation_low"), row.get("saturation_high")
        if ((low is not None and low < -saturation - 0.0001)
                or (high is not None and high > 100 - saturation + 0.0001)
                or (low is not None and high is not None and low > high)):
            raise Refused("out_of_range", "Color Editor saturation offsets must keep the absolute range within 0..100 for this RGB origin")
    if {"hue_low", "hue_high"} & patch.keys():
        low, high = row.get("hue_low"), row.get("hue_high")
        if low is not None and high is not None and high - low > (359.0001 if stored else 359):
            raise Refused("out_of_range", "Color Editor hue range cannot exceed 359 degrees")


def validate_range_patch(state, patch):
    """Check requested final geometry before dispatch; native checks its plan.

    An add's omitted factory offsets are unknown here. The detached native
    serialized plan checks both actual sides before any owner setter.
    """
    mode, action = patch.get("mode"), patch.get("action", "update")
    if mode not in {"advanced", "skin"} or action == "remove":
        return
    if action == "add":
        row = {**patch}
    else:
        ranges = state[mode]["ranges"]
        _require(patch["index"] < len(ranges), "Color Editor range is missing")
        row = {**ranges[patch["index"]], **patch}
    _validate_requested_extent(row, patch, adding=action == "add")


def verify_plan(before_groups, planned_groups, patch):
    old, new = groups_rows(before_groups), groups_rows(planned_groups)
    reset = patch.get("action") == "reset"
    if reset:
        _require(len(old[1]) == len(new[1]) == 9 and not new[0] and not new[2],
                 "Neutral reset must retain nine Basic sectors and remove Advanced/Skin")
        for a, b in zip(old[1], new[1]):
            _require(all(b[c] == (0 if c in (3, 4, 5) else a[c]) for c in range(18)),
                     "Neutral Basic reset changed a hidden column")
        return
    kind = {"advanced": 0, "basic": 1, "skin": 2}[patch["mode"]]
    _require(all(old[k] == new[k] for k in range(3) if k != kind), "Native plan changes another Color Editor group")
    action = patch.get("action", "update")
    index = (BASIC_SECTORS.index(patch["color"]) if kind == 1 else
             len(old[kind]) if action == "add" else patch["index"])
    if kind == 1:
        _require(len(old[1]) == 9 and action == "update" and
                 not (patch["color"] == "all" and "lightness" in patch), "Invalid Basic sector edit")
    previous, changed = old[kind], new[kind]
    if action == "remove":
        _require(changed == previous[:index] + previous[index + 1:] and 0 <= index < len(previous),
                 "Removal changed another Color Editor row")
        return
    add = action == "add"
    _require(len(changed) == len(previous) + int(add) and 0 <= index < len(changed), "Native range count/index mismatch")
    _require(all(a == b for i, (a, b) in enumerate(zip(previous, changed)) if i != index),
             "Native plan changes another range")
    wanted = {}
    for name, (column, scale) in COLUMNS.items():
        if name in patch:
            wanted[column] = float(patch[name]) if name in {"enabled", "inverted"} else _f32(_f32(patch[name]) / scale)
    if "rgb" in patch:
        wanted.update({6 + i: _f32(value) for i, value in enumerate(patch["rgb"])})
    for column, value in enumerate(changed[index]):
        if column in wanted:
            _require(_near(value, wanted[column]), "Native plan does not match requested Color Editor value")
        elif not add:
            _require(value == previous[index][column], "Native plan changes an unrequested hidden column")
    _require(changed[index][2] == kind, "Native range factory returned the wrong group")
    if kind in (0, 2):
        row = changed[index]
        _validate_requested_extent({"rgb": row[6:9], "hue_low": row[9], "hue_high": row[10],
                                    "saturation_low": row[11], "saturation_high": row[12]}, patch, adding=add, stored=True)


def verify_saved_change(before, after, receipt, patch):
    _require(receipt.get("state") == "committed" and receipt.get("dispatched") is True,
             "Native Color Editor operation has not committed")
    _require(before["source"] == after["source"], "Saved Color Editor identity changed")
    native_before = verify_live_saved(before, receipt["before_groups"])
    planned = groups_rows(receipt["planned_groups"])
    verify_plan(receipt["before_groups"], receipt["planned_groups"], patch)
    before_rows, after_rows = saved_rows(before), saved_rows(after)
    changed = {0, 1, 2} if patch.get("action") == "reset" else {{"advanced": 0, "basic": 1, "skin": 2}[patch["mode"]]}
    for kind in range(4):
        expected = planned[kind] if kind in changed else [r for r in before_rows if r[2] == kind]
        actual = [r for r in after_rows if r[2] == kind]
        # C1 may materialize its nine implicit Basic sectors on the first
        # requested Advanced/Skin write. Only the previously read native rows
        # are accepted, byte-value-for-byte-value across all eighteen columns.
        implicit_basic = (kind == 1 and kind not in changed and not expected
                          and len(native_before[1]) == 9 and actual == native_before[1])
        _require(actual == expected or implicit_basic,
                 "C1 saved unexpected Color Editor values or changed an untouched group")
    _require(all(row[2] in (0, 1, 2, 3) for row in before_rows + after_rows), "Unrecognized saved correction group")
    return {"source": "capture_one_native_bridge", "model_verified": True,
            "complete_saved_value_verified": True, "hidden_columns_preserved": True,
            "render_verified": False, "focus_used": False, "atomic": False}
