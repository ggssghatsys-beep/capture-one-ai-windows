"""Navigation tokens must never become permission to edit a local layer."""
from copy import deepcopy

import pytest

from c1ai.core import Core
from c1ai.macos_backend import MacOSBackend


@pytest.mark.parametrize("mac_navigation", [False, True])
def test_adjacent_navigation_is_backend_opt_in_and_never_qualifies_edits(tmp_path, mac_navigation):
    class Backend:
        navigation_actions = MacOSBackend.navigation_actions if mac_navigation else {"select"}
        action_hook = None

        def __init__(self):
            self.selected = 1
            self.moves = 0

        def image_action(self, action, request, observation):
            if action != "list":
                assert action == "next"
                def move():
                    self.moves += 1
                    self.selected = 2
                    return {"status": "completed", "model_verified": True, "after": self.observation()}
                return self.action_hook("variant.next", move, {})
            return {"status": "ok", "_navigation_observation": self.observation()}

        def observation(self):
            return {"target": {"instance": {"platform": "macos", "pid": 1},
                               "document": {"path": "/test/session.cosessiondb"},
                               "image": {"path": f"/test/{self.selected}.RAW"},
                               "variant": {"id": str(self.selected)}, "layer": None},
                    "values": {"collection": {"id": "1"}, "selected_ids": [str(self.selected)]},
                    "navigation": True, "evidence": {"source": "c1_applescript"}}

    backend = Backend()
    core = Core(backend, state_dir=tmp_path)
    try:
        listed = core.dispatch({"command": "images", "action": "list"})
        args = {key: listed[key] for key in ("target", "observation_token")}
        request = {"command": "images", "action": "next", "request_id": "next", **args}
        result = core.dispatch(request)
        if not mac_navigation:
            assert result["error"]["code"] == "navigation_only"
            assert backend.moves == 0
            return
        assert result["status"] == "completed" and backend.moves == 1
        repeated = core.dispatch(deepcopy(request))
        assert repeated["existing"] is True and backend.moves == 1
        after = result["after"]
        args = {key: after[key] for key in ("target", "observation_token")}
        denied = core.dispatch({"command": "adjust", "module": "exposure", "patch": {"exposure": 1},
                                "request_id": "no-edit", **args})
        assert denied["error"]["code"] == "navigation_only"
        backend.selected = 3
        stale = core.dispatch({"command": "images", "action": "next", "request_id": "stale", **args})
        assert stale["error"]["code"] == "target_changed" and backend.moves == 1
    finally:
        core.journal.close()
