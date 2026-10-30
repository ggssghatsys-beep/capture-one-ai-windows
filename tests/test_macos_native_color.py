"""Saved proofs reject hidden changes and verify native float setter semantics."""
import copy
import hashlib
import pytest
from c1ai.contracts import Refused
from c1ai.macos_native_color import enrich_state, verify_plan, verify_saved_change


def row(kind, **changes):
    values = [1, 1, kind, 0, 0, 0, 164, 114, 110, -25, 25, -30, 40, 25, 0, 0, 0, 0]
    for column, value in changes.items():
        values[int(column)] = value
    return values


def text(rows):
    return ";".join(",".join(str(value) for value in row) for row in rows)


def snapshot(rows):
    raw = text(rows)
    return {"available": True, "format": "capture_one_color_corrections_18_column_text",
            "source": {"variant": 1, "layer": "abc"}, "raw": raw, "rows": rows,
            "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def receipt(old, new):
    return {"state": "committed", "dispatched": True,
            "before_groups": [text(group) for group in old], "planned_groups": [text(group) for group in new]}


def test_saved_skin_update_preserves_every_hidden_column_and_other_group():
    old = [[row(0)], [], [row(2)]]
    new = copy.deepcopy(old)
    new[2][0][5] = 0.25  # Native lightness setter divides UI value by ten.
    patch = {"mode": "skin", "index": 0, "lightness": 2.5}
    before, after = snapshot(old[0] + old[2]), snapshot(new[0] + new[2])
    assert verify_saved_change(before, after, receipt(old, new), patch)["complete_saved_value_verified"]
    after["rows"][0][15] = 1
    after["raw"] = text(after["rows"])
    after["sha256"] = hashlib.sha256(after["raw"].encode()).hexdigest()
    with pytest.raises(Refused):
        verify_saved_change(before, after, receipt(old, new), patch)


def test_inversion_plan_cannot_change_unrequested_rgb_or_flags():
    old = [[row(0)], [], []]
    new = copy.deepcopy(old)
    new[0][0][15] = 1
    patch = {"mode": "advanced", "index": 0, "inverted": True}
    verify_plan(receipt(old, new)["before_groups"], receipt(old, new)["planned_groups"], patch)
    new[0][0][1] = 0
    with pytest.raises(Refused):
        verify_plan(receipt(old, new)["before_groups"], receipt(old, new)["planned_groups"], patch)


def test_skin_creation_removal_and_native_read_enrichment():
    basic = [row(1) for _ in range(9)]
    old, new = [[], basic, []], [[], basic, [row(2)]]
    patch = {"mode": "skin", "action": "add", "rgb": [164, 114, 110]}
    verify_plan(receipt(old, new)["before_groups"], receipt(old, new)["planned_groups"], patch)
    state = enrich_state({"advanced": {"ranges": []}}, snapshot(new[2]), receipt(old, new)["planned_groups"])
    assert state["skin"]["ranges"][0]["rgb"] == [164, 114, 110]
    assert state["skin"]["persisted_only"] is False
    verify_plan(receipt(new, old)["before_groups"], receipt(new, old)["planned_groups"],
                {"mode": "skin", "action": "remove", "index": 0})


def test_full_reset_removes_skin_and_advanced_without_resetting_basic_range():
    old = [[row(0)], [row(1, **{"3": 1, "4": 2, "5": 3}) for _ in range(9)], [row(2)]]
    new = [[], [row(1) for _ in range(9)], []]
    patch = {"action": "reset"}
    before = snapshot(old[1] + old[0] + old[2])
    after = snapshot(new[1])
    assert verify_saved_change(before, after, receipt(old, new), patch)["complete_saved_value_verified"]
    new[1][0][13] = 1
    with pytest.raises(Refused):
        verify_plan(receipt(old, new)["before_groups"], receipt(old, new)["planned_groups"], patch)


def test_first_skin_write_accepts_only_observed_implicit_basic_materialization():
    from c1ai.macos_color_editor import decode_saved_color_value
    native_before = [[], [row(1) for _ in range(9)], []]
    planned = copy.deepcopy(native_before)
    planned[2] = [row(2)]
    rows, encoded, storage = decode_saved_color_value(None)
    before = {"available": True, "format": "capture_one_color_corrections_18_column_text",
              "source": {"variant": 1, "layer": "abc"}, "raw": None, "rows": rows,
              "storage_state": storage, "sha256": hashlib.sha256(encoded).hexdigest()}
    patch = {"mode": "skin", "action": "add", "rgb": [164, 114, 110]}
    assert enrich_state({"advanced": {"ranges": []}}, before,
                        receipt(native_before, planned)["before_groups"])["skin"]["available"]
    # Both C1 save behaviors are valid: leave Basic implicit, or materialize
    # exactly the complete nine-sector native snapshot previously observed.
    for materialized in (planned[2], planned[1] + planned[2]):
        assert verify_saved_change(before, snapshot(materialized), receipt(native_before, planned), patch)
    bad = copy.deepcopy(planned[1] + planned[2])
    bad[0][13] += 1
    with pytest.raises(Refused):
        verify_saved_change(before, snapshot(bad), receipt(native_before, planned), patch)
    with pytest.raises(Refused):
        verify_saved_change(before, snapshot([row(0)] + planned[2]), receipt(native_before, planned), patch)


def test_read_saved_column_distinguishes_absent_empty_and_corrupt(tmp_path):
    import sqlite3
    from c1ai.macos_color_editor import read_color_editor
    path = tmp_path / "test.cosessiondb"
    image_uuid = "11111111-1111-1111-1111-111111111111"
    variant_uuid = "22222222-2222-2222-2222-222222222222"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
          CREATE TABLE ZIMAGE (Z_PK INTEGER, ZIMAGEUUID TEXT);
          CREATE TABLE ZVARIANT (Z_PK INTEGER, ZVARIANTUUID TEXT, ZIMAGE INTEGER,
            ZPROCESSINGENGINE INTEGER, ZADJUSTMENTLAYER INTEGER);
          CREATE TABLE ZVARIANTLAYER (Z_PK INTEGER, ZVARIANT INTEGER, ZINDEX INTEGER,
            ZMASKUUID TEXT, ZCOLORCORRECTIONS TEXT);
        """)
        connection.execute("INSERT INTO ZIMAGE VALUES (1,?)", (image_uuid,))
        connection.execute("INSERT INTO ZVARIANT VALUES (2,?,1,1300,3)", (variant_uuid,))
        connection.execute("INSERT INTO ZVARIANTLAYER VALUES (3,2,NULL,NULL,NULL)")
    def read():
        return read_color_editor(path, 2, "Background", expected_image_id=1,
                                 expected_engine=1300, expected_variant_uuid=variant_uuid)
    absent = read()
    assert absent["raw"] is None and absent["rows"] == [] and absent["storage_state"] == "absent"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE ZVARIANTLAYER SET ZCOLORCORRECTIONS='' WHERE Z_PK=3")
    empty = read()
    assert empty["raw"] == "" and empty["storage_state"] == "empty" and empty["sha256"] != absent["sha256"]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE ZVARIANTLAYER SET ZCOLORCORRECTIONS='broken' WHERE Z_PK=3")
    with pytest.raises(Refused) as error:
        read()
    assert error.value.code == "color_editor_saved_state_corrupt"


@pytest.mark.parametrize("native_token,enabled", [(True, False), (False, True)])
@pytest.mark.parametrize("mode", ["basic", "advanced", "skin", "reset"])
def test_changed_color_route_rejects_old_token_before_any_dispatch(tmp_path, monkeypatch, native_token, enabled, mode):
    from c1ai.macos_backend import MacOSBackend
    import c1ai.macos_bridge as bridge_module
    backend = MacOSBackend(tmp_path)
    backend._qualified = lambda observation: True
    monkeypatch.setattr(bridge_module, "configured", lambda root: enabled)
    calls = []
    def dispatched(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("stale route must never dispatch")
    backend._native = dispatched
    backend._adjust_native_color = dispatched
    backend._adjust_skin = dispatched
    backend.action_hook = dispatched
    observation = {"native_context": {"native_color_snapshot": {"groups": []}} if native_token else {}}
    with pytest.raises(Refused) as error:
        if mode == "reset":
            backend.apply_module_action("color_mixer", {"action": "reset"}, observation, None)
        else:
            backend.adjust_module("color_mixer", {"mode": mode, "index": 0, "hue": 1}, observation)
    assert error.value.code == "color_route_changed" and error.value.dispatched is False
    assert calls == []


def test_native_basic_uses_verified_sector_index_and_preserves_hidden_range():
    from c1ai.macos_native_color import BASIC_SECTORS
    old = [[row(0)], [row(1, **{"6": i + 100}) for i in range(9)], [row(2)]]
    assert BASIC_SECTORS[7] == "red" and BASIC_SECTORS[0] == "magenta"
    new = copy.deepcopy(old)
    new[1][7][3:6] = [2.5, -4.25, 0.15]
    patch = {"mode": "basic", "color": "red", "hue": 2.5, "saturation": -4.25, "lightness": 1.5}
    assert verify_saved_change(snapshot(old[1] + old[0] + old[2]), snapshot(new[1] + new[0] + new[2]),
                               receipt(old, new), patch)["complete_saved_value_verified"]
    new[1][7][9] -= 1
    with pytest.raises(Refused):
        verify_plan(receipt(old, new)["before_groups"], receipt(old, new)["planned_groups"], patch)


@pytest.mark.parametrize("mode,changes", [
    ("advanced", {"hue_low": -200, "hue_high": 200}),
    ("advanced", {"saturation_low": -90}),
    ("skin", {"saturation_high": 90}),
])
def test_native_range_final_constraints_reject_before_dispatch(tmp_path, monkeypatch, mode, changes):
    from c1ai.macos_backend import MacOSBackend
    import c1ai.macos_bridge as bridge_module
    backend = MacOSBackend(tmp_path)
    monkeypatch.setattr(bridge_module, "configured", lambda root: True)
    calls = []
    backend.action_hook = lambda *args, **kwargs: calls.append(args)
    backend.native_bridge = lambda: calls.append("bridge")
    initial = {"rgb": [200, 100, 100], "hue_low": -25, "hue_high": 25,
               "saturation_low": -25, "saturation_high": 25}
    observation = {"state": {mode: {"ranges": [initial]}},
                   "native_context": {"native_color_snapshot": {"groups": []}}}
    with pytest.raises(Refused) as error:
        backend._adjust_native_color({"mode": mode, "index": 0, **changes}, observation)
    assert error.value.code == "out_of_range" and calls == []


def test_native_numeric_edit_preserves_host_default_extents_and_rgb_checks_final_origin():
    from c1ai.macos_native_color import validate_range_patch
    # A host-created gray range may have extents crossing zero. Numeric-only
    # edits preserve that existing range rather than pretending to repair it.
    state = {"advanced": {"ranges": [{"rgb": [100, 100, 100], "hue_low": -200,
              "hue_high": 200, "saturation_low": -40, "saturation_high": 40}]}}
    validate_range_patch(state, {"mode": "advanced", "index": 0, "hue": 1, "smoothness": 20})
    state["advanced"]["ranges"][0].update(rgb=[200, 100, 100], hue_low=-25, hue_high=25,
                                          saturation_low=-30, saturation_high=40)
    with pytest.raises(Refused, match="absolute range"):
        validate_range_patch(state, {"mode": "advanced", "index": 0, "rgb": [100, 100, 100]})
    with pytest.raises(Refused, match="359"):
        validate_range_patch(state, {"mode": "advanced", "index": 0, "hue_low": -357})


def test_add_single_saturation_endpoint_uses_actual_detached_other_side():
    from c1ai.macos_native_color import validate_range_patch
    patch = {"mode": "advanced", "action": "add", "rgb": [200, 100, 100], "saturation_low": -10}
    # The explicit side is safe, but Python does not invent a default high side.
    validate_range_patch({}, patch)
    old = [[], [], []]
    actual = row(0, **{"6": 200, "7": 100, "8": 100, "11": -10, "12": 70})
    with pytest.raises(Refused, match="absolute range"):
        verify_plan(receipt(old, [[actual], [], []])["before_groups"],
                    receipt(old, [[actual], [], []])["planned_groups"], patch)
    # With no requested extent change, the native factory's default is retained.
    verify_plan(receipt(old, [[actual], [], []])["before_groups"],
                receipt(old, [[actual], [], []])["planned_groups"],
                {"mode": "advanced", "action": "add", "rgb": [200, 100, 100]})
