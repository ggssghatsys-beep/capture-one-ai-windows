"""Synthetic Basic matrices and UIA controls; no Capture One connection."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from c1ai import native_mixer as mixer
from c1ai.native_images import NativeAdapterError


def baseline():
    rows = []
    for color, signature in reversed(list(mixer.IDENTITIES.items())):
        rows.append([0 if color == "all" else 1, 1, 1, 0, 0, 0, *signature, -100, 100,
                     0 if color == "all" else 15, 0, 0, 0, 0])
    raw = ";".join(",".join(str(v) for v in row) for row in rows)
    return mixer.parse_basic(raw, 1300)


class MixerBackend:
    def __init__(self):
        self.target = {"instance": {"pid": 7, "started_at": "123", "version": "16.8.6.3784"},
                       "document": {"path": "synthetic.cosessiondb"}, "image": {"path": "synthetic.NEF"},
                       "variant": {"id": "candidate", "label": "synthetic [2]"}, "layer": "Background"}
        self.window = {"pid": 7, "hwnd": 8, "title": "synthetic", "version": "16.8.6.3784"}
        self.state = baseline(); self.color = "all"
        # Deliberately unrelated to DB row order and to the public color list.
        self.button_colors = ["yellow", "purple", "red", "all", "blue", "green", "magenta", "orange", "cyan"]
        self.actions, self.calls = [], []
        self.action_context = None; self.action_name = None
        self.drop_probe = self.drop_restore = self.refuse_restore = self.drop_formal = False
        self.ignore_probe = self.extra_change = False
        self.runtime_offset = 0

    def ui(self):
        return {"basic_runtime_id": [7, 1], "panel_runtime_id": [7, 2], "list_runtime_id": [7, 3],
                "buttons": [{"runtime_id": [7, 100 + i + self.runtime_offset], "parent_runtime_id": [7, 200 + i], "enabled": True} for i in range(9)],
                "fields": {field: {"runtime_id": [7, 300 + i], "value": self.state["colors"][self.color][field],
                                    "text": str(self.state["colors"][self.color][field]), "enabled": not (self.color == "all" and field == "lightness"),
                                    "read_only": False} for i, field in enumerate(mixer.COMPONENTS)},
                "native_color_identity_available": False, "brush_rgb_available": False}

    def observe(self, fields):
        self.calls.append(("observe", fields))
        return {"target": copy.deepcopy(self.target), "values": {}, "raw_state": {"window": copy.deepcopy(self.window)}, "evidence": {}}

    def saved(self, target): return copy.deepcopy(self.state)
    def guard(self, observation): return {"target": copy.deepcopy(observation["target"])}

    def call(self, name, **payload):
        self.calls.append((name, copy.deepcopy(payload)))
        if name == "mixer_state": return {"window": copy.deepcopy(self.window), "state": self.ui()}
        if self.action_name is None: raise AssertionError("Native write outside action journal")
        if payload["expected_mixer"] != self.ui(): raise NativeAdapterError("STALE_MIXER", "synthetic stale controls")
        if name == "mixer_select":
            selected = [i for i in range(9) if [7, 100 + i + self.runtime_offset] == payload["button_runtime_id"]]
            if len(selected) != 1: raise NativeAdapterError("MIXER_PATCH_UNAVAILABLE", "synthetic missing button")
            self.color = self.button_colors[selected[0]]
            return {"window": self.window, "accepted": True, "state": self.ui()}
        if name == "mixer_set":
            restoring = self.action_name.endswith(".restore")
            probing = self.action_name.endswith(".probe")
            if restoring and self.refuse_restore:
                self.refuse_restore = False
                error = NativeAdapterError("STALE_MIXER", "synthetic known no dispatch"); error.dispatched = False; raise error
            if not (probing and self.ignore_probe):
                self.state = mixer._patch_state(self.state, self.color, payload["field"], payload["value"])
            if probing and self.extra_change:
                other = next(c for c in mixer.IDENTITIES if c != self.color)
                self.state = mixer._patch_state(self.state, other, "saturation", 0.2)
            if (probing and self.drop_probe) or (restoring and self.drop_restore) or (self.action_name.startswith("mixer.apply.") and self.drop_formal):
                raise NativeAdapterError("TRANSPORT_LOST", "synthetic lost reply after native effect")
            return {"window": self.window, "accepted": True, "control_confirmed": True, "state": self.ui()}
        raise AssertionError(name)

    def action(self, name, function, intent=None):
        action = {"action_id": f"action-{len(self.actions)}", "name": name, "status": "dispatching",
                  "intent": {"before": copy.deepcopy(self.before), **copy.deepcopy(intent or {})}}
        self.actions.append(action)
        self.action_context = {"action_id": action["action_id"], "operation_id": "synthetic"}; self.action_name = name
        try:
            result = function(); action.update(status="completed", result=copy.deepcopy(result)); return result
        except Exception as error:
            action["status"] = "not_dispatched" if getattr(error, "dispatched", None) is False else "outcome_unknown"
            action["error"] = {"code": getattr(error, "code", type(error).__name__), "message": str(error)}
            raise
        finally:
            self.action_context = self.action_name = None


class NativeMixerTests(unittest.TestCase):
    def setUp(self):
        self.backend = MixerBackend(); self.addCleanup(patch.stopall)
        patch.object(mixer, "_saved", side_effect=self.backend.saved).start()
        patch("c1ai.native_modules.require_candidate").start()
        self.before = mixer.observe_mixer(self.backend); self.backend.before = copy.deepcopy(self.before)
        self.request_patch = {"mode": "basic", "color": "red", "hue": 2.5, "saturation": -3, "lightness": 1}

    def record(self):
        return {"status": "partial", "operation_id": "synthetic", "target": copy.deepcopy(self.before["target"]),
                "before": copy.deepcopy(self.before), "request": {"command": "adjust", "module": "color_mixer", "patch": copy.deepcopy(self.request_patch)},
                "actions": copy.deepcopy(self.backend.actions)}

    def qualify(self): return mixer.qualify_mixer(self.backend, self.before, timeout=0)
    def apply(self): return mixer.apply_mixer(self.backend, self.request_patch, self.before, timeout=0)

    def test_row_signature_does_not_infer_color_from_order_and_aliases_are_explicit(self):
        parsed = baseline()
        self.assertEqual(parsed["indexes"]["red"], 1)
        self.assertEqual(parsed["matrix"][parsed["indexes"]["purple"]][6:9], [mixer._float(v) for v in mixer.IDENTITIES["purple"][:3]])
        self.assertNotIn("pink", parsed["colors"])
        invalid = copy.deepcopy(parsed["matrix"]); invalid[0][6] = 255
        raw = ";".join(",".join(str(v) for v in row) for row in invalid)
        with self.assertRaises(NativeAdapterError): mixer.parse_basic(raw, 1300)
        with self.assertRaises(NativeAdapterError): mixer.parse_basic("0", 1200)

    def test_complete_probe_plan_is_unique_small_and_built_before_any_action(self):
        plan = mixer.probe_plan(self.before)
        self.assertEqual(len(plan["buttons"]), 9)
        self.assertEqual(len(plan["groups"]), 1)
        group = next(iter(plan["groups"].values()))
        self.assertAlmostEqual(group["probe_hue"], 0.1)
        self.assertEqual(set(group["possible_colors"]), set(mixer.IDENTITIES))
        self.assertEqual(self.backend.actions, [])
        self.backend.state["matrix"][0][1] = 0
        changed = mixer.observe_mixer(self.backend)
        with self.assertRaises(NativeAdapterError): mixer.probe_plan(changed)
        self.assertEqual(self.backend.actions, [])

    def test_nine_empirical_probes_restore_full_matrix_and_map_runtime_not_ordinal(self):
        binding = self.qualify()
        self.assertEqual(self.backend.state, self.before["state"])
        self.assertEqual(binding["mapping"]["red"], [7, 102])
        self.assertEqual(binding["mapping"]["all"], [7, 103])
        self.assertEqual(len(binding["probes"]), 9)
        self.assertFalse(binding["native_color_identity_verified"])
        self.assertEqual(self.backend.actions[0]["name"], "mixer.qualify.plan")
        self.assertEqual(sum(a["name"].endswith(".probe") for a in self.backend.actions), 9)
        self.assertEqual(sum(a["name"].endswith(".restore") for a in self.backend.actions), 9)
        for action in self.backend.actions:
            if action["name"].endswith(".restore"):
                self.assertEqual(action["result"]["after"]["state"], self.before["state"])

    def test_formal_adjustment_uses_qualified_runtime_and_preserves_every_other_cell(self):
        self.qualify()
        result = self.apply()
        expected = copy.deepcopy(self.before["state"])
        for field in mixer.COMPONENTS: expected = mixer._patch_state(expected, "red", field, self.request_patch[field])
        self.assertEqual(result["after"]["state"], expected)
        self.assertEqual(result["status"], "control_confirmed")
        self.assertTrue(result["requested_result_verified"])
        self.assertFalse(result["evidence"]["native_color_identity_verified"])
        self.assertFalse(result["evidence"]["render_verified"])
        self.assertEqual([a["name"] for a in self.backend.actions if a["name"].startswith("mixer.apply.")],
                         ["mixer.apply.hue", "mixer.apply.saturation", "mixer.apply.lightness"])

    def test_memory_binding_is_reused_only_for_identical_candidate_and_runtime(self):
        self.qualify(); count = len(self.backend.actions)
        result = self.apply()
        self.assertEqual(len(self.backend.actions) - count, 4)
        self.backend.before = mixer.observe_mixer(self.backend); self.before = copy.deepcopy(self.backend.before)
        self.backend.runtime_offset = 1000
        self.backend.before = mixer.observe_mixer(self.backend); self.before = copy.deepcopy(self.backend.before)
        count = len(self.backend.actions)
        with self.assertRaises(NativeAdapterError) as caught: self.apply()
        self.assertEqual(caught.exception.code, "MIXER_QUALIFICATION_REQUIRED")
        self.assertEqual(len(self.backend.actions), count)
        self.qualify()
        self.apply()
        self.assertEqual(sum(a["name"].endswith(".probe") for a in self.backend.actions[count:]), 9)

    def test_ordinary_apply_never_implicitly_starts_qualification(self):
        with self.assertRaises(NativeAdapterError) as caught: self.apply()
        self.assertEqual(caught.exception.code, "MIXER_QUALIFICATION_REQUIRED")
        self.assertEqual(self.backend.actions, [])
        self.assertFalse(any(name in {"mixer_set", "mixer_select"} for name, _ in self.backend.calls))

    def test_explicit_qualification_returns_verified_baseline_and_no_requested_patch(self):
        self.backend.calls.clear()
        result = mixer.apply_mixer(self.backend, {"action": "qualify"}, self.before, timeout=0)
        self.assertEqual(result["status"], "control_confirmed")
        self.assertTrue(result["requested_result_verified"])
        self.assertTrue(result["qualification_only"])
        self.assertFalse(result["requested_hsl_patch_applied"])
        self.assertEqual(result["after"]["state"], self.before["state"])
        evidence = result["evidence"]
        self.assertEqual(evidence["scope"], "mixer_runtime_binding_qualification")
        self.assertTrue(evidence["whole_matrix_baseline_verified"])
        self.assertTrue(evidence["baseline_ui_verified"])
        self.assertEqual(len(evidence["qualification_probes"]), 9)
        self.assertFalse(evidence["selection_restored"])
        self.assertEqual(evidence["probe_parameter_writes"], 18)
        self.assertGreaterEqual(evidence["elapsed_seconds"], 0)
        self.assertFalse(any(a["name"].startswith("mixer.apply.") for a in self.backend.actions))
        # This counts synthetic calls, not elapsed-time/live qualification.
        self.assertEqual(sum(name == "mixer_state" for name, _ in self.backend.calls), 56)
        self.assertEqual(sum(name == "observe" for name, _ in self.backend.calls), 112)
        self.assertEqual(sum(name == "mixer_select" for name, _ in self.backend.calls), 9)
        self.assertEqual(sum(name == "mixer_set" for name, _ in self.backend.calls), 18)

    def test_reused_restore_reference_still_gets_fresh_preselect_state_guard(self):
        original = self.backend.action
        def mutate_before_next_select(name, function, intent=None):
            if name == "mixer.qualify.1.select":
                self.backend.state = mixer._patch_state(self.backend.state, "red", "saturation", 0.2)
            return original(name, function, intent)
        with patch.object(self.backend, "action", side_effect=mutate_before_next_select):
            with self.assertRaises(NativeAdapterError) as caught: self.qualify()
        self.assertEqual(caught.exception.code, "MIXER_STATE_CHANGED")
        self.assertEqual(self.backend.actions[-1]["status"], "not_dispatched")
        self.assertEqual(sum(name == "mixer_select" for name, _ in self.backend.calls), 1)
        self.assertEqual(sum(name == "mixer_set" for name, _ in self.backend.calls), 2)

    def test_mapping_never_transfers_to_another_candidate(self):
        self.qualify()
        self.backend.target["variant"]["id"] = "other-candidate"
        self.before = mixer.observe_mixer(self.backend)
        count = len(self.backend.actions)
        with self.assertRaises(NativeAdapterError) as caught: self.apply()
        self.assertEqual(caught.exception.code, "MIXER_QUALIFICATION_REQUIRED")
        self.assertEqual(len(self.backend.actions), count)

    def test_lost_probe_reply_stops_then_original_operation_gets_one_bounded_restore(self):
        self.backend.drop_probe = True
        with self.assertRaises(NativeAdapterError): self.qualify()
        self.assertEqual(sum(a["name"].endswith(".probe") for a in self.backend.actions), 1)
        self.assertEqual(mixer.reconcile_mixer(self.backend, self.record())["outcome"], "unknown")
        self.backend.drop_probe = False
        result = mixer.recover_mixer(self.backend, self.record(), timeout=0)
        self.assertEqual(result["outcome"], "restored")
        self.assertEqual(result["evidence"]["scope"], "mixer_qualification_probe_baseline")
        self.assertTrue(result["evidence"]["whole_matrix_baseline_verified"])
        self.assertEqual(self.backend.state, self.before["state"])
        count = len(self.backend.actions)
        self.assertEqual(mixer.recover_mixer(self.backend, self.record(), timeout=0)["outcome"], "restored")
        self.assertEqual(len(self.backend.actions), count)

    def test_unknown_probe_with_current_baseline_cannot_be_cleared_or_retried(self):
        self.backend.ignore_probe = True
        with self.assertRaises(NativeAdapterError): self.qualify()
        count = len(self.backend.actions)
        self.assertEqual(self.backend.state, self.before["state"])
        self.assertEqual(mixer.recover_mixer(self.backend, self.record(), timeout=0)["outcome"], "unknown")
        self.assertEqual(len(self.backend.actions), count)

    def test_unknown_restore_with_current_baseline_never_replays(self):
        self.backend.drop_restore = True
        with self.assertRaises(NativeAdapterError): self.qualify()
        self.assertEqual(self.backend.state, self.before["state"])
        count = len(self.backend.actions)
        self.assertEqual(mixer.recover_mixer(self.backend, self.record(), timeout=0)["outcome"], "unknown")
        self.assertEqual(len(self.backend.actions), count)

    def test_known_not_dispatched_restore_can_get_one_new_recovery_action(self):
        self.backend.refuse_restore = True
        with self.assertRaises(NativeAdapterError): self.qualify()
        result = mixer.recover_mixer(self.backend, self.record(), timeout=0)
        self.assertEqual(result["outcome"], "restored")
        self.assertEqual(self.backend.actions[-1]["name"], "recover.mixer.qualify.0.restore")

    def test_two_rows_change_or_runtime_drift_never_produces_mapping_or_restore(self):
        self.backend.extra_change = True
        with self.assertRaises(NativeAdapterError): self.qualify()
        self.assertFalse(hasattr(self.backend, "_mixer_binding"))
        count = len(self.backend.actions)
        self.assertEqual(mixer.recover_mixer(self.backend, self.record(), timeout=0)["outcome"], "unknown")
        self.assertEqual(len(self.backend.actions), count)
        self.backend.runtime_offset += 1
        with self.assertRaises(NativeAdapterError): mixer.recover_mixer(self.backend, self.record(), timeout=0)

    def test_duplicate_button_effect_stops_after_restore_without_publishing_binding(self):
        self.backend.button_colors[1] = self.backend.button_colors[0]
        with self.assertRaises(NativeAdapterError): self.qualify()
        self.assertEqual(self.backend.state, self.before["state"])
        self.assertFalse(hasattr(self.backend, "_mixer_binding"))
        result = mixer.reconcile_mixer(self.backend, self.record())
        self.assertEqual(result["outcome"], "restored")
        self.assertEqual(len(result["evidence"]["probes"]), 2)

    def test_formal_apply_cannot_use_probe_restore_scope(self):
        self.qualify(); self.backend.drop_formal = True
        self.request_patch = {"mode": "basic", "color": "red", "hue": 2}
        with self.assertRaises(NativeAdapterError): self.apply()
        result = mixer.reconcile_mixer(self.backend, self.record())
        self.assertEqual(result["outcome"], "applied_not_restored")
        self.assertNotEqual(result["evidence"]["scope"], "mixer_qualification_probe_baseline")

    def test_formal_recorded_target_drift_is_not_cleared_by_returning_to_matching_image(self):
        self.qualify(); self.backend.drop_formal = True
        self.request_patch = {"mode": "basic", "color": "red", "hue": 2}
        with self.assertRaises(NativeAdapterError): self.apply()
        record = self.record(); record["actions"][-1]["error"]["code"] = "MIXER_TARGET_CHANGED"
        result = mixer.reconcile_mixer(self.backend, record)
        self.assertEqual(result["outcome"], "unknown")
        self.assertTrue(result["evidence"]["target_drift_recorded"])
        count = len(self.backend.actions)
        self.assertEqual(mixer.recover_mixer(self.backend, record, timeout=0)["outcome"], "unknown")
        self.assertEqual(len(self.backend.actions), count)

    def test_qualification_success_without_formal_apply_is_restored_not_not_applied(self):
        self.qualify()
        result = mixer.reconcile_mixer(self.backend, self.record())
        self.assertEqual(result["outcome"], "restored")
        self.assertEqual(len(result["evidence"]["probes"]), 9)
        self.assertFalse(result["evidence"]["selection_restored"])

    def test_recovery_refuses_other_target_or_tampered_ledger(self):
        self.qualify()
        record = self.record(); record["actions"][0]["intent"]["before"]["state"] = {}
        with self.assertRaises(NativeAdapterError): mixer.reconcile_mixer(self.backend, record)
        record = self.record(); record["actions"][0]["name"] = "unrelated.operation"
        with self.assertRaises(NativeAdapterError): mixer.reconcile_mixer(self.backend, record)
        self.backend.target["variant"]["id"] = "other"
        with self.assertRaises(NativeAdapterError): mixer.recover_mixer(self.backend, self.record(), timeout=0)

    def test_invalid_request_fails_before_plan_or_any_native_write(self):
        for invalid in ({"mode": "basic", "color": "all", "lightness": 0}, {"mode": "basic", "color": "red", "saturation": 81},
                        {"mode": "basic", "color": "red", "lightness": 51}, {"mode": "basic", "color": "red", "hue": True},
                        {"action": "qualify", "hue": 0}, {"action": "apply"}):
            with self.subTest(invalid=invalid), self.assertRaises(NativeAdapterError):
                mixer.apply_mixer(self.backend, invalid, self.before, timeout=0)
        self.assertEqual(self.backend.actions, [])

    def test_probe_python_preflight_refusal_is_not_a_phantom_unknown_write(self):
        original = self.backend.guard
        def guard(observation):
            if self.backend.action_name and self.backend.action_name.endswith(".probe"):
                raise NativeAdapterError("STALE_SYNTHETIC", "before native call")
            return original(observation)
        with patch.object(self.backend, "guard", side_effect=guard):
            with self.assertRaises(NativeAdapterError): self.qualify()
        self.assertEqual(self.backend.actions[-1]["status"], "not_dispatched")
        self.assertFalse(any(name == "mixer_set" for name, _ in self.backend.calls))
        self.assertEqual(mixer.reconcile_mixer(self.backend, self.record())["outcome"], "not_applied")

    def test_all_backend_routes_include_bounded_original_operation_recovery(self):
        from c1ai import native_modules
        from c1ai.windows_backend import WindowsBackend
        record = self.record()
        for name, call in (("observe_mixer", lambda: native_modules.observe_module(self.backend, "color_mixer")),
                           ("apply_mixer", lambda: native_modules.adjust_module(self.backend, "color_mixer", self.request_patch, self.before)),
                           ("reconcile_mixer", lambda: native_modules.reconcile_module(self.backend, record)),
                           ("recover_mixer", lambda: native_modules.recover_module(self.backend, record))):
            with patch.object(mixer, name, return_value={"synthetic": name}) as route:
                self.assertEqual(call(), {"synthetic": name}); route.assert_called_once()
        native = WindowsBackend.__new__(WindowsBackend)
        with patch.object(native_modules, "recover_module", return_value={"synthetic": "bounded"}) as route:
            self.assertEqual(native.recover(record), {"synthetic": "bounded"})
            route.assert_called_once_with(native, record)

    def test_real_core_explicit_qualification_recovers_under_original_action_journal(self):
        from c1ai.core import Core
        backend = self.backend
        backend.action_hook = None
        def journal_action(name, function, intent=None):
            def run():
                backend.action_name = name
                try: return function()
                finally: backend.action_name = None
            return backend.action_hook(name, run, intent)
        backend.action = journal_action
        backend.capabilities_for = lambda observation: {"modules": {"color_mixer": {"writable": True}}}
        backend.observe_module = lambda module: mixer.observe_mixer(backend)
        backend.adjust_module = lambda module, request, observed: mixer.apply_mixer(backend, request, observed, timeout=0)
        backend.reconcile = lambda record: mixer.reconcile_mixer(backend, record)
        backend.recover = lambda record: mixer.recover_mixer(backend, record, timeout=0)
        with tempfile.TemporaryDirectory() as directory:
            core = Core(backend, directory)
            try:
                observed = core.dispatch({"command": "read", "module": "color_mixer"})
                request = {"command": "adjust", "module": "color_mixer", "patch": {"action": "qualify"},
                           "target": observed["target"], "observation_token": observed["observation_token"], "request_id": "first-probe"}
                backend.drop_probe = True
                failed = core.dispatch(request)
                self.assertIn(failed["status"], {"partial", "outcome_unknown"}, failed)
                self.assertEqual(failed["actions"][-1]["name"], "mixer.qualify.0.probe")
                backend.drop_probe = False
                result = core.dispatch({"command": "operations", "action": "recover", "operation_id": failed["operation_id"]})
                self.assertEqual(result["status"], "restored", result)
                self.assertEqual(result["restore_scope"], "qualification_probe_baseline")
                self.assertFalse(result["requested_result_verified"])
                self.assertFalse(result["full_model_restore_verified"])
                self.assertIsNone(result["pixel_restore_verified"])
                self.assertEqual(result["actions"][-1]["name"], "recover.mixer.qualify.0.restore")
                count = len(result["actions"])
                again = core.dispatch({"command": "operations", "action": "recover", "operation_id": failed["operation_id"]})
                self.assertEqual(len(again["actions"]), count)
                fresh = core.dispatch({"command": "read", "module": "color_mixer"})
                refused = core.dispatch({**request, "patch": self.request_patch, "request_id": "ordinary-unqualified", "observation_token": fresh["observation_token"], "target": fresh["target"]})
                self.assertEqual(refused["status"], "refused")
                self.assertEqual(refused["error"]["code"], "MIXER_QUALIFICATION_REQUIRED")
                self.assertEqual(refused["actions"], [])
                qualified = core.dispatch({**request, "request_id": "qualification-retry", "observation_token": fresh["observation_token"], "target": fresh["target"]})
                self.assertEqual(qualified["status"], "control_confirmed", qualified)
                fresh = core.dispatch({"command": "read", "module": "color_mixer"})
                final = core.dispatch({**request, "patch": self.request_patch, "request_id": "actual-patch", "observation_token": fresh["observation_token"], "target": fresh["target"]})
                self.assertEqual(final["status"], "control_confirmed", final)
                self.assertNotEqual(qualified["operation_id"], final["operation_id"])
                self.assertFalse(any(a["name"].startswith("mixer.qualify.") for a in final["actions"]))
            finally: core.close()

    def test_windows_native_ops_allowlist_and_predispatch_error_classification(self):
        from c1ai.windows_backend import WindowsBackend, NativeError
        backend = WindowsBackend.__new__(WindowsBackend); backend.window = {"pid": 7, "hwnd": 8}
        calls = []
        def request(command, data, timeout):
            calls.append(json.loads(data)); return SimpleNamespace(stdout=b'{"ok":true,"result":{"synthetic":true}}')
        backend.worker = SimpleNamespace(host_path=Path("synthetic.ps1"), request=request)
        for name in ("mixer_state", "mixer_select", "mixer_set"):
            self.assertTrue(backend.call(name)["synthetic"])
            self.assertEqual(calls[-1]["op"], name)
        for mutated in (False, True):
            backend.worker.request = lambda *args: SimpleNamespace(stdout=json.dumps({"ok": False, "error": {
                "code": "SYNTHETIC", "message": "native failure", "possibly_mutated": mutated}}).encode())
            for name in ("mixer_select", "mixer_set"):
                with self.assertRaises(NativeError) as caught: backend.call(name)
                self.assertEqual(getattr(caught.exception, "dispatched", None), None if mutated else False)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell fake ValuePattern")
    def test_powershell_setvalue_is_once_guarded_and_never_falls_back_to_input(self):
        source = Path(mixer.__file__).parent / "native" / "mixer.ps1"
        quoted = "'" + str(source).replace("'", "''") + "'"
        code = f"""$ErrorActionPreference='Stop'
function Fail($Code,$Message,$Details) {{throw ($Code+': '+$Message)}}
function Field($Object,$Name,$Default=$null) {{if($Object.Contains($Name)){{return $Object[$Name]}};return $Default}}
function Names($Object) {{return @($Object.Keys)}}
function Same($A,$B) {{return (($A|ConvertTo-Json -Depth 30 -Compress) -ceq ($B|ConvertTo-Json -Depth 30 -Compress))}}
function Check-Guard($Guard) {{$script:guardCalls++}}
. {quoted}
$script:WindowInfo=@{{pid=999;hwnd=888;title='synthetic';version='fake'}}
$script:guardCalls=0;$script:setCalls=0;$script:MayHaveMutated=$false
$script:state=@{{fields=@{{hue=@{{value=0.0}}}}}}
function Mixer-State {{return $script:state}}
function Mixer-Basic {{return 'synthetic-basic'}}
$script:edit=[pscustomobject]@{{Current=[pscustomobject]@{{IsEnabled=$true}}}}
function Mixer-Edit($Basic,$Field) {{return $script:edit}}
$script:pattern=[pscustomobject]@{{Current=[pscustomobject]@{{IsReadOnly=$false}}}}
$script:pattern|Add-Member ScriptMethod SetValue {{param($Value)
  if(-not $script:MayHaveMutated){{throw 'Mutation marker must precede call'}}
  $script:setCalls++;$script:state.fields.hue.value=[double]$Value
}}
function Pattern($Element,$Name) {{if($Name -cne 'Value'){{throw 'Unexpected native pattern'}};return $script:pattern}}
$request=@{{op='mixer_set';guard=@{{synthetic=$true}};expected_mixer=$script:state;field='hue';value=0.1}}
$result=Set-MixerField $request
if(-not $result.accepted -or -not $result.control_confirmed -or $script:setCalls -ne 1 -or $script:guardCalls -lt 3){{throw 'Guarded single ValuePattern failed'}}
$script:setCalls=0;$script:MayHaveMutated=$false;$request.value=31
$rejected=$false
try{{$unexpected=Set-MixerField $request}}catch{{$rejected=$_.Exception.Message -like '*INVALID_MIXER_VALUE*'}}
if(-not $rejected -or $script:setCalls -ne 0 -or $script:MayHaveMutated){{throw 'Invalid value was dispatched'}}
$request.value=0;$script:edit.Current.IsEnabled=$false;$rejected=$false
try{{$unexpected=Set-MixerField $request}}catch{{$rejected=$_.Exception.Message -like '*MIXER_FIELD_UNAVAILABLE*'}}
if(-not $rejected -or $script:setCalls -ne 0 -or $script:MayHaveMutated){{throw 'Disabled field was dispatched'}}
'synthetic-value-boundary-ok'
"""
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "mixer-test.ps1"; script.write_text(code, encoding="utf-8-sig")
            exe = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            result = subprocess.run([str(exe), "-NoProfile", "-NonInteractive", "-File", str(script)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn(b"synthetic-value-boundary-ok", result.stdout)


if __name__ == "__main__": unittest.main()
