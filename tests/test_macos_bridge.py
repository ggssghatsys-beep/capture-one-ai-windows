"""Failure classification and the native selection marker, without a fake C1."""
from copy import deepcopy
import pytest

from c1ai.contracts import Refused
from c1ai.macos_bridge import NativeBridge
from c1ai.macos_mask_actions import verify_gradient_selection_point


def test_native_file_channel_busy_is_short_read_retry_without_dispatch(tmp_path, monkeypatch):
    import fcntl
    from c1ai.analysis_service import failure_details
    bridge = NativeBridge(tmp_path)
    bridge.root.mkdir(parents=True)
    monkeypatch.setattr(bridge, "ensure", lambda target: None)
    with (bridge.root / "channel.lock").open("w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Refused) as failure:
            bridge.call({"instance": {"pid": 123}}, "inspect")
    assert failure.value.dispatched is False
    assert failure_details(failure.value)["retry_after_ms"] == 50
    assert not (bridge.root / "request.json").exists()


def test_concurrent_analysis_never_falls_back_after_bridge_disabled(tmp_path, monkeypatch):
    from c1ai.macos_backend import MacOSBackend
    backend = MacOSBackend(tmp_path)
    monkeypatch.setattr("c1ai.macos_bridge.configured", lambda root: False)
    backend.observe = lambda fields: pytest.fail("Display must not enter official reads")
    with pytest.raises(Refused, match="Enable the Mac native bridge"):
        backend.analysis_observe()
    class DisabledBridge:
        def render(self, request, observation):
            raise Refused("native_bridge_disabled", "disabled")
    backend._bridge = DisabledBridge()
    # This observation was captured before the configuration changed.
    with pytest.raises(Refused, match="disabled"):
        backend.preview({"kind": "native"}, {"native_context": {"analysis_native": True}})


def test_native_uniform_gray_invert_uses_exact_bytes():
    from c1ai.macos_backend import MacOSBackend
    backend = object.__new__(MacOSBackend)
    layer = {"native_id": "mask", "name": "Mask", "is_image_layer": False}
    background = {"native_id": "Background", "name": "Image", "is_image_layer": True}
    target = {"instance": {}, "document": {}, "image": {}, "variant": {"id": "1"}, "layer": layer}
    saved = {1: {"background": {"ZEXPOSURE": 0}, "local:mask": {"ZNAME": "Mask", "ZEXPOSURE": 0}}}
    backend._mask_ax_parameters = lambda observed: deepcopy(saved)
    before = {"target": target, "native_context": {}, "state": {
        "layers": [background, layer], "selected_layer": layer,
        "details": [{"kind": "image"}, {"kind": "adjustment", "luma_range": None, "values": {"exposure": 0}}],
        "masks": [{"uuid": "mask", "mask_type": 0, "structure": {}, "content_sha256": "old",
                   "constant": {"verified": True, "density": 85 / 255, "value_8bit": 85}}]}}
    before["native_context"]["mask_ax_parameters"] = backend._mask_ax_parameter_snapshot(before, saved_layers=saved)
    after = deepcopy(before)
    after["state"]["masks"][0].update(content_sha256="inverted",
        constant={"verified": True, "density": 170 / 255, "value_8bit": 170})
    assert backend._native_mask_proof(before, after, {"action": "invert"}, {"state": "committed"})["model_verified"]
    after["state"]["masks"][0]["constant"].update(density=169 / 255, value_8bit=169)
    with pytest.raises(Refused):
        backend._native_mask_proof(before, after, {"action": "invert"}, {"state": "committed"})


def test_gradient_setup_failure_is_not_a_dispatched_write(tmp_path, monkeypatch):
    bridge = NativeBridge(tmp_path)
    def unavailable(target):
        raise Refused("bridge_restart_required", "new build needs a normal restart")
    monkeypatch.setattr(bridge, "ensure", unavailable)
    with pytest.raises(Refused) as failure:
        bridge.create_gradient({}, {}, "original-id", "mask", "linear_gradient", [])
    assert failure.value.dispatched is False


def test_gradient_poll_failure_cannot_erase_dispatch(tmp_path, monkeypatch):
    bridge = NativeBridge(tmp_path)
    calls = []
    def call(public_target, action, **fields):
        calls.append(action)
        if action == "gradient_create":
            return {"state": "pending", "dispatched": True}
        error = Refused("bridge_response_pending", "reply unavailable")
        error.dispatched = False  # Only the read-only poll itself wasn't a write.
        raise error
    monkeypatch.setattr(bridge, "call", call)
    monkeypatch.setattr("c1ai.macos_bridge.time.sleep", lambda seconds: None)
    with pytest.raises(Refused) as failure:
        bridge.create_gradient({}, {}, "original-id", "mask", "linear_gradient", [])
    assert failure.value.dispatched is True
    assert calls == ["gradient_create", "gradient_status"]


def test_cancelled_gradient_retains_receipt_for_unchanged_state_check(tmp_path, monkeypatch):
    bridge = NativeBridge(tmp_path)
    def unexpected_release(*args, **kwargs):
        pytest.fail("Cancelled receipt must survive until reconciliation")
    monkeypatch.setattr(bridge, "call", unexpected_release)
    receipt = {"state": "cancelled", "dispatched": True, "temporal_open": False}
    with pytest.raises(Refused) as failure:
        bridge._wait_gradient({}, "original-id", receipt)
    assert failure.value.dispatched is True
    assert failure.value.details == receipt


def test_mask_commit_waits_for_saved_state_proof_before_release(tmp_path, monkeypatch):
    bridge = NativeBridge(tmp_path)
    calls = []
    receipt = {"state": "committed", "dispatched": True, "temporal_open": False}
    def call(public_target, action, **fields):
        calls.append(action)
        assert action == "mask_apply"
        return receipt
    monkeypatch.setattr(bridge, "call", call)
    assert bridge.apply_mask({}, {}, "original-id", "create_subject") == receipt
    assert calls == ["mask_apply"]


@pytest.mark.parametrize("release_fails", [False, True])
def test_prewrite_rejection_releases_or_remains_reconcilable(tmp_path, monkeypatch, release_fails):
    bridge = NativeBridge(tmp_path)
    receipt = {"state": "rejected", "dispatched": False, "temporal_open": False}
    calls = []
    def call(public_target, action, **fields):
        calls.append(action)
        if action == "gradient_create":
            return receipt
        assert action == "gradient_release"
        if release_fails:
            raise OSError("release reply unavailable")
        return receipt
    monkeypatch.setattr(bridge, "call", call)
    monkeypatch.setattr("c1ai.macos_bridge.time.sleep", lambda seconds: None)
    with pytest.raises((Refused, OSError)) as failure:
        bridge.create_gradient({}, {}, "original-id", "mask", "linear_gradient", [])
    assert calls[1:] and set(calls[1:]) == {"gradient_release"}
    assert failure.value.dispatched is release_fails
    assert failure.value.details == receipt


def test_selection_marker_uses_native_midpoint_and_rejects_wrong_position():
    parameters = [796.7999877929688, 664., 3187.199951171875, 1992., .3499999940395355]
    verify_gradient_selection_point("1633.439941;1128.800049", "linear_gradient", parameters)
    with pytest.raises(Refused):
        verify_gradient_selection_point("1992;1328", "linear_gradient", parameters)
    verify_gradient_selection_point("100;200", "radial_gradient", [100, 200, 50, 0, 0, 80, .5, 0])
    with pytest.raises(Refused):
        verify_gradient_selection_point("NaN;200", "radial_gradient", [100, 200, 50, 0, 0, 80, .5, 0])
