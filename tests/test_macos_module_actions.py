"""Small guards for module actions; real C1 runs qualify the native setters."""
from copy import deepcopy
import pytest
from c1ai.contracts import Refused
from c1ai.macos_backend import MacOSBackend


def fixture(tmp_path):
    backend = MacOSBackend(tmp_path)
    backend._qualified = lambda _: True
    backend.action_hook = lambda name, fn, intent: fn()
    observation = {"target": {"instance": {"pid": 10}, "document": {"id": "test"},
                              "layer": "Background"},
                   "native_context": {"processing_engine": 1600},
                   "values": dict(exposure=1, contrast=2, brightness=3, saturation=4)}
    return backend, observation


def test_paste_rejects_engine_mismatch_before_write(tmp_path):
    backend, before = fixture(tmp_path)
    source = deepcopy(before)
    source["native_context"]["processing_engine"] = 1500
    backend.set_values = lambda *args: pytest.fail("must not dispatch")
    with pytest.raises(Refused, match="processing engine"):
        backend.apply_module_action("exposure", {"action": "paste", "source_observation_token": "source"}, before, source)


def test_reset_never_claims_custom_defaults_and_checks_readback(tmp_path):
    backend, before = fixture(tmp_path)
    def write(values, observation):
        assert values == dict.fromkeys(before["values"], 0)
        return {"observation": {**observation, "values": {**values, "brightness": 1}}}
    backend.set_values = write
    with pytest.raises(Refused, match="differs"):
        backend.apply_module_action("exposure", {"action": "reset"}, before, None)
    with pytest.raises(Refused, match="White Balance"):
        backend.apply_module_action("white_balance", {"action": "reset"}, before, None)


def test_rgb_change_rejects_unsafe_intermediate_range_before_dispatch(tmp_path):
    backend, before = fixture(tmp_path)
    before["state"] = {"advanced": {"ranges": [{"rgb": [100, 50, 50],
        "saturation_low": -40, "saturation_high": 40, "hue_low": -20, "hue_high": 20}]}}
    backend.action_hook = lambda *args: pytest.fail("must reject before native dispatch")
    with pytest.raises(Refused, match="Narrow the existing"):
        backend.adjust_module("color_mixer", {"mode": "advanced", "index": 0, "rgb": [100, 90, 90],
            "saturation_low": -5, "saturation_high": 50}, before)


@pytest.mark.parametrize("first,second", [({"action": "reset"}, {"mode": "advanced", "action": "add"}),
                                          ({"mode": "advanced", "action": "remove"}, {"action": "reset"}),
                                          ({"action": "paste"}, {"action": "reset"}),
                                          ({"action": "reset"}, {"action": "paste"})])
def test_unknown_mixer_structure_blocks_reset_and_range_replay(tmp_path, first, second):
    from c1ai.journal import Journal
    _, before = fixture(tmp_path)
    before["target"].update(image={"path": "/test/raw.nef"}, variant={"id": "100"})
    before["target"]["instance"]["platform"] = "macos"
    before["target"]["document"]["path"] = "/test/session.cosessiondb"
    before["evidence"] = {"source": "c1_applescript", "model_verified": True}
    journal = Journal(tmp_path)
    try:
        record, _ = journal.begin({"command": "adjust", "module": "color_mixer", "patch": first,
                                   "request_id": "first"}, before["target"], before)
        journal.update(record["operation_id"], status="outcome_unknown")
        with pytest.raises(Refused, match="unfinished structural"):
            journal.begin({"command": "adjust", "module": "color_mixer", "patch": second,
                           "request_id": "second"}, before["target"], before)
        # Inspection may need a different selection; this does not resolve or
        # replay the uncertain content change.
        selection, _ = journal.begin({"command": "adjust", "module": "layers",
            "patch": {"action": "select"}, "request_id": "inspect-selection"}, before["target"], before)
        assert selection["status"] == "dispatching"
        child_selection, _ = journal.begin({"command": "adjust", "module": "masks",
            "patch": {"action": "select_submask", "index": 0}, "request_id": "inspect-submask"}, before["target"], before)
        assert child_selection["status"] == "dispatching"
        assert journal.get(operation_id=record["operation_id"])["status"] == "outcome_unknown"
    finally:
        journal.close()


def test_unfinished_mask_blocks_selection_that_could_redirect_ai(tmp_path):
    from c1ai.journal import Journal
    _, before = fixture(tmp_path)
    before["target"].update(image={"path": "/test/raw.nef"}, variant={"id": "100"})
    before["target"]["instance"]["platform"] = "macos"
    before["target"]["document"]["path"] = "/test/session.cosessiondb"
    before["evidence"] = {"source": "c1_applescript", "model_verified": True}
    journal = Journal(tmp_path)
    try:
        record, _ = journal.begin({"command": "adjust", "module": "masks",
            "patch": {"action": "combine_ai", "kind": "subject", "mode": "add"},
            "request_id": "pending-ai"}, before["target"], before)
        journal.update(record["operation_id"], status="outcome_unknown")
        for module, action in [("layers", "select"), ("masks", "select_submask")]:
            with pytest.raises(Refused, match="unfinished mask"):
                journal.begin({"command": "adjust", "module": module,
                    "patch": {"action": action}, "request_id": module}, before["target"], before)
        # A user may already have selected another photo manually; the
        # unfinished host task still belongs to this instance and Session.
        other = deepcopy(before)
        other["target"]["variant"]["id"] = "101"
        other["target"]["image"]["path"] = "/test/another.nef"
        for action in ["select", "next", "previous"]:
            with pytest.raises(Refused, match="unfinished mask"):
                journal.begin({"command": "images", "action": action,
                    "request_id": action}, other["target"], other)
    finally:
        journal.close()


def test_local_paste_rejects_unset_source_before_dispatch(tmp_path):
    backend, before = fixture(tmp_path)
    source = deepcopy(before)
    source["target"]["layer"] = {"native_id": "local"}
    backend._local = lambda observation: isinstance(observation["target"]["layer"], dict)
    source["values"]["contrast"] = None
    backend.set_values = lambda *args: pytest.fail("unset source must not dispatch")
    with pytest.raises(Refused, match="unset local values"):
        backend.apply_module_action("exposure", {"action": "paste", "source_observation_token": "source"}, before, source)


def test_numeric_module_action_preserves_unrequested_null_fields(tmp_path):
    backend, before = fixture(tmp_path)
    before["values"]["shadow_recovery"] = None
    backend.set_values = lambda values, observation: {"observation": {
        **observation, "values": {**observation["values"], **values}}}
    result = backend.apply_module_action("exposure", {"action": "reset"}, before, None)
    assert result["after"]["values"]["shadow_recovery"] is None
