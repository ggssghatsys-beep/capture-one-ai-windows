"""Real native balance adapter/Core/SQLite with a synthetic TUBE transport."""
from copy import deepcopy
import math

import pytest

from c1ai import native_modules as native
from c1ai.core import Core
from c1ai.tangent_transport import FIELDS
from c1ai.windows_backend import NativeError
from test_core import FakeBackend


class BalanceBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.target["instance"]["started_at"] = "456"
        self.values_native = dict.fromkeys(FIELDS, 0.0)
        self.transport_calls, self.receipts = [], {}
        self.read_count = 0
        self.fail_read_at = None
        self.fail_delta_at = None
        self.after_delta = None
        self.before_action = None
        self.corrupt_read = None
        self.corrupt_query = None

    def observe(self, fields, *, allow_layer=False):
        return super().observe(fields)

    def capabilities_for(self, observation):
        return {"modules": {"color_balance": {"writable": True}}}

    def observe_module(self, module):
        return native.observe_module(self, module)

    def adjust_module(self, module, patch, observation):
        return native.adjust_module(self, module, patch, observation)

    def reconcile(self, record):
        return native.reconcile_module(self, record)

    def action(self, name, func, intent=None):
        if self.before_action:
            self.before_action(self)
        return self.action_hook(name, func, intent)

    def transport(self, request, **kwargs):
        command = request["command"]
        self.transport_calls.append(deepcopy(request))
        if command == "read":
            self.read_count += 1
            if self.read_count == self.fail_read_at:
                raise TimeoutError("final native read lost")
            values = deepcopy(self.values_native)
            if self.corrupt_read:
                self.corrupt_read(values)
            return {"status": "ok", "values": values,
                    "protocol_freshness": "serialized_parameter_id_response_without_request_sequence"}
        if command == "query":
            receipt = deepcopy(self.receipts.get(request["request_id"], {"status": "not_found"}))
            if self.corrupt_query:
                self.corrupt_query(receipt)
            return receipt
        assert command == "delta"
        context = self.action_context
        action = self.core.journal.get(operation_id=context["operation_id"])["actions"][-1]
        assert action["action_id"] == request["action_id"] == request["request_id"]
        assert action["status"] == "dispatching"
        count = sum(item["command"] == "delta" for item in self.transport_calls)
        if self.fail_delta_at == count:
            return {"status": "not_dispatched", "dispatched": False}
        field = request["field"]
        before = self.values_native[field]
        assert math.isclose(before, request["expected_before"], abs_tol=1e-4)
        self.values_native[field] += request["delta"]
        receipt = {"status": "completed", "dispatched": True, "accepted": True,
                   "parameter_readback_verified": True, "operation_id": request["operation_id"],
                   "action_id": request["action_id"], "request_id": request["request_id"], "field": field,
                   "instance": deepcopy(request["expected_instance"]), "intent": deepcopy(request),
                   "delta": request["delta"], "before": {"value": before},
                   "after": {"value": self.values_native[field]}}
        self.receipts[request["request_id"]] = deepcopy(receipt)
        if self.after_delta:
            self.after_delta(self)
        return receipt


@pytest.fixture
def system(tmp_path, monkeypatch):
    backend = BalanceBackend()
    core = Core(backend, tmp_path)
    backend.core = core
    monkeypatch.setattr("c1ai.tangent_transport.call", backend.transport)
    monkeypatch.setattr("c1ai.windows_backend.process_started", lambda pid: "456")
    monkeypatch.setattr(native, "saved_settings", lambda target: {})
    yield core, backend
    core.close()


def request(core, patch=None, request_id="balance"):
    observed = core.dispatch({"command": "read", "module": "color_balance"})
    assert observed["status"] == "ok", observed
    return {"command": "adjust", "request_id": request_id, "module": "color_balance",
            "patch": patch or {"wheel": "master", "x": 1}, "target": observed["target"],
            "observation_token": observed["observation_token"]}


def deltas(backend):
    return [item for item in backend.transport_calls if item["command"] == "delta"]


def pending_final_read(core, backend):
    item = request(core)
    backend.fail_read_at = backend.read_count + 2  # Core preflight succeeds; adapter final read fails.
    result = core.dispatch(item)
    assert result["status"] == "partial", result
    backend.fail_read_at = None
    return result


def test_absolute_patch_is_preplanned_into_bounded_independent_native_receipts(system):
    core, backend = system
    item = request(core, {"wheel": "shadows", "x": 12.5, "y": -6})
    result = core.dispatch(item)
    assert result["status"] == "control_confirmed", result
    sent = deltas(backend)
    assert [(r["field"], r["expected_before"], r["delta"]) for r in sent] == [
        ("shadow_x", 0, 5), ("shadow_x", 5, 5), ("shadow_x", 10, 2.5),
        ("shadow_y", 0, -5), ("shadow_y", -5, -1)]
    assert len({a["action_id"] for a in result["actions"]}) == 5
    assert result["result"]["requested_result_verified"] is True and result["model_verified"] is False
    assert core.dispatch(item)["existing"] is True and len(deltas(backend)) == 5


def test_target_drift_before_delta_is_known_not_dispatched(system):
    core, backend = system
    item = request(core)
    backend.before_action = lambda backend: backend.target["variant"].update(id="other")
    result = core.dispatch(item)
    assert result["status"] == "refused" and result["actions"][0]["status"] == "not_dispatched"
    assert not deltas(backend)


def test_target_drift_after_delta_is_unknown_and_stops_remaining_steps(system):
    core, backend = system
    item = request(core, {"wheel": "master", "x": 12})
    backend.after_delta = lambda backend: backend.target["variant"].update(id="other")
    result = core.dispatch(item)
    assert result["status"] == "outcome_unknown" and len(deltas(backend)) == 1
    assert core.dispatch(item)["existing"] is True and len(deltas(backend)) == 1


def test_target_returning_after_post_dispatch_drift_is_not_enough_to_clear_unknown(system):
    core, backend = system
    item = request(core)
    backend.after_delta = lambda backend: backend.target["variant"].update(id="other")
    record = core.dispatch(item)
    assert record["status"] == "outcome_unknown"
    backend.after_delta = None
    backend.target = deepcopy(record["target"])
    # Parameter matches, but TUBE did not identify which photo received it.
    assert backend.values_native["master_x"] == 1
    result = core.dispatch({"command": "operations", "action": "reconcile", "operation_id": record["operation_id"]})
    assert result["status"] == "outcome_unknown"
    assert len(deltas(backend)) == 1


def test_mid_plan_refusal_never_repeats_or_runs_later_steps(system):
    core, backend = system
    item = request(core, {"wheel": "master", "x": 12})
    backend.fail_delta_at = 2
    result = core.dispatch(item)
    assert result["status"] == "partial"
    assert [a["status"] for a in result["actions"]] == ["completed", "not_dispatched"]
    assert backend.values_native["master_x"] == 5 and len(deltas(backend)) == 2
    reconciled = core.dispatch({"command": "operations", "action": "reconcile", "operation_id": result["operation_id"]})
    assert reconciled["status"] == "applied_not_restored"
    evidence = reconciled["reconciliation"]["evidence"]
    assert evidence["scope"] == "confirmed_native_parameter_prefix"
    assert evidence["requested_result_verified"] is False and evidence["intent_completed"] is False
    assert evidence["confirmed_steps"] == 1 and evidence["planned_steps"] == 3
    assert len(deltas(backend)) == 2


def test_unrelated_parameter_drift_does_not_become_success(system):
    core, backend = system
    item = request(core)
    backend.after_delta = lambda backend: backend.values_native.update(highlight_x=1)
    result = core.dispatch(item)
    assert result["status"] == "partial" and result["error"]["code"] == "NATIVE_COLOR_MISMATCH"
    assert len(deltas(backend)) == 1


@pytest.mark.parametrize("corrupt", [
    lambda values: values.clear(),
    lambda values: values.pop("master_x"),
    lambda values: values.update(master_x=float("nan")),
    lambda values: values.update(master_x=True),
    lambda values: values.update(master_x=51),
    lambda values: values.update(extra=0),
])
def test_native_read_requires_exact_eleven_finite_in_range_fields(system, corrupt):
    core, backend = system
    backend.corrupt_read = corrupt
    result = core.dispatch({"command": "read", "module": "color_balance"})
    assert result["status"] == "refused" and result["error"]["code"] == "NATIVE_COLOR_UNAVAILABLE"
    assert "observation_token" not in result and not deltas(backend)


def test_completed_transport_receipt_and_fresh_full_values_resolve_lost_final_read(system):
    core, backend = system
    record = pending_final_read(core, backend)
    result = core.dispatch({"command": "operations", "action": "reconcile", "operation_id": record["operation_id"]})
    assert result["status"] == "applied_not_restored", result
    assert result["after"]["state"]["values"]["master_x"] == 1
    assert len(deltas(backend)) == 1
    assert any(item["command"] == "query" for item in backend.transport_calls)
    assert result["reconciliation"]["evidence"]["native_parameter_readback_verified"] is True


@pytest.mark.parametrize("corrupt", [
    lambda r: r.update(action_id="foreign"), lambda r: r.update(operation_id="foreign"),
    lambda r: r.update(request_id="foreign"), lambda r: r.update(field="shadow_x"),
    lambda r: r["instance"].update(started_at="other"), lambda r: r.update(delta=2),
    lambda r: r["before"].update(value=2), lambda r: r["after"].update(value=2),
    lambda r: r.update(status="outcome_unknown"), lambda r: r.update(parameter_readback_verified=False),
])
def test_reconciliation_rejects_foreign_or_unconfirmed_transport_evidence(system, corrupt):
    core, backend = system
    record = pending_final_read(core, backend)
    backend.corrupt_query = corrupt
    result = core.dispatch({"command": "operations", "action": "reconcile", "operation_id": record["operation_id"]})
    assert result["status"] == "outcome_unknown"
    assert len(deltas(backend)) == 1
