"""Focused identity and temporary-sharing failure checks; no C1/network calls."""
from types import SimpleNamespace
import copy
import sqlite3
import pytest

from c1ai.contracts import Refused
from c1ai import macos_pilot as pilot


def preview_state():
    layer = {"is_image_layer": True}
    return {"layers": {"layers": [layer], "selected_layer": layer, "details": []},
            "variant_uuid": "00000000-0000-0000-0000-000000000001"}


def test_duplicate_filenames_require_native_variant_identity():
    rows = [{"id": "100", "position": 1, "path": "/photos/same.nef"},
            {"id": "101", "position": 2, "path": "/photos/same.nef"}]
    def variant(row):
        return {"kVariantKey_VariantID": row["id"] + "/00000000-0000-0000-0000-000000000001",
                "kVariantKey_VariantNo": row["position"] - 1, "kVariantKey_VariantName": "same.nef",
                "kVariantKey_ChangeType": "new", "kVariantKey_Properties": {
                    "kVariantProperty_Missing": False, "kVariantProperty_Corrupted": False,
                    "kVariantProperty_IsOffline": False}}
    state = {"variants": [variant(row) for row in rows]}
    target = {"variant": {"id": "101"}}
    assert pilot.match_variant(state, rows, target)["kVariantKey_VariantID"].startswith("101/")
    state["variants"][1]["kVariantKey_VariantID"] = "999/00000000-0000-0000-0000-000000000001"
    with pytest.raises(Refused, match="outside"):
        pilot.match_variant(state, rows, target)


@pytest.mark.parametrize("failure_stage", ["http", "cleanup", "stop_journal"])
def test_http_failure_stops_owned_service_or_retains_recovery_receipt(tmp_path, monkeypatch, failure_stage):
    cleanup_fails = failure_stage == "cleanup"
    target = {"instance": {"pid": 123}, "variant": {"id": "101"}}
    config = {"collection_id": "approved", "images": {"100": "/photos/same.nef"}, "password": "x" * 40}
    monkeypatch.setattr(pilot, "load_config", lambda *args: config)
    monkeypatch.setattr(pilot, "check_port", lambda *args: None)
    monkeypatch.setattr(pilot, "saved_snapshot", lambda *args: {"state": preview_state()})
    calls = []
    def control(command, receipt):
        calls.append(command)
        if command == "inspect":
            return {"settings": {"mobile": False, "web": False}}
        if command == "start":
            return {"settings": {"port": 12345}, "rows": []}
        assert command == "stop"
        if cleanup_fails:
            raise Refused("cleanup_failed", "cleanup failed")
        return {"restored": True}
    monkeypatch.setattr(pilot, "control", control)
    def fail_http(*args):
        raise OSError("connection failed")
    monkeypatch.setattr(pilot, "PilotClient", fail_http)
    def hook(name, fn, intent):
        if name == "pilot.stop" and failure_stage == "stop_journal":
            raise OSError("journal unavailable")
        return fn()
    backend = SimpleNamespace(state_dir=tmp_path, action_hook=hook,
        _native=lambda *args: {"result": {"render_state": {"layers": [{}]}}})
    if failure_stage != "http":
        result = pilot.render(backend, {"kind": "native"}, {"target": target})
        assert result["status"] == "outcome_unknown"
        assert result["cleanup"]["sharing_stopped_and_settings_restored"] is (not cleanup_fails)
    else:
        with pytest.raises(OSError):
            pilot.render(backend, {"kind": "native"}, {"target": target})
    assert calls == ["inspect", "start", "stop"]
    assert (tmp_path / "macos-pilot-active.json").exists() is cleanup_fails


def test_scope_rejects_reused_native_id_for_a_different_raw(tmp_path):
    target = {"document": {"id": "session"}, "instance": {"version": "16.8.6.25"},
              "image": {"id": "100", "path": "/photos/other.nef"}}
    pilot.save_private(tmp_path / "macos-pilot.json", {"version": 1,
        "capture_one_version": "16.8.6.25", "document": target["document"], "collection_id": "approved",
        "images": {"100": "/photos/same.nef"}, "password": "x" * 40})
    with pytest.raises(Refused, match="outside"):
        pilot.load_config(tmp_path, target)


def test_cleanup_selection_change_prevents_image_delivery(tmp_path, monkeypatch):
    target = {"instance": {"pid": 123}, "variant": {"id": "101"}}
    monkeypatch.setattr(pilot, "load_config", lambda *args: {
        "collection_id": "approved", "images": {}, "password": "x" * 40})
    monkeypatch.setattr(pilot, "check_port", lambda *args: None)
    calls = []
    started = {"settings": {"port": 12345, "mobile": True, "web": False}, "rows": []}
    def control(command, receipt):
        calls.append(command)
        if command == "inspect" and calls == ["inspect"]:
            return {"settings": {"mobile": False, "web": False}}
        return {"restored": True} if command == "stop" else started
    monkeypatch.setattr(pilot, "control", control)
    monkeypatch.setattr(pilot, "PilotClient", lambda *args: SimpleNamespace(state=lambda: {}, image=lambda *args: b"image"))
    monkeypatch.setattr(pilot, "match_variant", lambda *args: {
        "kVariantKey_VariantID": "101/00000000-0000-0000-0000-000000000001"})
    snapshots = []
    def snapshot(*args):
        snapshots.append(True)
        if len(snapshots) == 3:
            raise Refused("target_changed", "Selection changed during album cleanup")
        return {"state": preview_state()}
    monkeypatch.setattr(pilot, "saved_snapshot", snapshot)
    backend = SimpleNamespace(state_dir=tmp_path, _native=snapshot, action_hook=lambda name, fn, intent: fn())
    with pytest.raises(Refused, match="Selection changed"):
        pilot.render(backend, {}, {"target": target})
    assert calls[-1] == "stop"
    assert not (tmp_path / "macos-pilot-active.json").exists()
    assert not (tmp_path / "native-previews").exists()


def test_saved_multilayer_binding_detects_mask_changes_and_uuid_mismatch(tmp_path):
    variant_uuid = "00000000-0000-0000-0000-000000000001"
    mask_uuid = "00000000-0000-0000-0000-000000000002"
    raw = tmp_path / "photo.nef"
    folder = tmp_path / "CaptureOne" / "Settings1680"
    folder.mkdir(parents=True)
    cos = folder / "photo.nef.cos"
    cos.write_text('<IMG><VAR Engine="1300"><E K="UUID" V="'+variant_uuid+'"/>'
                   '<E K="LowResProcessCounter" V="4"/><LA><E K="MaskUUID" V="'+mask_uuid+'"/></LA></VAR></IMG>')
    mask = folder / "photo.nef.comask"
    mask.write_bytes(b"\x04XConfirst")
    database = tmp_path / "test.cosessiondb"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE ZVARIANT (Z_PK, ZVARIANTUUID, ZIMAGE, ZPROCESSINGENGINE, ZADJUSTMENTLAYER, ZHIGHRESPROCESSCOUNTER, ZLOWRESPROCESSCOUNTER, ZSYNCPROCESSCOUNTER, ZDEFAULTLAYER, ZCOMBINEDSETTINGS)")
        db.execute("INSERT INTO ZVARIANT VALUES (1, ?, 2, 1300, 3, NULL, 4, NULL, 5, 6)", (variant_uuid,))
        db.execute("CREATE TABLE ZVARIANTLAYER (Z_PK, ZVARIANT, ZINDEX, ZMASKUUID, ZMETADATA, ZEXPOSURE)")
        db.executemany("INSERT INTO ZVARIANTLAYER VALUES (?,1,?,?,?,0)",
                       [(3,None,None,7),(4,0,mask_uuid,None),(5,None,None,8),(6,None,None,9)])
        db.execute("CREATE TABLE ZVARIANTMETADATA (Z_PK, ZLAYER, ZBASIC_RATING)")
        db.executemany("INSERT INTO ZVARIANTMETADATA VALUES (?,?,0)", [(7,3),(8,5),(9,6)])
    refs = [{"variant_uuid": variant_uuid, "is_image_layer": True, "native_id": "Background"},
            {"variant_uuid": variant_uuid, "is_image_layer": False, "native_id": mask_uuid}]
    target = {"instance": {"version": "16.8.6.25"}, "document": {"path": str(database)},
              "image": {"id": "2", "path": str(raw)}, "variant": {"id": "1"}, "layer": refs[1]}
    observation = {"target": target, "values": {}, "native_context": {"processing_engine": 1300}}
    layer_state = {"layers": refs, "selected_layer": refs[1], "details": []}
    def native(command, *args):
        return {"observation": copy.deepcopy(observation), "result": {"state": copy.deepcopy(layer_state)}}
    backend = SimpleNamespace(_native=native)
    baseline = pilot.saved_snapshot(backend, observation)
    # C1 may rebuild storage rows without changing the stable layer UUID.
    # Identity and content remain equal across a semantically identical rebuild,
    # including rebuilt global layers and metadata foreign keys.
    with sqlite3.connect(database) as db:
        db.execute("UPDATE ZVARIANTLAYER SET Z_PK=Z_PK+10, ZMETADATA=ZMETADATA+10")
        db.execute("UPDATE ZVARIANTMETADATA SET Z_PK=Z_PK+10, ZLAYER=ZLAYER+10")
        db.execute("UPDATE ZVARIANT SET ZADJUSTMENTLAYER=ZADJUSTMENTLAYER+10, ZDEFAULTLAYER=ZDEFAULTLAYER+10, ZCOMBINEDSETTINGS=ZCOMBINEDSETTINGS+10")
    rebuilt = pilot.saved_snapshot(backend, observation)
    assert rebuilt["state"] == baseline["state"]
    with sqlite3.connect(database) as db:
        db.execute("UPDATE ZVARIANTLAYER SET ZEXPOSURE=0.5 WHERE Z_PK=14")
    assert pilot.saved_snapshot(backend, observation)["state"]["database_digest"] != baseline["state"]["database_digest"]
    with sqlite3.connect(database) as db:
        db.execute("UPDATE ZVARIANTLAYER SET ZEXPOSURE=0 WHERE Z_PK=14")
        db.execute("UPDATE ZVARIANTMETADATA SET ZBASIC_RATING=1 WHERE Z_PK=17")
    assert pilot.saved_snapshot(backend, observation)["state"]["database_digest"] != baseline["state"]["database_digest"]
    mask.write_bytes(b"\x04XConchanged")
    assert pilot.saved_snapshot(backend, observation)["state"] != baseline["state"]
    cos.write_text(cos.read_text().replace(variant_uuid, "00000000-0000-0000-0000-000000000003"))
    with pytest.raises(Refused, match="UUID"):
        pilot.saved_snapshot(backend, observation)
