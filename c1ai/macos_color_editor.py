"""Read-only saved Color Editor evidence for the qualified Mac Session schema.

Capture One's official colorEditorSettings setter copies Basic, Advanced and
Skin Tone together. Their saved ZCOLORCORRECTIONS text is kept intact here;
public Basic/Advanced readback alone cannot verify a complete copy.

This module neither saves the Session nor writes adjustments. The caller must
ask C1 to save, bind the live layer, then compare this evidence before dispatch
and after C1's own save. The stored value is not an Apple Events setter payload.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import sqlite3
import uuid

from .contracts import Refused


def _positive_id(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Invalid " + name)
    if isinstance(value, str) and (not value.isascii() or not value.isdecimal()):
        raise ValueError("Invalid " + name)
    result = int(value)
    if result <= 0:
        raise ValueError("Invalid " + name)
    return result


def _color_rows(raw):
    """Validate the observed numeric text framing without inventing Skin names."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Color Editor is absent or inherited; an explicit saved value is required")
    encoded = raw.encode("utf-8")
    if len(encoded) > 1024 * 1024 or not raw.isascii():
        raise ValueError("Saved Color Editor encoding or size is outside the qualified scope")
    rows = raw.split(";")
    if rows[-1] == "":
        rows.pop()
    if not 1 <= len(rows) <= 512:
        raise ValueError("Unexpected saved Color Editor record count")
    values = []
    for row in rows:
        columns = row.split(",")
        if len(columns) != 18:
            raise ValueError("Unknown saved Color Editor record layout")
        # Numeric values are supplemental diagnostics. The exact original text,
        # including all correction groups, remains the old-value/copy evidence.
        numbers = [float(value) for value in columns]
        if any(not math.isfinite(value) for value in numbers):
            raise ValueError("Non-finite saved Color Editor value")
        values.append(numbers)
    return values, encoded


def decode_saved_color_value(raw):
    """Keep SQL NULL and empty text distinct from serialized/corrupt values.

    The NULL digest has a non-text prefix, so it cannot collide with an empty
    string or any valid ASCII correction table. No default rows are invented.
    """
    if raw is None:
        return [], b"\x00CaptureOne:ZCOLORCORRECTIONS:NULL", "absent"
    if raw == "":
        return [], b"", "empty"
    try:
        rows, encoded = _color_rows(raw)
    except (ValueError, TypeError, OverflowError) as error:
        failure = Refused("color_editor_saved_state_corrupt", str(error))
        failure.dispatched = False
        raise failure from error
    return rows, encoded, "explicit"


def require_explicit_basic_source(snapshot):
    """Qualify whole-module copies without normalizing implicit Basic defaults.

    The installed serializer stores correction group in column 2: Basic is 1.
    A source with implicit Basic defaults may save only Skin Tone, while the
    official setter materializes nine Basic rows at the destination. Such a
    source cannot satisfy the strict whole-string copy evidence used here.
    """
    try:
        if not isinstance(snapshot, dict) or not snapshot.get("available"):
            raise ValueError("An available saved source is required")
        rows, encoded = _color_rows(snapshot.get("raw"))
        if (snapshot.get("format") != "capture_one_color_corrections_18_column_text"
                or snapshot.get("rows") != rows
                or snapshot.get("sha256") != hashlib.sha256(encoded).hexdigest()
                or any(row[0] not in (0, 1) or row[1] not in (0, 1)
                       or row[2] not in (0, 1, 2) or row[15] not in (0, 1) for row in rows)):
            raise ValueError("Unknown or inconsistent saved Color Editor source")
        basic = [row for row in rows if row[2] == 1]
        if len(basic) != 9 or len({tuple(row[6:9]) for row in basic}) != 9:
            raise ValueError("Whole Color Editor paste requires nine explicit distinct Basic sectors; implicit defaults are unsupported")
    except (ValueError, TypeError, KeyError) as error:
        failure = Refused("color_editor_implicit_source", str(error))
        failure.dispatched = False
        raise failure from error


def read_color_editor(session_path, variant_id, layer_uuid, *, expected_image_id,
                      expected_engine, expected_variant_uuid):
    """Read one explicit Color Editor value in a normal read-only WAL transaction.

    layer_uuid is ``Background`` or the observed native MaskUUID. All expected
    identity fields are required; a missing/recreated layer never falls back to
    its name, another position, defaults or combined settings.

    ``source`` is stable content identity. ``storage_row_id`` is diagnostic only:
    C1 can rebuild layer records while retaining their MaskUUID. Compare source
    plus sha256 for a saved-value guard, not the entire returned dictionary.
    """
    try:
        path = Path(session_path)
        if not path.is_absolute() or path.suffix.lower() != ".cosessiondb":
            raise ValueError("Expected an absolute Capture One Session database path")
        variant_id = _positive_id(variant_id, "variant ID")
        image_id = _positive_id(expected_image_id, "image ID")
        engine = _positive_id(expected_engine, "processing engine")
        variant_uuid = str(uuid.UUID(expected_variant_uuid)).upper()
        layer_uuid = "Background" if layer_uuid == "Background" else str(uuid.UUID(layer_uuid)).upper()
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            schemas = {
                "ZVARIANT": {row[1] for row in connection.execute("PRAGMA table_info(ZVARIANT)")},
                "ZVARIANTLAYER": {row[1] for row in connection.execute("PRAGMA table_info(ZVARIANTLAYER)")},
                "ZIMAGE": {row[1] for row in connection.execute("PRAGMA table_info(ZIMAGE)")},
            }
            required = {
                "ZVARIANT": {"Z_PK", "ZVARIANTUUID", "ZIMAGE", "ZPROCESSINGENGINE", "ZADJUSTMENTLAYER"},
                "ZVARIANTLAYER": {"Z_PK", "ZVARIANT", "ZINDEX", "ZMASKUUID", "ZCOLORCORRECTIONS"},
                "ZIMAGE": {"Z_PK", "ZIMAGEUUID"},
            }
            if any(not columns <= schemas[table] for table, columns in required.items()):
                raise ValueError("Unrecognized saved Color Editor identity schema")
            variants = connection.execute(
                "SELECT v.Z_PK,v.ZVARIANTUUID,v.ZIMAGE,v.ZPROCESSINGENGINE,v.ZADJUSTMENTLAYER,"
                "i.ZIMAGEUUID FROM ZVARIANT v JOIN ZIMAGE i ON i.Z_PK=v.ZIMAGE WHERE v.Z_PK=?",
                (variant_id,),
            ).fetchall()
            if len(variants) != 1:
                raise ValueError("Saved variant or image is missing or ambiguous")
            variant = variants[0]
            if (str(uuid.UUID(variant["ZVARIANTUUID"])).upper() != variant_uuid or
                    variant["ZIMAGE"] != image_id or variant["ZPROCESSINGENGINE"] != engine):
                raise ValueError("Saved variant, image or processing engine changed")
            image_uuid = str(uuid.UUID(variant["ZIMAGEUUID"])).upper()
            if layer_uuid == "Background":
                layers = connection.execute(
                    "SELECT Z_PK,ZINDEX,ZMASKUUID,ZCOLORCORRECTIONS FROM ZVARIANTLAYER "
                    "WHERE ZVARIANT=? AND Z_PK=? AND ZINDEX IS NULL",
                    (variant_id, variant["ZADJUSTMENTLAYER"]),
                ).fetchall()
            else:
                layers = connection.execute(
                    "SELECT Z_PK,ZINDEX,ZMASKUUID,ZCOLORCORRECTIONS FROM ZVARIANTLAYER "
                    "WHERE ZVARIANT=? AND ZINDEX IS NOT NULL AND upper(ZMASKUUID)=?",
                    (variant_id, layer_uuid),
                ).fetchall()
            if len(layers) != 1:
                raise ValueError("Saved layer UUID is missing or ambiguous")
            layer = layers[0]
            raw = layer["ZCOLORCORRECTIONS"]
            rows, encoded, storage_state = decode_saved_color_value(raw)
        finally:
            connection.close()
        return {
            "source": {"session_path": str(path), "variant_id": variant_id,
                       "variant_uuid": variant_uuid, "image_id": image_id,
                       "image_uuid": image_uuid, "processing_engine": engine, "layer_uuid": layer_uuid},
            "storage_row_id": layer["Z_PK"],
            "raw": raw, "storage_state": storage_state, "sha256": hashlib.sha256(encoded).hexdigest(),
            "format": "capture_one_color_corrections_18_column_text",
            "rows": rows, "skin_values_decoded": False,
            "evidence": {"source": "session_sqlite_readonly", "includes_complete_saved_column": True,
                         "inherited_values_resolved": False, "live_state_checked": False, "atomic_with_host": False},
        }
    except Refused:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, OverflowError) as error:
        failure = Refused("color_editor_saved_state_unavailable", str(error))
        failure.dispatched = False
        raise failure from error
