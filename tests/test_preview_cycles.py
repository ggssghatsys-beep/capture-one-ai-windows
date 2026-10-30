"""Public Core/SQLite directory cycles with synthetic native controls only."""
import json
from pathlib import Path
import tempfile
import unittest

from c1ai.core import Core
from c1ai.native_images import NativeAdapterError
from c1ai.native_preview import prepare_export, restore_destination
from c1ai.native_reconcile import reconcile
from test_native_destination import DestinationBackend


class PublicPreviewCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.backend = DestinationBackend(self.temp.name)
        self.backend.target.update(image={"path": str(Path(self.temp.name) / "a.NEF")}, layer="Background",
                                   instance={"pid": 7, "started_at": "synthetic", "version": "16.8.6"})
        self.core = Core(self.backend, self.backend.state_dir)
        self.addCleanup(self.core.close)
        self.backend.action_hook = None
        self.backend.action = lambda name, func, intent=None: self.backend.action_hook(name, func, intent)
        def preview(request, observation):
            if request["action"] == "restore_destination":
                return dict(restore_destination(self.backend, observation), status="completed")
            return dict(prepare_export(self.backend, "AI Preview", observation=observation), status="completed")
        self.backend.preview = preview

    def request(self, action, request_id):
        observed = self.core.dispatch({"command": "read", "fields": []})
        return {"command": "preview", "kind": "render", "action": action, "request_id": request_id,
                "target": observed["target"], "observation_token": observed["observation_token"]}

    def entry(self):
        metadata = json.loads((self.backend.state_dir / "export-destination.json").read_text("utf-8"))
        return next(iter(metadata["destinations"].values()))


    def test_public_prepare_restore_can_repeat_with_distinct_durable_operations(self):
        operations = []
        for cycle in (1, 2):
            prepare_request = self.request("prepare", f"prepare-{cycle}")
            prepared = self.core.dispatch(prepare_request)
            self.assertEqual(prepared["status"], "completed")
            self.assertEqual(self.entry()["cycle"], cycle)
            self.assertEqual(self.entry()["status"], "selected")
            self.assertEqual(self.backend.directory, str(self.backend.state_dir / "export-slot"))
            first = prepared["actions"][0]
            self.assertEqual(first["intent"]["cycle"], cycle)
            previous = first["intent"]["metadata_entry_before"]
            if cycle == 1:
                self.assertIsNone(previous)
            else:
                self.assertEqual((previous["status"], previous["cycle"]), ("restored", 1))
            calls = len(self.backend.native_calls)
            self.assertTrue(self.core.dispatch(prepare_request)["existing"])
            self.assertEqual(len(self.backend.native_calls), calls)
            restored = self.core.dispatch(self.request("restore_destination", f"restore-{cycle}"))
            self.assertEqual(restored["status"], "completed")
            self.assertEqual(self.entry()["status"], "restored")
            self.assertEqual(self.backend.directory, self.temp.name)
            self.assertEqual(self.backend.root_label, "Session Output")
            operations.extend((prepared, restored))
        self.assertEqual(len({record["operation_id"] for record in operations}), 4)
        actions = [action for record in operations for action in record["actions"]]
        self.assertEqual(len({action["action_id"] for action in actions}), len(actions))
        self.assertEqual(self.core.journal.pending(self.backend.target), [])
        self.assertFalse(any(action["name"] == "preview.export" for action in actions))

    def test_second_cycle_unknown_blocks_new_request_without_repeating_directory_selection(self):
        self.assertEqual(self.core.dispatch(self.request("prepare", "prepare-1"))["status"], "completed")
        self.assertEqual(self.core.dispatch(self.request("restore_destination", "restore-1"))["status"], "completed")
        self.backend.lose_choose_reply = True
        second_request = self.request("prepare", "prepare-2")
        failed = self.core.dispatch(second_request)
        self.assertEqual(failed["status"], "outcome_unknown")
        self.assertEqual((self.entry()["status"], self.entry()["cycle"]), ("pending", 2))
        selections = sum(action == "choose_export_folder" for action, _ in self.backend.native_calls)
        self.assertEqual(self.core.dispatch(second_request)["operation_id"], failed["operation_id"])
        blocked = self.core.dispatch(self.request("prepare", "prepare-3"))
        self.assertEqual(blocked["error"]["code"], "DESTINATION_RECOVERY_REQUIRED")
        self.assertEqual(sum(action == "choose_export_folder" for action, _ in self.backend.native_calls), selections)

    def test_open_window_then_certified_menu_refusal_reconciles_only_after_metadata_rollback(self):
        self.core.dispatch(self.request("prepare", "prepare-1"))
        self.core.dispatch(self.request("restore_destination", "restore-1"))
        prior = self.entry()
        self.backend.opened = False
        original_call = self.backend.call
        def refuse_menu(action, **payload):
            if action == "invoke" and payload.get("mode") == "expand":
                error = NativeAdapterError("STALE_TARGET", "synthetic native text guard mismatch")
                error.dispatched = False
                raise error
            return original_call(action, **payload)
        self.backend.call = refuse_menu
        request = self.request("prepare", "prepare-2")
        failed = self.core.dispatch(request)
        self.assertEqual(failed["status"], "partial")
        self.assertEqual([(a["name"], a["status"]) for a in failed["actions"]],
                         [("preview.open_export", "completed"), ("preview.destination_menu", "not_dispatched")])
        self.assertEqual(self.entry(), prior)
        self.backend.reconcile = lambda record: reconcile(self.backend, record)
        count = sum(action in {"invoke", "choose_export_folder", "confirm_export_folder"}
                    for action, _ in self.backend.native_calls)
        result = self.core.dispatch({"command": "operations", "action": "reconcile", "operation_id": failed["operation_id"]})
        self.assertEqual(result["status"], "not_applied")
        self.assertTrue(result["reconciliation"]["evidence"]["navigation_applied"])
        self.assertTrue(result["reconciliation"]["evidence"]["metadata_rollback_verified"])
        self.assertFalse(result["reconciliation"]["evidence"]["export_dispatched"])
        self.assertEqual(sum(action in {"invoke", "choose_export_folder", "confirm_export_folder"}
                             for action, _ in self.backend.native_calls), count)
        self.assertEqual(self.entry(), prior)
        self.assertEqual(self.core.journal.pending(self.backend.target), [])
        self.assertTrue(self.core.dispatch(request)["existing"])
        self.backend.call = original_call
        self.assertEqual(self.core.dispatch(self.request("prepare", "prepare-3"))["status"], "completed")





if __name__ == "__main__":
    unittest.main()
