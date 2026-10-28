import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from c1ai.native_images import NativeAdapterError
from c1ai.native_preview import (EXPORT, SRGB, export_settings, prepare_export,
                                read_export, recover_export, recover_preview_navigation, submit_export, verify_recipe)


ROW = {"name": "P1.C1.ViewModels.Tools.Export.RecipeItemViewModel", "control_type": "ListItem", "class_name": "ListBoxItem"}
PARENTS = [{"automation_id": "listViewControl", "control_type": "List"},
           {"control_type": "Pane", "class_name": "ScrollViewer"},
           {"automation_id": "ExportMultipleRecipeView", "control_type": "Custom"},
           {"automation_id": "expander", "control_type": "Group"},
           {"automation_id": "Self", "control_type": "Custom"},
           {"automation_id": "PinnedToolsPanel", "control_type": "Custom"},
           {"automation_id": "InspectorPaletteTab", "control_type": "Custom"}]


class PreviewBackend:
    def __init__(self, directory):
        self.directory = directory
        self.state_dir = Path(directory) / "owner-state"
        self.state_dir.mkdir()
        self.window = {"pid": 7, "hwnd": 8, "title": "Synthetic", "version": "16.8.6"}
        self.target = {"variant": {"id": "owned-id", "label": "a.NEF"}}
        self.selected = "TIFF"
        self.actions = []
        self.action_records = []
        self.enabled = [False, True]
        self.before_mutation = None
        self.after_mutation = None
        self.truncated = False
        self.format = "JPEG"
        self.render = False
        self.opened = True
        self.parent_edges = True
        self.inspections = []

    def observe(self, fields):
        return {"target": copy.deepcopy(self.target), "values": {},
                "raw_state": {"window": copy.deepcopy(self.window), "values": {"selection": {"name": "a.NEF"}}}}

    def guard(self, observation):
        return {"bindings": {}, "expected": {}, "window_title": self.window["title"]}

    def action(self, name, fn, intent=None):
        if self.before_mutation:
            self.before_mutation(name)
        self.actions.append(name)
        self.action_records.append({"name": name, "intent": copy.deepcopy(intent), "status": "dispatching"})
        result = fn()
        if self.after_mutation:
            self.after_mutation(name)
        self.action_records[-1]["status"] = "completed"
        return result

    def nodes(self, only=None):
        rows, children = [], []
        for i, name in enumerate(("TIFF", "AI Preview")):
            if only is not None and name != only:
                continue
            rows.append({**ROW, "runtime_id": [7, i * 10], "selected": self.selected == name,
                         "enabled": True, "offscreen": False, "patterns": ["SelectionItem"],
                         "selector": {**ROW, "ancestors": [*copy.deepcopy(PARENTS), copy.deepcopy(EXPORT)]}})
            children.extend([
                {"automation_id": "NameLabel", "name": name, "control_type": "Text", "runtime_id": [7, i*10+1],
                 **({"parent_runtime_id": [7, i*10]} if self.parent_edges else {}),
                 "selector": {"automation_id": "NameLabel", "name": name, "ancestors": [copy.deepcopy(ROW), *copy.deepcopy(PARENTS)]}},
                {"automation_id": "EnabledCheckbox", "control_type": "CheckBox", "runtime_id": [7, i*10+2],
                 **({"parent_runtime_id": [7, i*10]} if self.parent_edges else {}),
                 "toggle": "On" if self.enabled[i] else "Off",
                 "selector": {"automation_id": "EnabledCheckbox", "ancestors": [copy.deepcopy(ROW), *copy.deepcopy(PARENTS)]}}])
        if only:
            return rows + children
        settings = [
            {"control_type": "ComboBox", "selection": [{"name": self.format}],
             "selector": {"previous_sibling": {"name": "格式"}, "ancestors": [{"class_name": "ExportFormatAndSizeToolView"}]}},
            {"control_type": "Text", "name": self.directory,
             "selector": {"control_type": "Text", "name": self.directory, "help_text": self.directory,
                          "previous_sibling": {"name": "样例路径"}, "ancestors": [copy.deepcopy(EXPORT)]}},
            {"control_type": "Text", "name": "preview.jpg", "selector": {"previous_sibling": {"name": "采样"}}},
            {"control_type": "MenuItem", "name": SRGB, "runtime_id": [7, 300],
             "selector": {"ancestors": [{"class_name": "OutputColorProfileMenuView"}]}},
            {"automation_id": "export", "control_type": "Button", "name": "导出 1 个图像",
             "selector": {"automation_id": "export", "control_type": "Button", "name": "导出 1 个图像"}}]
        return rows + children + settings

    def call(self, action, **payload):
        result = {"window": copy.deepcopy(self.window)}
        selector = payload.get("selector", {})
        if action == "read":
            if not self.opened:
                raise NativeAdapterError("CONTROL_NOT_FOUND", "not opened")
            if selector == EXPORT:
                return {**result, "node": {"automation_id": "export", "control_type": "Window"}}
            if selector.get("previous_sibling", {}).get("name") == "样例路径":
                return {**result, "node": {"control_type": "Text", "name": self.directory}}
        if action == "inspect":
            self.inspections.append(copy.deepcopy(selector))
            if not self.opened:
                raise NativeAdapterError("CONTROL_NOT_FOUND", "not opened")
            nodes = self.nodes(selector.get("child", {}).get("name"))
            return {**result, "nodes": nodes,
                    **({"profile": "export", "visited_nodes": len(nodes) + 20} if payload.get("profile") == "export" else {}),
                    "truncated": self.truncated, "read_errors": []}
        if action == "invoke":
            if payload["mode"] == "select":
                self.selected = selector["child"]["name"]
            elif selector.get("automation_id") == "MainButton":
                self.opened = True
            elif self.render:
                from PIL import Image, ImageCms
                exif = Image.Exif()
                exif[305] = "Capture One synthetic fixture"
                icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
                Image.new("RGB", (8, 6), (80, 120, 160)).save(Path(self.directory)/"preview.jpg", exif=exif, icc_profile=icc)
            return {**result, "accepted": True}
        raise AssertionError(action)


class NativePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = PreviewBackend(self.temp.name)
        self.output_root = self.backend.state_dir / "export-slot"
        self.output_root.mkdir()
        self.backend.directory = str(self.output_root)

    def prepare(self):
        return prepare_export(self.backend, "AI Preview", output_root=self.output_root)

    def render(self):
        self.backend.render = True
        receipt = submit_export(self.backend, self.prepare())
        result = read_export(self.backend, receipt, timeout=1, poll_interval=.001,
                             archive_dir=self.backend.state_dir / "renders")
        return receipt, result

    def test_checked_recipe_must_be_exact_configured_recipe(self):
        self.backend.enabled = [True, False]
        with self.assertRaises(NativeAdapterError):
            self.prepare()
        self.assertEqual(self.backend.actions, [])

    def test_recipe_selection_hook_then_fresh_settings(self):
        plan = self.prepare()
        self.assertEqual(self.backend.actions, ["preview.select_recipe"])
        self.assertTrue(plan["recipe_selection_changed"])
        self.assertEqual(plan["settings"]["output_path"], str(self.output_root/"preview.jpg"))
        self.assertEqual(self.backend.enabled, [False, True])

    def test_selected_recipe_uses_one_snapshot_then_one_new_dispatch_snapshot(self):
        self.backend.selected = "AI Preview"
        plan = self.prepare()
        self.assertEqual(self.backend.inspections, [EXPORT])
        self.assertEqual(plan["destination"]["changed"], False)
        submit_export(self.backend, plan)
        self.assertEqual(self.backend.inspections, [EXPORT, EXPORT])

    def test_recipe_selection_refreshes_once_after_mutation(self):
        self.prepare()
        self.assertEqual(self.backend.inspections, [EXPORT, EXPORT])
        self.assertEqual(self.backend.actions, ["preview.select_recipe"])

    def test_mismatched_parent_runtime_id_refuses_without_row_guessing(self):
        original_nodes = self.backend.nodes
        def corrupt(only=None):
            nodes = original_nodes(only)
            for node in nodes:
                if node.get("automation_id") == "EnabledCheckbox" and node.get("toggle") == "On":
                    node["parent_runtime_id"] = [7, 0]
            return nodes
        self.backend.nodes = corrupt
        with self.assertRaises(NativeAdapterError):
            self.prepare()
        self.assertEqual(self.backend.actions, [])

    def test_partially_missing_parent_edges_refuse_instead_of_silent_fallback(self):
        original_nodes = self.backend.nodes
        def missing(only=None):
            nodes = original_nodes(only)
            next(node for node in nodes if node.get("automation_id") == "NameLabel").pop("parent_runtime_id")
            return nodes
        self.backend.nodes = missing
        with self.assertRaises(NativeAdapterError):
            self.prepare()
        self.assertEqual(self.backend.actions, [])

    def test_old_worker_without_parent_edges_retains_scoped_verification(self):
        self.backend.parent_edges = False
        self.backend.selected = "AI Preview"
        plan = self.prepare()
        self.assertTrue(plan["destination"]["native_sample_path_verified"])
        self.assertEqual(len(self.backend.inspections), 3)
        self.assertIn("child", self.backend.inspections[1])

    def test_export_projection_requires_explicit_profile_and_complete_traversal_evidence(self):
        original = self.backend.call
        for missing in ("profile", "visited_nodes", "read_errors"):
            def incomplete(action, **payload):
                result = original(action, **payload)
                if action == "inspect":
                    result.pop(missing, None)
                return result
            self.backend.call = incomplete
            with self.subTest(missing=missing), self.assertRaises(NativeAdapterError) as caught:
                self.prepare()
            self.assertEqual(caught.exception.code, "INCOMPLETE_INSPECTION")
        self.assertEqual(self.backend.actions, [])

    def test_existing_output_blocks_export(self):
        (self.output_root/"preview.jpg").write_bytes(b"owned by somebody else")
        with self.assertRaises(NativeAdapterError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
        self.assertNotIn("preview.export", self.backend.actions)

    def test_recipe_change_after_plan_is_not_silently_fixed(self):
        plan = self.prepare()
        self.backend.selected = "TIFF"
        with self.assertRaises(NativeAdapterError):
            submit_export(self.backend, plan)
        self.assertEqual(self.backend.actions, ["preview.select_recipe"])

    def test_target_change_before_dispatch_prevents_export(self):
        plan = self.prepare()
        self.backend.target["variant"]["id"] = "other"
        with self.assertRaises(NativeAdapterError) as caught:
            submit_export(self.backend, plan)
        self.assertEqual(caught.exception.code, "PREVIEW_TARGET_CHANGED")
        self.assertNotIn("preview.export", self.backend.actions)

    def test_truncated_recipe_inspection_refuses(self):
        self.backend.truncated = True
        with self.assertRaises(NativeAdapterError):
            self.prepare()
        self.assertEqual(self.backend.actions, [])

    def test_guard_hook_failure_dispatches_no_export(self):
        plan = self.prepare()
        def reject(name):
            raise RuntimeError("writeahead failed")
        self.backend.before_mutation = reject
        with self.assertRaises(RuntimeError):
            submit_export(self.backend, plan)
        self.assertNotIn("preview.export", self.backend.actions)

    def test_open_dialog_also_passes_through_hook(self):
        self.backend.opened = False
        self.prepare()
        self.assertEqual(self.backend.actions, ["preview.open_export", "preview.select_recipe"])

    def navigation_record(self):
        before = self.backend.observe([])
        return {"status": "partial", "request": {"command": "preview", "action": "render"},
                "before": before, "target": before["target"],
                "actions": [{"name": "preview.open_export", "status": "completed", "result": {"accepted": True},
                             "intent": {"before": copy.deepcopy(before)}}]}

    def test_navigation_only_recovery_is_readonly_and_does_not_claim_parameters(self):
        result = recover_preview_navigation(self.backend, self.navigation_record())
        self.assertEqual(result["outcome"], "not_applied")
        self.assertTrue(result["evidence"]["navigation_applied"])
        self.assertFalse(result["evidence"]["export_dispatched"])
        self.assertFalse(result["evidence"]["unobserved_parameters_verified"])
        self.assertEqual(self.backend.actions, [])
        self.assertEqual(self.backend.inspections, [])

    def test_navigation_recovery_rejects_nonpending_missing_baseline_or_other_actions(self):
        for change in ("completed", "before", "action", "result"):
            with self.subTest(change=change):
                record = self.navigation_record()
                if change == "completed":
                    record["status"] = "completed"
                elif change == "before":
                    record["actions"][0]["intent"]["before"]["values"] = {"exposure": 1}
                elif change == "action":
                    record["actions"].append({"name": "preview.export"})
                else:
                    record["actions"][0]["result"] = {}
                self.assertEqual(recover_preview_navigation(self.backend, record)["outcome"], "unknown")
        self.assertEqual(self.backend.actions, [])

    def refused_directory_record(self, previous_entry):
        record = self.navigation_record()
        record["actions"].append({"name": "preview.destination_menu", "status": "not_dispatched",
                                  "error": {"dispatched": False, "code": "STALE_TARGET"},
                                  "intent": {"before": copy.deepcopy(record["before"]), "metadata_key": "key",
                                             "metadata_entry_before": copy.deepcopy(previous_entry)}})
        return record

    def test_navigation_recovery_accepts_certified_refusal_only_after_metadata_rollback(self):
        previous = {"status": "restored", "cycle": 1, "source_evidence": [{"sha256": "b" * 64}]}
        path = self.backend.state_dir / "export-destination.json"
        path.write_text(json.dumps({"schema": "c1ai-export-destination-v1", "destinations": {"key": previous}}), encoding="utf-8")
        saved = path.read_bytes()
        result = recover_preview_navigation(self.backend, self.refused_directory_record(previous))
        self.assertEqual(result["outcome"], "not_applied")
        self.assertTrue(result["evidence"]["navigation_applied"])
        self.assertTrue(result["evidence"]["metadata_rollback_verified"])
        self.assertEqual(result["evidence"]["not_dispatched_actions"], ["preview.destination_menu"])
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.backend.actions, [])

    def test_navigation_recovery_refuses_unknown_dispatch_or_incomplete_metadata_rollback(self):
        previous = {"status": "restored", "cycle": 1}
        path = self.backend.state_dir / "export-destination.json"
        path.write_text(json.dumps({"schema": "c1ai-export-destination-v1", "destinations": {"key": previous}}), encoding="utf-8")
        for change in ("unknown", "completed", "missing_marker", "false_only_in_details", "missing_baseline", "wrong_metadata"):
            with self.subTest(change=change):
                record = self.refused_directory_record(previous)
                action = record["actions"][1]
                if change in ("unknown", "completed"):
                    action["status"] = change
                elif change == "missing_marker":
                    action["error"] = {}
                elif change == "false_only_in_details":
                    action["error"] = {"details": {"possibly_mutated": False}}
                elif change == "missing_baseline":
                    action["intent"].pop("metadata_entry_before")
                else:
                    action["intent"]["metadata_entry_before"]["cycle"] = 2
                self.assertEqual(recover_preview_navigation(self.backend, record)["outcome"], "unknown")
        self.assertEqual(self.backend.actions, [])

    def test_navigation_refusal_with_no_previous_metadata_requires_absent_key(self):
        record = self.refused_directory_record(None)
        self.assertEqual(recover_preview_navigation(self.backend, record)["outcome"], "not_applied")
        path = self.backend.state_dir / "export-destination.json"
        path.write_text(json.dumps({"schema": "c1ai-export-destination-v1", "destinations": {"key": None}}), encoding="utf-8")
        self.assertEqual(recover_preview_navigation(self.backend, record)["outcome"], "unknown")

    def test_timeout_preserves_receipt_and_readback_never_replays(self):
        receipt = submit_export(self.backend, self.prepare())
        with self.assertRaises(NativeAdapterError) as caught:
            read_export(self.backend, receipt, timeout=0)
        self.assertEqual(caught.exception.code, "EXPORT_PENDING")
        self.assertEqual(caught.exception.details["receipt"], receipt)
        with self.assertRaises(NativeAdapterError):
            read_export(self.backend, receipt, timeout=0)
        self.assertEqual(self.backend.actions.count("preview.export"), 1)

    def test_full_decode_and_identity_check_preserve_export_file(self):
        self.backend.render = True
        receipt = submit_export(self.backend, self.prepare())
        result = read_export(self.backend, receipt, timeout=1, poll_interval=.001,
                             archive_dir=self.backend.state_dir / "renders")
        self.assertTrue(result["render_verified"])
        self.assertFalse(result["quality_approved"])
        self.assertEqual(result["image"]["width"], 8)
        self.assertTrue((self.output_root/"preview.jpg").is_file())
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(self.backend.actions.count("preview.export"), 1)

    def test_export_receipt_is_recorded_before_native_dispatch(self):
        plan = self.prepare()
        original_call = self.backend.call
        def verify_recorded(action, **payload):
            if action == "invoke" and payload["selector"].get("automation_id") == "export":
                saved = self.backend.action_records[-1]["intent"]["receipt"]
                self.assertEqual(saved["before"], plan["before"])
                self.assertEqual(saved["settings"], plan["settings"])
                self.assertNotIn("dispatch", saved)
            return original_call(action, **payload)
        self.backend.call = verify_recorded
        receipt = submit_export(self.backend, plan)
        self.assertIn("dispatch", receipt)
        self.assertNotIn("dispatch", self.backend.action_records[-1]["intent"]["receipt"])

    def test_output_appearing_during_writeahead_prevents_native_export(self):
        plan = self.prepare()
        def create_file(name):
            if name == "preview.export":
                Path(plan["settings"]["output_path"]).write_bytes(b"new external output")
        self.backend.before_mutation = create_file
        original_call, invoked = self.backend.call, []
        def count_invokes(action, **payload):
            if action == "invoke":
                invoked.append(payload)
            return original_call(action, **payload)
        self.backend.call = count_invokes
        with self.assertRaises(NativeAdapterError) as caught:
            submit_export(self.backend, plan)
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(invoked, [])

    def test_recovery_reads_saved_receipt_after_lost_dispatch_reply(self):
        self.backend.render = True
        plan = self.prepare()
        def lost_reply(name):
            if name == "preview.export":
                raise RuntimeError("native reply lost")
        self.backend.after_mutation = lost_reply
        with self.assertRaises(RuntimeError):
            submit_export(self.backend, plan)
        record = {"target": plan["before"]["target"], "actions": self.backend.action_records, "status": "outcome_unknown"}
        recovered = recover_export(self.backend, record, timeout=1)
        self.assertEqual(recovered["outcome"], "applied_not_restored")
        self.assertFalse(recovered["evidence"]["replayed"])
        self.assertEqual(self.backend.actions.count("preview.export"), 1)

    def test_owned_output_reuse_unlinks_only_matching_scratch_and_keeps_archive(self):
        _, result = self.render()
        archive = Path(result["path"])
        unrelated = Path(self.temp.name) / "keep.txt"
        unrelated.write_text("unrelated", encoding="utf-8")
        plan = self.prepare()
        self.assertTrue(plan["owned_output_released"])
        self.assertFalse(Path(result["export_path"]).exists())
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), result["image"]["sha256"])
        self.assertEqual(unrelated.read_text("utf-8"), "unrelated")
        cleanup = self.backend.action_records[-1]
        self.assertEqual(cleanup["name"], "preview.release_owned_export")
        self.assertEqual(cleanup["intent"]["owned_export"]["export_path"], result["export_path"])
        self.assertEqual(json.loads((self.backend.state_dir / "export-owned.json").read_text("utf-8"))["files"], {})

    def test_changed_scratch_is_never_cleaned(self):
        _, result = self.render()
        path = Path(result["export_path"])
        path.write_bytes(b"now belongs to another writer")
        with self.assertRaises(NativeAdapterError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "EXPORT_OWNERSHIP_INVALID")
        self.assertTrue(path.exists())
        self.assertNotIn("preview.release_owned_export", self.backend.actions)

    def test_changed_archive_prevents_scratch_cleanup(self):
        _, result = self.render()
        Path(result["path"]).write_bytes(b"archive damaged")
        with self.assertRaises(NativeAdapterError):
            self.prepare()
        self.assertTrue(Path(result["export_path"]).exists())

    def test_changed_expected_root_does_not_grant_cleanup_authority(self):
        _, result = self.render()
        with self.assertRaises(NativeAdapterError) as caught:
            prepare_export(self.backend, "AI Preview", output_root=Path(self.temp.name).parent)
        self.assertEqual(caught.exception.code, "PREVIEW_PATH_OUTSIDE_ROOT")
        self.assertTrue(Path(result["export_path"]).exists())

    def test_default_root_uses_code_bound_slot_and_verified_hash_lease(self):
        _, result = self.render()
        plan = prepare_export(self.backend, "AI Preview")
        self.assertEqual(plan["output_root"], str(self.output_root))
        self.assertTrue(plan["owned_output_released"])
        self.assertFalse(Path(result["export_path"]).exists())

    def test_cleanup_rechecks_ownership_after_action_hook(self):
        _, result = self.render()
        def change_file(name):
            if name == "preview.release_owned_export":
                Path(result["export_path"]).write_bytes(b"changed after writeahead")
        self.backend.before_mutation = change_file
        with self.assertRaises(NativeAdapterError):
            self.prepare()
        self.assertEqual(Path(result["export_path"]).read_bytes(), b"changed after writeahead")

    def test_cleanup_hook_failure_preserves_source_and_archive(self):
        _, result = self.render()
        def reject(name):
            raise RuntimeError("journal unavailable")
        self.backend.before_mutation = reject
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertTrue(Path(result["export_path"]).exists())
        self.assertTrue(Path(result["path"]).exists())

    def test_symlink_to_owned_archive_is_not_cleanup_eligible(self):
        _, result = self.render()
        source = Path(result["export_path"])
        source.unlink()
        try:
            source.symlink_to(result["path"])
        except OSError:
            self.skipTest("Windows account cannot create symbolic links")
        with self.assertRaises(NativeAdapterError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "PREVIEW_PATH_UNVERIFIED")
        self.assertTrue(source.is_symlink())
        self.assertTrue(Path(result["path"]).is_file())

    def test_symlink_ancestor_is_rejected_before_cleanup(self):
        actual = Path(self.temp.name) / "actual-output"
        actual.mkdir()
        linked = Path(self.temp.name) / "linked-output"
        try:
            linked.symlink_to(actual, target_is_directory=True)
        except OSError:
            self.skipTest("Windows account cannot create symbolic links")
        self.backend.directory = str(linked)
        with self.assertRaises(NativeAdapterError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "PREVIEW_PATH_UNVERIFIED")
        self.assertNotIn("preview.release_owned_export", self.backend.actions)

    def test_hardlinked_owned_output_is_not_cleanup_eligible(self):
        _, result = self.render()
        linked = Path(self.temp.name) / "another-reference.jpg"
        linked.hardlink_to(result["export_path"])
        with self.assertRaises(NativeAdapterError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "EXPORT_OWNERSHIP_INVALID")
        self.assertTrue(Path(result["export_path"]).is_file())
        self.assertTrue(linked.is_file())

    def test_recovery_refuses_duplicate_receipts_and_long_wait(self):
        receipt = submit_export(self.backend, self.prepare())
        record = {"target": receipt["before"]["target"], "actions": self.backend.action_records * 2, "status": "partial"}
        with self.assertRaises(NativeAdapterError):
            recover_export(self.backend, record, timeout=0)
        with self.assertRaises(NativeAdapterError):
            recover_export(self.backend, record, timeout=6)
        self.assertEqual(self.backend.actions.count("preview.export"), 1)

    def test_completed_export_never_recovers_from_reused_scratch_path(self):
        receipt, result = self.render()
        record = {"target": receipt["before"]["target"], "actions": self.backend.action_records, "status": "completed"}
        self.prepare()
        Path(result["export_path"]).write_bytes(b"new unrelated export")
        with self.assertRaises(NativeAdapterError) as caught:
            recover_export(self.backend, record, timeout=0)
        self.assertEqual(caught.exception.code, "EXPORT_RECOVERY_UNVERIFIED")

    def test_wrong_target_after_export_is_not_verified(self):
        self.backend.render = True
        receipt = submit_export(self.backend, self.prepare())
        self.backend.target["variant"]["id"] = "other"
        with self.assertRaises(NativeAdapterError) as caught:
            read_export(self.backend, receipt, timeout=1, poll_interval=.001)
        self.assertEqual(caught.exception.code, "PREVIEW_TARGET_CHANGED")


if __name__ == "__main__":
    unittest.main()
