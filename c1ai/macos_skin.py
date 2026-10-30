"""Read and verify saved Skin Tone parameters; no database or UI writes.

The 16.8.6 serializer and UI bindings establish these 18-column mappings.
This does not create a color range, resolve inherited settings, or claim that
the official scripting dictionary exposes individual Skin Tone properties.
"""
from __future__ import annotations

import hashlib
import math
import struct

from .contracts import Refused
from .macos_color_editor import _color_rows


# (saved column, UI-to-saved divisor, minimum, maximum)
FIELDS = {
    "hue": (3, 1, -5, 5),
    "saturation": (4, 1, -30, 30),
    "lightness": (5, 10, -10, 10),
    "smoothness": (13, 1, 1, 30),
    "uniformity_hue": (14, 1, 0, 100),
    "uniformity_saturation": (16, 1, 0, 100),
    "uniformity_lightness": (17, 1, 0, 100),
}


def _require(condition, message):
    if not condition:
        raise Refused("skin_saved_state_unverified", message)


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _rows(snapshot):
    _require(isinstance(snapshot, dict)
             and snapshot.get("format") == "capture_one_color_corrections_18_column_text",
             "A complete saved Color Editor snapshot is required")
    source, evidence = snapshot.get("source"), snapshot.get("evidence")
    keys = {"session_path", "variant_id", "variant_uuid", "image_id", "image_uuid",
            "processing_engine", "layer_uuid"}
    _require(isinstance(source, dict) and keys <= source.keys()
             and all(source[key] is not None for key in keys)
             and source["processing_engine"] == 1300
             and isinstance(evidence, dict) and evidence.get("source") == "session_sqlite_readonly"
             and evidence.get("includes_complete_saved_column") is True,
             "Saved Color Editor identity or format qualification is missing")
    try:
        rows, encoded = _color_rows(snapshot.get("raw"))
    except (ValueError, TypeError, OverflowError) as exc:
        raise Refused("skin_saved_state_unverified", str(exc)) from exc
    _require(snapshot.get("rows") == rows and snapshot.get("sha256") == hashlib.sha256(encoded).hexdigest(),
             "Saved Color Editor text, rows and digest disagree")
    _require(all(row[0] in (0, 1) and row[1] in (0, 1) and row[2] in (0, 1, 2, 3)
                 and row[15] in (0, 1) for row in rows),
             "Unknown Color Editor type or flag encoding")
    indices = [i for i, row in enumerate(rows) if row[2] == 2]
    _require(len(indices) <= 1, "Multiple Skin Tone ranges are outside the qualified scope")
    return rows, indices


def read_skin(snapshot):
    """Return the seven UI-unit values of the existing Skin range, if present."""
    rows, indices = _rows(snapshot)
    ranges = []
    if indices:
        row = rows[indices[0]]
        values = {name: row[column] * scale for name, (column, scale, _, _) in FIELDS.items()}
        _require(all(low <= values[name] <= high for name, (_, _, low, high) in FIELDS.items()),
                 "Saved Skin Tone values exceed the checked UI range")
        ranges.append({"index": 0, **values})
    return {"available": True, "ranges": ranges, "source": "session_sqlite_readonly",
            "persisted_only": True}


def verify_skin_update(before_snapshot, after_snapshot, index, patch):
    """Verify requested existing-range values and preserve every other column.

    ``index`` is the public Skin-range index (0), not a mixed-table row index.
    ``patch`` contains only the seven numeric fields. UI dispatch, process and
    current-layer binding remain the caller's responsibility.
    """
    before = read_skin(before_snapshot)
    after = read_skin(after_snapshot)
    _require(type(index) is int and index == 0 and len(before["ranges"]) == len(after["ranges"]) == 1,
             "Skin Tone update requires the same existing single range")
    _require(isinstance(patch, dict) and bool(patch) and patch.keys() <= FIELDS.keys(),
             "Only existing Skin Tone numeric fields may be updated")
    _require(before_snapshot["source"] == after_snapshot["source"],
             "The saved Session, variant, image, engine or layer changed")
    old, old_indices = _rows(before_snapshot)
    new, new_indices = _rows(after_snapshot)
    _require(len(old) == len(new) and old_indices == new_indices,
             "Skin Tone range creation, removal or reordering is not supported")
    expected = {}
    for field, value in patch.items():
        column, scale, low, high = FIELDS[field]
        _require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
                 "Invalid Skin Tone value for " + field)
        # POSlider bindings call a float setter; lightness then divides by 10.
        expected[column] = _f32(_f32(value) / scale)
    for row_index, (previous, current) in enumerate(zip(old, new)):
        for column, (a, b) in enumerate(zip(previous, current)):
            if row_index == old_indices[0] and column in expected:
                wanted = expected[column]
                # Only float32 storage rounding is tolerated, never UI steps.
                tolerance = max(1e-7, abs(wanted) * 1.2e-7)
                _require(math.isclose(b, wanted, rel_tol=0, abs_tol=tolerance),
                         "Saved Skin Tone value does not match the request")
            else:
                _require(a == b, "An unrequested Color Editor column or range changed")
    return {"source": "session_sqlite_readonly", "complete_saved_value_verified": True,
            "requested_fields": list(patch), "skin": after, "render_verified": False,
            "live_model_verified": False}
