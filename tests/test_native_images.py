import copy
import unittest
import uuid

from c1ai.native_images import (BROWSER, SUMMARY, VIEWER, NativeAdapterError,
                               clone_selected, list_images, parse_summary, recover_clone_navigation, select_image)


def variant_uuid(value):
    return str(uuid.UUID(int=value))


class ImageBackend:
    def __init__(self, selection_pattern=True):
        self.window = {"pid": 7, "hwnd": 8, "title": "Synthetic", "version": "16.8.6"}
        self.rows = [(1, "a.NEF"), (2, "b.NEF")]
        self.selected = 1
        self.label = "a.NEF"
        self.pattern = selection_pattern
        self.actions = []
        self.action_records = []
        self.calls = []
        self.truncated = False
        self.browser_truncated = False
        self.clone_extra = False
        self.clone_unselected = False
        self.clone_changed_document = False
        self.clone_insert_before_existing = False
        self.inventory_invalid = False
        self.summary_override = None
        self.hide_clone_in_browser = False
        self.command_enabled = True
        self.hook_allowed = True
        self.switch_document = False
        self.document = {"path": "synthetic.cosessiondb"}

    def summary(self):
        return self.summary_override or f"1 个，共 {len(self.rows)} 个"

    def variant_inventory(self, observation):
        filename = self.label.split(" [")[0]
        rows = [{"id": variant_uuid(variant), "index": i, "browser_variant_id": variant}
                for i, (variant, _) in enumerate(row for row in self.rows if row[1] == filename)]
        if self.inventory_invalid:
            rows[0]["id"] = "not-a-uuid"
        return rows

    def observe(self, fields):
        return {"target": {"variant": {"id": variant_uuid(self.selected), "label": self.label},
                           "document": copy.deepcopy(self.document),
                           "image": {"path": self.label.split(" [")[0]}}, "values": {},
                "raw_state": {"window": copy.deepcopy(self.window), "values": {
                    "selection": {"name": self.label}, "edit_scope": {"name": self.summary()}}}}

    def guard(self, observation):
        return {"window_title": self.window["title"], "bindings": {}, "expected": {}}

    def action(self, name, fn, intent=None):
        if not self.hook_allowed:
            raise RuntimeError("journal unavailable")
        self.actions.append(name)
        record = {"name": name, "intent": copy.deepcopy(intent), "status": "dispatching"}
        self.action_records.append(record)
        result = fn()
        record.update(status="completed", result=copy.deepcopy(result))
        return result

    def call(self, action, **payload):
        self.calls.append((action, copy.deepcopy(payload)))
        result = {"window": copy.deepcopy(self.window)}
        selector = payload.get("selector", {})
        if action == "read":
            if selector.get("name") == "复制变体":
                return {**result, "node": {"name": "复制变体", "control_type": "MenuItem", "enabled": self.command_enabled,
                        "offscreen": False, "patterns": ["Invoke"],
                        "selector": {"name": "复制变体", "control_type": "MenuItem"}}}
            value = self.summary() if selector == SUMMARY else self.label
            return {**result, "node": {"name": value}}
        if action == "inspect":
            if selector == BROWSER:
                nodes = []
                for variant, filename in self.rows:
                    if self.hide_clone_in_browser and variant == 3:
                        continue
                    name = f"Variant: {filename} - VariantID: {variant}"
                    node = {"name": name, "control_type": "DataItem", "enabled": True, "offscreen": False,
                            "patterns": ["SelectionItem"] if self.pattern else ["SynchronizedInput"],
                            "selector": {"name": name, "control_type": "DataItem"}}
                    if self.pattern:
                        node["selected"] = variant == self.selected
                    nodes.append(node)
            else:
                nodes = [{"name": "复制变体", "control_type": "MenuItem",
                          "selector": {"name": "复制变体", "control_type": "MenuItem"}}]
            return {**result, "nodes": nodes,
                    "truncated": self.truncated or (self.browser_truncated and selector == BROWSER), "read_errors": []}
        if action == "invoke":
            if selector.get("name") == "图像(I)":
                return {**result, "accepted": True, "after": {"expand_state": "Expanded"}}
            if payload["mode"] == "select":
                self.selected = int(selector["name"].rsplit(" ", 1)[1])
                self.label = dict(self.rows)[self.selected]
                if self.switch_document:
                    self.document["path"] = "other.cosessiondb"
            elif selector.get("name") == "复制变体":
                if self.clone_insert_before_existing:
                    self.rows.insert(2, (3, "a.NEF"))
                else:
                    self.rows.append((3, "a.NEF"))
                if self.clone_extra:
                    self.rows.append((4, "a.NEF"))
                if not self.clone_unselected:
                    self.selected, self.label = 3, "a.NEF [2]"
                if self.clone_changed_document:
                    self.document["path"] = "other.cosessiondb"
            return {**result, "accepted": True}
        raise AssertionError(action)


class NativeImageTests(unittest.TestCase):
    def test_list_never_promotes_browser_id_to_model_id(self):
        result = list_images(ImageBackend(False))
        self.assertIsNone(result["items"][0]["selected"])
        self.assertFalse(result["production_batch_qualified"])

    def test_unsupported_selection_dispatches_nothing(self):
        backend = ImageBackend(False)
        with self.assertRaises(NativeAdapterError) as error:
            select_image(backend, 2)
        self.assertEqual(error.exception.code, "NATIVE_SELECTION_UNSUPPORTED")
        self.assertEqual(backend.actions, [])

    def test_native_selection_uses_hook_and_independent_readback(self):
        backend = ImageBackend()
        result = select_image(backend, 2)
        self.assertEqual(backend.actions, ["images.select"])
        self.assertTrue(result["selection_pattern_verified"])
        self.assertFalse(result["selected_variant_id_readback_verified"])
        self.assertEqual(result["after"]["target"]["variant"]["id"], variant_uuid(2))

    def test_noop_selection_has_no_mutation(self):
        backend = ImageBackend()
        self.assertFalse(select_image(backend, 1)["changed"])
        self.assertEqual(backend.actions, [])

    def test_selection_cannot_adopt_another_document_with_same_native_id(self):
        backend = ImageBackend()
        backend.switch_document = True
        with self.assertRaises(NativeAdapterError) as caught:
            select_image(backend, 2)
        self.assertEqual(caught.exception.code, "SELECTION_OUTCOME_UNKNOWN")
        self.assertEqual(backend.actions, ["images.select"])

    def test_invalid_source_inventory_blocks_clone(self):
        backend = ImageBackend()
        backend.inventory_invalid = True
        with self.assertRaises(NativeAdapterError):
            clone_selected(backend)
        self.assertEqual(backend.actions, [])

    def test_truncated_browser_is_only_supplemental_to_source_inventory(self):
        backend = ImageBackend(False)
        backend.browser_truncated = True
        result = clone_selected(backend)
        self.assertTrue(result["new_source_variant_verified"])
        self.assertFalse(result["new_browser_entry_verified"])
        self.assertEqual(result["browser_evidence"]["code"], "INCOMPLETE_INSPECTION")

    def test_clone_identifies_one_candidate_without_claiming_native_selection(self):
        backend = ImageBackend(False)
        result = clone_selected(backend)
        self.assertEqual(result["candidate_browser_variant_id"], 3)
        self.assertEqual(backend.actions, ["images.open_menu", "images.clone"])
        self.assertFalse(result["selected_variant_id_readback_verified"])
        self.assertEqual(result["candidate_variant_id"], variant_uuid(3))
        self.assertEqual(result["after"]["raw_state"]["values"]["selection"]["name"], "a.NEF [2]")
        self.assertFalse(any(action == "inspect" and payload.get("selector", {}).get("name") == "图像(I)"
                             for action, payload in backend.calls))
        self.assertEqual(backend.action_records[0]["intent"]["source_variants"], result["source_variants_before"])

    def test_multiple_new_candidates_stop_without_retry(self):
        backend = ImageBackend(False)
        backend.clone_extra = True
        with self.assertRaises(NativeAdapterError) as error:
            clone_selected(backend)
        self.assertEqual(error.exception.code, "CLONE_OUTCOME_UNKNOWN")
        self.assertEqual(backend.actions.count("images.clone"), 1)

    def test_clone_in_filtered_virtualized_browser_uses_same_source_db_delta(self):
        backend = ImageBackend(False)
        backend.summary_override = "1/81 (滤镜)"
        backend.hide_clone_in_browser = True
        result = clone_selected(backend)
        self.assertTrue(result["new_source_variant_verified"])
        self.assertFalse(result["new_browser_entry_verified"])
        self.assertTrue(result["browser_evidence"]["virtualized"])
        self.assertFalse(result["browser_evidence"]["enumeration_complete"])
        self.assertEqual(len(result["source_variants_before"]), 1)
        self.assertEqual(len(result["source_variants_after"]), 2)
        self.assertEqual(backend.actions.count("images.clone"), 1)

    def test_unique_new_variant_must_be_currently_bound_variant(self):
        backend = ImageBackend(False)
        backend.clone_unselected = True
        with self.assertRaises(NativeAdapterError) as caught:
            clone_selected(backend)
        self.assertEqual(caught.exception.code, "CLONE_OUTCOME_UNKNOWN")
        self.assertEqual(backend.actions.count("images.clone"), 1)

    def test_clone_allows_existing_variant_ordinal_to_shift_without_changing_uuid(self):
        backend = ImageBackend(False)
        backend.rows.append((5, "a.NEF"))
        backend.clone_insert_before_existing = True
        result = clone_selected(backend)
        old = {row["id"]: row["index"] for row in result["source_variants_before"]}
        new = {row["id"]: row["index"] for row in result["source_variants_after"]}
        self.assertEqual(old[variant_uuid(5)], 1)
        self.assertEqual(new[variant_uuid(5)], 2)
        self.assertEqual(result["candidate_variant_id"], variant_uuid(3))

    def test_clone_cannot_adopt_new_document(self):
        backend = ImageBackend(False)
        backend.clone_changed_document = True
        with self.assertRaises(NativeAdapterError) as caught:
            clone_selected(backend)
        self.assertEqual(caught.exception.code, "CLONE_OUTCOME_UNKNOWN")
        self.assertEqual(backend.actions.count("images.clone"), 1)

    def test_filtered_summary_fullmatch_and_known_punctuation_only(self):
        self.assertEqual(parse_summary("1/81 (滤镜)")["total_count"], 81)
        self.assertTrue(parse_summary("1/81 (滤镜)")["filtered"])
        for text in ("1/81 (滤镜)\n", "1/81", "1/81 (filtered)", "prefix 1/81 (滤镜)",
                     "1/81.0 (滤镜)", "1 个，共 2 个\n", "1/0 (滤镜)"):
            with self.subTest(text=text), self.assertRaises(NativeAdapterError):
                parse_summary(text)

    def test_filtered_multiple_selection_is_refused_before_clone(self):
        backend = ImageBackend(False)
        for summary in ("0/81 (滤镜)", "2/81 (滤镜)"):
            backend.summary_override = summary
            with self.subTest(summary=summary), self.assertRaises(NativeAdapterError):
                clone_selected(backend)
        self.assertEqual(backend.actions, [])

    def test_list_reports_virtualization_without_claiming_all_photos(self):
        backend = ImageBackend(False)
        backend.summary_override = "1/81 (滤镜)"
        result = list_images(backend)
        self.assertTrue(result["virtualized"])
        self.assertFalse(result["enumeration_complete"])
        self.assertEqual(result["scope"], "current_browser_items_only")
        self.assertEqual(len(result["items"]), 2)

    def test_failed_writeahead_prevents_menu_open_and_clone(self):
        backend = ImageBackend()
        backend.hook_allowed = False
        with self.assertRaises(RuntimeError):
            clone_selected(backend)
        self.assertFalse(any(action == "invoke" for action, _ in backend.calls))

    def menu_only_record(self, backend):
        backend.command_enabled = False
        before = backend.observe([])
        with self.assertRaises(NativeAdapterError):
            clone_selected(backend, before)
        return {"request": {"command": "images", "action": "clone"}, "target": before["target"],
                "before": before, "actions": backend.action_records, "status": "partial"}

    def test_menu_only_recovery_requires_unchanged_persisted_source_inventory(self):
        backend = ImageBackend(False)
        record = self.menu_only_record(backend)
        recovered = recover_clone_navigation(backend, record)
        self.assertEqual(recovered["outcome"], "not_applied")
        self.assertTrue(recovered["evidence"]["navigation_applied"])
        self.assertFalse(recovered["evidence"]["clone_dispatched"])
        self.assertFalse(recovered["evidence"]["unobserved_parameters_verified"])
        self.assertEqual(recovered["evidence"]["observed_fields"], [])
        self.assertEqual(backend.actions, ["images.open_menu"])

    def test_old_menu_only_record_without_source_baseline_is_not_auto_cleared(self):
        backend = ImageBackend(False)
        record = self.menu_only_record(backend)
        record["actions"][0]["intent"].pop("source_variants")
        self.assertEqual(recover_clone_navigation(backend, record)["outcome"], "unknown")

    def test_menu_recovery_never_handles_dispatched_clone(self):
        backend = ImageBackend(False)
        record = self.menu_only_record(backend)
        record["actions"].append({"name": "images.clone", "status": "outcome_unknown"})
        self.assertEqual(recover_clone_navigation(backend, record)["outcome"], "unknown")
        self.assertEqual(backend.actions, ["images.open_menu"])

    def test_menu_only_recovery_refuses_new_source_variant(self):
        backend = ImageBackend(False)
        record = self.menu_only_record(backend)
        backend.rows.append((7, "a.NEF"))
        self.assertEqual(recover_clone_navigation(backend, record)["outcome"], "unknown")

    def test_menu_only_recovery_refuses_changed_observed_field(self):
        backend = ImageBackend(False)
        record = self.menu_only_record(backend)
        record["before"]["values"] = {"exposure": 0}
        record["actions"][0]["intent"]["before"]["values"] = {"exposure": 0}
        self.assertEqual(recover_clone_navigation(backend, record)["outcome"], "unknown")


if __name__ == "__main__":
    unittest.main()
