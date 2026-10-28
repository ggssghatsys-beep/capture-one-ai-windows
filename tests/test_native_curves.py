"""Synthetic UI/SQLite observations and isolated user-folder fixtures only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from c1ai import native_curves as curves
from c1ai.native_images import NativeAdapterError


def geometry(channel, points):
    return {"selected_channel": channel,
            "radios": [{"channel": c, "name": label, "runtime_id": [7, i + 20],
                        "selected": c == channel, "enabled": True} for i, (c, (label, _)) in enumerate(curves.CHANNELS.items())],
            "canvas": {"runtime_id": [7, 1], "rect": {"x": 200, "y": 300, "width": 399, "height": 296}},
            "points": [{"name": f"{x * 265 - 3},{y * 196.333333333 - 3}", "runtime_id": [7, i + 100],
                        "rect": {"x": 200 + x * 399 - 5, "y": 300 + (1 - y) * 296 - 5,
                                 "width": 10, "height": 10}} for i, (x, y) in enumerate(points)]}


class CurveBackend:
    def __init__(self, directory):
        self.state_dir = Path(directory) / "state"; self.state_dir.mkdir()
        self.target = {"instance": {"pid": 7, "started_at": "synthetic", "version": "16.8.6"},
                       "document": {"path": "synthetic.cosessiondb"}, "image": {"path": "synthetic.NEF"},
                       "variant": {"id": "12345678-1234-1234-1234-123456789012", "label": "synthetic.NEF [2]"}, "layer": "Background"}
        self.window = {"pid": 7, "hwnd": 8, "title": "synthetic", "version": "16.8.6"}
        self.channels = {channel: [[0.0, 0.0], [1.0, 1.0]] for channel in curves.CHANNELS}
        self.channel, self.engine = "rgb", 1600
        self.actions, self.calls = [], []
        self.in_action = False
        self.missing_menu = self.do_not_apply = self.fail_after_apply = self.change_other = False
        self.ui_override = None

    def observe(self, fields):
        self.calls.append(("observe", list(fields)))
        return {"target": copy.deepcopy(self.target), "values": {}, "raw_state": {"window": copy.deepcopy(self.window)}, "evidence": {}}

    def saved(self, target):
        self.assert_target = target
        return copy.deepcopy(self.channels), self.engine, {"ZPROCESSINGENGINE": self.engine}

    def guard(self, observation):
        return {"synthetic_target": copy.deepcopy(observation["target"])}

    def call(self, action, **payload):
        self.calls.append((action, copy.deepcopy(payload)))
        state = self.ui_override or geometry(self.channel, self.channels[self.channel])
        if action == "curve_state":
            return {"window": copy.deepcopy(self.window), "state": copy.deepcopy(state)}
        if not self.in_action:
            raise AssertionError("Native mutation outside action journal")
        if state != payload["expected_curve"]:
            raise NativeAdapterError("STALE_CURVE", "synthetic stale curve")
        if action == "curve_select":
            self.channel = payload["channel"]
            return {"window": self.window, "accepted": True}
        if action == "curve_preset_menu":
            items = [] if self.missing_menu else [{"name": p.stem, "runtime_id": [4, i], "enabled": True, "offscreen": False}
                    for i, p in enumerate(curves._roots(self)[0].glob("*.copreset"))]
            return {"window": self.window, "accepted": True, "menu": {"anchor_runtime_id": [7, 90], "items": items,
                    "scope": "curve_presets_descendants"}}
        if action == "curve_apply_preset":
            items = [row for row in payload["expected_menu"]["items"] if row["name"] == payload["preset_name"]]
            if len(items) != 1:
                error = NativeAdapterError("CURVE_PRESET_UNAVAILABLE", "synthetic missing item"); error.dispatched = False; raise error
            path = curves._roots(self)[0] / (payload["preset_name"] + ".copreset")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != payload["preset_sha256"]:
                raise AssertionError("wrong native preset hash")
            xml = ET.fromstring(data); mapping = {entry.get("K"): entry.get("V") for entry in xml}
            channel = payload["channel"]
            if not self.do_not_apply:
                self.channels[channel] = [[float(n) for n in pair.split(",")] for pair in mapping[curves.CHANNELS[channel][1]].split(";")]
            if self.change_other:
                other = next(c for c in curves.CHANNELS if c != channel)
                self.channels[other][0][1] = 1e-9
            if self.fail_after_apply:
                raise NativeAdapterError("TRANSPORT_LOST", "synthetic response lost after native invoke")
            return {"window": self.window, "accepted": True}
        raise AssertionError(action)

    def action(self, name, function, intent=None):
        record = {"name": name, "status": "dispatching", "intent": {"before": copy.deepcopy(self.before), **copy.deepcopy(intent or {})}}
        self.actions.append(record); self.in_action = True
        try:
            result = function(); record.update(status="completed", result=copy.deepcopy(result)); return result
        except Exception as error:
            record["status"] = "not_dispatched" if getattr(error, "dispatched", None) is False else "outcome_unknown"
            raise
        finally:
            self.in_action = False


class NativeCurveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.local = Path(self.directory.name) / "local"; self.local.mkdir()
        self.backend = CurveBackend(self.directory.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}).start()
        patch.object(curves, "_saved", side_effect=self.backend.saved).start()
        patch("c1ai.native_modules.require_candidate").start()
        self.before = curves.observe_curves(self.backend)
        self.backend.before = copy.deepcopy(self.before)
        self.patch = {"channel": "red", "points": [[0, 0], [0.5, 0.6], [1, 1]]}

    def apply(self):
        return curves.apply_curve(self.backend, self.patch, self.before, timeout=0)

    def record(self):
        return {"status": "partial", "request": {"command": "adjust", "module": "curves", "patch": copy.deepcopy(self.patch)},
                "target": copy.deepcopy(self.before["target"]), "before": copy.deepcopy(self.before), "actions": copy.deepcopy(self.backend.actions)}

    def test_one_channel_tool_preset_has_dual_evidence_and_journals_all_writes(self):
        result = self.apply()
        self.assertEqual(result["status"], "control_confirmed")
        self.assertTrue(result["requested_result_verified"])
        self.assertEqual([a["name"] for a in self.backend.actions],
                         ["curves.select_channel", "curves.stage_preset", "curves.open_presets", "curves.apply_preset"])
        self.assertEqual(result["after"]["state"]["channels"]["red"], self.patch["points"])
        for channel in set(curves.CHANNELS) - {"red"}:
            self.assertEqual(result["after"]["state"]["channels"][channel], self.before["state"]["channels"][channel])
        evidence = result["evidence"]
        self.assertTrue(evidence["geometry"]["ui_geometry_verified"])
        self.assertFalse(evidence["native_numeric_readback"])
        self.assertFalse(evidence["render_verified"])
        xml = ET.parse(evidence["preset"]["path"]).getroot()
        self.assertEqual(xml.attrib, {"Engine": "900"})
        self.assertEqual({e.get("K") for e in xml}, {"GradationCurveRed", "Name", "UUID", "StyleSource"})
        stage = self.backend.actions[1]["intent"]["receipt"]
        self.assertEqual(hashlib.sha256(stage["content_utf8"].encode()).hexdigest(), stage["sha256"])

    def test_curve_already_equal_selects_channel_without_staging_or_applying(self):
        self.patch["points"] = [[0, 0], [1, 1]]
        result = self.apply()
        self.assertTrue(result["evidence"]["curve_apply_not_dispatched"])
        self.assertEqual([a["name"] for a in self.backend.actions], ["curves.select_channel"])
        self.assertFalse(curves._roots(self.backend)[0].exists())

    def test_all_five_channels_survive_and_even_tiny_unrequested_change_refuses(self):
        self.backend.change_other = True
        with self.assertRaises(NativeAdapterError) as raised:
            self.apply()
        self.assertEqual(raised.exception.code, "CURVE_RESULT_UNKNOWN")
        self.assertEqual(curves.reconcile_curve(self.backend, self.record())["outcome"], "unknown")

    def test_target_or_layer_drift_is_rejected_before_writes(self):
        for part in ("variant", "layer"):
            with self.subTest(part=part):
                self.backend.target = copy.deepcopy(self.before["target"])
                if part == "variant": self.backend.target["variant"]["id"] = "other"
                else: self.backend.target["layer"] = {"runtime_id": [1], "name": "layer"}
                with self.assertRaises(NativeAdapterError): self.apply()
        self.assertEqual(self.backend.actions, [])

    def test_saved_baseline_and_native_geometry_must_agree_before_dispatch(self):
        self.backend.ui_override = geometry("rgb", [[0, 0], [1, 0.9]])
        with self.assertRaises(NativeAdapterError) as raised: self.apply()
        self.assertEqual(raised.exception.code, "CURVE_GEOMETRY_MISMATCH")
        self.assertEqual(self.backend.actions, [])

    def test_observed_real_report_endpoints_fit_only_geometry_not_numeric_api(self):
        ui = geometry("rgb", [[0, 0], [1, 1]])
        ui["canvas"]["rect"] = {"x": 3241, "y": 1402, "width": 399, "height": 296}
        ui["points"] = [{"name": "-3,-3", "runtime_id": [7, 100], "rect": {"x": 3237, "y": 1692, "width": 10, "height": 10}},
                        {"name": "262,193.33333333333334", "runtime_id": [7, 101], "rect": {"x": 3634, "y": 1398, "width": 10, "height": 10}}]
        evidence = curves.verify_geometry(ui, [[0, 0], [1, 1]], channel="rgb")
        self.assertEqual(evidence["max_error_pixels"], 1)
        self.assertFalse(evidence["native_numeric_readback"])
        with self.assertRaises(NativeAdapterError): curves.verify_geometry(ui, [[0, 0], [1, 0.98]])

    def test_preset_slots_are_bounded_and_alternate_with_hash_ownership(self):
        for cycle in range(3):
            for channel in curves.CHANNELS:
                receipt = curves.stage_preset(self.backend, channel, [[0, 0], [0.5, 0.5 + cycle / 10], [1, 1]])
                self.assertEqual(receipt["slot"], "B" if cycle == 1 else "A")
        root, manifest = curves._roots(self.backend)
        self.assertEqual(len(list(root.glob("*.copreset"))), 10)
        saved = json.loads(manifest.read_text())
        self.assertEqual(len(saved["slots"]), 10)
        self.assertEqual(len(list(root.iterdir())), 10)

    def test_existing_unowned_or_changed_owned_file_is_not_overwritten(self):
        root, _ = curves._roots(self.backend); root.mkdir(parents=True)
        user = root / "C1AI-red-A.copreset"; user.write_bytes(b"user-owned")
        with self.assertRaises(NativeAdapterError): curves.stage_preset(self.backend, "red", [[0, 0], [1, 1]])
        self.assertEqual(user.read_bytes(), b"user-owned")
        self.assertEqual(self.backend.actions, [])
        receipt = curves.stage_preset(self.backend, "blue", [[0, 0], [1, 1]])
        curves.stage_preset(self.backend, "blue", [[0, 0], [0.5, 0.6], [1, 1]])
        owned = Path(receipt["path"]); owned.write_bytes(b"external-edit")
        with self.assertRaises(NativeAdapterError): curves.stage_preset(self.backend, "blue", [[0, 0], [1, 1]])
        self.assertEqual(owned.read_bytes(), b"external-edit")

    def test_symlink_or_hardlink_slot_is_never_replaced(self):
        root, _ = curves._roots(self.backend); root.mkdir(parents=True)
        external = self.local / "user-preset"; external.write_bytes(b"user")
        path = root / "C1AI-red-A.copreset"
        try: path.hardlink_to(external)
        except OSError: self.skipTest("Hardlinks unavailable")
        with self.assertRaises(NativeAdapterError): curves.stage_preset(self.backend, "red", [[0, 0], [1, 1]])
        self.assertEqual(external.read_bytes(), b"user")

    def test_journal_failure_occurs_before_any_file_change(self):
        with patch.object(self.backend, "action", side_effect=RuntimeError("journal write failed")):
            with self.assertRaises(RuntimeError): curves.stage_preset(self.backend, "rgb", [[0, 0], [1, 1]])
        root, manifest = curves._roots(self.backend)
        self.assertFalse(root.exists()); self.assertFalse(manifest.exists())

    def test_apply_python_preflight_rejection_is_known_not_dispatched(self):
        original = self.backend.action
        def action(name, function, intent=None):
            if name == "curves.apply_preset":
                Path(intent["receipt"]["path"]).write_bytes(b"external edit before dispatch")
            return original(name, function, intent)
        with patch.object(self.backend, "action", side_effect=action):
            with self.assertRaises(NativeAdapterError): self.apply()
        self.assertEqual(self.backend.actions[-1]["name"], "curves.apply_preset")
        self.assertEqual(self.backend.actions[-1]["status"], "not_dispatched")
        self.assertFalse(any(name == "curve_apply_preset" for name, _ in self.backend.calls))
        self.assertEqual(curves.reconcile_curve(self.backend, self.record())["outcome"], "not_applied")

    def test_stage_readonly_preflight_rejection_is_known_not_dispatched(self):
        original = self.backend.action
        def action(name, function, intent=None):
            path = Path(intent["receipt"]["path"]); path.parent.mkdir(parents=True)
            path.write_bytes(b"user-created between plan and dispatch")
            return original(name, function, intent)
        with patch.object(self.backend, "action", side_effect=action):
            with self.assertRaises(NativeAdapterError): curves.stage_preset(self.backend, "red", self.patch["points"])
        self.assertEqual(self.backend.actions[-1]["status"], "not_dispatched")
        self.assertFalse(curves._roots(self.backend)[1].exists())

    def test_failed_first_file_stage_keeps_bounded_auditable_slot_and_no_native_apply(self):
        original = curves._atomic_write
        def failure(path, data):
            if path.suffix == ".copreset": raise OSError("synthetic crash after manifest")
            return original(path, data)
        with patch.object(curves, "_atomic_write", side_effect=failure):
            with self.assertRaises(OSError): self.apply()
        before_count = len(self.backend.actions)
        result = curves.reconcile_curve(self.backend, self.record())
        self.assertEqual(result["outcome"], "not_applied")
        self.assertEqual(len(self.backend.actions), before_count)
        self.assertTrue(result["evidence"]["navigation_or_artifact_actions_recorded"])
        # Explicit later stages alternate B then the absent, registered A;
        # no user file is silently overwritten and the slot count stays fixed.
        self.assertEqual(curves.stage_preset(self.backend, "red", self.patch["points"])["slot"], "B")
        self.assertEqual(curves.stage_preset(self.backend, "red", self.patch["points"])["slot"], "A")

    def test_no_apply_dispatch_can_reconcile_navigation_without_replaying(self):
        self.backend.missing_menu = True
        with self.assertRaises(NativeAdapterError): self.apply()
        count = len(self.backend.actions)
        result = curves.reconcile_curve(self.backend, self.record())
        self.assertEqual(result["outcome"], "not_applied")
        self.assertTrue(result["evidence"]["curve_apply_not_dispatched"])
        self.assertEqual(len(self.backend.actions), count)

    def test_lost_apply_response_needs_matching_ui_and_all_saved_channels(self):
        self.backend.fail_after_apply = True
        with self.assertRaises(NativeAdapterError): self.apply()
        count = len(self.backend.actions)
        result = curves.reconcile_curve(self.backend, self.record())
        self.assertEqual(result["outcome"], "applied_not_restored")
        self.assertEqual(len(self.backend.actions), count)
        self.backend.ui_override = geometry("red", [[0, 0], [1, 1]])
        with self.assertRaises(NativeAdapterError): curves.reconcile_curve(self.backend, self.record())

    def test_accepted_apply_with_unchanged_baseline_stays_unknown(self):
        self.backend.do_not_apply = True
        with self.assertRaises(NativeAdapterError): self.apply()
        self.assertEqual(curves.reconcile_curve(self.backend, self.record())["outcome"], "unknown")

    def test_completed_wrong_ledger_or_tampered_slot_cannot_clear_pending(self):
        self.backend.fail_after_apply = True
        with self.assertRaises(NativeAdapterError): self.apply()
        record = self.record(); record["status"] = "completed"
        with self.assertRaises(NativeAdapterError): curves.reconcile_curve(self.backend, record)
        record = self.record(); record["actions"][-1]["intent"]["before"]["state"] = {}
        self.assertEqual(curves.reconcile_curve(self.backend, record)["outcome"], "unknown")
        path = Path(self.backend.actions[-1]["intent"]["receipt"]["path"]); path.write_bytes(b"external")
        self.assertEqual(curves.reconcile_curve(self.backend, self.record())["outcome"], "unknown")

    def test_invalid_points_channel_and_timeout_never_create_an_action(self):
        for invalid in ({"channel": "cyan", "points": [[0, 0], [1, 1]]},
                        {"channel": "red", "points": [[0, 0], [0, 0.5], [1, 1]]},
                        {"channel": "red", "points": [[0, 0], [1, float("nan")]]},
                        {"channel": "red", "points": [[0, False], [1, 1]]}):
            with self.subTest(invalid=invalid), self.assertRaises(NativeAdapterError):
                curves.apply_curve(self.backend, invalid, self.before)
        with self.assertRaises(NativeAdapterError): curves.apply_curve(self.backend, self.patch, self.before, timeout=11)
        self.assertEqual(self.backend.actions, [])

    def test_native_module_routes_curve_read_write_and_original_record_recovery(self):
        from c1ai import native_modules
        with patch.object(curves, "observe_curves", return_value={"synthetic": "read"}) as observe:
            self.assertEqual(native_modules.observe_module(self.backend, "curves"), {"synthetic": "read"})
            observe.assert_called_once_with(self.backend)
        with patch.object(curves, "apply_curve", return_value={"synthetic": "write"}) as apply:
            self.assertEqual(native_modules.adjust_module(self.backend, "curves", self.patch, self.before), {"synthetic": "write"})
            apply.assert_called_once_with(self.backend, self.patch, self.before)
        record = self.record()
        with patch.object(curves, "reconcile_curve", return_value={"synthetic": "recover"}) as recover:
            self.assertEqual(native_modules.reconcile_module(self.backend, record), {"synthetic": "recover"})
            recover.assert_called_once_with(self.backend, record)

    def test_windows_call_permits_curve_ops_and_only_trusted_predispatch_errors_mark_false(self):
        from c1ai.windows_backend import WindowsBackend, NativeError
        backend = WindowsBackend.__new__(WindowsBackend)
        backend.window = {"pid": 7, "hwnd": 8}
        responses = []
        def request(command, data, timeout):
            responses.append(json.loads(data))
            return SimpleNamespace(stdout=json.dumps({"ok": True, "result": {"synthetic": True}}).encode())
        backend.worker = SimpleNamespace(host_path=Path("synthetic.ps1"), request=request)
        for operation in ("curve_state", "curve_select", "curve_preset_menu", "curve_apply_preset"):
            self.assertTrue(backend.call(operation)["synthetic"])
            self.assertEqual(responses[-1]["op"], operation)
        for mutated in (True, False):
            backend.worker.request = lambda *args: SimpleNamespace(stdout=json.dumps({"ok": False, "error": {
                "code": "SYNTHETIC", "message": "synthetic native error", "possibly_mutated": mutated}}).encode())
            for operation in ("curve_select", "curve_preset_menu", "curve_apply_preset"):
                with self.assertRaises(NativeError) as caught: backend.call(operation)
                self.assertEqual(getattr(caught.exception, "dispatched", None), None if mutated else False)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell file validation")
    def test_powershell_slot_validation_holds_read_lock_and_blocks_extra_keys(self):
        receipt = curves.stage_preset(self.backend, "red", self.patch["points"])
        source = Path(curves.__file__).parent / "native" / "curve.ps1"
        def quoted(value): return "'" + str(value).replace("'", "''") + "'"
        code = f"""$ErrorActionPreference='Stop'
function Fail($Code,$Message,$Details) {{ throw ($Code+': '+$Message) }}
. {quoted(source)}
$file=Curve-PresetFile {quoted(receipt['name'])} {quoted(receipt['sha256'])} 'red'
try {{
  $blocked=$false
  try {{$writer=[IO.File]::Open($file.path,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite);$writer.Dispose()}} catch {{$blocked=$true}}
  if(-not $blocked){{throw 'File was writable while preparing native invoke'}}
}} finally {{$file.stream.Dispose()}}
$text=[IO.File]::ReadAllText({quoted(receipt['path'])})
$text=$text.Replace('</SL>','<E K="Exposure" V="1" /></SL>')
[IO.File]::WriteAllText({quoted(receipt['path'])},$text,[Text.UTF8Encoding]::new($false))
$sha=[Security.Cryptography.SHA256]::Create()
try{{$hash=([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes({quoted(receipt['path'])})))).Replace('-','').ToLowerInvariant()}}finally{{$sha.Dispose()}}
$rejected=$false
try {{$unexpected=Curve-PresetFile {quoted(receipt['name'])} $hash 'red';$unexpected.stream.Dispose()}} catch {{$rejected=$_.Exception.Message -like '*CURVE_PRESET_UNOWNED*'}}
if(-not $rejected){{throw 'Extra preset setting was not rejected'}}
'synthetic-file-boundary-ok'
"""
        script = Path(self.directory.name) / "curve-file-test.ps1"; script.write_text(code, encoding="utf-8-sig")
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        done = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-File", str(script)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", errors="replace"))
        self.assertIn(b"synthetic-file-boundary-ok", done.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell synthetic Invoke boundary")
    def test_powershell_apply_has_one_fake_invoke_after_guard_and_file_hash_checks(self):
        receipt = curves.stage_preset(self.backend, "red", self.patch["points"])
        source = Path(curves.__file__).parent / "native" / "curve.ps1"
        def quoted(value): return "'" + str(value).replace("'", "''") + "'"
        code = f"""$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Fail($Code,$Message,$Details) {{ throw ($Code+': '+$Message) }}
function Field($Object,$Name,$Default=$null) {{if($Object.Contains($Name)){{return $Object[$Name]}};return $Default}}
function Names($Object) {{return @($Object.Keys)}}
function Same($A,$B) {{return (($A|ConvertTo-Json -Depth 30 -Compress) -ceq ($B|ConvertTo-Json -Depth 30 -Compress))}}
function Check-Guard($Guard) {{$script:guardCalls++}}
. {quoted(source)}
$script:WindowInfo=@{{pid=999;hwnd=888;title='synthetic';version='fake'}}
$script:guardCalls=0;$script:invokeCalls=0;$script:MayHaveMutated=$false
$script:fakeState=@{{selected_channel='red'}}
$script:fakeMenu=@{{anchor_runtime_id=@(1,2);scope='curve_presets_descendants';items=@(@{{name={quoted(receipt['name'])};runtime_id=@(1,3);enabled=$true;offscreen=$false}})}}
function Curve-State {{return $script:fakeState}}
function Curve-PresetMenu {{return $script:fakeMenu}}
function Curve-PresetAnchor {{return 'synthetic-anchor'}}
$script:fakeElement=[pscustomobject]@{{Current=[pscustomobject]@{{ControlType=[System.Windows.Automation.ControlType]::MenuItem}}}}
$script:fakeElement|Add-Member ScriptMethod GetRuntimeId {{return ,@(1,3)}}
function Curve-One($Root,$Condition,$Description) {{return $script:fakeElement}}
$script:fakePattern=[pscustomobject]@{{}}
$script:fakePattern|Add-Member ScriptMethod Invoke {{$script:invokeCalls++}}
function Pattern($Element,$Name) {{if($Name -cne 'Invoke'){{throw 'Unexpected pattern'}};return $script:fakePattern}}
$request=@{{op='curve_apply_preset';channel='red';guard=@{{synthetic=$true}};expected_curve=$script:fakeState;expected_menu=$script:fakeMenu;preset_name={quoted(receipt['name'])};preset_sha256={quoted(receipt['sha256'])}}}
$result=Apply-CurvePreset $request
if(-not $result.accepted -or $script:invokeCalls -ne 1 -or $script:guardCalls -lt 3 -or -not $script:MayHaveMutated){{throw 'Single-invoke contract failed'}}
$script:invokeCalls=0;$script:MayHaveMutated=$false;$request.preset_sha256=('0'*64)
$failed=$false
try {{$unexpected=Apply-CurvePreset $request}} catch {{$failed=$_.Exception.Message -like '*CURVE_PRESET_CHANGED*'}}
if(-not $failed -or $script:invokeCalls -ne 0 -or $script:MayHaveMutated){{throw 'Hash mismatch dispatched an invoke'}}
'synthetic-invoke-boundary-ok'
"""
        script = Path(self.directory.name) / "curve-invoke-test.ps1"; script.write_text(code, encoding="utf-8-sig")
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        done = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-File", str(script)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", errors="replace"))
        self.assertIn(b"synthetic-invoke-boundary-ok", done.stdout)


if __name__ == "__main__":
    unittest.main()
