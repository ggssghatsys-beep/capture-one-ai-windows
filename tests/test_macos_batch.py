"""Isolated batch safety checks; no Capture One or production owner is used."""
from copy import deepcopy

from c1ai.core import Core
from c1ai.formatting import compact_response


class BatchBackend:
    batch_supported = True
    action_hook = None

    def __init__(self, fail_after_write=None):
        self.selected = 1
        self.values = {1: 0.0, 2: 0.0, 3: 0.0}
        self.writes = []
        self.selections = []
        self.fail_after_write = fail_after_write

    def target(self):
        return {
            "instance": {"platform": "macos", "pid": 1},
            "document": {"path": "/test/session.cosessiondb"},
            "image": {"path": f"/test/photo{self.selected}.RAW"},
            "variant": {"id": str(self.selected)},
            "layer": "Background",
        }

    def observe(self, fields):
        return {
            "target": self.target(),
            "values": {field: self.values[self.selected] for field in fields},
            "evidence": {"source": "c1_applescript", "model_verified": True},
        }

    def capabilities_for(self, observation):
        return {"fields": {"exposure": {
            "writable": True, "min": -4, "max": 4, "tolerance": 0.00001,
        }}}

    def set_values(self, patch, observation):
        self.writes.append((self.selected, deepcopy(patch)))
        self.values[self.selected] = patch["exposure"]
        if self.selected == self.fail_after_write:
            raise TimeoutError("Lost readback after the candidate changed")
        return {"model_verified": True,
                "observation": self.observe(list(observation["values"]))}

    def image_action(self, action, request, observation):
        assert action == "select"

        def select():
            self.selected = request["variant_id"]
            self.selections.append(self.selected)
            return {"status": "completed", "after": self.observe([]), "model_verified": True}

        return self.action_hook("variant.select", select)

    def close(self):
        pass


def prepare_batch(core, backend, count):
    items = []
    for candidate in range(1, count + 1):
        backend.selected = candidate
        observed = core.dispatch({"command": "read", "fields": ["exposure"]})
        assert observed["status"] == "ok"
        items.append({"target": observed["target"], "module": "exposure",
                      "patch": {"exposure": 0.25},
                      "observation_token": observed["observation_token"]})
    backend.selected = 1
    return {"command": "adjust", "request_id": "batch-check", "batch": items}


def test_batch_after_identifies_last_candidate_and_same_id_never_replays(tmp_path):
    backend = BatchBackend()
    core = Core(backend, state_dir=tmp_path)
    try:
        request = prepare_batch(core, backend, 2)
        result = core.dispatch(request)
        compact = compact_response(result, command="adjust")
        assert result["status"] == "completed"
        assert compact["after"]["target"] == backend.target()
        assert compact["after"]["values"] == {"exposure": 0.25}
        assert backend.selected == 2
        assert result["result"]["completed_adjustments"] == 2
        effects = deepcopy((backend.writes, backend.selections))
        repeated = core.dispatch(request)
        assert repeated["existing"] is True
        assert repeated["replayed"] is False
        assert repeated["after"] == result["after"]
        assert (backend.writes, backend.selections) == effects
    finally:
        core.close()


def test_batch_stops_after_unknown_write_without_processing_later_candidate(tmp_path):
    backend = BatchBackend(fail_after_write=2)
    core = Core(backend, state_dir=tmp_path)
    try:
        request = prepare_batch(core, backend, 3)
        result = core.dispatch(request)
        assert result["status"] == "outcome_unknown"
        assert result["after"] is None
        assert backend.values == {1: 0.25, 2: 0.25, 3: 0.0}
        assert backend.selected == 2
        assert backend.selections == [2]
        assert [candidate for candidate, _ in backend.writes] == [1, 2]
        children = result["result"]["children"]
        assert children[-1]["request_id"] == "batch-check:adjust:1"
        assert children[-1]["status"] == "outcome_unknown"
        assert not any(record["request_id"].endswith(":2") for record in core.journal.list())
        effects = deepcopy((backend.writes, backend.selections))
        repeated = core.dispatch(request)
        assert repeated["status"] == "outcome_unknown"
        assert repeated["existing"] is True
        assert repeated["replayed"] is False
        assert (backend.writes, backend.selections) == effects
    finally:
        core.close()
