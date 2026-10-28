"""Read-only evidence tests using generated receipts and sidecars only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from c1ai.native_reconcile import reconcile


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uuid = "c257430f-28c1-4e68-a05b-f55475c8b09d"
        self.other_uuid = "11111111-2222-4333-8444-555555555555"
        self.target = {"instance": {"pid": 7, "started_at": "old-start", "version": "test"},
                       "document": {"path": str(self.root / "Synthetic.cosessiondb")},
                       "image": {"path": str(self.root / "same.NEF")},
                       "variant": {"id": self.uuid, "label": "same.NEF [2]"}, "layer": "Background"}
        table = [[0.0] * 18 for _ in range(9)]
        table[0][1], table[2][4], table[3][2] = 6.0, -11.0, 0.899999
        self.table = self.table_text(table)
        window = {"pid": 7, "title": "Synthetic", "version": "test"}
        self.old = {"id": "legacy-operation", "created_at": 1000,
                    "before_state": {"window": window,
                                     "values": {"selection": {"name": "same.NEF [2]"},
                                                "edit_scope": {"name": "1 个，共 2 个"}}},
                    "requested_style_patch": {"ColorCorrections": self.table},
                    "apply": {"accepted": True, "identity_verified": True, "window": window,
                              "before": {"toggle": "Off"}, "after": {"toggle": "On"}},
                    "error": {"details": {"possibly_mutated": False}}}
        self.source = self.root / "legacy.json"
        self.sidecar = self.root / "same.NEF.cos"
        self.record = {"operation_id": "legacy-operation", "legacy": True, "target": copy.deepcopy(self.target)}
        self.backend = SimpleNamespace(state_dir=self.root, config={"legacy_reconciliation": {
            "operation_id": "legacy-operation", "source_path": str(self.source), "sidecar_path": str(self.sidecar),
            "target": copy.deepcopy(self.target),
            "audit_scope": "import_and_single_style_toggle_only", "stopped_processes": [{"pid": 7, "stopped": True}]}})
        self.backend.observe = Mock(side_effect=AssertionError("Legacy reconciliation must not read or operate UI"))
        self.write_source()
        self.write_sidecar([(self.uuid, [self.table])])

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def table_text(rows):
        return ";".join(",".join(str(value) for value in row) for row in rows)

    def write_source(self, synchronize_import=True):
        self.source.write_text(json.dumps(self.old), encoding="utf-8")
        checksum = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.backend.config["legacy_reconciliation"]["source_sha256"] = checksum
        if synchronize_import:
            self.record.update(source_path=str(self.source.resolve()), source_sha256=checksum)

    def write_sidecar(self, variants):
        document = ET.Element("CaptureOne")
        for uuid, tables in variants:
            variant = ET.SubElement(document, "VAR")
            ET.SubElement(variant, "E", K="UUID", V=uuid)
            for index, table in enumerate(tables):
                style = ET.SubElement(variant, "SL")
                ET.SubElement(style, "E", K="UUID", V=f"c1-rewritten-style-{index}")
                ET.SubElement(style, "E", K="ColorCorrections", V=table)
        ET.ElementTree(document).write(self.sidecar, encoding="utf-8", xml_declaration=True)
        os.utime(self.sidecar, (2000, 2000))

    def assert_legacy_rejected(self):
        try:
            result = reconcile(self.backend, self.record)
        except (RuntimeError, ValueError):
            return
        self.assertEqual(result["outcome"], "unknown")

    def test_complete_unique_style_match_confirms_attachment_only(self):
        before = self.sidecar.read_bytes()
        result = reconcile(self.backend, self.record)
        self.assertEqual(result["outcome"], "applied_not_restored")
        self.assertEqual(result["evidence"]["scope"], "style_attachment_persisted")
        self.assertFalse(result["evidence"]["effective_mixer_parameters_verified"])
        self.assertFalse(result["evidence"]["restored"])
        self.assertFalse(result["evidence"]["model_verified"])
        self.assertEqual(result["evidence"]["variant_uuid"], self.uuid)
        self.assertEqual(self.sidecar.read_bytes(), before)
        self.backend.observe.assert_not_called()

    def test_equal_distinctive_values_do_not_replace_full_table_comparison(self):
        rows = [row.split(",") for row in self.table.split(";")]
        rows[-1][-1] = "0.25"
        self.write_sidecar([(self.uuid, [self.table_text(rows)])])
        self.assert_legacy_rejected()

    def test_missing_last_table_column_does_not_match(self):
        rows = [row.split(",") for row in self.table.split(";")]
        rows[-1].pop()
        self.write_sidecar([(self.uuid, [self.table_text(rows)])])
        self.assert_legacy_rejected()

    def test_correct_table_in_other_variant_cannot_substitute_target_uuid(self):
        self.write_sidecar([(self.uuid, ["0,0;0,0"]), (self.other_uuid, [self.table])])
        self.assert_legacy_rejected()

    def test_record_uuid_cannot_replace_the_audited_recovery_target(self):
        self.write_sidecar([(self.uuid, [self.table]), (self.other_uuid, [self.table])])
        self.record["target"]["variant"]["id"] = self.other_uuid
        self.assert_legacy_rejected()

    def test_missing_or_duplicate_exact_variant_uuid_is_rejected(self):
        for variants in ([(self.other_uuid, [self.table])],
                         [(self.uuid, [self.table]), (self.uuid.upper(), [self.table])]):
            with self.subTest(variants=variants):
                self.write_sidecar(variants)
                self.assert_legacy_rejected()

    def test_multiple_matching_style_layers_are_not_unique_evidence(self):
        self.write_sidecar([(self.uuid, [self.table, self.table])])
        self.assert_legacy_rejected()

    def test_older_sidecar_and_unaccepted_toggle_do_not_unlock(self):
        os.utime(self.sidecar, (500, 500))
        self.assert_legacy_rejected()
        self.write_sidecar([(self.uuid, [self.table])])
        self.old["apply"]["accepted"] = False
        self.write_source()
        self.assert_legacy_rejected()

    def test_source_modified_after_hash_is_rejected(self):
        self.source.write_text("{}", encoding="utf-8")
        self.assert_legacy_rejected()

    def test_unfinished_mutation_audit_does_not_unlock(self):
        self.backend.config["legacy_reconciliation"]["audit_scope"] = "unknown"
        self.assert_legacy_rejected()

    def test_receipt_operation_id_cannot_be_substituted(self):
        self.old["id"] = "different-operation"
        self.write_source()
        self.assert_legacy_rejected()

    def test_original_selection_label_cannot_be_substituted(self):
        self.old["before_state"]["values"]["selection"]["name"] = "other.NEF [2]"
        self.write_source()
        self.assert_legacy_rejected()

    def test_config_source_hash_cannot_replace_imported_receipt(self):
        self.old["created_at"] = 999
        self.write_source(synchronize_import=False)
        self.assert_legacy_rejected()

    def test_original_document_window_cannot_be_substituted(self):
        self.old["before_state"]["window"]["title"] = "Other Session"
        self.write_source()
        self.assert_legacy_rejected()

    def test_empty_value_nonadjust_operations_cannot_unlock(self):
        for command in ("images", "preview", "context", "read"):
            with self.subTest(command=command):
                record = {"target": copy.deepcopy(self.target), "before": {"values": {}},
                          "request": {"command": command}, "actions": []}
                result = reconcile(self.backend, record)
                self.assertEqual(result["outcome"], "unknown")
        self.backend.observe.assert_not_called()

    def test_numeric_reconciliation_requires_actual_patch_and_exact_target(self):
        record = {"target": copy.deepcopy(self.target), "before": {"values": {"exposure": 0}},
                  "request": {"command": "adjust", "patch": {}}}
        self.assertEqual(reconcile(self.backend, record)["outcome"], "unknown")
        self.backend.observe.assert_not_called()
        record["request"]["patch"] = {"exposure": 1}
        observed = {"target": copy.deepcopy(self.target), "values": {"exposure": 1}}
        self.backend.observe = Mock(return_value=observed)
        self.assertEqual(reconcile(self.backend, record)["outcome"], "applied_not_restored")
        observed["target"]["variant"]["id"] = self.other_uuid
        self.assertEqual(reconcile(self.backend, record)["outcome"], "unknown")

    def test_preview_recovery_requires_one_saved_export_receipt(self):
        record = {"target": self.target, "request": {"command": "preview"}, "status": "partial",
                  "actions": [{"name": "preview.export", "status": "completed", "intent": {"receipt": {
                      "schema": "c1ai-export-receipt-v1", "before": {"target": self.target}, "job": "one"}}}]}
        result = {"target": self.target, "render_verified": True, "source": "capture_one_export"}
        with patch("c1ai.native_preview.read_export", return_value=result) as reader:
            self.assertEqual(reconcile(self.backend, record)["outcome"], "applied_not_restored")
            reader.assert_called_once()
        record["actions"].append(copy.deepcopy(record["actions"][0]))
        with patch("c1ai.native_preview.read_export") as reader:
            self.assertEqual(reconcile(self.backend, record)["outcome"], "unknown")
            reader.assert_not_called()

    def test_numeric_restore_reconciliation_requires_recorded_restore_and_complete_bounded_values(self):
        baseline = {"exposure": 0, "contrast": 0, "brightness": 7}
        record = {"target": copy.deepcopy(self.target), "before": {"values": baseline},
                  "request": {"command": "adjust", "patch": {"exposure": 1, "contrast": 2}},
                  "actions": [{"name": "set:exposure"}, {"name": "set:contrast"}]}
        self.backend.observe = Mock(return_value={"target": copy.deepcopy(self.target), "values": dict(baseline)})
        self.assertEqual(reconcile(self.backend, record)["outcome"], "unknown")
        record["actions"].append({"name": "restore:contrast", "status": "outcome_unknown"})
        result = reconcile(self.backend, record)
        self.assertEqual(result["outcome"], "restored")
        self.assertFalse(result["evidence"]["model_verified"])
        for values, outcome in (({**baseline, "exposure": 1}, "partial"),
                                ({**baseline, "exposure": 0.5}, "unknown"),
                                ({**baseline, "brightness": 8}, "unknown"),
                                ({"exposure": 0, "contrast": 0}, "unknown")):
            with self.subTest(values=values):
                self.backend.observe.return_value["values"] = values
                self.assertEqual(reconcile(self.backend, record)["outcome"], outcome)


    def test_completed_export_reconciliation_cannot_read_reused_output_slot(self):
        record = {"target": self.target, "request": {"command": "preview"}, "status": "completed",
                  "actions": [{"name": "preview.export", "status": "completed", "intent": {"receipt": {
                      "schema": "c1ai-export-receipt-v1", "before": {"target": self.target}}}}]}
        with patch("c1ai.native_preview.read_export") as reader, self.assertRaises(RuntimeError):
            reconcile(self.backend, record)
        reader.assert_not_called()

    def test_export_window_only_navigation_uses_qualified_readonly_helper(self):
        before = {"target": self.target, "values": {"exposure": 0.2}}
        record = {"target": self.target, "before": before, "request": {"command": "preview"}, "status": "partial",
                  "actions": [{"name": "preview.open_export", "status": "completed", "intent": {"before": before},
                               "result": {"accepted": True}}]}
        self.backend.observe = Mock(return_value=copy.deepcopy(before))
        with patch("c1ai.native_preview.read_node", return_value={"automation_id": "export", "control_type": "Window"}):
            result = reconcile(self.backend, record)
        self.assertEqual(result["outcome"], "not_applied")
        self.assertTrue(result["evidence"]["navigation_applied"])
        self.assertFalse(result["evidence"]["export_dispatched"])
        self.assertFalse(result["evidence"]["model_verified"])
        for status in ("dispatching", "outcome_unknown", "not_dispatched"):
            record["actions"][0]["status"] = status
            self.backend.observe.reset_mock()
            self.assertEqual(reconcile(self.backend, record)["outcome"], "unknown")
            self.backend.observe.assert_not_called()

    def test_completed_open_with_other_effect_does_not_unlock_as_navigation(self):
        record = {"target": self.target, "before": {"target": self.target, "values": {}},
                  "request": {"command": "preview"}, "status": "partial",
                  "actions": [{"name": "preview.open_export", "status": "completed", "result": {"accepted": True}},
                              {"name": "preview.select_recipe", "status": "completed", "result": {"accepted": True}}]}
        self.assertEqual(reconcile(self.backend, record)["outcome"], "unknown")
        self.backend.observe.assert_not_called()

    def test_normal_prepare_destination_receipts_use_destination_recovery(self):
        record = {"target": self.target, "request": {"command": "preview", "action": "prepare"}, "status": "partial",
                  "actions": [{"name": "preview.select_owned_directory", "status": "outcome_unknown"}]}
        expected = {"outcome": "unknown", "target": self.target, "evidence": {"scope": "test"}}
        with patch("c1ai.native_preview.recover_owned_destination", return_value=expected) as helper:
            self.assertEqual(reconcile(self.backend, record), expected)
            helper.assert_called_once_with(self.backend, record)

    def test_historical_menu_only_clone_without_inventory_baseline_is_not_a_reusable_exception(self):
        before = {"target": self.target, "values": {}}
        record = {"operation_id": "7020beaa44fa43e9a15c1ed59ad2094c", "target": self.target, "before": before,
                  "request": {"command": "images", "action": "clone"}, "status": "partial",
                  "actions": [{"name": "images.open_menu", "status": "completed", "intent": {"before": before},
                               "result": {"accepted": True, "after": {"expand_state": "Expanded"}}}]}
        result = reconcile(self.backend, record)
        self.assertEqual(result["outcome"], "unknown")
        self.backend.observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
