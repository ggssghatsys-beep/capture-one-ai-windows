"""Regressions for opaque Skin Tone source identity and implicit Basic rows."""
from copy import deepcopy
import hashlib

import pytest

from c1ai.contracts import Refused, canonical, digest
from c1ai.journal import Journal
from c1ai.macos_backend import MacOSBackend
from c1ai.macos_color_editor import _color_rows, require_explicit_basic_source


def observation():
    return {"target": {"instance": {"platform": "macos", "pid": 1}, "layer": "Background"},
            "module": "color_mixer", "values": {"color_mixer": {"basic": "unchanged"}},
            "state": {"basic": "unchanged"}, "native_context": {
                "layer_state": {"selected_layer": {"native_id": "Background"}, "layers": []},
                "color_editor_snapshot": {"available": True, "source": {"variant_uuid": "same"},
                                          "sha256": "a" * 64}}}


def test_mac_skin_only_read_never_overwrites_old_source_token(tmp_path):
    journal = Journal(tmp_path)
    try:
        old = observation()
        token = journal.observe(old)
        skin_changed = deepcopy(old)
        skin_changed["native_context"]["color_editor_snapshot"]["sha256"] = "b" * 64
        assert journal.observe(skin_changed) != token
        assert journal.observation(token) == old
        layers_changed = deepcopy(old)
        layers_changed["native_context"]["layer_state"]["layers"] = [{"native_id": "new-local"}]
        assert journal.observe(layers_changed) != token
        assert journal.observation(token) == old
        legacy = digest({"target": old["target"], "values": old["values"], "module": old["module"]})
        with journal.db:
            journal.db.execute("INSERT INTO observations VALUES (?, ?)", (legacy, canonical(skin_changed)))
        with pytest.raises(Refused, match="older token"):
            journal.observation(legacy)
        # The Windows observation identity contract is deliberately unchanged.
        old["target"]["instance"]["platform"] = "windows"
        skin_changed["target"]["instance"]["platform"] = "windows"
        assert journal.observe(old) == journal.observe(skin_changed)
    finally:
        journal.close()


def test_complete_copy_rejects_implicit_basic_source():
    skin = [1, 1, 2] + [0] * 15
    def saved(rows):
        raw = ";".join(",".join(map(str, row)) for row in rows) + ";"
        parsed, encoded = _color_rows(raw)
        return {"available": True, "raw": raw, "rows": parsed,
                "format": "capture_one_color_corrections_18_column_text",
                "sha256": hashlib.sha256(encoded).hexdigest()}
    with pytest.raises(Refused, match="nine explicit"):
        require_explicit_basic_source(saved([skin]))
    basic = []
    for index in range(9):
        row = [1, 1, 1] + [0] * 15
        row[6] = index
        basic.append(row)
    require_explicit_basic_source(saved(basic + [skin]))


def test_reconcile_cannot_call_skin_change_not_applied(tmp_path):
    backend = MacOSBackend(tmp_path)
    before = observation()
    after = deepcopy(before)
    after["native_context"]["color_editor_snapshot"]["sha256"] = "b" * 64
    backend.observe_module = lambda module: after
    record = {"request": {"module": "color_mixer", "patch": {"action": "paste"}},
              "target": before["target"], "before": before}
    result = backend.reconcile(record)
    assert result["outcome"] == "unknown"
    assert result["evidence"]["replayed"] is False
