"""Native mixer helper integrated with the real Core and durable journal.

The synthetic provider deliberately scrambles swatch order; it never opens C1.
"""
from copy import deepcopy

import pytest

from c1ai import native_mixer as mixer
from c1ai.core import Core
from test_native_mixer import MixerBackend


class CoreMixer(MixerBackend):
    def capabilities_for(self, observation):
        return {"modules": {"color_mixer": {"writable": True}}}

    def observe_module(self, module):
        assert module == "color_mixer"
        return mixer.observe_mixer(self)

    def adjust_module(self, module, patch, observation):
        return mixer.apply_mixer(self, patch, observation, timeout=0)

    def reconcile(self, record):
        return mixer.reconcile_mixer(self, record)

    def recover(self, record):
        return mixer.recover_mixer(self, record, timeout=0)

    def action(self, name, function, intent=None):
        prior = self.action_name
        self.action_name = name
        try:
            return self.action_hook(name, function, intent)
        finally:
            self.action_name = prior


@pytest.fixture
def system(tmp_path, monkeypatch):
    backend = CoreMixer()
    core = Core(backend, tmp_path)
    monkeypatch.setattr(mixer, "_saved", backend.saved)
    monkeypatch.setattr("c1ai.native_modules.require_candidate", lambda *args: None)
    yield core, backend
    core.close()


def request(core, *, qualify=False):
    observed = core.dispatch({"command": "read", "module": "color_mixer"})
    assert observed["status"] == "ok", observed
    return {"command": "adjust", "module": "color_mixer", "request_id": "qualification" if qualify else "actual-edit",
            "patch": {"action": "qualify"} if qualify else {
                "mode": "basic", "color": "red", "hue": 2.5, "saturation": -3, "lightness": 1},
            "target": observed["target"], "observation_token": observed["observation_token"]}


def test_explicit_qualification_and_requested_edit_have_separate_durable_operations(system):
    core, backend = system
    baseline = deepcopy(backend.state)
    item = request(core, qualify=True)
    result = core.dispatch(item)
    assert result["status"] in {"completed", "control_confirmed"}, result
    probes = [a for a in result["actions"] if a["name"].endswith(".probe")]
    restores = [a for a in result["actions"] if a["name"].endswith(".restore")]
    assert len(probes) == len(restores) == 9
    ids = {a["action_id"] for a in result["actions"]}
    assert all(p["probe_action_id"] in ids and p["restore_action_id"] in ids
               for p in result["result"]["evidence"]["qualification_probes"])
    assert result["after"]["state"] == baseline
    assert result["result"]["evidence"]["scope"] == "mixer_runtime_binding_qualification"
    assert not any(a["name"].startswith("mixer.apply.") for a in result["actions"])
    before_calls = len(backend.calls)
    assert core.dispatch(item)["existing"] is True and len(backend.calls) == before_calls
    edited = core.dispatch(request(core))
    assert edited["status"] == "control_confirmed", edited
    assert edited["operation_id"] != result["operation_id"]
    assert not any(a["name"].startswith("mixer.qualify.") for a in edited["actions"])
    assert edited["after"]["state"]["colors"]["red"] == {"hue": 2.5, "saturation": -3.0,
        "lightness": mixer._float(0.1) * 10}


def test_unqualified_normal_adjust_never_probes_or_writes(system):
    core, backend = system
    baseline = deepcopy(backend.state)
    result = core.dispatch(request(core))
    assert result["status"] == "refused", result
    assert result["error"]["code"] == "MIXER_QUALIFICATION_REQUIRED"
    assert result["actions"] == [] and backend.state == baseline
    assert not any(name in {"mixer_select", "mixer_set"} for name, _ in backend.calls)


@pytest.mark.parametrize("defect", ["scope", "whole_matrix_baseline_verified", "baseline_ui_verified", "baseline"])
def test_qualification_cannot_claim_success_without_unchanged_baseline_and_exact_scope(system, defect):
    core, backend = system
    original = backend.adjust_module

    def corrupt(module, patch, observation):
        result = original(module, patch, observation)
        if defect == "baseline":
            result["after"]["state"]["colors"]["red"]["hue"] += 1
        else:
            result["evidence"][defect] = False
        return result

    backend.adjust_module = corrupt
    result = core.dispatch(request(core, qualify=True))
    assert result["status"] == "partial", result
    assert result["error"]["code"] == "qualification_unverified"
    assert any(a["name"].endswith(".probe") for a in result["actions"])


@pytest.mark.parametrize("lost_probe", [True, False])
def test_original_operation_recovers_one_probe_or_known_refused_restore_once(system, lost_probe):
    core, backend = system
    baseline = deepcopy(backend.state)
    backend.drop_probe = lost_probe
    backend.refuse_restore = not lost_probe
    pending = core.dispatch(request(core, qualify=True))
    assert pending["status"] in {"partial", "outcome_unknown"}
    backend.drop_probe = False
    action = {"command": "operations", "action": "recover", "operation_id": pending["operation_id"]}
    result = core.dispatch(action)
    assert result["status"] == "restored", result
    assert result["restore_scope"] == "qualification_probe_baseline"
    assert result["requested_result_verified"] is False and result["full_model_restore_verified"] is False
    assert backend.state == baseline
    assert len([a for a in result["actions"] if a["name"] == "recover.mixer.qualify.0.restore"]) == 1
    assert not any(a["name"].startswith("mixer.apply.") for a in result["actions"])
    before_calls = len(backend.calls)
    assert core.dispatch(action)["existing"] is True and len(backend.calls) == before_calls


def test_unknown_probe_restore_is_not_repeated_even_when_matrix_has_returned(system):
    core, backend = system
    baseline = deepcopy(backend.state)
    backend.drop_restore = True
    pending = core.dispatch(request(core, qualify=True))
    assert pending["status"] == "outcome_unknown" and backend.state == baseline
    backend.drop_restore = False
    original_actions = len(pending["actions"])
    result = core.dispatch({"command": "operations", "action": "recover", "operation_id": pending["operation_id"]})
    assert result["status"] == "outcome_unknown"
    assert len(result["actions"]) == original_actions
    assert not any(a["name"].startswith("recover.mixer") for a in result["actions"])
