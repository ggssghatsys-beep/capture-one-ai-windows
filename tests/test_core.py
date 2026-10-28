import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from c1ai.core import Core
from c1ai.contracts import FIELDS


class FakeBackend:
    def __init__(self):
        self.target = {"instance": {"pid": 123, "started_at": "first", "version": "test"},
                       "document": {"path": "C:/test/test.cosessiondb"},
                       "image": {"path": "C:/test/one.tif"},
                       "variant": {"label": "one [2]", "id": 24}, "layer": "Background"}
        self.values = dict.fromkeys(FIELDS, 0)
        self.values["temperature"] = 5000
        self.calls = []
        self.fail_field = None
        self.fail_after_write = False
        self.action_hook = None
        self.core = None
        self.assert_writeahead = False

    def context(self):
        return {"target": copy.deepcopy(self.target), "capabilities": {"fields": {
            field: {"writable": True, "min": 0 if field == "temperature" else -100,
                    "max": 20000 if field == "temperature" else 100}
            for field in FIELDS}}}

    def observe(self, fields):
        return {"target": copy.deepcopy(self.target), "values": {field: self.values[field] for field in fields},
                "evidence": {"source": "fake_control", "observation_number": len(self.calls)}}

    def set_value(self, field, value, expected_observation):
        if self.assert_writeahead:
            records = self.core.journal.list()
            record = self.core.journal.get(operation_id=records[0]["operation_id"])
            assert record["status"] == "dispatching"
            assert record["actions"][-1]["status"] == "dispatching"
        self.calls.append((field, value))
        if field == self.fail_field and not self.fail_after_write:
            raise TimeoutError("worker timed out")
        self.values[field] = value
        if field == self.fail_field:
            raise TimeoutError("lost response after write")
        return {"accepted": True, "control_readback": value}

    def image_action(self, action, request, observation):
        if action == "list":
            return {"images": [copy.deepcopy(self.target)]}
        def first():
            self.calls.append(("image", action))
            return {"accepted": True}
        self.action_hook("image:" + action, first)
        if request.get("fail_second"):
            self.action_hook("image:verify", lambda: (_ for _ in ()).throw(TimeoutError("image response lost")))
        return {"status": "completed", "target": copy.deepcopy(self.target)}

    def preview(self, request, observation):
        receipt = {"target": copy.deepcopy(self.target), "output_path": "fake-preview.jpg", "settings": {"icc": "sRGB"}}
        def render():
            records = self.core.journal.list()
            action = self.core.journal.get(operation_id=records[0]["operation_id"])["actions"][-1]
            assert action["status"] == "dispatching" and action["intent"]["receipt"] == receipt
            return {"status": "completed", "path": "fake-preview.jpg", "target": self.target}
        return self.action_hook("render", render, intent={"receipt": receipt})

    def reconcile(self, record):
        return {"outcome": "applied_not_restored", "target": copy.deepcopy(self.target),
                "evidence": {"source": "fake_independent_reader", "matched": True,
                             "observation": self.observe(list(record.get("before", {}).get("values", {})))}}


class BackendFailure(RuntimeError):
    def __init__(self, dispatched=None, details=None):
        super().__init__("synthetic structured backend failure")
        self.dispatched = dispatched
        self.details = details


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = FakeBackend()
        self.core = Core(self.backend, self.temp.name)
        self.backend.core = self.core

    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def test_context_dialogs_skip_uia_when_native_inventory_reports_blocking_window(self):
        candidate = {"hwnd": 456, "owner_hwnd": 123, "uia_modal_verified": False}
        self.backend.context = Mock(return_value={"target": None, "editing_ready": False,
            "reason": "owned_window_blocks_main", "native_windows": {
                "blocking_modal_possible": True, "modal_candidates": [candidate]}})
        self.backend.call = Mock(side_effect=AssertionError("UIA must not be queried while blocked"))
        result = self.core.dispatch({"command": "context", "include_dialogs": True})
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["editing_ready"])
        self.assertEqual(result["dialogs"], {"source": "native_window_inventory", "candidates": [candidate],
            "uia_queried": False, "uia_skipped_reason": "owned_window_blocks_main"})
        self.backend.call.assert_not_called()

    def test_context_dialogs_keep_normal_route_without_native_blocker(self):
        original = self.backend.context()
        self.backend.context = Mock(return_value=dict(original, native_windows={"blocking_modal_possible": False}))
        self.backend.call = Mock(return_value={"dialogs": []})
        result = self.core.dispatch({"command": "context", "include_dialogs": True})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dialogs"], {"dialogs": []})
        self.backend.call.assert_called_once_with("dialogs")

    def test_context_dialogs_skip_uia_for_disabled_main_or_incomplete_native_inventory(self):
        for native in ({"blocking_modal_possible": None, "main_enabled": False, "modal_candidates": []},
                       {"blocking_modal_possible": False, "main_enabled": True, "enumeration_complete": False}):
            with self.subTest(native=native):
                self.backend.context = Mock(return_value={"target": None, "editing_ready": False, "native_windows": native})
                self.backend.call = Mock(side_effect=AssertionError("Do not fall back to UIA when native state is degraded"))
                result = self.core.dispatch({"command": "context", "include_dialogs": True})
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["dialogs"]["uia_skipped_reason"], "native_window_state_unavailable")
                self.assertFalse(result["dialogs"]["uia_queried"])
                self.backend.call.assert_not_called()

    def request(self, patch_values=None, request_id="first", module="exposure"):
        observation = self.core.dispatch({"command": "read"})
        return {"command": "adjust", "request_id": request_id, "target": observation["target"],
                "module": module, "patch": patch_values or {"exposure": 1},
                "observation_token": observation["observation_token"]}

    def pending_preview(self):
        def open_then_fail(request, observation):
            self.backend.action_hook("preview.open_export", lambda: {"accepted": True})
            raise RuntimeError("Recipe requires configuration")
        self.backend.preview = open_then_fail
        observation = self.core.dispatch({"command": "read", "fields": []})
        return self.core.dispatch({"command": "preview", "request_id": "pending-preview",
                                   "target": observation["target"], "observation_token": observation["observation_token"]})

    def recovery_result(self):
        return {"outcome": "applied_not_restored", "target": copy.deepcopy(self.backend.target),
                "evidence": {"scope": "fixed_destination_confirmation", "path_verified": True}}

    def test_control_receipt_is_not_model_verification(self):
        self.backend.assert_writeahead = True
        result = self.core.dispatch(self.request({"exposure": 1, "contrast": 2}))
        self.assertEqual(result["status"], "control_confirmed")
        self.assertFalse(result["model_verified"])
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual(result["after"]["values"]["exposure"], 1)

    def test_same_request_returns_record_without_replaying(self):
        request = self.request()
        first = self.core.dispatch(request)
        second = self.core.dispatch(request)
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertTrue(second["existing"])
        self.assertEqual(self.backend.calls, [("exposure", 1)])

    def test_request_id_content_conflict(self):
        request = self.request()
        self.core.dispatch(request)
        request["patch"]["exposure"] = 2
        result = self.core.dispatch(request)
        self.assertEqual(result["error"]["code"], "request_id_conflict")
        self.assertEqual(len(self.backend.calls), 1)

    def test_stale_observation_and_target_change_refused(self):
        request = self.request()
        self.backend.values["contrast"] = 9
        result = self.core.dispatch(request)
        self.assertEqual(result["error"]["code"], "stale_observation")
        request = self.request()
        self.backend.target["variant"]["id"] = 25
        result = self.core.dispatch(request)
        self.assertEqual(result["error"]["code"], "target_changed")
        self.assertFalse(self.backend.calls)

    def test_token_is_content_based_not_observation_time(self):
        first = self.core.dispatch({"command": "read", "module": "exposure"})
        self.backend.calls.append(("unrelated", 0))
        second = self.core.dispatch({"command": "read", "module": "exposure"})
        self.assertEqual(first["observation_token"], second["observation_token"])

    def test_identity_only_observation_prepares_without_reading_fields(self):
        observation = self.core.dispatch({"command": "read", "fields": []})
        self.assertEqual(observation["values"], {})
        result = self.core.dispatch({"command": "images", "action": "prepare_adjustments", "request_id": "prepare",
            "target": observation["target"], "observation_token": observation["observation_token"]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["actions"]), 1)

    def test_readonly_image_list_does_not_create_operation(self):
        result = self.core.dispatch({"command": "images", "action": "list"})
        self.assertEqual(len(result["images"]), 1)
        self.assertFalse(self.core.journal.list())

    def test_peek_matches_normal_operation_get_and_list_shapes(self):
        result = self.core.dispatch(self.request())
        for request in ({"command": "operations", "operation_id": result["operation_id"]},
                        {"command": "operations", "action": "get", "request_id": "first"},
                        {"command": "operations", "action": "list"}):
            with self.subTest(request=request):
                self.assertEqual(self.core.peek(request), self.core.dispatch(request))

    def test_peek_during_long_preview_returns_committed_dispatching_progress(self):
        started, release = threading.Event(), threading.Event()
        completed = []
        def render():
            started.set()
            if not release.wait(5):
                raise TimeoutError("Synthetic preview remained blocked")
            return {"status": "completed", "path": "synthetic-preview.jpg"}
        self.backend.preview = lambda request, observation: self.backend.action_hook("render", render)
        observation = self.core.dispatch({"command": "read", "fields": []})
        request = {"command": "preview", "request_id": "long-preview", "target": observation["target"],
                   "observation_token": observation["observation_token"]}
        writer = threading.Thread(target=lambda: completed.append(self.core.dispatch(request)))
        writer.start()
        try:
            self.assertTrue(started.wait(2))
            self.backend.context = Mock(side_effect=AssertionError("peek must not call context"))
            self.backend.observe = Mock(side_effect=AssertionError("peek must not observe Capture One"))
            begin = time.monotonic()
            progress = self.core.peek({"command": "operations", "action": "get", "request_id": "long-preview"})
            listed = self.core.peek({"command": "operations", "action": "list"})
            self.assertLess(time.monotonic() - begin, 0.8)
            self.assertEqual(progress["status"], "dispatching")
            self.assertEqual(progress["actions"][0]["status"], "dispatching")
            self.assertEqual(listed["operations"][0]["operation_id"], progress["operation_id"])
            self.assertTrue(writer.is_alive())
            self.backend.context.assert_not_called()
            self.backend.observe.assert_not_called()
        finally:
            release.set()
            writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(completed[0]["status"], "completed")

    def test_peek_does_not_read_uncommitted_writer_changes(self):
        result = self.core.dispatch(self.request())
        writer = self.core.journal.db
        row = writer.execute("SELECT payload FROM operations WHERE operation_id=?", (result["operation_id"],)).fetchone()
        uncommitted = json.loads(row[0])
        uncommitted["status"] = "not_a_committed_status"
        writer.execute("BEGIN IMMEDIATE")
        try:
            writer.execute("UPDATE operations SET payload=? WHERE operation_id=?",
                           (json.dumps(uncommitted), result["operation_id"]))
            progress = self.core.peek({"command": "operations", "operation_id": result["operation_id"]})
            self.assertEqual(progress["status"], "control_confirmed")
        finally:
            writer.rollback()

    def test_peek_opens_and_closes_its_own_enforced_readonly_connection(self):
        result = self.core.dispatch(self.request())
        real_connect, readers = sqlite3.connect, []
        def connect(database, **kwargs):
            self.assertIn("mode=ro", database)
            self.assertTrue(kwargs.get("uri"))
            reader = real_connect(database, **kwargs)
            self.assertIsNot(reader, self.core.journal.db)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("DELETE FROM operations")
            reader.rollback()
            readers.append(reader)
            return reader
        with patch("c1ai.journal.sqlite3.connect", side_effect=connect):
            output = self.core.peek({"command": "operations", "operation_id": result["operation_id"]})
        self.assertEqual(output["operation_id"], result["operation_id"])
        self.assertEqual(len(readers), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            readers[0].execute("SELECT 1")

    def test_peek_unknown_request_does_not_invent_queued_status(self):
        result = self.core.peek({"command": "operations", "request_id": "accepted-but-not-started"})
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertFalse(self.core.journal.list())

    def test_peek_rejects_mutations_and_cannot_clear_pending(self):
        self.backend.fail_field = "exposure"
        pending = self.core.dispatch(self.request())
        for action in ("recover", "restore", "reconcile", "delete"):
            with self.subTest(action=action):
                output = self.core.peek({"command": "operations", "action": action,
                                         "operation_id": pending["operation_id"]})
                self.assertEqual(output["error"]["code"], "readonly_only")
        self.assertEqual(self.core.peek({"command": "adjust"})["error"]["code"], "readonly_only")
        self.assertEqual(self.core.peek({"command": "operations", "action": "list", "bad": float("nan")})["status"], "refused")
        inspected = self.core.peek({"command": "operations", "operation_id": pending["operation_id"]})
        self.assertEqual(inspected["status"], "outcome_unknown")
        blocked = self.core.dispatch(self.request(request_id="still-blocked"))
        self.assertEqual(blocked["error"]["code"], "pending_operation")

    def test_unsupported_preview_kind_does_not_export(self):
        request = self.request()
        request.update(command="preview", kind="viewer")
        result = self.core.dispatch(request)
        self.assertEqual(result["error"]["code"], "unsupported")
        self.assertFalse(self.backend.calls)

    def test_public_preview_rejects_unknown_and_historical_configuration_actions(self):
        for action in ("configure_owned_destination", "destination_picker", "destination_options", "typo"):
            with self.subTest(action=action):
                request = self.request(request_id="unsupported-" + action)
                request.update(command="preview", action=action)
                result = self.core.dispatch(request)
                self.assertEqual(result["error"]["code"], "unsupported")
        self.assertFalse(self.core.journal.list())

    def test_restore_destination_uses_target_token_and_durable_action(self):
        request = self.request()
        request.update(command="preview", action="restore_destination")
        dispatched = []
        def restore(request, before):
            self.backend.action_hook("preview.restore_destination", lambda: dispatched.append(True),
                                     intent={"previous_root": "recorded-original-root"})
            return {"status": "completed", "restored": True, "target": before["target"]}
        self.backend.preview = restore
        missing = {key: value for key, value in request.items() if key != "observation_token"}
        self.assertEqual(self.core.dispatch(missing)["error"]["code"], "missing_precondition")
        self.assertFalse(dispatched)
        result = self.core.dispatch(request)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["actions"][0]["intent"]["previous_root"], "recorded-original-root")
        self.assertEqual(len(dispatched), 1)
        self.assertTrue(self.core.dispatch(request)["existing"])
        self.assertEqual(len(dispatched), 1)

    def test_existing_historical_configuration_can_be_read_and_recovered_without_new_call(self):
        request = self.request()
        request.update(command="preview", action="configure_owned_destination")
        observation = self.core.journal.observation(request["observation_token"])
        record, _ = self.core.journal.begin(request, observation["target"], observation)
        self.core.journal.update(record["operation_id"], status="partial")
        self.backend.preview = Mock(side_effect=AssertionError("Historical operation must not be replayed"))
        duplicate = self.core.dispatch(request)
        self.assertTrue(duplicate["existing"])
        self.assertEqual(duplicate["operation_id"], record["operation_id"])
        self.backend.recover = Mock(return_value=self.recovery_result())
        output = self.core.dispatch({"command": "operations", "action": "recover", "operation_id": record["operation_id"]})
        self.assertEqual(output["status"], "applied_not_restored")
        self.backend.preview.assert_not_called()

    def test_invalid_backend_result_never_becomes_completed_after_side_effect(self):
        for index, invalid in enumerate((None, {"status": "failed"}, {"path": "not-an-explicit-success"}, {"status": True})):
            with self.subTest(result=invalid), tempfile.TemporaryDirectory() as state_dir:
                backend = FakeBackend()
                core = Core(backend, state_dir)
                try:
                    observation = core.dispatch({"command": "read", "fields": []})
                    def preview(request, before):
                        backend.action_hook("preview.open_export", lambda: {"accepted": True})
                        return invalid
                    backend.preview = preview
                    result = core.dispatch({"command": "preview", "request_id": str(index), "target": observation["target"],
                                            "observation_token": observation["observation_token"]})
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["error"]["code"], "invalid_backend_result")
                finally:
                    core.close()

    def test_export_receipt_is_durable_before_dispatch(self):
        request = self.request()
        request.update(command="preview", kind="render")
        output = self.core.dispatch(request)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["actions"][0]["intent"]["receipt"]["output_path"], "fake-preview.jpg")

    def test_backend_success_cannot_hide_swallowed_unknown_action(self):
        def preview(request, before):
            try:
                self.backend.action_hook("preview.export", lambda: (_ for _ in ()).throw(TimeoutError("unknown")))
            except TimeoutError:
                pass
            return {"status": "completed"}
        self.backend.preview = preview
        request = self.request()
        request.update(command="preview")
        output = self.core.dispatch(request)
        self.assertEqual(output["status"], "outcome_unknown")
        self.assertEqual(output["error"]["code"], "invalid_backend_result")

    def test_backend_refusal_cannot_erase_completed_navigation(self):
        def preview(request, before):
            self.backend.action_hook("preview.open_export", lambda: {"accepted": True})
            return {"status": "refused"}
        self.backend.preview = preview
        request = self.request()
        request.update(command="preview")
        output = self.core.dispatch(request)
        self.assertEqual(output["status"], "partial")
        self.assertEqual(output["error"]["code"], "invalid_backend_result")

    def test_restore_refuses_unqualified_backend(self):
        result = self.core.dispatch(self.request())
        original = self.backend.context
        def unqualified():
            context = original()
            context["capabilities"]["fields"]["exposure"]["writable"] = False
            return context
        self.backend.context = unqualified
        output = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(output["error"]["code"], "unsupported")
        self.assertEqual(self.backend.values["exposure"], 1)

    def test_restore_timeout_after_effect_can_reconcile_without_replaying_that_field(self):
        from c1ai.native_reconcile import reconcile
        self.backend.reconcile = lambda record: reconcile(self.backend, record)
        applied = self.core.dispatch(self.request({"exposure": 1, "contrast": 2}))
        self.backend.fail_field, self.backend.fail_after_write = "contrast", True
        request = {"command": "operations", "operation_id": applied["operation_id"]}
        first = self.core.dispatch({**request, "action": "restore"})
        self.assertEqual(first["status"], "outcome_unknown")
        self.assertEqual(self.backend.values["contrast"], 0)
        reconciled = self.core.dispatch({**request, "action": "reconcile"})
        self.assertEqual(reconciled["status"], "partial")
        self.backend.fail_field = None
        restored = self.core.dispatch({**request, "action": "restore"})
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(restored["operation_id"], applied["operation_id"])
        self.assertEqual(self.backend.calls.count(("contrast", 0)), 1)
        self.assertEqual(self.backend.calls.count(("exposure", 0)), 1)

    def test_reconciliation_cannot_authorize_replaying_an_unknown_restore(self):
        from c1ai.native_reconcile import reconcile
        self.backend.reconcile = lambda record: reconcile(self.backend, record)
        applied = self.core.dispatch(self.request({"exposure": 1, "contrast": 2}))
        self.backend.fail_field = "contrast"
        request = {"command": "operations", "operation_id": applied["operation_id"]}
        self.core.dispatch({**request, "action": "restore"})
        self.core.dispatch({**request, "action": "reconcile"})
        self.backend.fail_field = None
        blocked = self.core.dispatch({**request, "action": "restore"})
        self.assertEqual(blocked["error"]["code"], "restore_action_unresolved")
        self.assertEqual(blocked["status"], "outcome_unknown")
        self.assertEqual(self.backend.calls.count(("contrast", 0)), 1)
        self.assertEqual(self.backend.values["exposure"], 1)

    def test_restoration_already_effective_after_timeout_reconciles_terminal(self):
        from c1ai.native_reconcile import reconcile
        self.backend.reconcile = lambda record: reconcile(self.backend, record)
        applied = self.core.dispatch(self.request())
        self.backend.fail_field, self.backend.fail_after_write = "exposure", True
        request = {"command": "operations", "operation_id": applied["operation_id"]}
        self.core.dispatch({**request, "action": "restore"})
        reconciled = self.core.dispatch({**request, "action": "reconcile"})
        self.assertEqual(reconciled["status"], "restored")
        self.assertEqual(reconciled["restore_observation"]["values"]["exposure"], 0)
        self.assertEqual(reconciled["restore_scope"], "observed_numeric_values")
        self.assertFalse(reconciled["full_model_restore_verified"])
        self.assertIsNone(reconciled["pixel_restore_verified"])
        self.assertFalse(reconciled["model_verified"])
        self.assertTrue(self.core.dispatch({**request, "action": "restore"})["existing"])
        self.assertEqual(self.backend.calls.count(("exposure", 0)), 1)

    def test_numeric_reconcile_restored_requires_complete_baseline_evidence(self):
        self.backend.fail_field = "exposure"
        applied = self.core.dispatch(self.request())
        self.backend.reconcile = lambda record: {"outcome": "restored", "target": self.backend.target,
                                                "evidence": {"scope": "control_readback"}}
        output = self.core.dispatch({"command": "operations", "action": "reconcile", "operation_id": applied["operation_id"]})
        self.assertEqual(output["error"]["code"], "invalid_evidence")
        self.assertEqual(self.core.journal.get(operation_id=applied["operation_id"])["status"], "outcome_unknown")

    def test_numeric_restore_records_observation_scope_without_claiming_model_or_pixel_reversal(self):
        applied = self.core.dispatch(self.request())
        restored = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": applied["operation_id"]})
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(restored["restore_scope"], "observed_numeric_values")
        self.assertFalse(restored["full_model_restore_verified"])
        self.assertIsNone(restored["pixel_restore_verified"])
        stored = self.core.journal.get(operation_id=applied["operation_id"])
        self.assertEqual(stored["restore_scope"], "observed_numeric_values")

    def test_legacy_terminal_numeric_receipt_gets_scope_without_replay_or_rewrite(self):
        request = self.request()
        applied = self.core.dispatch(request)
        self.core.journal.update(applied["operation_id"], status="restored")
        writes = self.core.journal.db.total_changes
        calls = list(self.backend.calls)
        for action in ("get", "restore", "reconcile", "recover"):
            result = self.core.dispatch({"command": "operations", "action": action, "operation_id": applied["operation_id"]})
            self.assertEqual(result["restore_scope"], "observed_numeric_values")
            self.assertFalse(result["full_model_restore_verified"])
            self.assertIsNone(result["pixel_restore_verified"])
        self.assertEqual(self.core.dispatch(request)["restore_scope"], "observed_numeric_values")
        self.assertEqual(self.core.peek({"command": "operations", "request_id": request["request_id"]})["restore_scope"],
                         "observed_numeric_values")
        for list_result in (self.core.dispatch({"command": "operations", "action": "list"}),
                            self.core.peek({"command": "operations", "action": "list"})):
            self.assertEqual(list_result["operations"][0]["restore_scope"], "observed_numeric_values")
        self.assertEqual(self.backend.calls, calls)
        self.assertEqual(self.core.journal.db.total_changes, writes)
        self.assertNotIn("restore_scope", self.core.journal.get(operation_id=applied["operation_id"]))

    def test_writeahead_failure_prevents_dispatch(self):
        request = self.request()
        with patch.object(self.core.journal, "start_action", side_effect=OSError("disk full")):
            result = self.core.dispatch(request)
        self.assertFalse(self.backend.calls)
        self.assertEqual(result["status"], "refused")

    def test_failed_final_observation_is_recorded_as_not_dispatched(self):
        request = self.request()
        observe = self.core._observe
        count = 0
        def fail_final(fields):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("reader disconnected before setter")
            return observe(fields)
        with patch.object(self.core, "_observe", side_effect=fail_final):
            result = self.core.dispatch(request)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["actions"][0]["status"], "not_dispatched")
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.core.dispatch({"command": "context"})["pending_operations"])

    def test_trusted_backend_no_dispatch_evidence_does_not_leave_pending(self):
        request = self.request()
        self.backend.set_value = Mock(side_effect=BackendFailure(dispatched=False,
                                                               details={"possibly_mutated": False}))
        result = self.core.dispatch(request)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["actions"][0]["status"], "not_dispatched")
        self.assertIs(result["actions"][0]["error"]["dispatched"], False)
        self.assertFalse(self.core.dispatch({"command": "context"})["pending_operations"])

    def test_raw_false_details_and_nonboolean_markers_do_not_certify_no_dispatch(self):
        for index, error in enumerate((BackendFailure(details={"possibly_mutated": False}),
                                       BackendFailure(dispatched="false"), BackendFailure(dispatched=0),
                                       TimeoutError("response lost"), ValueError("malformed protocol"))):
            with self.subTest(error=error):
                # Independent state roots avoid clearing any previous uncertainty.
                with tempfile.TemporaryDirectory() as state_dir:
                    backend = FakeBackend()
                    core = Core(backend, state_dir)
                    try:
                        observation = core.dispatch({"command": "read", "module": "exposure"})
                        request = {"command": "adjust", "request_id": f"unknown-{index}", "module": "exposure",
                                   "patch": {"exposure": 1}, "target": observation["target"],
                                   "observation_token": observation["observation_token"]}
                        backend.set_value = Mock(side_effect=error)
                        result = core.dispatch(request)
                        self.assertEqual(result["status"], "outcome_unknown")
                        self.assertEqual(result["actions"][0]["status"], "outcome_unknown")
                    finally:
                        core.close()

    def test_successful_set_then_read_false_evidence_remains_unknown(self):
        request = self.request()
        observe, count = self.core._observe, 0
        def fail_after_set(fields):
            nonlocal count
            count += 1
            if count == 3:
                raise BackendFailure(dispatched=False, details={"possibly_mutated": False})
            return observe(fields)
        with patch.object(self.core, "_observe", side_effect=fail_after_set):
            result = self.core.dispatch(request)
        self.assertEqual(self.backend.values["exposure"], 1)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIs(result["actions"][0]["error"]["dispatched"], True)

    def test_no_dispatch_second_field_preserves_prior_partial_write(self):
        setter = self.backend.set_value
        def refuse_second(field, value, observation):
            if field == "contrast":
                raise BackendFailure(dispatched=False)
            return setter(field, value, observation)
        self.backend.set_value = refuse_second
        result = self.core.dispatch(self.request({"exposure": 1, "contrast": 2}))
        self.assertEqual(result["status"], "partial")
        self.assertEqual([action["status"] for action in result["actions"]], ["completed", "not_dispatched"])
        self.assertEqual(self.backend.values["exposure"], 1)
        restored = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(self.backend.values["exposure"], 0)

    def test_timeout_after_write_is_not_replayed(self):
        request = self.request()
        self.backend.fail_field, self.backend.fail_after_write = "exposure", True
        result = self.core.dispatch(request)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(self.backend.values["exposure"], 1)
        self.core.dispatch(request)
        self.assertEqual(len(self.backend.calls), 1)
        blocked = self.core.dispatch(self.request({"contrast": 2}, "second"))
        self.assertEqual(blocked["error"]["code"], "pending_operation")

    def test_second_field_failure_keeps_first_field_receipt(self):
        self.backend.fail_field = "contrast"
        result = self.core.dispatch(self.request({"exposure": 1, "contrast": 2, "brightness": 3}))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["progress"], {"exposure": "control_confirmed"})
        self.assertEqual([a["status"] for a in result["actions"]], ["completed", "outcome_unknown"])
        self.assertEqual(self.backend.values["brightness"], 0)

    def test_pending_survives_pid_and_core_restart(self):
        self.backend.fail_field = "exposure"
        result = self.core.dispatch(self.request())
        self.core.close()
        self.core = Core(self.backend, self.temp.name)
        self.backend.target["instance"] = {"pid": 999, "started_at": "second", "version": "test"}
        self.backend.fail_field = None
        context = self.core.dispatch({"command": "context"})
        self.assertEqual(context["pending_operations"][0]["operation_id"], result["operation_id"])
        self.core.dispatch({"command": "read"})
        self.core.dispatch({"command": "operations", "action": "get", "request_id": "first"})
        blocked = self.core.dispatch(self.request(request_id="new"))
        self.assertEqual(blocked["error"]["code"], "pending_operation")

    def test_shared_state_blocks_another_core(self):
        self.backend.fail_field = "exposure"
        self.core.dispatch(self.request())
        other_backend = FakeBackend()
        other_core = Core(other_backend, self.temp.name)
        try:
            observation = other_core.dispatch({"command": "read"})
            result = other_core.dispatch({"command": "adjust", "request_id": "other", "module": "exposure",
                "target": observation["target"], "observation_token": observation["observation_token"],
                "patch": {"exposure": 2}})
            self.assertEqual(result["error"]["code"], "pending_operation")
            self.assertFalse(other_backend.calls)
        finally:
            other_core.close()

    def test_pending_blocks_other_images_in_same_document(self):
        self.backend.fail_field = "exposure"
        self.core.dispatch(self.request())
        self.backend.target["image"]["path"] = "C:/test/user-original.nef"
        self.backend.target["variant"] = {"label": "user-original", "id": 99}
        blocked = self.core.dispatch(self.request(request_id="different-image"))
        self.assertEqual(blocked["error"]["code"], "pending_operation")

    def test_reconciliation_rejects_another_variant(self):
        self.backend.fail_field = "exposure"
        result = self.core.dispatch(self.request())
        self.backend.target["variant"]["id"] = 30
        output = self.core.dispatch({"command": "operations", "action": "reconcile", "operation_id": result["operation_id"]})
        self.assertEqual(output["error"]["code"], "target_changed")

    def test_restore_timeout_preserves_operation_and_never_replays(self):
        result = self.core.dispatch(self.request({"exposure": 1, "contrast": 2}))
        self.backend.fail_field, self.backend.fail_after_write = "exposure", True
        output = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(output["status"], "outcome_unknown")
        self.assertEqual(output["operation_id"], result["operation_id"])
        self.assertEqual(output["restore_observation"]["values"]["contrast"], 0)
        count = len(self.backend.calls)
        again = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(again["error"]["code"], "unconfirmed_after")
        self.assertEqual(len(self.backend.calls), count)

    def test_reconcile_can_qualify_unknown_after_for_conditional_restore(self):
        self.backend.fail_field, self.backend.fail_after_write = "exposure", True
        result = self.core.dispatch(self.request())
        self.backend.fail_field = None
        resolved = self.core.dispatch({"command": "operations", "action": "reconcile", "operation_id": result["operation_id"]})
        self.assertTrue(resolved["reconciled_numeric_after"])
        restored = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(self.backend.values["exposure"], 0)
        self.assertEqual(restored["actions"][0]["status"], "outcome_unknown")
        self.assertNotEqual(restored["actions"][0]["action_id"], restored["actions"][1]["action_id"])

    def test_old_reconciliation_does_not_qualify_a_later_restore_timeout(self):
        self.backend.fail_field, self.backend.fail_after_write = "exposure", True
        result = self.core.dispatch(self.request())
        self.core.dispatch({"command": "operations", "action": "reconcile", "operation_id": result["operation_id"]})
        first_restore = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(first_restore["status"], "outcome_unknown")
        count = len(self.backend.calls)
        again = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(again["error"]["code"], "unconfirmed_after")
        self.assertEqual(len(self.backend.calls), count)

    def test_restore_uses_original_operation_and_new_actions(self):
        result = self.core.dispatch(self.request({"exposure": 1, "contrast": 2}))
        restored = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(restored["operation_id"], result["operation_id"])
        self.assertEqual(len({a["action_id"] for a in restored["actions"]}), 4)
        self.assertEqual(self.backend.values["exposure"], 0)
        self.assertEqual(self.backend.values["contrast"], 0)
        calls = len(self.backend.calls)
        self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(len(self.backend.calls), calls)

    def test_terminal_reconciliation_never_reopens_restored_or_completed_operations(self):
        adjusted = self.core.dispatch(self.request())
        restored = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": adjusted["operation_id"]})
        self.backend.reconcile = Mock(side_effect=AssertionError("Terminal receipts are immutable during reconciliation"))
        again = self.core.dispatch({"command": "operations", "action": "reconcile", "operation_id": restored["operation_id"]})
        self.assertEqual(again["status"], "restored")
        self.assertTrue(again["existing"])
        self.assertFalse(self.core.dispatch({"command": "context"})["pending_operations"])
        self.backend.reconcile.assert_not_called()

    def test_restore_refuses_manual_changes(self):
        result = self.core.dispatch(self.request())
        self.backend.values["exposure"] = 3
        restored = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": result["operation_id"]})
        self.assertEqual(restored["error"]["code"], "restore_conflict")
        self.assertEqual(self.backend.values["exposure"], 3)

    def test_reconciliation_needs_backend_evidence_not_user_success(self):
        self.backend.fail_field = "exposure"
        result = self.core.dispatch(self.request())
        self.backend.reconcile = lambda record: {"outcome": "restored", "target": self.backend.target}
        output = self.core.dispatch({"command": "operations", "action": "reconcile", "operation_id": result["operation_id"],
                                     "success": True})
        self.assertEqual(output["error"]["code"], "missing_evidence")
        self.assertTrue(self.core.dispatch({"command": "context"})["pending_operations"])

    def test_readonly_reconciliation_can_confirm_without_restoring(self):
        self.backend.fail_field = "exposure"
        result = self.core.dispatch(self.request())
        calls = len(self.backend.calls)
        output = self.core.dispatch({"command": "operations", "action": "reconcile", "operation_id": result["operation_id"]})
        self.assertEqual(output["status"], "applied_not_restored")
        self.assertEqual(len(self.backend.calls), calls)
        self.assertFalse(self.core.dispatch({"command": "context"})["pending_operations"])

    def test_recover_keeps_operation_and_journals_new_action_before_dispatch(self):
        pending = self.pending_preview()
        def confirm():
            record = self.core.journal.get(operation_id=pending["operation_id"])
            self.assertEqual(record["actions"][-1]["status"], "dispatching")
            self.backend.calls.append(("confirm", 1))
            return {"accepted": True}
        def recover(record):
            self.backend.action_hook("preview.confirm", confirm, intent={"path": "fixed"})
            return self.recovery_result()
        self.backend.recover = Mock(side_effect=recover)
        output = self.core.dispatch({"command": "operations", "action": "recover", "operation_id": pending["operation_id"],
                                     "steps": ["ignored arbitrary instruction"]})
        self.assertEqual(output["status"], "applied_not_restored")
        self.assertEqual(output["operation_id"], pending["operation_id"])
        self.assertEqual(len(output["actions"]), 2)
        self.assertNotEqual(output["actions"][0]["action_id"], output["actions"][1]["action_id"])
        self.assertNotIn("steps", self.backend.recover.call_args.args[0])
        self.core.dispatch({"command": "operations", "action": "recover", "operation_id": pending["operation_id"]})
        self.assertEqual(self.backend.recover.call_count, 1)
        self.assertEqual(self.backend.calls, [("confirm", 1)])
        self.assertIsNone(self.backend.action_hook)

    def test_recover_never_replays_an_unknown_recovery_action(self):
        pending = self.pending_preview()
        def confirm():
            self.backend.calls.append(("confirm", 1))
            raise TimeoutError("Confirmation response lost")
        def recover(record):
            self.backend.action_hook("preview.confirm", confirm, intent={"path": "fixed"})
            return self.recovery_result()
        self.backend.recover = recover
        request = {"command": "operations", "action": "recover", "operation_id": pending["operation_id"]}
        first = self.core.dispatch(request)
        self.assertEqual(first["status"], "outcome_unknown")
        second = self.core.dispatch(request)
        self.assertEqual(second["status"], "outcome_unknown")
        self.assertEqual(second["error"]["code"], "recovery_action_unresolved")
        self.assertEqual(len(second["actions"]), 2)
        self.assertEqual(self.backend.calls, [("confirm", 1)])

    def test_recover_reuses_completed_receipt_after_post_action_read_failure(self):
        pending = self.pending_preview()
        reads = 0
        def confirm():
            self.backend.calls.append(("confirm", 1))
            return {"accepted": True}
        def recover(record):
            nonlocal reads
            result = self.backend.action_hook("preview.confirm", confirm, intent={"path": "fixed"})
            self.assertTrue(result["accepted"])
            reads += 1
            if reads == 1:
                raise TimeoutError("Read after confirmation failed")
            return self.recovery_result()
        self.backend.recover = recover
        request = {"command": "operations", "action": "recover", "operation_id": pending["operation_id"]}
        self.assertEqual(self.core.dispatch(request)["status"], "partial")
        second = self.core.dispatch(request)
        self.assertEqual(second["status"], "applied_not_restored")
        self.assertEqual(len(second["actions"]), 2)
        self.assertEqual(self.backend.calls, [("confirm", 1)])

    def test_recover_rejects_changed_intent_for_an_existing_action_name(self):
        pending = self.pending_preview()
        destination = "first"
        def confirm():
            self.backend.calls.append(("confirm", 1))
            return {"accepted": True}
        def recover(record):
            self.backend.action_hook("preview.confirm", confirm, intent={"path": destination})
            raise RuntimeError("Configuration still unverified")
        self.backend.recover = recover
        request = {"command": "operations", "action": "recover", "operation_id": pending["operation_id"]}
        self.core.dispatch(request)
        destination = "different"
        second = self.core.dispatch(request)
        self.assertEqual(second["error"]["code"], "recovery_action_conflict")
        self.assertEqual(self.backend.calls, [("confirm", 1)])

    def test_recover_cannot_bypass_another_pending_operation(self):
        pending = self.pending_preview()
        source = Path(self.temp.name) / "another-legacy.json"
        source.write_text("{}", encoding="utf-8")
        self.core.import_legacy({"operation_id": "another", "target": self.backend.target}, source)
        self.backend.recover = Mock()
        output = self.core.dispatch({"command": "operations", "action": "recover", "operation_id": pending["operation_id"]})
        self.assertEqual(output["error"]["code"], "pending_operation")
        self.backend.recover.assert_not_called()

    def test_recover_wrong_target_evidence_cannot_unlock(self):
        pending = self.pending_preview()
        result = self.recovery_result()
        result["target"]["variant"]["id"] = "other-variant"
        self.backend.recover = Mock(return_value=result)
        output = self.core.dispatch({"command": "operations", "action": "recover", "operation_id": pending["operation_id"]})
        self.assertEqual(output["status"], "partial")
        self.assertEqual(output["error"]["code"], "target_changed")
        self.assertTrue(self.core.dispatch({"command": "context"})["pending_operations"])

    def test_image_side_effects_have_individual_actions(self):
        request = self.request()
        request = {key: value for key, value in request.items() if key not in {"module", "patch"}}
        request.update(command="images", action="create_candidate", fail_second=True)
        result = self.core.dispatch(request)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(len(result["actions"]), 2)
        self.core.dispatch(request)
        self.assertEqual(self.backend.calls, [("image", "clone")])

    def test_legacy_import_keeps_source_hash_and_blocks(self):
        source = Path(self.temp.name) / "legacy.json"
        source.write_text('{"old":"evidence"}', encoding="utf-8")
        imported = self.core.import_legacy({"operation_id": "f69d11e1217e43e490f196e434a861ba", "target": self.backend.target}, source)
        self.assertEqual(imported["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(imported["status"], "outcome_unknown")
        blocked = self.core.dispatch(self.request())
        self.assertEqual(blocked["error"]["code"], "pending_operation")

    def test_unsupported_and_invalid_patches_never_dispatch(self):
        for values, module in [({"curve": [0, 1]}, "curve"), ({"temperature": 5000}, "exposure"),
                               ({"exposure": True}, "exposure"), ({"exposure": 1000}, "exposure")]:
            result = self.core.dispatch(self.request(values, module=module))
            self.assertEqual(result["status"], "refused")
        self.assertFalse(self.backend.calls)

    def test_numeric_capability_uses_preflight_observation_without_context_roundtrip(self):
        request = self.request()
        capabilities = self.backend.context()["capabilities"]
        observed = []
        def derive(observation):
            observed.append(copy.deepcopy(observation))
            return capabilities
        self.backend.capabilities_for = derive
        self.backend.context = Mock(side_effect=AssertionError("Capabilities must use the existing observation"))
        applied = self.core.dispatch(request)
        self.assertEqual(applied["status"], "control_confirmed")
        self.assertEqual(observed[0]["target"], request["target"])
        self.assertEqual(observed[0]["observation_token"], request["observation_token"])
        restored = self.core.dispatch({"command": "operations", "action": "restore", "operation_id": applied["operation_id"]})
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(observed[-1]["values"]["exposure"], 1)
        self.backend.context.assert_not_called()

    def test_nonnumeric_operation_has_no_unused_context_read(self):
        request = self.request()
        request.update(command="images", action="clone")
        self.backend.context = Mock(side_effect=AssertionError("Image action does not need numeric capabilities"))
        self.assertEqual(self.core.dispatch(request)["status"], "completed")
        self.backend.context.assert_not_called()

    def test_patch_schema_rejection_precedes_all_native_observation(self):
        request = self.request()
        self.backend.observe = Mock(side_effect=AssertionError("Invalid schema must be rejected before a native read"))
        self.backend.context = Mock(side_effect=AssertionError("Invalid schema must not query capabilities"))
        for module, patch_values in (("curves", {"curve": [0, 1]}), ("exposure", {"temperature": 5000}),
                                     ("exposure", {"exposure": True}), ("exposure", {})):
            with self.subTest(module=module, patch=patch_values):
                output = self.core.dispatch({**request, "module": module, "patch": patch_values})
                self.assertEqual(output["status"], "refused")
        self.backend.observe.assert_not_called()
        self.backend.context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
