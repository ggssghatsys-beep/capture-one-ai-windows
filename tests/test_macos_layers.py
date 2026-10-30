"""Layer adapter receipts and nullable local values; no Capture One calls."""
from copy import deepcopy

import pytest

from c1ai.contracts import Refused
from c1ai.core import Core
from c1ai.macos_backend import MacOSBackend
from c1ai.journal import Journal


def test_layer_tokens_bind_hidden_contents_without_changing_windows(tmp_path):
    journal = Journal(tmp_path)
    observation = {"target": {"instance": {"platform": "macos"}}, "values": {"layers": {}},
                   "module": "layers", "native_context": {"duplicate_snapshot": "original"}}
    original = journal.observe(observation)
    observation["native_context"]["duplicate_snapshot"] = "changed hidden curve or mask"
    assert journal.observe(observation) != original
    assert journal.observation(original)["native_context"]["duplicate_snapshot"] == "original"
    observation["native_context"] = {}
    with pytest.raises(Refused, match="Read layers again"):
        journal.observation(journal.observe(observation))
    observation["target"]["instance"]["platform"] = "windows"
    windows = journal.observe(observation)
    observation["native_context"]["duplicate_snapshot"] = "ignored Mac-only detail"
    assert journal.observe(observation) == windows


def test_background_only_observation_binds_sibling_mask_file(tmp_path, monkeypatch):
    """A RAW's other variants may own masks although this candidate has none."""
    import hashlib
    raw = tmp_path / "photo.raw"
    mask_file = tmp_path / "CaptureOne" / "Settings1680" / "photo.raw.comask"
    mask_file.parent.mkdir(parents=True)
    mask_file.write_bytes(b"another variant's mask bytes")
    image = {"native_id": "Background", "name": "Image", "is_image_layer": True}
    state = {"layers": [image], "details": [{}], "selected_layer": image}
    payload = {"observation": {"target": {"layer": "Background", "image": {"path": str(raw)}},
                               "native_context": {"layer_state": state}, "evidence": {}},
               "result": {"state": state}}
    backend = MacOSBackend(tmp_path)
    backend._native = lambda *args, **kwargs: deepcopy(payload)
    backend._mask_ax_parameters = lambda observed: {}
    backend._mask_ax_parameter_snapshot = lambda observed, **kwargs: "parameters"
    backend._bridge_observation = lambda observed: observed
    def no_ui(*args, **kwargs):
        raise Refused("ui_disabled", "No UI calls in this parser regression")
    monkeypatch.setattr("c1ai.macos_ax.run_ax", no_ui)
    observed = backend._mask_observation(deepcopy(payload))
    assert observed["state"]["masks"] == []
    assert observed["native_context"]["mask_file_snapshot"] == {
        str(mask_file): hashlib.sha256(mask_file.read_bytes()).hexdigest()}


def test_native_layer_result_preserves_public_module_for_core_reconciliation(tmp_path):
    image = {"native_id": "Background", "name": "Image", "is_image_layer": True}
    layer = {"native_id": "a", "name": "Layer", "is_image_layer": False}
    state = {"layers": [image, layer], "details": [{}, {}], "selected_layer": layer}
    target = {"instance": {"platform": "macos"}, "document": {"path": "/session"},
              "image": {"path": "/photo"}, "variant": {"id": "1"}, "layer": layer}
    public = {"module": "layers", "target": target, "state": state, "native_context": {
        "layer_action_baseline": {"state": {**state, "masks": []}}, "duplicate_snapshot": "complete-saved"}}
    mask_read = {"module": "masks", "target": target, "state": {**state, "masks": []}}
    backend = MacOSBackend(tmp_path)
    backend.observe_module = lambda module: deepcopy(public)
    result = backend._layer_action_result(mask_read, "move", "complete-saved")
    record = {"target": target, "before": public, "request": {"module": "layers"}}
    assert Core._validated_outcome(record, {"target": target, "outcome": "not_applied",
                                           "evidence": {"observation": result}}) == "not_applied"
    with pytest.raises(Refused, match="Complete saved layer state changed"):
        backend._layer_action_result(mask_read, "move", "different-saved")


@pytest.mark.parametrize("selection_changed", [False, True])
def test_cancelled_native_ai_combination_releases_only_unchanged_state(tmp_path, selection_changed):
    layer = {"native_id": "a", "name": "Layer", "is_image_layer": False}
    target = {"instance": {}, "document": {}, "image": {}, "variant": {}, "layer": layer}
    native = {"layer_uuid": "a", "submask_index": -1}
    before = {"module": "masks", "target": target, "native_context": {
        "bridge_target": native, "mask_file_snapshot": {"private.comask": "hash"}, "mask_ax_parameters": "parameters"},
        "state": {"layers": [layer], "selected_layer": layer, "masks": [{"uuid": "a", "structure": {
            "mask_type": 0, "composition_raw": [1, 1.0, 0, 0]}}]}}
    patch = {"action": "combine_ai", "kind": "background", "mode": "subtract"}
    backend = MacOSBackend(tmp_path)
    after = deepcopy(before)
    if selection_changed:
        after["native_context"]["bridge_target"]["submask_index"] = 0
    backend.observe_module = lambda module: deepcopy(after)
    backend._mask_ax_parameter_snapshot = lambda observed: "parameters"
    receipt = {"operation_id": "original", "kind": "combine_ai", "target": native,
               "state": "cancelled", "dispatched": True, "callback_received": True, "temporal_open": False,
               "arguments": backend._layer_action_arguments(before, patch)}
    calls = []
    class Bridge:
        def inspect(self, target):
            return {"mask_operation_present": True}
        def call(self, target, action, **fields):
            calls.append(action)
            assert fields["operation_id"] == "original"
            return receipt
    backend.native_bridge = lambda: Bridge()
    result = backend._reconcile_native_layer_action({"operation_id": "original", "before": before, "request": {"patch": patch}})
    assert result["outcome"] == ("unknown" if selection_changed else "not_applied")
    assert calls == (["mask_status"] if selection_changed else ["mask_status", "mask_release"])


def test_duplicate_requires_complete_source_and_preserves_unrelated_content():
    source = {"native_id": "aa", "name": "source", "is_image_layer": False}
    copied = {**source, "native_id": "bb", "name": "source copy"}
    image = {"native_id": "Background", "name": "Image", "is_image_layer": True}
    detail = {"name": "source", "values": {"exposure": .35}}
    target = {"instance": {}, "document": {}, "image": {}, "variant": {"id": "1"}}
    before = {"target": target, "state": {"layers": [image, source], "selected_layer": source,
              "details": [{}, detail], "masks": [{"uuid": "aa", "content_sha256": "gradient"}]}}
    after = deepcopy(before)
    after["state"].update(layers=[image, source, copied], selected_layer=copied,
        details=[{}, detail, {**detail, "name": copied["name"]}],
        masks=[*before["state"]["masks"], {"uuid": "bb", "content_sha256": "gradient"}])
    row = {"ZNAME": "source", "ZMASKUUID": "aa", "ZINDEX": 0,
           "ZCOLORCORRECTIONS": b"complete hidden Skin Tone", "ZMETADATA": {"ZLAYER": "local:aa"}}
    saved = {"layers": {1: {"background": {"ZEXPOSURE": 0}, "local:aa": row}}}
    actual = deepcopy(saved)
    actual["layers"][1]["local:bb"] = {**row, "ZNAME": copied["name"], "ZMASKUUID": "bb",
                                      "ZINDEX": 1, "ZMETADATA": {"ZLAYER": "local:bb"}}
    MacOSBackend._verify_layer_duplicate(before, after, saved, actual)
    for role, field, value in [("local:bb", "ZCOLORCORRECTIONS", b"lost Skin"),
                                ("background", "ZEXPOSURE", 1)]:
        changed = deepcopy(actual)
        changed["layers"][1][role][field] = value
        with pytest.raises(Refused):
            MacOSBackend._verify_layer_duplicate(before, after, saved, changed)
    after["state"]["masks"][-1]["content_sha256"] = "different gradient"
    with pytest.raises(Refused):
        MacOSBackend._verify_layer_duplicate(before, after, saved, actual)


def test_mac_ai_combination_allows_only_same_parent_uuid_rename():
    background = {"runtime_id": [1, 2, 0], "native_id": "Background", "name": "Image", "is_image_layer": True}
    parent = {"runtime_id": [1, 2, 1, 10], "native_id": "mask", "name": "Subject", "is_image_layer": False,
              "identity_source": "session_mask_uuid_readonly"}
    old = [background, parent]
    target = {"instance": {"platform": "macos"}, "document": {}, "image": {}, "variant": {}, "layer": parent}
    before = {"target": target, "state": {"layers": old, "selected_layer": parent}}
    changed = {**parent, "name": "Mask 1"}
    new = [background, changed]
    after = {"target": {**target, "layer": changed}, "state": {"layers": new, "selected_layer": changed,
             "details": [{"name": "Image"}, {"name": "Mask 1"}]}}
    record = {"before": before, "target": target,
              "request": {"module": "masks", "patch": {"action": "combine_ai", "mode": "subtract", "kind": "background"}}}
    transition = {"before": target, "after": after["target"], "evidence": {
        "before_layers": old, "after_layers": new, "selected_layer": changed, "selected_parent_rename": "native_combine_ai"}}
    Core._structured_target(record, after, transition)
    changed["native_id"] = "different-mask"
    with pytest.raises(Refused, match="same selected parent UUID"):
        Core._structured_target(record, after, transition)


def test_unset_local_value_is_not_zero_and_does_not_change_windows_matching():
    assert Core._matches(None, None, {"nullable": True})
    assert not Core._matches(None, None, {})
    assert not Core._matches(None, 0, {"nullable": True})
    assert not Core._matches(0, None, {"nullable": True})
    assert Core._matches(0.6, 0.6, {"nullable": True, "tolerance": 0.0001})


@pytest.mark.parametrize("action", ["select", "rename"])
def test_layer_receipt_verifies_result_and_retains_native_identity(tmp_path, action):
    background = {"runtime_id": [1, 2, 3], "name": "Background", "is_image_layer": True}
    first = {"runtime_id": [1, 2, 4], "name": "same", "is_image_layer": False, "native_id": "first"}
    second = {"runtime_id": [1, 2, 5], "name": "same", "is_image_layer": False, "native_id": "second"}
    state = {"layers": [background, first, second], "selected_layer": first}
    target = {"instance": {"pid": 1}, "document": {"path": "/test/session"},
              "image": {"id": "10"}, "variant": {"id": "2"}, "layer": first}
    before = {"target": target, "module": "layers", "state": state, "values": {"layers": state}}
    patch = {"action": action, "layer_ref": second if action == "select" else first}
    after_state = deepcopy(state)
    if action == "select":
        after_state["selected_layer"] = after_state["layers"][2]
    else:
        patch["name"] = "renamed"
        after_state["layers"][1]["name"] = "renamed"
        after_state["selected_layer"] = after_state["layers"][1]
    backend = MacOSBackend(tmp_path)
    backend._qualified = lambda _: True
    calls = []
    backend.action_hook = lambda name, fn, intent: (calls.append(name), fn())[1]
    backend._native = lambda *args, **kwargs: {
        "observation": {"target": {**target, "layer": after_state["selected_layer"]}, "evidence": {}},
        "result": {"state": after_state}}
    result = backend.adjust_module("layers", patch, before)
    assert calls == ["layers." + action]
    assert result["requested_result_verified"] is True
    record = {"before": before, "target": target, "request": {"module": "layers", "patch": patch}}
    Core._structured_target(record, result["after"], result["target_transition"])
    # A recreated same-name layer must not inherit the old reference.
    replaced = deepcopy(result)
    replaced["after"]["state"]["layers"][1]["runtime_id"][-1] = 99
    with pytest.raises(Refused):
        Core._structured_target(record, replaced["after"], replaced["target_transition"])

@pytest.mark.parametrize("failure", ["stale", "io", "poll_target", "poll_value"])
def test_mask_read_guards_never_rebind_or_claim_dispatch(tmp_path, monkeypatch, failure):
    """Read-only failures cannot lock edits; delayed mask saves cannot rebind a write."""
    background = {"runtime_id": [1, 2, 0], "name": "Image", "is_image_layer": True, "native_id": "Background"}
    local = {"runtime_id": [1, 2, 1, 3], "name": "A", "is_image_layer": False, "native_id": "a"}
    state = {"layers": [background, local], "selected_layer": local, "details": [{}, {"exposure": 0}]}
    mask = {"uuid": "a", "content_sha256": "empty", "constant": {"verified": True, "density": 0}}
    target = {"instance": {"pid": 1}, "document": {}, "image": {}, "variant": {}, "layer": local}
    before = {"target": target, "module": "masks", "state": {**state, "masks": [mask]}}
    drift = deepcopy(before)
    drift["state"]["masks"][0].update(content_sha256="full", constant={"verified": True, "density": 1})
    if failure == "poll_target":
        drift["target"]["layer"] = background
        drift["state"]["selected_layer"] = background
    if failure == "poll_value":
        drift["state"]["details"][1]["exposure"] = 3
    backend = MacOSBackend(tmp_path)
    backend._qualified = lambda _: True
    backend.action_hook = lambda name, fn, intent: fn()
    calls = []
    def native(*args, **kwargs):
        calls.append(args[0])
        return {"observation": {"target": target, "evidence": {}}, "result": {"state": state}}
    backend._native = native
    backend._mask_observation = lambda payload: deepcopy(before)
    observations = iter([deepcopy(before), drift])
    def observe(module):
        if failure == "io":
            raise OSError("unavailable file")
        return drift if failure == "stale" else next(observations)
    backend.observe_module = observe
    monkeypatch.setattr("c1ai.macos_backend.time.sleep", lambda _: None)
    with pytest.raises((Refused, OSError)) as error:
        backend.adjust_module("masks", {"action": "fill", "density": 1}, before)
    if failure in {"stale", "io"}:
        assert not calls
        assert error.value.dispatched is False
    else:
        assert calls == ["masks_adjust"]
        assert error.value.code == "target_changed"

def test_mask_copy_binds_source_and_destination_without_fabricating_object_id():
    background = {"runtime_id": [1, 2, 0], "name": "Image", "is_image_layer": True, "native_id": "Background"}
    def ref(n, name):
        return {"runtime_id": [1, 2, 1, n], "name": name, "is_image_layer": False,
                "native_id": str(n), "variant_uuid": "v", "identity_source": "session_mask_uuid_readonly"}
    dst, src, new = ref(3, "same"), ref(4, "same"), ref(5, "same")
    details = [{}, {"kind": "adjustment", "luma_range": None, "values": {"exposure": 0}},
               {"kind": "subject mask", "luma_range": None, "values": {"exposure": .6}}]
    target = {"instance": {"platform": "macos", "pid": 1}, "document": {}, "image": {}, "variant": {}, "layer": dst}
    before = {"target": target, "state": {"layers": [background, dst, src], "selected_layer": dst,
              "details": details, "masks": [{"uuid": "3", "content_sha256": "empty"}, {"uuid": "4", "content_sha256": "subject"}]}}
    after = deepcopy(before)
    after["target"]["layer"] = new
    after["state"]["layers"][1] = new
    after["state"]["selected_layer"] = new
    after["state"]["details"][1]["kind"] = "subject mask"
    after["state"]["masks"][0] = {"uuid": "5", "content_sha256": "subject"}
    patch = {"action": "copy", "source_layer": src}
    assert MacOSBackend._verified_mask_copy(before, after, patch)
    transition = {"before": target, "after": after["target"], "evidence": {
        "copy_identity": "native_mask_uuid_replaced_by_copy", "before_layers": before["state"]["layers"],
        "after_layers": after["state"]["layers"], "selected_layer": new}}
    record = {"before": before, "target": target, "request": {"module": "masks", "patch": patch}}
    Core._structured_target(record, after, transition)
    drift = deepcopy(after)
    drift["state"]["details"][1]["values"]["exposure"] = .6
    assert not MacOSBackend._verified_mask_copy(before, drift, patch)
    drift = deepcopy(after)
    drift["state"]["masks"][1]["content_sha256"] = "changed_source"
    assert not MacOSBackend._verified_mask_copy(before, drift, patch)
    # Windows cannot use the Mac replacement proof instead of a real object ID.
    target["instance"]["platform"] = "windows"
    after["target"]["instance"]["platform"] = "windows"
    with pytest.raises(Refused):
        Core._structured_target(record, after, transition)


def test_layer_move_changes_only_requested_order_and_saved_indices():
    image = {"native_id": "Background", "name": "Image", "is_image_layer": True}
    first = {"native_id": "synthetic-a", "name": "A", "is_image_layer": False}
    second = {"native_id": "synthetic-b", "name": "B", "is_image_layer": False}
    target = {"instance": {}, "document": {}, "image": {}, "variant": {"id": "1"}, "layer": second}
    before = {"target": target, "state": {
        "layers": [image, first, second], "selected_layer": second,
        "details": [{}, {"values": {"exposure": .25}}, {"values": {"exposure": -.5}}],
        "masks": [{"uuid": first["native_id"], "content_sha256": "mask-a"},
                  {"uuid": second["native_id"], "content_sha256": "mask-b"}]}}
    expected = [image, second, first]
    after = deepcopy(before)
    after["state"]["layers"] = expected
    after["state"]["details"] = [before["state"]["details"][i] for i in (0, 2, 1)]
    saved_before = {"layers": {1: {
        "background": {"ZEXPOSURE": 0},
        "local:synthetic-a": {"ZINDEX": 0, "ZEXPOSURE": .25, "ZCOLORCORRECTIONS": b"hidden-a"},
        "local:synthetic-b": {"ZINDEX": 1, "ZEXPOSURE": -.5, "ZCOLORCORRECTIONS": b"hidden-b"}}},
        "masks": {"synthetic-mask-file": b"complete-mask-bytes"}}
    saved_after = deepcopy(saved_before)
    saved_after["layers"][1]["local:synthetic-a"]["ZINDEX"] = 1
    saved_after["layers"][1]["local:synthetic-b"]["ZINDEX"] = 0
    MacOSBackend._verify_layer_move(before, after, saved_before, saved_after, expected)

    for failure in ("order", "selection", "parameter", "mask", "hidden_parameter", "mask_bytes", "index"):
        changed, saved = deepcopy((after, saved_after))
        if failure == "order":
            changed["state"]["layers"] = before["state"]["layers"]
        elif failure == "selection":
            changed["state"]["selected_layer"] = first
        elif failure == "parameter":
            changed["state"]["details"][1]["values"]["exposure"] = 2
        elif failure == "mask":
            changed["state"]["masks"][0]["content_sha256"] = "changed-mask"
        elif failure == "hidden_parameter":
            saved["layers"][1]["local:synthetic-a"]["ZCOLORCORRECTIONS"] = b"lost-hidden-settings"
        elif failure == "mask_bytes":
            saved["masks"]["synthetic-mask-file"] = b"changed-mask-bytes"
        else:
            saved["layers"][1]["local:synthetic-a"]["ZINDEX"] = 0
        with pytest.raises(Refused):
            MacOSBackend._verify_layer_move(before, changed, saved_before, saved, expected)
