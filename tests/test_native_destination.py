import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from c1ai.native_images import NativeAdapterError
from c1ai.native_preview import (EXPORT, build_historical_destination_metadata, ensure_owned_destination,
                                export_settings, prepare_export,
                                restore_destination, verify_recipe)
from test_native_preview import PreviewBackend


class DestinationBackend(PreviewBackend):
    def __init__(self, directory):
        super().__init__(directory)
        self.selected = "AI Preview"
        self.config = {"preview_recipe": "AI Preview"}
        self.target["document"] = {"path": "synthetic.cosessiondb"}
        self.picker = {"dialog_hwnd": 123, "helper_pid": 456, "export_hwnd": 789,
                       "helper_started_at": "synthetic", "ok_hwnd": 42}
        self.picker_open = False
        self.navigation = None
        self.edit_text = ""
        self.choose_completes = False
        self.lose_choose_reply = False
        self.change_picker = False
        self.bad_navigation = False
        self.opening_reads = 0
        self.native_calls = []
        self.sample_bindings = []
        self.sample_suffix = ""
        self.previous_directory = directory
        self.root_label = "Session Output"
        self.restore_option_available = True
        self.restore_reply_lost = False
        self.legacy_confirm_stalls = False
        self.lose_current_reply = False

    def nodes(self, only=None):
        nodes = super().nodes(only)
        if only is not None:
            return nodes
        for node in nodes:
            if node.get("selector", {}).get("previous_sibling", {}).get("name") == "样例路径":
                node["name"] = self.directory + self.sample_suffix
                node["selector"].update(name=node["name"], help_text=node["name"], control_type="Text",
                                        class_name="TextBlock", ancestors=[{"class_name": "ExportDestinationToolView", "control_type": "Custom"}, copy.deepcopy(EXPORT)])
        nodes.append({"automation_id": "", "name": self.root_label, "control_type": "MenuItem",
                      "enabled": True, "offscreen": False, "patterns": ["ExpandCollapse"], "runtime_id": [7, 600],
                      "selector": {"name": self.root_label, "control_type": "MenuItem", "class_name": "MenuItem",
                                   "ancestors": [{"class_name": "ExportDestinationRootFolderSelector", "control_type": "Custom"}]}})
        return nodes

    def call(self, action, **payload):
        self.native_calls.append((action, copy.deepcopy(payload)))
        selector = payload.get("selector", {})
        result = {"window": copy.deepcopy(self.window)}
        if action == "read" and selector.get("previous_sibling", {}).get("name") == "样例路径":
            self.sample_bindings.append(copy.deepcopy(selector))
            if "name" in selector or "help_text" in selector:
                raise AssertionError("Mutable directory text remained bound")
            return {**result, "node": {"name": self.directory + self.sample_suffix, "control_type": "Text"}}
        if action == "read" and selector.get("name") == "选择文件夹…":
            return {**result, "node": {"name": "选择文件夹…", "control_type": "MenuItem", "enabled": True,
                                       "offscreen": False, "patterns": ["Invoke"], "selector": copy.deepcopy(selector)}}
        if action == "read" and selector.get("name") == "Session Output" and selector.get("ancestors"):
            if not self.restore_option_available:
                raise NativeAdapterError("CONTROL_NOT_FOUND", "recorded option absent")
            return {**result, "node": {"name": "Session Output", "control_type": "MenuItem", "enabled": True,
                                       "offscreen": False, "patterns": ["Invoke"], "selector": copy.deepcopy(selector)}}
        if action == "invoke" and selector.get("name") == "Session Output" and payload.get("mode") == "invoke":
            self.directory, self.root_label = self.previous_directory, "Session Output"
            if self.restore_reply_lost:
                raise NativeAdapterError("ACTION_OUTCOME_UNKNOWN", "reply lost")
            return {**result, "accepted": True}
        if action == "invoke" and selector.get("name") == self.root_label and payload.get("mode") == "expand":
            return {**result, "accepted": True}
        if action == "invoke" and selector.get("name") == "选择文件夹…":
            self.picker_open = True
            return {**result, "accepted": True}
        if action == "inspect_folder_picker":
            if self.opening_reads:
                self.opening_reads -= 1
                raise NativeAdapterError("FOLDER_PICKER_UNAVAILABLE", "helper starting")
            if not self.picker_open:
                raise NativeAdapterError("FOLDER_PICKER_UNAVAILABLE", "closed")
            return {**result, "picker": copy.deepcopy(self.picker),
                    "navigation": {"folder_path": self.navigation or self.directory, "hierarchy_verified": True,
                                   "edit_text": self.edit_text}}
        if action == "choose_export_folder":
            self.navigation = self.directory if self.bad_navigation else payload["path"]
            self.edit_text = payload["path"]
            if self.choose_completes:
                self.directory, self.picker_open = payload["path"], False
                self.root_label = "Owned export-slot"
            if self.change_picker:
                self.picker["helper_pid"] = 999
            if self.lose_choose_reply:
                raise NativeAdapterError("FOLDER_SUBMIT_UNCERTAIN", "reply lost")
            return {**result, "accepted": True, "path_readback_verified": True, "destination_verified": False}
        if action == "confirm_export_folder":
            if not self.legacy_confirm_stalls:
                self.directory, self.picker_open = payload["path"], False
                self.root_label = "Owned export-slot"
            return {**result, "accepted": True, "navigation_completed": True, "destination_verified": False}
        if action == "select_current_export_folder":
            if payload.get("expected_edit_text") != self.edit_text:
                raise NativeAdapterError("FOLDER_EDIT_CHANGED", "native edit text changed")
            self.edit_text = ""
            self.directory, self.picker_open = payload["path"], False
            self.root_label = "Owned export-slot"
            if self.lose_current_reply:
                raise NativeAdapterError("FOLDER_SUBMIT_UNCERTAIN", "current-directory reply lost")
            return {**result, "accepted": True, "current_folder_selected": True, "destination_verified": False}
        return super().call(action, **payload)


class OwnedDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.backend = DestinationBackend(self.temp.name)

    def ensure(self):
        return ensure_owned_destination(self.backend, self.backend.observe([]))

    def test_already_owned_directory_has_no_ui_mutation(self):
        root = self.backend.state_dir / "export-slot"
        root.mkdir()
        self.backend.directory = str(root)
        result = self.ensure()
        self.assertFalse(result["changed"])
        self.assertTrue(result["native_sample_path_verified"])
        self.assertEqual(self.backend.actions, [])
        self.assertIsNone(result["previous_root"])

    def test_two_phase_setup_preserves_previous_root_and_user_file(self):
        original = Path(self.temp.name) / "preview.jpg"
        original.write_bytes(b"user-owned JPEG placeholder")
        original_call = self.backend.call
        def check_baseline(action, **payload):
            if action == "invoke" and payload.get("mode") == "expand":
                record = json.loads((self.backend.state_dir / "export-destination.json").read_text("utf-8"))
                self.assertEqual(next(iter(record["destinations"].values()))["previous_root"], self.temp.name)
                self.assertEqual(self.backend.action_records[-1]["status"], "dispatching")
            return original_call(action, **payload)
        self.backend.call = check_baseline
        result = self.ensure()
        self.assertTrue(result["changed"])
        self.assertEqual(self.backend.actions, ["preview.destination_menu", "preview.choose_directory",
                                                "preview.select_owned_directory", "preview.select_current_directory"])
        self.assertEqual(original.read_bytes(), b"user-owned JPEG placeholder")
        self.assertEqual(Path(result["settings"]["output_path"]).parent, self.backend.state_dir / "export-slot")
        for selector in self.backend.sample_bindings:
            self.assertNotIn("name", selector)
            self.assertNotIn("help_text", selector)
            self.assertEqual(selector["previous_sibling"]["name"], "样例路径")
            self.assertIn(EXPORT, selector["ancestors"])
        for action in self.backend.action_records:
            self.assertEqual(action["intent"]["oldroot"], self.temp.name)
            self.assertEqual(action["intent"]["path"], result["root"])
        state = json.loads(Path(result["metadata_path"]).read_text("utf-8"))
        self.assertEqual(next(iter(state["destinations"].values()))["status"], "selected")

    def test_completed_choose_does_not_send_second_confirmation(self):
        self.backend.choose_completes = True
        result = self.ensure()
        self.assertIsNone(result["confirmation"])
        self.assertNotIn("preview.select_current_directory", self.backend.actions)

    def test_unknown_choose_is_never_repeated_or_followed_by_confirmation(self):
        self.backend.lose_choose_reply = True
        with self.assertRaises(NativeAdapterError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.code, "FOLDER_SUBMIT_UNCERTAIN")
        with self.assertRaises(NativeAdapterError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.code, "DESTINATION_RECOVERY_REQUIRED")
        self.assertEqual(self.backend.actions.count("preview.select_owned_directory"), 1)
        self.assertNotIn("preview.select_current_directory", self.backend.actions)

    def test_navigation_and_picker_identity_must_match_before_confirmation(self):
        for field in ("bad_navigation", "change_picker"):
            with self.subTest(field=field), tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
                backend = DestinationBackend(directory)
                setattr(backend, field, True)
                with self.assertRaises(NativeAdapterError):
                    ensure_owned_destination(backend, backend.observe([]))
                self.assertNotIn("preview.select_current_directory", backend.actions)

    def test_waiting_for_picker_never_reopens_it(self):
        self.backend.opening_reads = 1
        self.ensure()
        self.assertEqual(self.backend.actions.count("preview.choose_directory"), 1)

    def test_journal_failure_prevents_any_native_destination_action(self):
        def reject(name):
            raise RuntimeError("journal failed")
        self.backend.before_mutation = reject
        with self.assertRaises(RuntimeError):
            self.ensure()
        self.assertFalse(any(action in ("invoke", "choose_export_folder", "confirm_export_folder")
                             for action, payload in self.backend.native_calls))
        self.assertFalse((self.backend.state_dir / "export-destination.json").exists())
        self.assertEqual(self.backend.action_records, [])

    def test_symlink_owned_slot_is_refused_before_any_ui_action(self):
        root = self.backend.state_dir / "export-slot"
        try:
            root.symlink_to(self.temp.name, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic link creation is unavailable")
        with self.assertRaises(NativeAdapterError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.code, "PREVIEW_PATH_UNVERIFIED")
        self.assertEqual(self.backend.actions, [])

    def test_recipe_drift_after_confirmation_is_not_reported_success(self):
        def drift(name):
            if name == "preview.select_current_directory":
                self.backend.enabled = [True, False]
        self.backend.after_mutation = drift
        with self.assertRaises(NativeAdapterError):
            self.ensure()
        state = json.loads((self.backend.state_dir / "export-destination.json").read_text("utf-8"))
        self.assertEqual(next(iter(state["destinations"].values()))["status"], "pending")

    def test_prepare_export_includes_initial_owned_destination_setup(self):
        plan = prepare_export(self.backend, "AI Preview", self.backend.observe([]))
        self.assertTrue(plan["destination"]["changed"])
        self.assertEqual(plan["output_root"], str(self.backend.state_dir / "export-slot"))
        self.assertEqual(self.backend.actions.count("preview.select_owned_directory"), 1)

    def test_restore_exact_previous_menu_choice_and_sample_path(self):
        self.ensure()
        result = restore_destination(self.backend, self.backend.observe([]))
        self.assertTrue(result["restored"])
        self.assertTrue(result["menu_choice_verified"])
        self.assertEqual(self.backend.directory, self.temp.name)
        self.assertEqual(self.backend.actions[-2:], ["preview.restore_destination_menu", "preview.restore_destination"])
        before = list(self.backend.actions)
        self.assertFalse(restore_destination(self.backend, self.backend.observe([]))["changed"])
        self.assertEqual(self.backend.actions, before)
        self.assertTrue(self.ensure()["changed"])

    def test_two_complete_setup_restore_cycles_preserve_previous_entry_and_sources(self):
        path = self.backend.state_dir / "export-destination.json"
        sources = [{"path": "original-report.json", "sha256": "a" * 64, "operation_id": "source-op"}]
        self.ensure()
        data = json.loads(path.read_text("utf-8"))
        initial = next(iter(data["destinations"].values()))
        self.assertEqual(initial["cycle"], 1)
        initial.update(source_evidence=sources, imported_at=123,
                       historical_menu_option_selector={"name": "Session Output", "control_type": "MenuItem"})
        path.write_text(json.dumps(data), encoding="utf-8")
        restore_destination(self.backend, self.backend.observe([]))
        restored = next(iter(json.loads(path.read_text("utf-8"))["destinations"].values()))
        self.assertEqual(restored["status"], "restored")
        before_actions = len(self.backend.action_records)
        self.assertTrue(self.ensure()["changed"])
        actions = self.backend.action_records[before_actions:]
        self.assertEqual([item["name"] for item in actions], ["preview.destination_menu", "preview.choose_directory",
                                                           "preview.select_owned_directory", "preview.select_current_directory"])
        self.assertEqual(actions[0]["intent"]["metadata_entry_before"], restored)
        self.assertEqual(actions[0]["intent"]["cycle"], 2)
        current = next(iter(json.loads(path.read_text("utf-8"))["destinations"].values()))
        self.assertEqual(current["cycle"], 2)
        self.assertEqual(current["source_evidence"], sources)
        self.assertEqual(current["historical_menu_option_selector"], initial["historical_menu_option_selector"])
        self.assertEqual(current["imported_at"], 123)
        self.assertNotIn("metadata_entry_before", current)
        first_expand = next(payload for action, payload in self.backend.native_calls
                            if action == "invoke" and payload.get("mode") == "expand")
        self.assertEqual(first_expand["guard"]["expected"]["export_destination"]["name"], self.temp.name)
        self.assertTrue(restore_destination(self.backend, self.backend.observe([]))["restored"])
        self.assertEqual(self.backend.actions.count("preview.select_owned_directory"), 2)
        self.assertEqual(self.backend.actions.count("preview.select_current_directory"), 2)
        self.assertEqual(self.backend.actions.count("preview.restore_destination"), 2)
        self.assertEqual(self.backend.directory, self.temp.name)

    @unittest.skipUnless(os.name == "nt", "Trailing backslash is a Windows path separator")
    def test_native_directory_guards_preserve_fresh_sample_trailing_backslash(self):
        self.backend.sample_suffix = "\\"
        original = self.backend.call
        verified = []
        def enforce_literal_name(action, **payload):
            expected = payload.get("guard", {}).get("expected", {}).get("export_destination")
            if action == "invoke" and expected is not None:
                self.assertEqual(expected["name"], self.backend.directory + "\\")
                verified.append(expected["name"])
            return original(action, **payload)
        self.backend.call = enforce_literal_name
        self.assertTrue(self.ensure()["changed"])
        self.assertTrue(restore_destination(self.backend, self.backend.observe([]))["restored"])
        self.assertTrue(self.ensure()["changed"])
        root = str(self.backend.state_dir / "export-slot")
        self.assertEqual(verified, [self.temp.name + "\\", root + "\\", root + "\\", self.temp.name + "\\"])
        entry = next(iter(json.loads((self.backend.state_dir / "export-destination.json").read_text("utf-8"))["destinations"].values()))
        self.assertEqual(entry["previous_root"], self.temp.name)
        self.assertEqual(entry["owned_root"], root)

    def test_restored_cycle_rejects_user_directory_or_menu_changes_without_mutation(self):
        self.ensure()
        restore_destination(self.backend, self.backend.observe([]))
        path = self.backend.state_dir / "export-destination.json"
        saved = path.read_bytes()
        other = Path(self.temp.name) / "user-chosen"
        other.mkdir()
        for directory, label in ((str(other), "Session Output"),
                                 (self.temp.name, "Different mode"),
                                 (str(self.backend.state_dir / "export-slot"), "Session Output")):
            with self.subTest(directory=directory, label=label):
                self.backend.directory, self.backend.root_label = directory, label
                count = len(self.backend.actions)
                with self.assertRaises(NativeAdapterError) as caught:
                    self.ensure()
                self.assertEqual(caught.exception.code, "DESTINATION_RECOVERY_REQUIRED")
                self.assertEqual(len(self.backend.actions), count)
                self.assertEqual(path.read_bytes(), saved)

    def test_owned_path_cannot_bypass_inconsistent_metadata(self):
        self.ensure()
        path = self.backend.state_dir / "export-destination.json"
        original = json.loads(path.read_text("utf-8"))
        for field, value in (("status", "unknown"), ("status", "pending"), ("status", "restoring"),
                             ("document", {"path": "other"}), ("recipe_name", "Other recipe"),
                             ("owned_root", self.temp.name)):
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(original)
                next(iter(data["destinations"].values()))[field] = value
                path.write_text(json.dumps(data), encoding="utf-8")
                count = len(self.backend.actions)
                with self.assertRaises(NativeAdapterError) as caught:
                    self.ensure()
                self.assertEqual(caught.exception.code, "DESTINATION_RECOVERY_REQUIRED")
                self.assertEqual(len(self.backend.actions), count)

    def test_new_cycle_journal_failure_preserves_restored_record(self):
        self.ensure()
        restore_destination(self.backend, self.backend.observe([]))
        path = self.backend.state_dir / "export-destination.json"
        saved, count = path.read_bytes(), len(self.backend.actions)
        def reject(name):
            raise RuntimeError("journal failed")
        self.backend.before_mutation = reject
        with self.assertRaises(RuntimeError):
            self.ensure()
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(len(self.backend.actions), count)


    def test_restore_without_recorded_menu_evidence_refuses_before_ui_write(self):
        self.ensure()
        path = self.backend.state_dir / "export-destination.json"
        data = json.loads(path.read_text("utf-8"))
        next(iter(data["destinations"].values())).pop("previous_menu_selector")
        path.write_text(json.dumps(data), encoding="utf-8")
        before = list(self.backend.actions)
        with self.assertRaises(NativeAdapterError) as caught:
            restore_destination(self.backend, self.backend.observe([]))
        self.assertEqual(caught.exception.code, "DESTINATION_RESTORE_UNSUPPORTED")
        self.assertEqual(self.backend.actions, before)

    def test_missing_original_menu_option_does_not_fallback_to_folder_picker(self):
        self.ensure()
        self.backend.restore_option_available = False
        chooses = self.backend.actions.count("preview.select_owned_directory")
        with self.assertRaises(NativeAdapterError):
            restore_destination(self.backend, self.backend.observe([]))
        self.assertEqual(self.backend.actions.count("preview.select_owned_directory"), chooses)
        self.assertNotIn("preview.restore_destination", self.backend.actions)


    @staticmethod
    def record(observation, actions):
        return {"status": "partial", "request": {"command": "preview", "action": "prepare"},
                "target": copy.deepcopy(observation["target"]), "before": copy.deepcopy(observation),
                "actions": copy.deepcopy(actions)}

    def test_restore_start_journal_failure_does_not_mark_metadata_pending(self):
        self.ensure()
        path = self.backend.state_dir / "export-destination.json"
        saved = path.read_bytes()
        count = len(self.backend.actions)
        def reject(name):
            raise RuntimeError("journal failed")
        self.backend.before_mutation = reject
        with self.assertRaises(RuntimeError):
            restore_destination(self.backend, self.backend.observe([]))
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(len(self.backend.actions), count)

    def test_native_confirmed_not_dispatched_rolls_back_only_local_metadata(self):
        original = self.backend.call
        def refuse(action, **payload):
            if action == "invoke" and payload.get("mode") == "expand":
                error = NativeAdapterError("GUARD_MISMATCH", "native certified before dispatch")
                error.dispatched = False
                raise error
            return original(action, **payload)
        self.backend.call = refuse
        with self.assertRaises(NativeAdapterError) as caught:
            self.ensure()
        self.assertIs(caught.exception.dispatched, False)
        data = json.loads((self.backend.state_dir / "export-destination.json").read_text("utf-8"))
        self.assertEqual(data["destinations"], {})
        self.assertIsNone(self.backend.action_records[0]["intent"]["metadata_entry_before"])

    def test_metadata_rollback_failure_does_not_report_action_not_dispatched(self):
        from c1ai import native_preview
        original_call, original_save = self.backend.call, native_preview._save_destination_metadata
        saves = []
        def save(backend, metadata):
            saves.append(True)
            if len(saves) == 2:
                raise OSError("metadata write refused")
            return original_save(backend, metadata)
        def refuse(action, **payload):
            if action == "invoke" and payload.get("mode") == "expand":
                error = NativeAdapterError("GUARD_MISMATCH", "native certified before dispatch")
                error.dispatched = False
                raise error
            return original_call(action, **payload)
        self.backend.call = refuse
        with patch.object(native_preview, "_save_destination_metadata", save):
            with self.assertRaises(NativeAdapterError) as caught:
                self.ensure()
        self.assertEqual(caught.exception.code, "DESTINATION_METADATA_RECOVERY_REQUIRED")
        self.assertIsNot(getattr(caught.exception, "dispatched", None), False)
        data = json.loads((self.backend.state_dir / "export-destination.json").read_text("utf-8"))
        self.assertEqual(next(iter(data["destinations"].values()))["status"], "pending")









    def test_new_setup_refuses_empty_picker_edit_before_recording_current_selection(self):
        def clear_edit(name):
            if name == "preview.select_owned_directory":
                self.backend.edit_text = ""
        self.backend.after_mutation = clear_edit
        with self.assertRaises(NativeAdapterError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.code, "DESTINATION_UNVERIFIED")
        self.assertNotIn("preview.select_current_directory", self.backend.actions)
        self.assertFalse(any(action == "select_current_export_folder" for action, _ in self.backend.native_calls))

    def historical_sources(self):
        observation = self.backend.observe([])
        recipe = verify_recipe(self.backend, observation, "AI Preview", allow_select=False)
        settings = export_settings(recipe, allow_existing=True)
        root_menu = next(node for node in recipe["nodes"] if "ExpandCollapse" in node.get("patterns", []))
        option = {"name": "Session Output", "control_type": "MenuItem", "patterns": ["Invoke"],
                  "enabled": True, "runtime_id": [7, 800],
                  "selector": {"name": "Session Output", "control_type": "MenuItem",
                               "ancestors": [{"name": root_menu["name"], "control_type": "MenuItem"},
                                             {"class_name": "ExportDestinationRootFolderSelector"}]}}
        reports = []
        for name, action, nodes in (("prepare", "preview.open_export", recipe["nodes"]),
                                    ("destination_options", "preview.destination_menu", [option])):
            reports.append({"operation_id": name + "-synthetic", "status": "completed", "created_at": 123,
                            "request": {"command": "preview", "action": name}, "target": observation["target"],
                            "before": observation, "actions": [{"name": action, "status": "completed", "result": {"accepted": True}}],
                            "result": {"target": observation["target"], "nodes": nodes, "settings": settings}})
        source_paths, hashes = [], []
        for index, report in enumerate(reports):
            path = Path(self.temp.name) / (str(index) + ".json")
            path.write_text(json.dumps(report), encoding="utf-8")
            source_paths.append(path)
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        root = self.backend.state_dir / "export-slot"
        root.mkdir()
        self.backend.directory, self.backend.root_label = str(root), "Owned export-slot"
        current = verify_recipe(self.backend, observation, "AI Preview", allow_select=False)
        return observation, current, dict(prepare_path=source_paths[0], options_path=source_paths[1],
                                          prepare_sha256=hashes[0], options_sha256=hashes[1])

    def test_historical_metadata_builder_is_pure_hash_bound_and_mode_exact(self):
        observation, recipe, arguments = self.historical_sources()
        native_count = len(self.backend.native_calls)
        result = build_historical_destination_metadata(self.backend, observation, recipe, **arguments)
        self.assertFalse(result["persisted"])
        self.assertEqual(result["entry"]["previous_root"], self.temp.name)
        self.assertEqual(result["entry"]["previous_menu_name"], "Session Output")
        self.assertEqual(result["entry"]["source_evidence"][0]["sha256"], arguments["prepare_sha256"])
        self.assertFalse(Path(result["path"]).exists())
        self.assertEqual(len(self.backend.native_calls), native_count)
        self.assertEqual(self.backend.actions, [])

    def test_historical_metadata_builder_refuses_mismatched_hash_and_document(self):
        observation, recipe, arguments = self.historical_sources()
        wrong = dict(arguments, prepare_sha256="0" * 64)
        with self.assertRaises(NativeAdapterError) as caught:
            build_historical_destination_metadata(self.backend, observation, recipe, **wrong)
        self.assertEqual(caught.exception.code, "DESTINATION_SOURCE_MISMATCH")
        observation["target"]["document"] = {"path": "other"}
        with self.assertRaises(NativeAdapterError) as caught:
            build_historical_destination_metadata(self.backend, observation, recipe, **arguments)
        self.assertEqual(caught.exception.code, "DESTINATION_SOURCE_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
