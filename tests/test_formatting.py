"""Presentation must shrink diagnostics without changing operation evidence."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path

from c1ai.formatting import compact_response


def observation(identity="candidate", value=0.5):
    return {"target": {"variant": {"id": identity}}, "values": {"exposure": value},
            "observation_token": "token-" + identity,
            "evidence": {"source": "windows_uia_control", "model_verified": False},
            "raw_state": {"nodes": [{"selector": {"name": "locator"}}] * 100}}


def test_compact_read_preserves_target_values_provenance_and_token():
    original = {"status": "ok", **observation()}
    untouched = deepcopy(original)
    compact = compact_response(original, command="read")
    assert compact == {key: original[key] for key in (
        "status", "target", "values", "evidence", "observation_token")}
    assert original == untouched
    assert len(json.dumps(compact)) < len(json.dumps(original)) / 5


def test_degraded_context_preserves_not_editable_and_native_window_evidence():
    original = {"status": "ok", "target": None, "editing_ready": False,
                "reason": "owned_window_blocks_main", "capabilities": {"modules": {}, "fields": {}},
                "native_windows": {"main_enabled": False, "blocking_modal_possible": True,
                    "enumeration_complete": True, "selected_target_verified": False,
                    "uia_queried": False, "automatic_close_permitted": False,
                    "modal_candidates": [{"hwnd": 22, "pid": 5, "title": "Setup", "owner_chain": [11]}]},
                "pending_operations": []}
    assert compact_response(original, command="context") == original


def test_compact_write_keeps_operation_error_and_actual_candidate_after():
    failure = {"code": "PARTIAL", "message": "Second field outcome is unknown",
               "details": {"possibly_mutated": True, "receipt": {"output_path": "saved.jpg"},
                           "raw_state": {"important_failure_context": "retained"}}}
    original = {"operation_id": "operation-1", "request_id": "request-1", "status": "partial",
                "progress": {"exposure": "control_confirmed"}, "before": observation("original", 0),
                "after": observation("original", 0), "error": failure, "model_verified": False,
                "result": {"after": observation(), "candidate_variant_id": "candidate",
                           "nodes": [{"selector": {"name": "large"}}] * 20,
                           "render_verified": False},
                "actions": [{"action_id": "action-1", "name": "set:exposure", "status": "completed",
                             "intent": {"field": "exposure", "before": 0, "after": 0.5},
                             "result": {"control_confirmed": True, "observation": observation()}}]}
    compact = compact_response(original, command="images")
    assert compact["operation_id"] == "operation-1"
    assert compact["request_id"] == "request-1"
    assert compact["status"] == "partial"
    assert compact["after"]["target"]["variant"]["id"] == "candidate"
    assert compact["after"]["observation_token"] == "token-candidate"
    assert compact["error"] == failure
    assert compact["actions"][0]["intent"] == {"field": "exposure", "before": 0, "after": 0.5}
    assert compact["model_verified"] is False
    assert "nodes" not in compact["result"]
    assert "before" not in compact


def test_full_operation_get_is_unchanged_and_list_is_compact():
    record = {"operation_id": "one", "request_id": "request", "status": "completed",
              "before": observation(), "after": observation(), "actions": []}
    assert compact_response(record, command="operations", action="get") == record
    listed = compact_response({"status": "ok", "operations": [record]}, command="operations", action="list")
    assert listed["operations"][0]["operation_id"] == "one"
    assert "before" not in listed["operations"][0]


def test_mcp_proxy_is_bounded_and_preserves_original_hash_and_icc(tmp_path, monkeypatch):
    import base64
    from PIL import Image, ImageCms
    from c1ai import mcp_server

    archive = tmp_path / "renders"
    archive.mkdir()
    original_path = archive / "full-c1-render.jpg"
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (3200, 2400), (123, 80, 31)).save(original_path, icc_profile=icc, quality=95)
    source_data = original_path.read_bytes()
    digest = hashlib.sha256(source_data).hexdigest()
    monkeypatch.setattr(mcp_server, "state_directory", lambda: tmp_path)
    original = {"status": "completed", "operation_id": "render-operation", "request_id": "render-request",
                "model_verified": False, "result": {"source": "capture_one_export", "path": str(original_path),
                "render_verified": True, "quality_approved": False, "image": {"sha256": digest},
                "limits": "Parameters not observed are outside this evidence."}}
    delivered = mcp_server._preview_content(compact_response(original, command="preview"))
    metadata = delivered.structuredContent
    proxy = metadata["image_delivery"]
    assert metadata["operation_id"] == "render-operation"
    assert metadata["request_id"] == "render-request"
    assert metadata["result"]["source"] == "capture_one_export"
    assert metadata["result"]["image"]["sha256"] == digest
    assert metadata["model_verified"] is False and metadata["result"]["render_verified"] is True
    assert metadata["result"]["limits"] == original["result"]["limits"]
    assert proxy["source"] == "analysis_proxy"
    assert proxy["derived_from_sha256"] == digest
    assert proxy["actual_render_path"] == str(original_path)
    assert proxy["original_render_preserved"] is True and proxy["icc_preserved"] is True
    assert (proxy["width"], proxy["height"]) == (1600, 1200)
    proxy_path = Path(proxy["path"])
    assert proxy_path != original_path
    assert hashlib.sha256(proxy_path.read_bytes()).hexdigest() == proxy["sha256"]
    assert original_path.read_bytes() == source_data
    image_block = next(block for block in delivered.content if block.type == "image")
    image_bytes = base64.b64decode(image_block.data)
    assert hashlib.sha256(image_bytes).hexdigest() == proxy["sha256"]
    with Image.open(io.BytesIO(image_bytes)) as image:
        assert max(image.size) == 1600 and image.info["icc_profile"] == icc


def test_cli_compact_uses_same_formatter_and_keeps_error(monkeypatch, capsys):
    from c1ai import __main__, client

    original = {"status": "ok", **observation()}
    monkeypatch.setattr(client, "call", lambda *args, **kwargs: original)
    assert __main__.main(["read", "--json", '{"fields": ["exposure"]}', "--compact"]) == 0
    actual = json.loads(capsys.readouterr().out)
    assert actual == compact_response(original, command="read")


def test_mcp_get_keeps_full_logs_and_recover_is_public(monkeypatch):
    import asyncio
    from c1ai import mcp_server

    record = {"status": "completed", "operation_id": "one", "before": observation(), "after": observation()}
    monkeypatch.setattr(mcp_server, "call", lambda request: deepcopy(record))
    assert mcp_server.operations("get", operation_id="one") == record
    compact = mcp_server.operations("recover", operation_id="one")
    assert compact["operation_id"] == "one" and "before" not in compact
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert "recover" in tools["operations"].inputSchema["properties"]["action"]["enum"]


def test_successful_numeric_focus_side_effect_survives_compact_response():
    focus = {"before": {"foreground": 101, "pid": 10, "focus": 101, "readable": True},
             "after": {"foreground": 202, "pid": 20, "focus": 203, "readable": True},
             "unchanged": False, "hooks_installed": True, "samples": 12,
             "sample_interval_ms": 15, "changes": [{"reason": "sample"}] * 10}
    record = {"operation_id": "focus-operation", "request_id": "focus-request",
              "status": "control_confirmed", "model_verified": False, "actions": [{
              "action_id": "focus-action", "name": "set:exposure", "status": "completed",
              "result": {"control_confirmed": True, "model_verified": False, "backend": {
                  "accepted": True, "verified": True, "identity_verified": True,
                  "focus_policy": "observe", "focus": focus}}}]}
    compact = compact_response(record, command="adjust")
    assert compact["status"] == "control_confirmed" and compact["model_verified"] is False
    evidence = compact["actions"][0]["result"]["backend_evidence"]
    assert evidence["accepted"] is True and evidence["identity_verified"] is True
    assert evidence["focus"]["before"] == focus["before"]
    assert evidence["focus"]["after"] == focus["after"]
    assert evidence["focus"]["unchanged"] is False
    assert evidence["focus"]["recorded_changes"] == 10
    assert evidence["focus"]["observation_complete"] is True
    assert "changes" not in evidence["focus"]
    assert record["actions"][0]["result"]["backend"]["focus"]["changes"] == focus["changes"]


def test_unavailable_focus_observation_is_not_reported_as_stable():
    record = {"operation_id": "unknown-focus", "status": "control_confirmed", "result": {
        "focus": {"unchanged": False, "hooks_installed": False, "before": {"readable": False},
                  "after": {"readable": False}, "changes": []}}}
    focus = compact_response(record, command="adjust")["result"]["focus"]
    assert focus["unchanged"] is False and focus["observation_complete"] is False


def test_restored_summary_preserves_limited_scope_and_fresh_baseline_values():
    record = {"operation_id": "restore-op", "request_id": "adjust-request", "status": "restored",
              "after": observation(value=0.2), "restore_observation": observation(value=0),
              "restore_scope": "observed_numeric_values", "full_model_restore_verified": False,
              "pixel_restore_verified": None}
    untouched = deepcopy(record)
    compact = compact_response(record, command="operations", action="restore")
    assert compact["operation_id"] == "restore-op" and compact["request_id"] == "adjust-request"
    assert compact["restore_scope"] == "observed_numeric_values"
    assert compact["full_model_restore_verified"] is False
    assert compact["pixel_restore_verified"] is None
    assert compact["restore_values"] == {"exposure": 0}
    assert compact["after"]["values"] == compact["restore_values"]
    assert compact["after"]["observation_token"] == "token-candidate"
    assert compact["restore_observation"]["evidence"]["model_verified"] is False
    assert "raw_state" not in compact["restore_observation"]
    assert record == untouched
    assert compact_response(record, command="operations", action="get") == untouched


def test_old_restored_summary_uses_reconciled_baseline_without_rewriting_receipt():
    record = {"operation_id": "old-restore", "status": "restored", "after": observation(value=0.2),
              "reconciliation": {"outcome": "restored", "evidence": {
                  "scope": "control_readback", "observation": observation(value=0)}}}
    untouched = deepcopy(record)
    compact = compact_response(record, command="operations", action="reconcile")
    assert compact["after"]["values"]["exposure"] == 0
    assert compact["restore_values"] == {"exposure": 0}
    assert record == untouched and record["after"]["values"]["exposure"] == 0.2
    record["restore_observation"] = observation(value=0.1)
    compact = compact_response(record, command="operation_summary")
    assert compact["restore_values"] == {"exposure": 0}
    assert compact["after"]["values"] == {"exposure": 0}
    assert record["restore_observation"]["values"] == {"exposure": 0.1}


def test_incomplete_reconciliation_does_not_replace_known_restore_observation():
    record = {"operation_id": "restore", "status": "restored",
              "restore_observation": observation(value=0),
              "reconciliation": {"outcome": "restored", "evidence": {"observation": {
                  "target": observation()["target"], "values": {}}}}}
    assert compact_response(record, command="operation_summary")["restore_values"] == {"exposure": 0}


def test_unknown_reconciliation_does_not_invent_restored_values_or_pixel_proof():
    record = {"operation_id": "unknown", "status": "outcome_unknown", "after": observation(value=0.2),
              "reconciliation": {"outcome": "unknown", "evidence": {"observation": observation(value=0)}}}
    compact = compact_response(record, command="operation_summary")
    assert "restore_values" not in compact and "restore_observation" not in compact
    assert compact["after"]["values"]["exposure"] == 0.2
    assert "pixel_restore_verified" not in compact


def test_compact_pending_mixer_qualification_keeps_explicit_requested_intent():
    record = {"operation_id": "qualification", "status": "partial", "request": {
        "command": "adjust", "module": "color_mixer", "patch": {"action": "qualify"}}}
    compact = compact_response(record, command="operation_summary")
    assert compact["module"] == "color_mixer"
    assert compact["requested_patch"] == {"action": "qualify"}
    assert "requested_result_verified" not in compact


def test_viewer_delivery_has_separate_archive_and_does_not_claim_render_or_icc(tmp_path, monkeypatch):
    from PIL import Image
    from c1ai import mcp_server

    folder = tmp_path / "viewer-captures"
    folder.mkdir()
    image = folder / "capture.png"
    Image.new("RGB", (1280, 720), "grey").save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    monkeypatch.setattr(mcp_server, "state_directory", lambda: tmp_path)
    response = {"status": "completed", "result": {"source": "viewer_capture", "path": str(image),
                "image": {"sha256": digest}, "capture_verified": True, "render_verified": False,
                "limits": "Display only; render freshness not proven"}}
    delivered = mcp_server._preview_content(response)
    assert any(block.type == "image" for block in delivered.content)
    output = delivered.structuredContent
    proxy = output["image_delivery"]
    assert output["result"]["render_verified"] is False
    assert proxy["derived_from_source"] == "viewer_capture" and proxy["icc_preserved"] is False
    assert proxy["actual_capture_path"] == str(image) and "actual_render_path" not in proxy
    assert proxy["original_render_preserved"] is False
    response["result"]["source"] = "capture_one_export"
    refused = mcp_server._preview_content(response)
    assert refused.structuredContent["image_delivery"]["status"] == "unavailable"
    assert not any(block.type == "image" for block in refused.content)
