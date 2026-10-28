"""Synthetic Session/UIA tests. No Capture One process is created or contacted."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from c1ai.windows_backend import NativeError, WindowsBackend, resolve_identity


class WindowsBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "Synthetic.cosessiondb"
        with closing(sqlite3.connect(self.session)) as db, db:
            db.executescript("""
                CREATE TABLE ZPATHLOCATION (Z_PK INTEGER PRIMARY KEY, ZISRELATIVE INTEGER,
                    ZRELATIVEPATH TEXT, ZWINROOT TEXT);
                CREATE TABLE ZIMAGE (Z_PK INTEGER PRIMARY KEY, ZIMAGEFILENAME TEXT,
                    ZIMAGEUUID TEXT, ZIMAGELOCATION INTEGER);
                CREATE TABLE ZVARIANT (Z_PK INTEGER PRIMARY KEY, ZIMAGE INTEGER,
                    ZVARIANTUUID TEXT, ZINDEX INTEGER);
            """)
        self.add_image(1, "Selects", "same.NEF", [(11, "UUID-ORIGINAL", 0), (12, "UUID-CANDIDATE", 1)])

    def tearDown(self):
        self.temp.cleanup()

    def add_image(self, identity, folder, filename, variants):
        directory = self.root / folder
        directory.mkdir(exist_ok=True)
        (directory / filename).write_bytes(b"synthetic raw content")
        with closing(sqlite3.connect(self.session)) as db, db:
            db.execute("INSERT INTO ZPATHLOCATION VALUES (?, 1, ?, '')", (identity, folder))
            db.execute("INSERT INTO ZIMAGE VALUES (?, ?, ?, ?)", (identity, filename, f"IMAGE-{identity}", identity))
            db.executemany("INSERT INTO ZVARIANT VALUES (?, ?, ?, ?)",
                           [(pk, identity, uuid, index) for pk, uuid, index in variants])

    def backend(self, label="same.NEF [2]", scope="1 个，共 2 个"):
        backend = WindowsBackend.__new__(WindowsBackend)
        backend.state_dir = self.root
        backend.config = {"session_path": str(self.session), "qualified_version": "test-version",
                          "qualified_fields": ["exposure", "temperature", "tint"]}
        backend.window = {"pid": 7, "hwnd": 8, "title": "Synthetic", "version": "test-version"}
        backend.worker = SimpleNamespace(starts=0)
        backend.action_hook = None
        backend.call = Mock(return_value={"window": copy.deepcopy(backend.window), "values": {
            "selection": {"name": label}, "edit_scope": {"name": scope},
            "base_image": {"image_layer_selected": True}, "exposure": {"value": "0"}}})
        return backend

    def observe(self, backend, fields=None):
        with patch("c1ai.windows_backend.process_started", return_value="process-start"):
            return backend.observe([] if fields is None else fields)

    def register(self, observation, **changes):
        target = observation["target"]
        candidate = {"document": target["document"], "image": target["image"],
                     "source_variant": {"id": "uuid-original", "label": "same.NEF"}}
        candidate.update(changes)
        (self.root / "candidates.json").write_text(json.dumps({target["variant"]["id"]: candidate}), encoding="utf-8")

    def test_unique_filename_and_ordinal_yield_saved_uuid_without_native_claim(self):
        result = resolve_identity(self.session, "same.NEF [2]")
        self.assertEqual(result["variant"]["id"], "uuid-candidate")
        self.assertEqual(result["identity_evidence"]["database_variant_id"], 12)
        self.assertFalse(result["identity_evidence"]["selected_uuid_native_readback"])

    def test_same_filename_in_multiple_folders_is_ambiguous(self):
        self.add_image(2, "Capture", "same.NEF", [(21, "OTHER-UUID", 1)])
        with self.assertRaises(NativeError) as raised:
            resolve_identity(self.session, "same.NEF [2]")
        self.assertEqual(raised.exception.code, "AMBIGUOUS_IMAGE")

    def test_missing_source_and_missing_variant_are_not_invented(self):
        with self.assertRaises(NativeError):
            resolve_identity(self.session, "same.NEF [3]")
        (self.root / "Selects" / "same.NEF").unlink()
        with self.assertRaises(NativeError) as raised:
            resolve_identity(self.session, "same.NEF")
        self.assertEqual(raised.exception.code, "SOURCE_UNAVAILABLE")

    def test_identity_and_inventory_connections_are_enforced_readonly(self):
        digest_before = hashlib.sha256(self.session.read_bytes()).hexdigest()
        connect = sqlite3.connect
        opened = []
        def readonly_connection(database, **kwargs):
            self.assertTrue(kwargs.get("uri"))
            self.assertIn("mode=ro", database)
            connection = connect(database, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE ZVARIANT SET ZINDEX = 999")
            opened.append(connection)
            return connection
        try:
            with patch("c1ai.windows_backend.sqlite3.connect", side_effect=readonly_connection):
                mapped = resolve_identity(self.session, "same.NEF [2]")
                result = self.backend().variant_inventory({"target": mapped})
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
        finally:
            for connection in opened:
                connection.close()
        self.assertEqual([row["id"] for row in result], ["uuid-original", "uuid-candidate"])
        self.assertEqual(hashlib.sha256(self.session.read_bytes()).hexdigest(), digest_before)

    def test_only_qualified_single_selection_formats_are_accepted(self):
        for scope in ("1 个，共 81 个", "1/81 (滤镜)"):
            with self.subTest(scope=scope):
                observation = self.observe(self.backend(scope=scope))
                self.assertEqual(observation["evidence"]["single_selected_count"], 1)
        for scope in ("2/81 (滤镜)", "1/0 (滤镜)", "0 个，共 81 个", "selected 1 of 81",
                      "prefix 1/81 (滤镜)", "1/81 (滤镜) suffix", "1/81 (滤镜)\n", "-1/81 (滤镜)"):
            with self.subTest(scope=scope), self.assertRaises(NativeError) as raised:
                self.observe(self.backend(scope=scope))
            self.assertEqual(raised.exception.code, "SINGLE_SELECTION_REQUIRED")

    def test_wrong_document_or_layer_cannot_produce_an_observation(self):
        backend = self.backend()
        backend.call.return_value["window"]["title"] = "Other Session"
        with self.assertRaises(NativeError) as raised:
            self.observe(backend)
        self.assertEqual(raised.exception.code, "DOCUMENT_CHANGED")
        backend = self.backend()
        backend.call.return_value["values"]["base_image"]["image_layer_selected"] = False
        with self.assertRaises(NativeError) as raised:
            self.observe(backend, ["exposure"])
        self.assertEqual(raised.exception.code, "BASE_LAYER_REQUIRED")

    def test_identity_only_observation_can_bind_one_native_adjustment_layer(self):
        backend = self.backend()
        raw = backend.call.return_value
        raw["values"]["base_image"]["image_layer_selected"] = False
        layer = {"runtime_id": [42, 9], "name": "Adjustment 1", "is_image_layer": False}
        backend.call.side_effect = [raw, {"selected": [layer]}]
        observed = self.observe(backend)
        self.assertEqual(observed["target"]["layer"], layer)
        self.assertEqual(observed["values"], {})

    def test_original_and_unregistered_variants_cannot_be_set(self):
        for label in ("same.NEF", "same.NEF [2]"):
            backend = self.backend(label=label)
            observation = self.observe(backend, ["exposure"])
            backend.call.reset_mock()
            with self.assertRaises(NativeError) as raised:
                backend.set_value("exposure", 1, observation)
            self.assertEqual(raised.exception.code, "CANDIDATE_REQUIRED")
            backend.call.assert_not_called()

    def test_registry_uuid_alone_does_not_authorize_another_source(self):
        backend = self.backend()
        observation = self.observe(backend, ["exposure"])
        self.register(observation, image={"path": "another.NEF"})
        backend.call.reset_mock()
        with self.assertRaises(NativeError) as raised:
            backend.set_value("exposure", 1, observation)
        self.assertEqual(raised.exception.code, "CANDIDATE_REQUIRED")
        backend.call.assert_not_called()

    def test_candidate_set_carries_full_numeric_and_target_guard(self):
        backend = self.backend()
        observation = self.observe(backend, ["exposure"])
        self.register(observation)
        backend.call.reset_mock()
        backend.call.return_value = {"accepted": True, "verified": True}
        with patch("c1ai.windows_backend.process_started", return_value="process-start"):
            result = backend.set_value("exposure", 1, observation)
        self.assertFalse(result["model_verified"])
        kwargs = backend.call.call_args.kwargs
        self.assertEqual(kwargs["guard"]["expected"]["exposure"], {"value": "0"})
        self.assertEqual(kwargs["guard"]["expected"]["selection"], {"name": "same.NEF [2]"})
        self.assertEqual(kwargs["post_guard"]["expected"]["base_image"], {"image_layer_selected": True})

    def test_numeric_transport_preserves_existing_focus_without_activation(self):
        backend = self.backend()
        observation = self.observe(backend, ["exposure"])
        self.register(observation)
        backend.call = WindowsBackend.call.__get__(backend, WindowsBackend)
        response = {"ok": True, "result": {"accepted": True, "verified": True}}
        backend.worker = SimpleNamespace(host_path=self.root / "unused-host.ps1",
                                         request=Mock(return_value=SimpleNamespace(stdout=json.dumps(response).encode("utf-8"))))
        with patch("c1ai.windows_backend.process_started", return_value="process-start"):
            backend.set_value("exposure", 1, observation)
        backend.worker.request.assert_called_once()
        payload = json.loads(backend.worker.request.call_args.args[1])
        self.assertEqual(payload["op"], "set")
        self.assertEqual(payload["mode"], "number")
        self.assertIs(payload["background_only"], False)
        self.assertEqual((payload["pid"], payload["hwnd"]), (7, 8))
        self.assertEqual(payload["guard"]["expected"]["exposure"], {"value": "0"})
        self.assertEqual(payload["post_guard"]["expected"]["selection"], {"name": "same.NEF [2]"})

    def test_pid_reuse_stops_candidate_set(self):
        backend = self.backend()
        observation = self.observe(backend, ["exposure"])
        self.register(observation)
        backend.call.reset_mock()
        with patch("c1ai.windows_backend.process_started", return_value="different-start"), self.assertRaises(NativeError):
            backend.set_value("exposure", 1, observation)
        backend.call.assert_not_called()

    def test_context_exposes_writes_only_for_registered_candidate_and_version(self):
        backend = self.backend()
        observation = self.observe(backend)
        backend.observe = Mock(return_value=observation)
        with patch('c1ai.native_windows.find_native_main',return_value={**backend.window,'started_at':'1'}), patch(
                'c1ai.native_windows.native_window_inventory',return_value={'blocking_modal_possible':False}):
            self.assertFalse(backend.context()["capabilities"]["fields"]["exposure"]["writable"])
            self.register(observation)
            self.assertTrue(backend.context()["capabilities"]["fields"]["exposure"]["writable"])
            backend.config["qualified_version"] = "other-version"
            self.assertFalse(backend.context()["capabilities"]["fields"]["exposure"]["writable"])

    def test_blocking_native_window_returns_diagnostics_without_uia(self):
        backend=self.backend();backend.observe=Mock(side_effect=AssertionError('UIA would block'))
        with patch('c1ai.native_windows.find_native_main',return_value={**backend.window,'started_at':'1'}), patch(
                'c1ai.native_windows.native_window_inventory',return_value={'blocking_modal_possible':True}):
            result=backend.context()
        self.assertFalse(result['editing_ready']);self.assertIsNone(result['target'])
        self.assertEqual(result['capabilities'],{'modules':{},'fields':{}})
        backend.observe.assert_not_called()

    def test_empty_viewer_retains_native_diagnostics_without_write_capability(self):
        backend=self.backend();backend.observe=Mock(side_effect=NativeError('CONTROL_NOT_FOUND','no selected image'))
        with patch('c1ai.native_windows.find_native_main',return_value={**backend.window,'started_at':'1'}), patch(
                'c1ai.native_windows.native_window_inventory',return_value={'blocking_modal_possible':False}):
            result=backend.context()
        self.assertFalse(result['editing_ready']);self.assertIsNone(result['target'])
        self.assertEqual(result['reason'],'CONTROL_NOT_FOUND')

    def test_disabled_main_with_no_same_process_dialog_skips_uia(self):
        backend=self.backend();backend.observe=Mock(side_effect=AssertionError('External helper may block UIA'))
        with patch('c1ai.native_windows.find_native_main',return_value={**backend.window,'started_at':'1'}), patch(
                'c1ai.native_windows.native_window_inventory',return_value={'blocking_modal_possible':None,'main_enabled':False}):
            result=backend.context()
        self.assertFalse(result['editing_ready']);backend.observe.assert_not_called()

    def test_capabilities_for_existing_observation_uses_no_native_reads(self):
        backend = self.backend()
        observation = self.observe(backend)
        self.register(observation)
        backend.observe = Mock(side_effect=AssertionError("Capability derivation must not re-observe C1"))
        backend.call.reset_mock()
        capabilities = backend.capabilities_for(observation)
        self.assertTrue(capabilities["fields"]["exposure"]["writable"])
        stale_candidate = copy.deepcopy(observation)
        stale_candidate["target"]["image"]["path"] = "another.NEF"
        self.assertFalse(backend.capabilities_for(stale_candidate)["fields"]["exposure"]["writable"])
        changed_version = copy.deepcopy(observation)
        changed_version["target"]["instance"]["version"] = "unqualified-version"
        self.assertFalse(backend.capabilities_for(changed_version)["fields"]["exposure"]["writable"])
        backend.observe.assert_not_called()
        backend.call.assert_not_called()

    def test_only_explicit_qualified_fields_are_writable_on_a_candidate(self):
        backend = self.backend()
        observation = self.observe(backend)
        self.register(observation)
        fields = backend.capabilities_for(observation)["fields"]
        self.assertEqual({name for name, metadata in fields.items() if metadata["writable"]},
                         {"exposure", "temperature", "tint"})
        self.assertTrue(all(metadata["readable"] for metadata in fields.values()))
        backend.config.pop("qualified_fields")
        self.assertFalse(any(metadata["writable"] for metadata in backend.capabilities_for(observation)["fields"].values()))

    def test_experimental_fields_require_explicit_opt_in_and_still_need_identity_qualification(self):
        backend = self.backend()
        observation = self.observe(backend)
        self.register(observation)
        backend.config["experimental_fields"] = ["contrast"]
        fields = backend.capabilities_for(observation)["fields"]
        self.assertTrue(fields["contrast"]["writable"])
        self.assertIn("experiment", fields["contrast"]["qualification"].lower())
        self.assertFalse(fields["brightness"]["writable"])
        backend.config["qualified_version"] = "other-version"
        self.assertFalse(any(metadata["writable"] for metadata in backend.capabilities_for(observation)["fields"].values()))

    def test_malformed_field_qualification_never_implicitly_enables_fields(self):
        backend = self.backend()
        observation = self.observe(backend)
        self.register(observation)
        for configured in ("exposure", True, {"exposure": True}, ["exposure", "unknown"], ["exposure", None]):
            with self.subTest(configured=configured):
                backend.config["qualified_fields"] = configured
                try:
                    fields = backend.capabilities_for(observation)["fields"]
                except NativeError:
                    continue
                self.assertFalse(any(metadata["writable"] for metadata in fields.values()))

    def test_inventory_returns_only_variants_of_bound_source(self):
        self.add_image(2, "Capture", "other.NEF", [(21, "OTHER-UUID", 0)])
        mapped = resolve_identity(self.session, "same.NEF [2]")
        result = self.backend().variant_inventory({"target": mapped})
        self.assertEqual({row["id"] for row in result}, {"uuid-original", "uuid-candidate"})

    def test_inventory_rejects_duplicate_uuid_anchor(self):
        self.add_image(2, "Capture", "other.NEF", [(21, "UUID-CANDIDATE", 0)])
        mapped = resolve_identity(self.session, "same.NEF [2]")
        with self.assertRaises(NativeError):
            self.backend().variant_inventory({"target": mapped})

    def test_inventory_does_not_substitute_a_different_source_for_target_path(self):
        mapped = resolve_identity(self.session, "same.NEF [2]")
        mapped["image"] = {"path": str(self.root / "other.NEF")}
        with self.assertRaises(NativeError):
            self.backend().variant_inventory({"target": mapped})

    def call_failure(self, response, action="set"):
        backend = self.backend()
        payload = response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")
        backend.worker = SimpleNamespace(host_path=self.root / "unused-host.ps1",
                                         request=Mock(return_value=SimpleNamespace(stdout=payload)))
        with self.assertRaises(Exception) as raised:
            WindowsBackend.call(backend, action, mode="number")
        return raised.exception

    def test_native_write_error_only_certifies_explicit_boolean_false(self):
        response = {"ok": False, "error": {"code": "CONTROL_DISABLED", "message": "Control is disabled",
                                           "details": None, "possibly_mutated": False}}
        error = self.call_failure(response)
        self.assertIs(getattr(error, "dispatched", None), False)
        for flag in (True, None, 0, "false"):
            with self.subTest(flag=flag):
                response["error"]["possibly_mutated"] = flag
                self.assertIsNot(getattr(self.call_failure(response), "dispatched", None), False)

    def test_readonly_error_does_not_certify_the_surrounding_write_action(self):
        response = {"ok": False, "error": {"code": "CONTROL_NOT_FOUND", "message": "Read target missing",
                                           "details": None, "possibly_mutated": False}}
        error = self.call_failure(response, action="snapshot")
        self.assertIsNot(getattr(error, "dispatched", None), False)

    def test_malformed_error_envelope_never_certifies_no_dispatch(self):
        valid_error = {"code": "CONTROL_DISABLED", "message": "Control disabled", "possibly_mutated": False}
        for response in ({"error": valid_error}, {"ok": 0, "error": valid_error},
                         {"ok": False, "error": {"possibly_mutated": False}},
                         {"ok": False, "error": {"code": "", "message": "", "possibly_mutated": False}},
                         b'{"ok":false,"error":'):
            with self.subTest(response=response):
                error = self.call_failure(response)
                self.assertIsNot(getattr(error, "dispatched", None), False)

    def test_worker_timeout_does_not_certify_no_dispatch(self):
        backend = self.backend()
        backend.worker = SimpleNamespace(host_path=self.root / "unused-host.ps1",
                                         request=Mock(side_effect=TimeoutError("worker response lost")))
        with self.assertRaises(TimeoutError) as raised:
            WindowsBackend.call(backend, "set", mode="number")
        self.assertIsNot(getattr(raised.exception, "dispatched", None), False)


if __name__ == "__main__":
    unittest.main()
