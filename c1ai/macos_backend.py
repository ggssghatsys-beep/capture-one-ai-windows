"""Guarded macOS adapter using Capture One's official scripting dictionary."""
from __future__ import annotations

import hashlib
import copy
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import tempfile
import time
import threading
import xml.etree.ElementTree as ET

from .contracts import FIELDS, MODULES, STRUCTURED_MODULES, Refused, digest, validate_structured_patch

APP_PATH = Path("/Applications/Capture One.app")
SUPPORTED_FIELDS = FIELDS
VERIFIED_FIELDS = frozenset(FIELDS)
SUPPORTED_VERSION = "16.8.6.25"
IMPLEMENTED_MODULES = frozenset({"curves", "levels", "color_mixer", "color_balance"})
VERIFIED_MODULES = IMPLEMENTED_MODULES
LOCAL_VERIFIED_FIELDS = frozenset(FIELDS)
LOCAL_VERIFIED_MODULES = IMPLEMENTED_MODULES
LOCAL_VERIFIED_RESET_ACTIONS = frozenset({"exposure", "hdr", "curves", "levels", "color_balance", "color_mixer"})
LOCAL_VERIFIED_PASTE_ACTIONS = frozenset({"exposure", "hdr", "white_balance", "curves", "levels", "color_balance", "color_mixer"})
FIELD_PROPERTIES = {f: f.replace("_", " ") for f in FIELDS}
FIELD_PROPERTIES["highlight_recovery"] = "highlight adjustment"
FIELD_RANGES = {"exposure": (-4, 4, 0.00001), "contrast": (-50, 50, 0.0001),
                "brightness": (-50, 50, 0.0001), "saturation": (-100, 100, 0.0001),
                "temperature": (800, 14000, 0.1), "tint": (-50, 50, 0.0001),
                **{f: (-100, 100, 0.0001) for f in FIELDS if f.endswith("_recovery")}}


def inspect_installation(app_path=APP_PATH):
    """Read vendor metadata/dictionary without launching or controlling the app."""
    app_path = Path(app_path)
    contents = app_path / "Contents"
    with (contents / "Info.plist").open("rb") as source:
        info = plistlib.load(source)
    dictionary = contents / "Resources" / "CaptureOne.sdef"
    raw = dictionary.read_bytes()
    tree = ET.fromstring(raw)
    settings = tree.find(".//class[@name='adjustment settings']")
    if settings is None:
        raise Refused("scripting_dictionary_changed", "No adjustment settings class in the installed dictionary")
    properties = {p.get("name"): p for p in settings.findall("property")}
    fields = {}
    for field in SUPPORTED_FIELDS:
        entry = properties.get(FIELD_PROPERTIES[field])
        if entry is not None:
            access = entry.get("access", "rw")
            fields[field] = {"property": FIELD_PROPERTIES[field], "type": entry.get("type"),
                             "access": access, "apple_event_code": entry.get("code"),
                             "unit": "EV" if field == "exposure" else "K" if field == "temperature" else "C1 value"}
    return {"platform": "macOS", "architecture": platform.machine(),
            "os_version": platform.mac_ver()[0], "application_path": str(app_path),
            "version": info.get("CFBundleShortVersionString"),
            "build": info.get("CFBundleVersion"), "bundle_id": info.get("CFBundleIdentifier"),
            "dictionary": {"path": str(dictionary), "sha256": hashlib.sha256(raw).hexdigest(),
                           "fields": fields},
            "interface": "official_apple_events",
            "live_validation": {"version": SUPPORTED_VERSION,
                                "numeric_modules": ["exposure", "white_balance", "hdr"],
                                "structured_modules": sorted(VERIFIED_MODULES),
                                "scope": "representative_values_on_registered_image_layer_candidates",
                                "visual_review": "capture_one_viewer_and_pilot_exposure_loop",
                                "mcp_image_delivery": True, "preview_backend": "capture_one_pilot",
                                "preview_scope": "selected RAW within the approved collection; saved composite including local layers"}}


class MacOSBackend:
    analysis_display_concurrent = True
    module_actions_supported = True
    batch_supported = True
    navigation_actions = frozenset({"select", "next", "previous"})

    def __init__(self, state_dir=None):
        if state_dir is None:
            from .macos_ipc import state_directory
            state_dir = state_directory()
        self.state_dir = Path(state_dir)
        self.action_hook = None
        self._bridge = None
        self._bridge_init_lock = threading.Lock()
        self._analysis_instance = None

    def native_bridge(self):
        if self._bridge is None:
            with self._bridge_init_lock:
                if self._bridge is None:
                    from .macos_bridge import NativeBridge
                    self._bridge = NativeBridge(self.state_dir)
        return self._bridge

    def _bridge_observation(self, observation):
        from .macos_bridge import configured
        if configured(self.state_dir):
            try:
                native = self.native_bridge().inspect(observation["target"])
                observation["native_context"].update(bridge_target=native["target"],
                    process_revision=native["target"]["revision"], raw_dimensions=native["raw_dimensions"],
                    bridge_mask_actions=native.get("mask_actions", []),
                    bridge_layer_actions=native.get("layer_actions", []))
            except Refused as error:
                observation["native_context"]["bridge_unavailable"] = {"code": error.code, "message": str(error)}
        return observation

    def analysis_observe(self):
        """Read-only fast identity/revision stamp; never saves or registers a write token."""
        from .macos_bridge import configured
        if not configured(self.state_dir):
            raise Refused("native_analysis_unavailable", "Enable the Mac native bridge for live RGB analysis")
        bridge = self.native_bridge()
        instance = self._analysis_instance
        if instance is not None:
            try:
                os.kill(instance["pid"], 0)
            except ProcessLookupError:
                instance = None
        if instance is None:
            # Process metadata only: a display worker must never enter the
            # editing executor's Apple Events/save/action-context path.
            import ctypes
            process = ctypes.CDLL("/usr/lib/libproc.dylib")
            try:
                ids = subprocess.check_output(["/usr/bin/pgrep", "-x", "Capture One"], text=True).split()
            except subprocess.CalledProcessError:
                ids = []
            candidates = []
            for value in ids:
                buffer = ctypes.create_string_buffer(4096)
                pid = int(value)
                if process.proc_pidpath(pid, buffer, len(buffer)) > 0 and os.fsdecode(buffer.value) == str(APP_PATH / "Contents/MacOS/Capture One"):
                    candidates.append(pid)
            if len(candidates) != 1:
                raise Refused("application_instance_required", "Run one installed Capture One instance")
            with (APP_PATH / "Contents/Info.plist").open("rb") as stream:
                version = plistlib.load(stream)["CFBundleShortVersionString"]
            instance = {"platform": "macos", "pid": candidates[0], "version": version}
        snapshot = bridge.call({"instance": instance}, "analysis_snapshot")
        native = snapshot["target"]
        if (native["pid"] != instance["pid"] or ("started_at" in instance and
                not math.isclose(float(instance["started_at"]), snapshot["launched_at"], rel_tol=0, abs_tol=1e-6))):
            raise Refused("analysis_instance_changed", "Native analysis process identity changed")
        instance = {**instance, "started_at": instance.get("started_at", str(snapshot["launched_at"]))}
        self._analysis_instance = instance
        uid = native["layer_uuid"]
        if uid == "Background":
            layer = "Background"
            selected = {"native_id": uid, "is_image_layer": True, "name": "Background"}
        else:
            import uuid
            identifier = str(uuid.UUID(uid)).upper()
            compact = identifier.replace("-", "")
            # Keep the existing public reference layout for exact target matching.
            # This analysis-only observation never issues an editing token; the
            # actual read source is the bound native model, recorded below.
            layer = {"runtime_id": [native["pid"], int(native["variant_id"]), 1] +
                         [int(compact[i:i + 8], 16) for i in range(0, 32, 8)],
                     "name": snapshot["layer_name"], "is_image_layer": False, "native_id": identifier,
                     "variant_uuid": native["variant_uuid"], "identity_source": "session_mask_uuid_readonly"}
            selected = layer
        target = {"instance": instance,
                  "document": {"id": snapshot["document_id"], "path": native["document_path"]},
                  "image": {"id": snapshot["image_id"], "path": native["image_path"]},
                  "variant": {"id": native["variant_id"], "label": snapshot["variant_label"]}, "layer": layer}
        return {"target": target, "values": {}, "native_context": {
                    "analysis_native": True, "bridge_target": native, "process_revision": native["revision"],
                    "viewer_stamp": snapshot.get("viewer_stamp"),
                    "image_path": native["image_path"], "layer_state": {"selected_layer": selected}},
                "evidence": {"source": "native_model_analysis_identity", "read_only": True,
                             "write_qualification": False, "session_saved": False}}

    def _native(self, command, fields, observation=None, **extra):
        environment = inspect_installation()
        declarations = environment["dictionary"]["fields"]
        layer_route = command in {"layers_read", "layers_adjust", "local_read", "local_set", "local_module_read", "local_module_set", "color_editor_paste", "masks_read", "masks_adjust"}
        mutation = command not in {"read", "snapshot", "compare_candidate", "module_read", "local_module_read", "inventory", "layer_inventory", "layers_read", "local_read", "masks_read"}
        photo_only = command in {"inventory", "select", "next", "previous", "layer_inventory"}
        if any(f not in declarations or declarations[f]["type"] != "real"
               or declarations[f]["access"] not in ({"rw"} if command in {"set", "local_set"} else {"r", "rw"}) for f in fields):
            raise Refused("scripting_dictionary_changed", "Requested field is absent from the installed scripting dictionary")
        if mutation and not getattr(self, "action_context", None):
            raise Refused("journal_required", "Native mutation requires a durable Core action")
        request = {"app_path": str(APP_PATH), "bundle_id": environment["bundle_id"],
                   "version": environment["version"], "fields": list(fields), "command": command, **extra}
        if observation is not None:
            request.update(target=observation["target"], before={} if observation.get("module") or observation.get("navigation") else observation["values"])
            if photo_only:
                request["target"] = {**request["target"], "layer": None}
            request["expected_engine"] = observation.get("native_context", {}).get("processing_engine")
            if layer_route:
                request["before_layers"] = observation.get("native_context", {}).get("layer_state")
        if request.get("module") == "color_mixer":
            # The shared official helper exposes only Basic/Advanced. Persisted
            # Skin Tone remains guarded by the Python saved-value checks.
            if "expected_state" in request:
                request["expected_state"] = (observation or {}).get("native_context", {}).get("apple_event_color_state",
                    {**request["expected_state"], "skin": {"available": False}})
            if "source_observation" in request:
                item = request["source_observation"]
                request["source_observation"] = {**item, "state": item.get("native_context", {}).get("apple_event_color_state",
                    {**item["state"], "skin": {"available": False}})}
        try:
            native_dir = Path(__file__).parent / "native"
            # Only trusted bundled source is executable. Requests remain a JSON
            # argv value; both Image and local layers use the same module code.
            source = (native_dir / "macos_adjustments.js").read_text(encoding="utf-8") + "\n" + (
                native_dir / ("macos_layers.js" if layer_route else "macos.js")).read_text(encoding="utf-8")
            result = subprocess.run(
                ["/usr/bin/osascript", "-l", "JavaScript", "-",
                 json.dumps(request, ensure_ascii=False, allow_nan=False)],
                input=source, capture_output=True, text=True, timeout=30 if mutation else 12)
        except subprocess.TimeoutExpired as error:
            failure = Refused("apple_events_timeout", "Capture One scripting timed out. "
                              + ("The result is unknown; query the original request_id and never replay it." if mutation
                                 else "Check macOS Automation permission and the application's dialogs; this was a read-only call."))
            failure.dispatched = mutation
            raise failure from error
        if result.returncode:
            raise Refused("apple_events_error", result.stderr.strip() or "Native scripting process failed")
        payload = json.loads(result.stdout)
        if payload.get("ok") is not True:
            error = payload.get("error", {})
            failure = Refused(error.get("code", "apple_events_error"), error.get("message", "Native call failed"))
            failure.dispatched = error.get("dispatched") is not False
            failure.details = error.get("details")
            raise failure
        observation = payload["observation"]
        if set(observation.get("values", {})) != set(fields):
            raise Refused("incomplete_observation", "Native read did not return all requested fields")
        self._analysis_instance = dict(observation["target"]["instance"])
        observation["evidence"] = {
            "source": "c1_applescript", "interface": "official_apple_events",
            "model_verified": True, "atomic": False,
            "photo_identity_native_readback": True, "adjustment_scope": "none" if photo_only else "background",
            "single_selected_count": 1, "target_checked_before_and_after": True,
            "identity_source": "native_script_ids_scoped_to_process_and_session_file",
            "live_validation": "native_photo_readback" if photo_only else "numeric_read_write_and_viewer_review" if
                observation["target"]["instance"]["version"] == SUPPORTED_VERSION else "unqualified_version",
            "mcp_image_delivery_verified": False,
        }
        if layer_route:
            observation["evidence"].update(
                interface="official_apple_events_with_readonly_session_identity", identity_source="session_mask_uuid_readonly",
                adjustment_scope="background" if observation["target"]["layer"] == "Background" else "local",
                live_validation="representative_subject_layer_numeric_and_structured_loop" if command in
                    {"local_module_read", "local_module_set", "local_read", "local_set"} else
                    "representative_layer_and_mask_operations", session_saved_for_identity=True,
                concurrent_layer_structure_changes_supported=False, render_verified=False)
        return payload

    def observe(self, fields):
        if set(fields) - set(SUPPORTED_FIELDS):
            raise Refused("unsupported", "Requested numeric module is not implemented on Mac")
        try:
            observation = self._native("read", fields)["observation"]
        except Refused as error:
            if error.code != "background_required":
                raise
            observation = self._native("local_read", fields)["observation"]
        return self._bridge_observation(observation)

    def _candidates(self):
        path = self.state_dir / "macos-candidates.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _candidate(self, observation):
        target = {**observation["target"], "layer": "Background"}
        return self._candidates().get(digest(target), {}).get("target") == target

    def _qualified(self, observation):
        return (observation is not None and observation["target"]["instance"]["version"] == SUPPORTED_VERSION
                and (observation["target"]["layer"] == "Background" or self._local(observation))
                and self._candidate(observation))

    @staticmethod
    def _local(observation):
        layer = observation.get("target", {}).get("layer") if observation else None
        state = observation.get("native_context", {}).get("layer_state", {}) if observation else {}
        if not (isinstance(layer, dict) and layer.get("identity_source") == "session_mask_uuid_readonly"
                and not layer.get("is_image_layer") and state.get("selected_layer") == layer):
            return False
        try:
            return state["details"][state["layers"].index(layer)]["kind"] in {
                "adjustment", "filled", "subject mask", "background mask"}
        except (ValueError, KeyError, IndexError, TypeError):
            return False

    def capabilities_for(self, observation=None):
        qualified = self._qualified(observation)
        background = observation is not None and observation["target"].get("layer") == "Background"
        local = self._local(observation)
        from .macos_pilot import available as pilot_available
        preview_available = (background or local) and pilot_available(self.state_dir, observation["target"])
        result = {
            "fields": {field: {"readable": (background or local) and field in SUPPORTED_FIELDS,
                                "writable": qualified and field in (VERIFIED_FIELDS if background else LOCAL_VERIFIED_FIELDS),
                                "nullable": local and field in SUPPORTED_FIELDS,
                                "implemented": field in SUPPORTED_FIELDS,
                                "min": FIELD_RANGES[field][0], "max": FIELD_RANGES[field][1],
                                "tolerance": FIELD_RANGES[field][2], "precision": 0,
                                "backend": "c1_applescript",
                                "qualification": ("native_read_observed" if observation is not None and field in observation.get("values", {})
                                                  else "implemented_pending_live" if field in SUPPORTED_FIELDS else "not_implemented"),
                                "write_verification": "live_verified" if field in (LOCAL_VERIFIED_FIELDS if local else VERIFIED_FIELDS) else "experimental_pending_live",
                                "verified_version": SUPPORTED_VERSION}
                       for field in FIELDS},
            "modules": {module: {"readable": (background or local) and module in IMPLEMENTED_MODULES,
                                  "writable": qualified and module in IMPLEMENTED_MODULES,
                                  "implemented": module in IMPLEMENTED_MODULES,
                                  "live_validation_scope": "representative_subject_mask_values" if local else "representative_image_layer_values",
                                  "qualification": "live_verified" if (background and module in VERIFIED_MODULES or local and module in LOCAL_VERIFIED_MODULES) else
                                      "implemented_pending_live" if module in IMPLEMENTED_MODULES else "not_implemented",
                                  **({"reason": "Stable native layer identity and complete mask state are not available through the qualified route"}
                                     if module in {"layers", "masks"} else {}),
                                  **({"wheel_layout": "red_east_only", "readback_tolerance": 0.003}
                                     if module == "color_balance" else {}),
                                  **({"modes": {"basic": True, "advanced_update": True,
                                                "advanced_add_remove": True,
                                                "skin": qualified and bool(observation.get("native_context", {}).get("skin_ax", {}).get("available")),
                                                "advanced_rgb": True, "advanced_inverted": False},
                                         "skin_availability": ("requires_module_read" if "skin_ax" not in (observation or {}).get("native_context", {}) else
                                                               "ready" if qualified and observation["native_context"]["skin_ax"].get("available") else "unavailable"),
                                         "skin_unavailable_reason": (observation or {}).get("native_context", {}).get("skin_ax", {}).get("reason"),
                                         "skin_qualification": "live_representative_local_hue_update_restore_and_preview",
                                         "skin_scope": "explicit_opt_in_accessibility_update_existing_single_range_seven_fields",
                                         "saturation_bounds": "offsets constrained by the source RGB saturation to absolute 0..100"}
                                     if module == "color_mixer" else {})}
                        for module in STRUCTURED_MODULES},
            "module_actions": {module: {
                "reset": {"available": qualified and module != "white_balance",
                          "kind": "neutral_values", "custom_camera_defaults_used": False,
                          "scope": "basic_advanced_only" if module == "color_mixer" else "complete_exposed_module",
                          "qualification": "live_verified" if module in (LOCAL_VERIFIED_RESET_ACTIONS if local else {"exposure", "curves", "color_mixer"}) else
                              "not_implemented" if module == "white_balance" else "implemented_pending_live"},
                "paste": {"available": qualified,
                          "same_instance_session_and_engine_required": True,
                          "source_scope": "observed_image_or_local_layer", "copies_mask": False, "unset_numeric_source": "reject",
                          **({"complete_explicit_saved_snapshots_required": True,
                              "source_requires_nine_explicit_basic_sectors": True,
                              "scope": "complete_color_editor_including_skin_tone"} if module == "color_mixer" else {}),
                          "qualification": "live_verified" if module in (LOCAL_VERIFIED_PASTE_ACTIONS if local else {"exposure", "curves", "color_mixer"}) else
                              "implemented_pending_live"}}
                for module in (*MODULES, "curves", "levels", "color_mixer", "color_balance")},
            "images": {"collection_inventory": True, "uuid_selection": False,
                       "navigation_from_local_layer": True,
                       "navigation_token_actions": sorted(self.navigation_actions),
                       "native_id_selection": observation is not None,
                       "create_candidate": background and observation["target"]["instance"]["version"] == SUPPORTED_VERSION},
            "layer_inventory": {"readable_in_context": True, "writable": False,
                                "stable_layer_identity": False, "mask_pixels_readable": False},
            "preview": {"default": "native", "native": {"available": preview_available,
                        "render_verified": preview_available, "backend": "capture_one_pilot",
                        "qualification": "live_saved_multilayer_composite_and_subject_exposure_loop",
                        "reason": "Requires explicit Pilot sharing configuration and stable saved Session state",
                        "temporary_lan_service": True, "map_points": False},
                        "render": {"available": False, "export_required": True,
                                   "qualification": "not_implemented"}},
            "batch": {"writable": qualified and background, "max_items": 16, "atomic": False,
                      "explicit_observed_candidates_required": True},
        }
        result["modules"]["layers"] = {
            "readable": observation is not None, "writable": qualified, "implemented": True,
            "actions": ["select", "rename", "create_empty", "duplicate", "move", "set_enabled", "set_opacity", "remove_created"],
            "qualification": "live_select_rename_same_name_layers_and_create_empty",
            "action_qualification": {"move": "live_two_simple_linear_layers_order_and_saved_contents_and_preview", "set_enabled": "live_verified", "set_opacity": "live_verified", "remove_created": "live_verified"},
            "duplicate": {"qualification": "live_simple_linear_layer_full_copy_and_composite_preview", "requires_explicit_ui_configuration": True,
                          "source": "official_duplicate_selected_layer_menu", "optional_name": True},
            "move": {"requires_explicit_mouse_configuration": True, "foreground_and_pointer_used": True,
                     "scope": "visible_collapsed_parent_rows; zero_based_local_index_bottom_to_top"},
            "opacity": {"min": 1, "max": 100, "integer": True, "disable_with_enabled": True},
            "identity_source": "session_mask_uuid_readonly", "session_saved_for_identity": True,
            "concurrent_layer_structure_changes_supported": False}
        result["modules"]["masks"] = {
            "readable": observation is not None, "writable": qualified, "implemented": True,
            "actions": ["luma_range", "clear_luma_range", "create_subject", "create_background", "fill", "clear", "copy", "invert", "feather", "refine", "linear_gradient", "combine", "combine_ai", "select_submask", "delete_submask"], "qualification": "live_representative_native_mask_commands",
            "action_qualification": {"clear_luma_range": "live_verified", "raster_invert": "live_representative_pixel_complement_and_preview",
                                     "linear_gradient_invert": "live_representative_parameter_swap_and_preview_roundtrip",
                                     "radial_gradient_invert": "implemented_pending_live",
                                     "linear_gradient": "experimental_creation_observed_geometry_correspondence_unverified",
                                     "combine": "live_representative_linear_source_add_subtract_intersect_and_preview",
                                     "feather": "live_representative_native_command_and_preview", "refine": "live_representative_native_command_and_preview",
                                     "combine_ai": "live_representative_background_add_subtract_intersect_and_preview", "select_submask": "live_repeated_parent_and_child_selection",
                                     "delete_submask": "live_background_child_removal_parent_preserved"},
            "accessibility_actions": {"requires_explicit_ui_configuration": True,
                                      "combine": "simple_source_into_new_child; native_copy_menu; whole_parent_required",
                                      "combine_ai": "subject_or_background_add_subtract_intersect_on_whole_parent",
                                      "submask_indices": "saved_child_order; live_full_outline_mapping_required"},
            "gradient_creation": {"coordinate_spaces": ["viewer"], "raw_mapping": False,
                                  "experimental": True, "result_status": "partial",
                                  "requested_position_verified": False,
                                  "linear_midpoint": [0.5], "empty_local_layer_required": True,
                                  "fit_view_required": True, "requires_explicit_mouse_configuration": True,
                                  "keyboard_layouts": ["US", "ABC"],
                                  "shortcut": "active_set_unique_unmodified_L", "radial": "not_implemented"},
            "native_filter_scope": "neutral_simple_raster_with_complete_pixel_readback",
            "native_filter_amounts": {"feather": {"min": 0, "max": 100}, "refine": {"min_exclusive": 0, "max": 300}},
            "native_filter_verification": "command_amount_acknowledged; algorithm_and_amount_readback_not_verified",
            "clear_luma_range_scope": "flat_composite_with_exactly_one_direct_luma_and_a_surviving_component",
            "copy_returns_new_mask_uuid": True,
            "read_scope": "layer_identity_saved_mask_structure_and_luma_parameters", "mask_pixels_readable": False,
            "coverage_verified": False, "fill_density": [1], "invert_scope": "constant_empty_or_full_or_completely_decoded_neutral_simple_raster_or_parameter_verified_neutral_constant_fill_linear_radial_gradient",
            "luma_behavior": "first_write_adds_luma_submask; not_intersection_with_existing_mask",
            "concurrent_layer_structure_changes_supported": False}
        if local:
            result["layer_inventory"].update(stable_layer_identity=True, writable=qualified,
                identity_scope="same_instance_session_variant_and_native_mask_uuid")
        from .macos_bridge import configured
        if configured(self.state_dir):
            color_context = (observation or {}).get("native_context", {})
            color_ready = bool(color_context.get("native_color_snapshot") and
                               color_context.get("color_editor_snapshot", {}).get("available"))
            mixer = result["modules"]["color_mixer"] if "modules" in result else None
            if mixer is not None:
                mixer["modes"].update(skin=qualified and color_ready, advanced_inverted=qualified and color_ready)
                mixer.update(skin_availability="ready" if color_ready else "requires_module_read",
                             skin_qualification="live_native_add_range_and_seven_fields_update_remove_and_preview", skin_scope="native_single_range_add_update_remove_all_fields",
                             backend="capture_one_native_bridge", advanced_inverted_qualification="live_native_saved_state_and_preview")
            result["module_actions"]["color_mixer"]["reset"].update(
                available=qualified and color_ready, scope="basic_advanced_skin", qualification="live_native_full_neutral_reset_and_preview")
            ready = bool((observation or {}).get("native_context", {}).get("bridge_target"))
            result["preview"]["native"] = {"available": ready, "backend": "capture_one_native_bridge",
                "qualification": "representative_public_native_loop_16.8.6.25", "temporary_lan_service": False,
                "map_points": False, "mask": ready and local,
                "reason": (observation or {}).get("native_context", {}).get("bridge_unavailable")}
            masks = result["modules"]["masks"]
            masks["actions"].append("radial_gradient")
            masks["mask_pixels_readable"] = ready and local
            masks["coverage_verified"] = "representative_linear_radial_gray_preview" if ready else False
            masks["action_qualification"].update(linear_gradient="representative_public_native_loop_16.8.6.25", radial_gradient="representative_public_native_loop_16.8.6.25")
            masks["gradient_creation"] = {"available": ready and qualified and local, "coordinate_spaces": ["raw"],
                "native": True, "mouse_used": False, "empty_local_layer_required": False,
                "scope": "empty_layer_create_or_same_type_simple_gradient_update",
                "existing_gradient_update": True, "preserves_layer_uuid_and_adjustments": True,
                "linear_midpoint": [0.01, 0.99], "radial": True, "minimum_axis_ratio": 0.01}
            native_actions = (observation or {}).get("native_context", {}).get("bridge_mask_actions", [])
            if native_actions:
                masks["native_bridge_actions"] = native_actions
                masks["action_qualification"].update({action: "representative_native_bridge_saved_state_and_preview"
                                                       for action in native_actions})
                masks["action_qualification"]["radial_gradient_invert"] = "representative_native_bridge_parameter_and_preview"
                masks["native_bridge_whole_parent_actions"] = ["invert", "fill", "clear"]
                masks["invert_scope"] = "neutral_simple_raster_constant_linear_radial; composite_and_luma_parent_refused"
                masks["ai_creation_feature_gate"] = "Capture One CapabilitiesManager.CMFeatureAIMasking"
            layer_actions = (observation or {}).get("native_context", {}).get("bridge_layer_actions", [])
            if layer_actions:
                layers = result["modules"]["layers"]
                layers["native_bridge_actions"] = layer_actions
                layers["duplicate"].update(requires_explicit_ui_configuration=False,
                    source="native_duplicateSelectedLayer", qualification="live_native_simple_linear_full_copy_and_preview")
                layers["move"].update(requires_explicit_mouse_configuration=False,
                    foreground_and_pointer_used=False, scope="observed_uuid_complete_local_order")
                layers["action_qualification"]["move"] = "live_native_two_layer_move_return_saved_contents_and_preview"
                masks["native_bridge_structure_actions"] = layer_actions
                masks["accessibility_actions"].update(requires_explicit_ui_configuration=False,
                    backend="capture_one_native_bridge", submask_indices="saved_child_order_with_native_selectedSubmaskIndex")
                masks["action_qualification"].update(combine="live_native_linear_source_subtract_and_preview",
                    combine_ai="live_native_background_subtract_and_preview",
                    select_submask="live_native_repeated_child_selection", delete_submask="live_native_selected_child_remove_raster_survivor")
            result["analysis"] = {"implemented": True, "native_render": ready, "active_layer": ready,
                "views": ["waveform", "parade", "histogram", "vectorscope"],
                "pixel_transport": "native_uncompressed_BGRA8", "window_precision": "display",
                "default_ai_precision": "exact", "display_max_edge": 640, "exact_max_edge": 1600,
                "live_observation": "native_identity_revision_without_session_save"}
        return result

    def context(self):
        result = {"environment": inspect_installation(), "target": None,
                  "editing_ready": False, "capabilities": self.capabilities_for(),
                  "reason": "registered_candidate_required"}
        try:
            inventory = self._native("layer_inventory", [])
            result.update(layer_inventory=inventory["result"]["layer_inventory"],
                          layer_inventory_target=inventory["observation"]["target"], native_connection="available")
            if result["layer_inventory"]["selected"]["kind"] != "background":
                observation = self.observe(MODULES["exposure"])
                if ({**observation["target"], "layer": None} != inventory["observation"]["target"]
                        or not self._local(observation)):
                    raise Refused("target_changed", "Photo or local layer scope changed while reading context")
                result.update(target=observation["target"], reason="local_adjustments_and_layer_navigation",
                              capabilities=self.capabilities_for(observation), editing_ready=self._qualified(observation),
                              guidance="Read layers, select a returned layer_ref, then read the desired module before editing. Do not concurrently reorder/delete layers.")
                return result
            observation = self.observe(SUPPORTED_FIELDS)
            if {**observation["target"], "layer": None} != inventory["observation"]["target"]:
                raise Refused("target_changed", "Photo changed while reading context")
            result.update(target=observation["target"], evidence=observation["evidence"],
                          native_connection="available", capabilities=self.capabilities_for(observation),
                          editing_ready=self._qualified(observation))
            if result["editing_ready"]:
                result["reason"] = "native_candidate_ready"
        except Refused as error:
            result.update(native_connection="unavailable", reason=error.code,
                          connection_error={"code": error.code, "message": str(error)})
        return result

    @staticmethod
    def _navigation_observation(payload):
        observed = payload["observation"]
        return {**observed, "navigation": True,
                "values": {"collection": payload["result"]["collection"],
                           "selected_ids": [observed["target"]["variant"]["id"]]}}

    def call(self, operation, **payload):
        raise Refused("unsupported", "Mac UI fallback is not implemented")

    def image_action(self, action, request, observation):
        if action == "list":
            scope, offset, limit = request.get("scope", "filtered"), request.get("offset", 0), request.get("limit", 200)
            payload = self._native("inventory", [], scope=scope, offset=offset, limit=limit)
            result, observed = payload["result"], payload["observation"]
            collection = result["collection"]
            token = digest({"instance": observed["target"]["instance"], "document": observed["target"]["document"],
                            "collection": collection, "scope": scope})
            if request.get("collection_token") and request["collection_token"] != token:
                raise Refused("collection_changed", "Collection, filter or sort changed; restart pagination")
            navigation = self._navigation_observation(payload)
            return {"status": "ok", "items": result["items"], "scope": scope,
                    "collection_id": collection["id"], "collection_name": collection["name"],
                    "collection_token": token, "total": result["total"], "offset": offset,
                    "next_offset": offset + len(result["items"]) if offset + len(result["items"]) < result["total"] else None,
                    "enumeration_complete": True, "source": "c1_applescript",
                    "order": "native_scripting_collection", "_navigation_observation": navigation}
        if action in {"select", "next", "previous"}:
            if action == "select" and (request.get("variant_uuid") or type(request.get("variant_id")) is not int or request["variant_id"] < 1):
                raise Refused("unsupported", "Use the observed positive native script variant_id on Mac; it is not a UUID")
            def select():
                payload = self._native(action, [], observation, variant_id=request.get("variant_id"),
                                       expected_collection=observation["values"].get("collection") if observation.get("navigation") else None)
                return {"status": "completed", "after": self._navigation_observation(payload), "model_verified": True}
            return self.action_hook("variant." + action, select, {"variant_id": request.get("variant_id")})
        if action != "clone":
            raise Refused("unsupported", "This image action is not implemented on Mac")
        if observation["target"]["instance"]["version"] != SUPPORTED_VERSION:
            raise Refused("version_unqualified", "Candidate creation requires the checked Mac build")
        def clone():
            payload = self._native("clone", list(observation["values"]), observation)
            after = payload["observation"]
            records = self._candidates()
            records[digest(after["target"])] = {"target": after["target"], "source_target": observation["target"],
                                               "creation": dict(self.action_context)}
            descriptor, temporary = tempfile.mkstemp(prefix="macos-candidates-", dir=self.state_dir)
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(records, output, ensure_ascii=False, allow_nan=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.state_dir / "macos-candidates.json")
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {"status": "completed", "after": after, "model_verified": True,
                    "evidence": payload["result"], "registered_candidate": True}
        return self.action_hook("variant.clone", clone, {"source": observation["target"]})

    def set_values(self, patch, observation):
        local = self._local(observation)
        if not self._qualified(observation) or not patch.keys() <= (LOCAL_VERIFIED_FIELDS if local else VERIFIED_FIELDS):
            error = Refused("unqualified_candidate", "Only a registered same-instance Mac candidate can be edited")
            error.dispatched = False
            raise error
        if local and patch.keys() & {"temperature", "tint"}:
            state = observation.get("native_context", {}).get("layer_state", {})
            try:
                selected = state["layers"].index(state["selected_layer"])
                values = state["details"][selected]["values"]
                initialized = all(values.get(field) is not None for field in ("temperature", "tint"))
            except (KeyError, ValueError, IndexError, TypeError):
                initialized = False
            if not initialized:
                error = Refused("unsupported", "Local White Balance requires both observed temperature and tint; unset initialization is not qualified")
                error.dispatched = False
                raise error
        for field, value in patch.items():
            lower, upper, _ = FIELD_RANGES[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
                error = Refused("out_of_range", "Native scalar is outside the enabled Mac range")
                error.dispatched = False
                raise error
        payload = self._native("local_set" if local else "set", list(observation["values"]), observation, patch=patch)
        return {"status": "completed", "model_verified": True, "observation": payload["observation"]}

    def preview(self, request, observation):
        from .macos_bridge import configured
        if observation.get("native_context", {}).get("analysis_native") is True or configured(self.state_dir):
            return self.native_bridge().render(request, observation)
        from .macos_pilot import render
        return render(self, request, observation)

    def observe_module(self, module):
        if module in {"layers", "masks"}:
            payload = self._native(module + "_read", [], module=module)
            if module == "masks":
                return self._mask_observation(payload)
            from .macos_bridge import configured
            if configured(self.state_dir):
                result = self._mask_observation(payload)
                saved = self._color_editor_untouched(result, result, exclude_color=False)
                if saved["masks"] != result["native_context"]["mask_file_snapshot"]:
                    raise Refused("mask_state_changed", "Saved layer contents changed while binding native actions")
                result["native_context"]["layer_action_baseline"] = {
                    "state": result["state"], "saved": self._layer_action_saved(saved)}
                result["native_context"]["duplicate_snapshot"] = self._saved_layer_digest(saved)
                result.update(module="layers", state=result["native_context"]["layer_state"])
                result["values"] = {"layers": result["state"]}
                return result
            result = self._module_observation(module, payload)
            result["native_context"]["duplicate_snapshot"] = self._duplicate_snapshot(result)
            return result
        if module not in IMPLEMENTED_MODULES:
            raise Refused("unsupported", f"Mac module is not implemented: {module}")
        try:
            payload = self._native("module_read", [], module=module)
        except Refused as error:
            if error.code != "background_required":
                raise
            payload = self._native("local_module_read", [], module=module)
        result = self._module_observation(module, payload)
        return self._color_editor_observation(result) if module == "color_mixer" else result

    @staticmethod
    def _saved_color_editor(observation):
        from .macos_color_editor import read_color_editor
        target, context = observation["target"], observation["native_context"]
        selected = context["layer_state"]["selected_layer"]
        return read_color_editor(target["document"]["path"], target["variant"]["id"], selected["native_id"],
                                 expected_image_id=target["image"]["id"],
                                 expected_engine=context["processing_engine"],
                                 expected_variant_uuid=selected["variant_uuid"])

    def _color_editor_observation(self, result):
        layers = self._native("layers_read", [])
        if layers["observation"]["target"] != result["target"]:
            raise Refused("target_changed", "Color Editor target changed during its official save")
        if (result["native_context"].get("layer_state") is not None and
                result["native_context"]["layer_state"] != layers["result"]["state"]):
            raise Refused("layers_changed", "Color Editor layers changed during its official save")
        result["native_context"]["layer_state"] = layers["result"]["state"]
        try:
            saved = self._saved_color_editor(result)
        except Refused as error:
            if error.code != "color_editor_saved_state_unavailable":
                raise
            saved = {"available": False, "reason": error.code}
        check = self._module_observation("color_mixer", self._native(
            "local_module_read" if self._local(result) else "module_read", [], module="color_mixer"))
        if (check["target"] != result["target"] or check["state"] != {**result["state"], "skin": {"available": False}} or
                check["native_context"]["processing_engine"] != result["native_context"]["processing_engine"] or
                (self._local(result) and check["native_context"]["layer_state"] != result["native_context"]["layer_state"])):
            raise Refused("color_editor_state_changed", "Color Editor changed during saved-value observation")
        if "raw" in saved:
            again = self._saved_color_editor(result)
            if saved["source"] != again["source"] or saved["sha256"] != again["sha256"]:
                raise Refused("color_editor_state_changed", "Saved Color Editor changed during readback")
            saved["available"] = True
        result["native_context"]["color_editor_snapshot"] = saved
        result["evidence"]["complete_color_editor_saved_snapshot"] = saved["available"]
        from .macos_skin import read_skin
        try:
            skin = read_skin(saved)
        except Refused as error:
            skin = {"available": False, "reason": error.code, "source": "session_sqlite_readonly"}
        result["state"] = {**result["state"], "skin": skin}
        result["values"] = {"color_mixer": result["state"]}
        result["native_context"]["apple_event_color_state"] = check["state"]
        from .macos_bridge import configured
        if configured(self.state_dir):
            from .macos_native_color import enrich_state
            native = self.native_bridge().inspect(result["target"])
            live = self.native_bridge().call(result["target"], "color_read", target=native["target"])
            if live["target"] != native["target"]:
                raise Refused("color_editor_state_changed", "Native Color Editor changed during read")
            result["native_context"].update(bridge_target=native["target"],
                process_revision=native["target"]["revision"], bridge_color_actions=native.get("color_actions", []),
                native_color_snapshot=live)
            if saved.get("available"):
                result["state"] = enrich_state(result["state"], saved, live["groups"])
                result["values"] = {"color_mixer": result["state"]}
            result["native_context"]["color_native_guard"] = self._saved_layer_digest(self._color_editor_untouched(result, result))
            result["native_context"]["skin_ax"] = {"available": False, "reason": "native_bridge_preferred"}
            result["evidence"]["skin_read_source"] = "native_color_model_and_complete_saved_session"
        else:
            result["native_context"]["skin_ax"] = self._skin_ax_read(result, optional=True)
            result["evidence"]["skin_read_source"] = "saved_session_only; not_exposed_by_apple_events"
        return result

    def _skin_ax_read(self, observation, *, optional=False):
        """A capability is offered only for opted-in, actually readable controls."""
        try:
            from .macos_ax import run_ax
            from .macos_skin import FIELDS
            skin = observation["state"].get("skin", {})
            if not skin.get("available") or len(skin.get("ranges", [])) != 1:
                raise Refused("skin_range_required", "Read one explicit existing Skin Tone range")
            controls = run_ax(self.state_dir, observation["target"], "read_skin")
            values = controls.get("values", {})
            if set(values) != set(FIELDS) or any(type(values[key]) not in (int, float)
                    or not math.isfinite(values[key]) or not math.isclose(values[key], skin["ranges"][0][key],
                        rel_tol=0, abs_tol=max(1e-5, abs(skin["ranges"][0][key]) * 1e-6)) for key in FIELDS):
                raise Refused("skin_ui_state_mismatch", "Visible Skin Tone controls do not match the saved selected layer")
            return {**controls, "available": True}
        except (Refused, ImportError, OSError) as error:
            if not optional:
                error.dispatched = False
                raise
            return {"available": False, "reason": getattr(error, "code", "skin_ui_unavailable")}

    def _check_color_editor_current(self, expected):
        current = self.observe_module("color_mixer")
        old, new = (item.get("native_context", {}) for item in (expected, current))
        a, b = old.get("color_editor_snapshot", {}), new.get("color_editor_snapshot", {})
        if (current["target"] != expected["target"] or current["state"] != expected["state"]
                or old.get("layer_state") != new.get("layer_state")
                or any(a.get(key) != b.get(key) for key in ("available", "source", "sha256"))):
            raise Refused("stale_observation", "Color Editor, hidden saved settings or layer state changed")
        return current

    def _color_route(self, observation):
        """A token is bound to the route actually used for its Color read."""
        from .macos_bridge import configured
        enabled = configured(self.state_dir)
        observed_native = bool(observation.get("native_context", {}).get("native_color_snapshot"))
        if enabled != observed_native:
            error = Refused("color_route_changed", "Color Editor backend configuration changed; read Color Editor again before editing")
            error.dispatched = False
            raise error
        return enabled

    def _adjust_native_color(self, patch, observation):
        """One guarded detached-model edit, verified against all saved columns."""
        from .macos_native_color import verify_live_saved, verify_saved_change, validate_range_patch
        self._color_route(observation)
        validate_range_patch(observation["state"], patch)
        context = observation.get("native_context", {})
        native = context.get("native_color_snapshot")
        action = "reset" if patch.get("action") == "reset" else patch.get("mode")
        if (not native or action not in context.get("bridge_color_actions", [])
                or native.get("target") != context.get("bridge_target")):
            raise Refused("native_color_observation_required", "Read Color Editor with the matching native bridge before editing")
        verify_live_saved(context.get("color_editor_snapshot", {}), native["groups"])
        def apply():
            wrote, receipt = False, None
            try:
                current = self._check_color_editor_current(observation)
                now = current["native_context"]
                if (now.get("bridge_target") != context["bridge_target"]
                        or now.get("native_color_snapshot") != native
                        or now.get("color_native_guard") != context.get("color_native_guard")):
                    raise Refused("stale_observation", "Native Color Editor revision or model changed")
                untouched = self._color_editor_untouched(current, current)
                try:
                    receipt = self.native_bridge().apply_color(current["target"], context["bridge_target"],
                        self.action_context["operation_id"], patch, native["groups"])
                except Exception as error:
                    wrote = getattr(error, "dispatched", True)
                    raise
                wrote = True
                after = self.observe_module("color_mixer")
                if (after["target"] != current["target"]
                        or after["native_context"]["layer_state"] != now["layer_state"]
                        or after["native_context"].get("bridge_target") != receipt.get("after_target")
                        or self._color_editor_untouched(after, after) != untouched):
                    raise Refused("native_color_readback_mismatch", "Native Color Editor changed target, layer, another adjustment or mask")
                proof = verify_saved_change(now["color_editor_snapshot"],
                    after["native_context"]["color_editor_snapshot"], receipt, patch)
                self.native_bridge().call(after["target"], "color_release", operation_id=self.action_context["operation_id"])
                if patch.get("action") == "reset":
                    proof.update(reset_kind="neutral_values", reset_scope="basic_advanced_skin",
                                 skin_tone_reset=True, reset_to_c1_defaults=False)
                return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                        "evidence": proof}
            except Exception as error:
                error.dispatched = wrote
                if receipt is not None:
                    error.details = receipt
                raise
        return self.action_hook("color_mixer.native_" + action, apply, {"patch": patch})

    def _adjust_skin(self, patch, observation):
        from .macos_skin import FIELDS, read_skin, verify_skin_update
        if (patch.get("action", "update") != "update" or patch.get("index") != 0
                or set(patch) - {"mode", "action", "index"} - set(FIELDS)):
            raise Refused("unsupported", "Mac Skin Tone supports seven fields on the existing single range only")
        changes = {key: value for key, value in patch.items() if key in FIELDS}
        if not changes:
            raise Refused("invalid_patch", "Specify an existing Skin Tone field")
        saved = observation.get("native_context", {}).get("color_editor_snapshot", {})
        skin = read_skin(saved)
        if len(skin["ranges"]) != 1:
            raise Refused("skin_range_required", "Create a Skin Tone range in Capture One before updating its values")
        for key, value in changes.items():
            _, _, low, high = FIELDS[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise Refused("out_of_range", "Skin Tone value is outside the qualified native range")

        def apply():
            wrote = False
            try:
                from .macos_ax import run_ax
                current = self._check_color_editor_current(observation)
                untouched = self._color_editor_untouched(observation, observation)
                for field, value in changes.items():
                    current = self._check_color_editor_current(current)
                    controls = self._skin_ax_read(current)
                    previous = current["native_context"]["color_editor_snapshot"]
                    old_value = current["state"]["skin"]["ranges"][0][field]
                    if value != old_value:
                        def before_skin_dispatch():
                            self._check_color_editor_current(current)
                            bound = self._native("layers_read", [])
                            if (bound["observation"]["target"] != current["target"] or
                                    bound["result"]["state"] != current["native_context"]["layer_state"]):
                                raise Refused("target_changed", "Skin Tone target changed immediately before UI dispatch")
                            saved_now = self._saved_color_editor(current)
                            if saved_now["source"] != previous["source"] or saved_now["sha256"] != previous["sha256"]:
                                raise Refused("skin_state_changed", "Skin Tone changed immediately before UI dispatch")
                            if self._color_editor_untouched(observation, observation) != untouched:
                                raise Refused("skin_state_changed", "Saved adjustments or masks changed during UI lookup")
                        try:
                            receipt = run_ax(self.state_dir, current["target"], "set_skin", field=field,
                                             value=value, expected_old=controls["values"][field],
                                             before_dispatch=before_skin_dispatch)
                        except Exception as error:
                            wrote = wrote or getattr(error, "dispatched", True)
                            raise
                        wrote = wrote or receipt.get("dispatched") is not False
                    after = self.observe_module("color_mixer")  # Official save, no direct persistence writes.
                    if (after["target"] != current["target"] or after["native_context"]["layer_state"] != current["native_context"]["layer_state"]):
                        raise Refused("target_changed", "Skin Tone update changed its selected layer or unrelated native parameters")
                    verify_skin_update(previous, after["native_context"]["color_editor_snapshot"], 0, {field: value})
                    if self._color_editor_untouched(observation, observation) != untouched:
                        raise Refused("skin_readback_mismatch", "Skin Tone update changed another saved parameter or mask")
                    current = after
                proof = verify_skin_update(saved, current["native_context"]["color_editor_snapshot"], 0, changes)
                return {"status": "control_confirmed", "requested_result_verified": True, "after": current,
                        "evidence": {**proof, "interface": "explicit_opt_in_macos_accessibility",
                                     "model_verified": False, "persisted_readback_verified": True,
                                     "other_parameters_and_masks_unchanged": True, "atomic": False}}
            except Exception as error:
                error.dispatched = wrote
                raise
        return self.action_hook("color_mixer.skin_update", apply, {"patch": patch})

    @staticmethod
    def _color_editor_untouched(before, source, *, exclude_color=True, include_masks=True):
        """All real-layer parameters and masks, excluding derived combined settings."""
        import sqlite3
        from .macos_pilot import _saved_database_content, _sidecar_bytes
        target = before["target"]
        records = {}
        connection = sqlite3.connect(Path(target["document"]["path"]).as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            for item in (before, source):
                vid = int(item["target"]["variant"]["id"])
                if vid in records:
                    continue
                variant = dict(connection.execute("SELECT * FROM ZVARIANT WHERE Z_PK=?", (vid,)).fetchone())
                rows = [dict(row) for row in connection.execute("SELECT * FROM ZVARIANTLAYER WHERE ZVARIANT=?", (vid,))]
                metadata = [dict(row) for row in connection.execute(
                    "SELECT m.* FROM ZVARIANTMETADATA m JOIN ZVARIANTLAYER l ON m.Z_PK=l.ZMETADATA WHERE l.ZVARIANT=?", (vid,))]
                content = _saved_database_content(variant, rows, metadata)["layers"]
                content.pop("combined")  # C1's derived composite, not another editable layer.
                for row in content.values():
                    row.pop("Z_OPT", None)  # Core Data storage revision, not an adjustment.
                    if isinstance(row.get("ZMETADATA"), dict):
                        row["ZMETADATA"].pop("Z_OPT", None)
                records[vid] = content
            layer = before["native_context"]["layer_state"]["selected_layer"]
            role = "background" if layer["is_image_layer"] else "local:" + layer["native_id"].lower()
            if exclude_color:
                records[int(target["variant"]["id"])][role].pop("ZCOLORCORRECTIONS")
        except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
            raise Refused("color_editor_guard_unavailable", "Cannot bind complete non-Color-Editor saved parameters") from error
        finally:
            connection.close()
        masks = {}
        for item in ((before, source) if include_masks else ()):
            raw = Path(item["target"]["image"]["path"])
            path = raw.parent / "CaptureOne" / "Settings1680" / (raw.name + ".comask")
            if str(path) in masks:
                if masks[str(path)] is None and len(item["native_context"]["layer_state"]["layers"]) > 1:
                    raise Refused("saved_state_unavailable", "Local layers require the shared saved mask file")
                continue
            content = _sidecar_bytes(path, 256 * 1024 * 1024, optional=len(item["native_context"]["layer_state"]["layers"]) == 1)
            masks[str(path)] = hashlib.sha256(content).hexdigest() if content is not None else None
        return {"layers": records, "masks": masks}

    def _paste_color_editor(self, patch, before, source):
        if source.get("module") != "color_mixer":
            raise Refused("wrong_source", "Read the complete source Color Editor before pasting")
        for item in (source, before):
            saved = item.get("native_context", {}).get("color_editor_snapshot", {})
            if not saved.get("available") or "raw" not in saved:
                raise Refused("color_editor_snapshot_required", "Complete explicit saved source and destination Color Editor observations are required")
        from .macos_color_editor import require_explicit_basic_source
        require_explicit_basic_source(source["native_context"]["color_editor_snapshot"])
        def apply():
            # The observed source is never silently replaced by a newer live source.
            try:
                fresh = self.observe_module("color_mixer")
                if (fresh["target"] != before["target"] or fresh["state"] != before["state"] or
                        fresh["native_context"]["layer_state"] != before["native_context"]["layer_state"]):
                    raise Refused("stale_observation", "Destination Color Editor or layer state changed")
                for item in (source, before):
                    saved = item["native_context"]["color_editor_snapshot"]
                    now = self._saved_color_editor(item)
                    if now["source"] != saved["source"] or now["sha256"] != saved["sha256"]:
                        raise Refused("stale_source" if item is source else "stale_observation", "Saved Color Editor differs from its observation")
                untouched = self._color_editor_untouched(before, source)
            except Exception as error:
                error.dispatched = False
                raise
            payload = self._native("color_editor_paste", [], before, module="color_mixer",
                                   expected_state=before["state"], source_observation=source,
                                   destination_snapshot=before["native_context"]["color_editor_snapshot"])
            # A later failure follows a possible write; preserve outcome-unknown.
            try:
                after = self._color_editor_observation(self._module_observation("color_mixer", payload))
                wanted = source["native_context"]["color_editor_snapshot"]
                source_after = self._saved_color_editor(source)
                destination_after = after["native_context"]["color_editor_snapshot"]
                if (not destination_after.get("available") or destination_after["sha256"] != wanted["sha256"] or
                        source_after["source"] != wanted["source"] or source_after["sha256"] != wanted["sha256"] or
                        self._color_editor_untouched(before, source) != untouched):
                    raise Refused("paste_readback_mismatch", "Complete Color Editor or unchanged parameter/mask evidence differs")
            except Exception as error:
                error.dispatched = True
                raise
            return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                    "evidence": {"source": "official_apple_events_with_readonly_session_evidence", "model_verified": True,
                                 "paste_scope": "complete_color_editor_including_skin_tone", "complete_saved_value_verified": True,
                                 "other_parameters_and_masks_unchanged": True, "render_verified": False, "atomic": False}}
        return self.action_hook("color_mixer.paste", apply, {"source_observation_token": patch["source_observation_token"]})

    def _mask_observation(self, payload):
        from .macos_masks import read_mask_file, MaskReadError
        result = self._module_observation("masks", payload)
        saved_parameters = self._mask_ax_parameters(result)
        parameter_snapshot = self._mask_ax_parameter_snapshot(result, saved_layers=saved_parameters)
        selected = result["state"]["selected_layer"]
        if not selected["is_image_layer"]:
            result["native_context"]["gradient_selection_point"] = saved_parameters[int(result["target"]["variant"]["id"])][
                "local:" + selected["native_id"].lower()].get("ZLAYERSELECTPOINT")
        raw = Path(result["target"]["image"]["path"])
        path = raw.parent / "CaptureOne" / "Settings1680" / (raw.name + ".comask")
        # The RAW's file may contain masks owned only by sibling variants.
        # Bind those bytes even when this candidate has no local layers.
        records = []
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        try:
            for layer in result["state"]["layers"]:
                if layer["is_image_layer"]:
                    continue
                mask = read_mask_file(path, layer["native_id"])
                root = mask["structure"]
                if (layer == result["state"]["selected_layer"] and root.get("mask_type") == 0
                        and root.get("composition_raw") == [1, 1.0, 0, 0]
                        and not any(key in root for key in ("children", "luma_range", "other_fields",
                                                           "constant_fill", "linear_gradient_raw", "radial_gradient_raw"))):
                    try:
                        decoded = read_mask_file(path, layer["native_id"], decode_raster=True)
                        if decoded["file_sha256"] != mask["file_sha256"]:
                            raise Refused("mask_state_changed", "Mask file changed before raster decoding")
                        mask = decoded
                    except MaskReadError:
                        # Unsupported pixels must not prevent ordinary structure
                        # reads; pixel-dependent commands reject this evidence.
                        mask["raster"] = {"decoded": False, "reason": "qualified_raster_decoder_unavailable"}
                current = mask.pop("file_sha256")
                mask.pop("blocks_count")  # Global allocation is not this layer's content.
                if file_digest is not None and file_digest != current:
                    raise MaskReadError("Mask file changed between layer reads")
                file_digest = current
                records.append(mask)
        except MaskReadError as error:
            raise Refused("mask_state_unavailable", str(error)) from error
        check = self._native("layers_read", [])
        if check["observation"]["target"] != result["target"] or check["result"]["state"] != result["state"]:
            raise Refused("mask_state_changed", "Native layer state changed during saved mask read")
        if (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None) != file_digest:
            raise Refused("mask_state_changed", "Saved mask changed during native readback")
        if self._mask_ax_parameter_snapshot(result) != parameter_snapshot:
            raise Refused("mask_state_changed", "Saved layer parameters changed during mask observation")
        result["native_context"]["mask_ax_parameters"] = parameter_snapshot
        result["native_context"]["mask_file_snapshot"] = {str(path): file_digest}
        # Keep the native guard separate from the richer module observation.
        state = {**result["state"], "masks": records}
        result.update(state=state, values={"masks": state})
        modes = {0: "add", 5: "subtract", 2: "intersect"}
        result["submask_inventory"] = [{"layer_ref": layer,
            "children": [{"index": index, "mask_type": child.get("mask_type"),
                          "generator": child.get("metadata", {}).get("MaskGenerator"),
                          "mode": modes.get((child.get("composition_raw") or [None, None, None])[2]),
                          "content_sha256": child["content_sha256"]}
                         for index, child in enumerate(mask["structure"].get("children", []))],
            "selection_source": "live_accessibility_required; saved_SelectedSubmaskIndex_is_not_live"}
            for layer in state["layers"] if not layer["is_image_layer"]
            for mask in records if mask["uuid"] == layer["native_id"]]
        from .macos_bridge import configured
        try:
            if configured(self.state_dir):
                raise Refused("native_bridge_preferred", "RAW gradient creation uses the configured native bridge")
            from .macos_ax import run_ax
            view = run_ax(self.state_dir, result["target"], "read_gradient_view")["view"]
            result["gradient_view"] = {"available": True, "coordinate_space": "viewer", "view": view,
                                       "raw_coordinate_mapping": False}
        except Refused as error:
            result["gradient_view"] = {"available": False, "reason": error.code}
        result["evidence"].update(mask_structure_source="session_comask_readonly",
            mask_pixels_decoded=any(mask.get("raster", {}).get("decoded") is True for mask in records),
            decoded_scope="selected_neutral_simple_raster_only", coverage_verified=False,
            luma_behavior="first_write_adds_a_luma_submask; subsequent_writes_update_native_luma")
        return self._bridge_observation(result)

    @staticmethod
    def _module_observation(module, payload):
        state = payload["result"]["state"]
        observation = payload["observation"]
        return {**observation, "module": module, "state": state, "values": {module: state},
                "evidence": {**observation["evidence"], "native_numeric_readback": True,
                             "render_verified": False,
                             "live_validation": "live_verified" if module in (VERIFIED_MODULES if
                                 observation["target"]["layer"] == "Background" else LOCAL_VERIFIED_MODULES) else "implemented_pending_live"}}

    @staticmethod
    def _mask_ax_layout(observation, view):
        """Join the complete reversed outline to native UUID order; never join by name."""
        state, rows = observation["state"], view.get("rows")
        if not isinstance(rows, list) or not rows:
            raise Refused("mask_ui_layout_unqualified", "Read the complete native Layers outline")
        if any(row.get("index") != index or type(row.get("AXDisclosureLevel")) is not int
               or row["AXDisclosureLevel"] not in (0, 1) for index, row in enumerate(rows)):
            raise Refused("mask_ui_layout_unqualified", "Nested or incomplete Layers outlines are unsupported")
        roots = [index for index, row in enumerate(rows) if row["AXDisclosureLevel"] == 0]
        if len(roots) != len(state["layers"]):
            raise Refused("mask_ui_layout_unqualified", "Visible parent rows do not cover the complete native layer stack")
        masks = {mask["uuid"]: mask for mask in state["masks"]}
        mapping = {}
        def text_matches(row, names):
            texts = row.get("cell_texts")
            return isinstance(texts, list) and len(texts) == 1 and isinstance(texts[0], str) and texts[0] in names
        labels = {"AISubject": {"Subject", "主题"}, "AIBackground": {"Background", "背景"}}
        for position, parent_row in enumerate(roots):
            native_index = len(roots) - position - 1
            layer = state["layers"][native_index]
            parts = rows[parent_row].get("cell_texts")
            # AXRow itself has no label on the qualified build. Its sole cell
            # contains ordered static texts, independent of selection imagery.
            # These labels only cross-check the complete native UUID ordering.
            parent_matches = layer["is_image_layer"] and text_matches(rows[parent_row], {layer["name"], "Image Layer", "图像图层"})
            if not layer["is_image_layer"] and isinstance(parts, list):
                if (len(parts) == 2 and parts[1] == layer["name"]
                        and isinstance(parts[0], str) and parts[0].endswith("%")):
                    try:
                        opacity = float(parts[0][:-1])
                        parent_matches = (math.isfinite(opacity) and 0 <= opacity <= 100
                            and math.isclose(opacity, state["details"][native_index]["opacity"], rel_tol=0, abs_tol=0.0001))
                    except (ValueError, TypeError, KeyError, IndexError):
                        parent_matches = False
            if not parent_matches:
                raise Refused("mask_ui_layout_unqualified", "Parent row labels do not match the full reversed native inventory")
            end = roots[position + 1] if position + 1 < len(roots) else len(rows)
            child_rows = list(range(parent_row + 1, end))
            children = [] if layer["is_image_layer"] else masks[layer["native_id"]]["structure"].get("children", [])
            if any("children" in child for child in children):
                raise Refused("mask_ui_layout_unqualified", "Nested child selection is outside the qualified interface")
            expanded = rows[parent_row].get("AXDisclosing") is True
            if len(child_rows) != (len(children) if expanded else 0):
                raise Refused("mask_ui_layout_unqualified", "Expanded child rows do not match the saved component count")
            row_map = {-1: parent_row}
            for offset, row_index in enumerate(child_rows):
                child_index = len(children) - offset - 1
                child = children[child_index]
                metadata = child.get("metadata", {})
                names = set(labels.get(metadata.get("MaskGenerator"), set()))
                names.update(metadata[key] for key in ("Name", "MaskName") if isinstance(metadata.get(key), str))
                if child.get("mask_type") == 257:
                    names.update({"Luma Range", "亮度范围"})
                if isinstance(metadata.get("SubmaskName"), str) and metadata["SubmaskName"]:
                    names = {metadata["SubmaskName"]}
                if not names or not text_matches(rows[row_index], names):
                    raise Refused("mask_ui_layout_unqualified", "Child row identifiers do not match their saved reversed component order")
                row_map[child_index] = row_index
            mapping[layer["native_id"]] = {"layer_ref": layer, "parent_row_index": parent_row, "row_map": row_map}
        selected = view.get("selected_rows")
        actual = [index for index, row in enumerate(rows) if row.get("AXSelected") is True]
        if selected != actual or len(actual) != 1:
            raise Refused("mask_ui_selection_unqualified", "Exactly one live Layers outline row must be selected")
        parent = mapping[state["selected_layer"]["native_id"]]
        indices = [index for index, row in parent["row_map"].items() if row == actual[0]]
        if len(indices) != 1:
            raise Refused("mask_ui_selection_unqualified", "The live row does not belong to the natively selected parent UUID")
        return {"source": "macos_accessibility", "layer_ref": parent["layer_ref"],
                "submask_index": indices[0], "rows": rows, "row_index": actual[0],
                "parent_row_index": parent["parent_row_index"], "row_map": parent["row_map"]}

    def _mask_ax_parameters(self, observation, *, allow_rename=False):
        content = self._color_editor_untouched(observation, observation, exclude_color=False, include_masks=False)["layers"]
        if allow_rename:
            selected = observation["state"]["selected_layer"]
            content[int(observation["target"]["variant"]["id"])]["local:" + selected["native_id"].lower()].pop("ZNAME")
        return content

    def _duplicate_snapshot(self, observation):
        return self._saved_layer_digest(self._color_editor_untouched(observation, observation, exclude_color=False))

    @staticmethod
    def _saved_layer_digest(saved):
        encoded = json.dumps(saved, sort_keys=True, separators=(",", ":"),
                             default=lambda value: {"blob_hex": value.hex()} if isinstance(value, bytes) else value)
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _verify_layer_duplicate(before, after, saved_before, saved_after):
        """C1 appends one complete copy; all original layers remain byte-identical."""
        for key in ("instance", "document", "image", "variant"):
            if before["target"][key] != after["target"][key]:
                raise Refused("target_changed", "Layer duplication changed photo or Session")
        old, new = before["state"], after["state"]
        source, selected = old["selected_layer"], new["selected_layer"]
        if (source["is_image_layer"] or len(new["layers"]) != len(old["layers"]) + 1
                or new["layers"][:-1] != old["layers"] or selected != new["layers"][-1]
                or selected["is_image_layer"] or selected["native_id"] in {r["native_id"] for r in old["layers"]}
                or new["details"][:-1] != old["details"]):
            raise Refused("layer_copy_mismatch", "Duplication must append one new layer and preserve the complete original inventory")
        source_detail = {**old["details"][old["layers"].index(source)], "name": selected["name"]}
        if new["details"][-1] != source_detail:
            raise Refused("layer_copy_mismatch", "Copied layer properties differ from the source")
        bm = {m["uuid"]: m["content_sha256"] for m in old["masks"]}
        am = {m["uuid"]: m["content_sha256"] for m in new["masks"]}
        if am != {**bm, selected["native_id"]: bm[source["native_id"]]}:
            raise Refused("layer_copy_mismatch", "Copied mask differs or another mask changed")
        vid = int(before["target"]["variant"]["id"])
        old_rows, new_rows = saved_before["layers"][vid], saved_after["layers"][vid]
        role = "local:" + selected["native_id"].lower()
        src_role = "local:" + source["native_id"].lower()
        if set(new_rows) != set(old_rows) | {role} or any(new_rows[k] != v for k, v in old_rows.items()):
            raise Refused("layer_copy_mismatch", "Duplication changed unrelated complete saved parameters")
        expected = {**old_rows[src_role], "ZNAME": selected["name"], "ZMASKUUID": selected["native_id"],
                    "ZINDEX": len(old["layers"]) - 1}
        if isinstance(expected.get("ZMETADATA"), dict):
            expected["ZMETADATA"] = {**expected["ZMETADATA"], "ZLAYER": role}
        if new_rows[role] != expected:
            raise Refused("layer_copy_mismatch", "Copied complete saved parameters differ from the source")

    def _native_layer_route(self, action, observation):
        from .macos_bridge import configured
        enabled = configured(self.state_dir)
        observed = action in observation.get("native_context", {}).get("bridge_layer_actions", [])
        if enabled != observed:
            raise Refused("native_layer_route_changed", "Read layers or masks again with the matching native bridge configuration")
        return enabled

    @staticmethod
    def _layer_action_saved(saved):
        """Durable JSON form of complete saved values; no Session writes."""
        result = json.loads(json.dumps(saved, default=lambda value: {"blob_hex": value.hex()} if isinstance(value, bytes) else value))
        result["layers"] = {int(key): value for key, value in result["layers"].items()}
        return result

    @staticmethod
    def _layer_action_before(observation):
        if observation.get("module") != "layers":
            return observation
        baseline = observation.get("native_context", {}).get("layer_action_baseline")
        if not isinstance(baseline, dict) or not isinstance(baseline.get("state"), dict):
            raise Refused("incomplete_observation", "Read the complete native layer action baseline")
        return {**observation, "state": baseline["state"]}

    @staticmethod
    def _layer_action_arguments(before, patch):
        from .macos_mask_actions import preflight_combine_existing
        action, state = patch["action"], before["state"]
        parent = state["selected_layer"]
        if parent["is_image_layer"]:
            raise Refused("adjustment_layer_required", "Select an observed local layer")
        arguments = {"expected_layer_ids": [row["native_id"] for row in state["layers"] if not row["is_image_layer"]]}
        if action in {"duplicate", "move"} and parent != patch["layer_ref"]:
            raise Refused("layer_not_selected", "Select the exact observed source layer")
        if action == "duplicate":
            if "name" in patch:
                arguments["name"] = patch["name"]
        elif action == "move":
            index = patch["index"]
            if type(index) is not int or not 0 <= index < len(arguments["expected_layer_ids"]):
                raise Refused("invalid_layer_index", "Use an existing zero-based local layer index")
            expected = [row["native_id"] for row in state["layers"] if not row["is_image_layer"] and row != parent]
            expected.insert(index, parent["native_id"])
            arguments["expected_after_layer_ids"] = expected
        elif action == "combine":
            source = patch["source_layer"]
            preflight_combine_existing(state, parent, source, patch["mode"])
            arguments.update(source_uuid=source["native_id"], mode=patch["mode"], submask_name=source["name"])
        else:
            root = next(mask for mask in state["masks"] if mask["uuid"] == parent["native_id"])["structure"]
            children = root.get("children", [])
            if (root.get("composition_raw") != [1, 1.0, 0, 0] or root.get("mask_type") not in {0, 255}
                    or any("children" in child for child in children)):
                raise Refused("unsupported", "Native child actions currently qualify neutral raster or flat composite parents")
            if action == "combine_ai":
                if patch["kind"] not in {"subject", "background"} or patch["mode"] not in {"add", "subtract", "intersect"} or len(children) >= 64:
                    raise Refused("unsupported", "Use a qualified AI source and mode with room for one component")
                arguments.update(ai_kind=patch["kind"], mode=patch["mode"])
            else:
                index = patch["index"]
                if (type(index) is not int or index < -1 or index >= len(children)
                        or action == "delete_submask" and (index < 0 or len(children) < 2)):
                    raise Refused("submask_missing", "Read an existing child index; deletion needs a surviving component")
                if (action == "delete_submask" and len(children) == 2 and children[1 - index].get("mask_type") != 0):
                    raise Refused("unsupported", "Only raster survivors are qualified when deletion collapses a composite")
                arguments["index"] = index
        native_index = before.get("native_context", {}).get("bridge_target", {}).get("submask_index")
        if action == "delete_submask" and native_index != patch["index"]:
            raise Refused("submask_not_selected", "Select and read the exact child before deleting")
        if action in {"duplicate", "move", "combine", "combine_ai"} and native_index != -1:
            raise Refused("whole_parent_required", "Select and read the whole parent before this action")
        return arguments

    def _native_layer_proof(self, before, after, patch, receipt, operation_id):
        from .macos_mask_actions import (_bound, _children, _copy_component_content, _combine_destination,
                                        _require, verify_remove_submask, verify_combine_ai)
        action, parent = patch["action"], before["state"]["selected_layer"]
        arguments = self._layer_action_arguments(before, patch)
        if (receipt.get("operation_id") != operation_id or receipt.get("kind") != action
                or receipt.get("target") != before["native_context"].get("bridge_target")
                or receipt.get("state") != "committed" or receipt.get("temporal_open") is not False
                or receipt.get("callback_received") is not True
                or receipt.get("before_layer_ids") != list(reversed(arguments["expected_layer_ids"]))
                or any(receipt.get("arguments", {}).get(key) != value for key, value in arguments.items())):
            raise Refused("native_layer_receipt_mismatch", "The terminal receipt does not bind this original layer request")
        if any(before["target"][key] != after["target"][key] for key in ("instance", "document", "image", "variant")):
            raise Refused("target_changed", "Native layer action changed photo or Session")
        live = after.get("native_context", {}).get("bridge_target", {})
        if (live.get("layer_uuid") != after["state"]["selected_layer"]["native_id"]
                or receipt.get("after_layer_ids") != list(reversed([r["native_id"] for r in after["state"]["layers"] if not r["is_image_layer"]]))):
            raise Refused("native_layer_receipt_mismatch", "Native selected layer or final order does not match saved readback")
        proof = {"interface": "c1_native_layer_command", "native_receipt": receipt,
                 "model_verified": True, "render_verified": False, "mouse_used": False, "atomic": False}
        if action in {"duplicate", "move"}:
            if live.get("submask_index") != -1:
                raise Refused("whole_parent_required", "Native layer action did not preserve whole-parent selection")
            saved = self._layer_action_saved(before["native_context"]["layer_action_baseline"]["saved"])
            current = self._layer_action_saved(self._color_editor_untouched(after, after, exclude_color=False))
            proof["saved_layer_snapshot"] = self._saved_layer_digest(current)
            if action == "duplicate":
                self._verify_layer_duplicate(before, after, saved, current)
                if (after["state"]["selected_layer"]["native_id"] != receipt.get("created_mask_uuid")
                        or "name" in patch and after["state"]["selected_layer"]["name"] != patch["name"]):
                    raise Refused("layer_copy_mismatch", "Copied layer identity or name differs from the native request")
                proof["complete_layer_and_mask_copy_verified"] = True
            else:
                by_uuid = {row["native_id"]: row for row in before["state"]["layers"]}
                expected = [before["state"]["layers"][0]] + [by_uuid[key] for key in arguments["expected_after_layer_ids"]]
                self._verify_layer_move(before, after, saved, current, expected)
                proof.update(complete_saved_parameters_and_masks_preserved=True, no_change=expected == before["state"]["layers"])
            return proof
        if self._mask_ax_parameter_snapshot(after) != before["native_context"]["mask_ax_parameters"]:
            raise Refused("mask_readback_mismatch", "Native child action changed complete saved layer parameters")
        if action == "delete_submask":
            proof.update(verify_remove_submask(before["state"], after["state"], parent, patch["index"]))
            proof.update(selection_verified=True, selection_source="native_selectedSubmaskIndex_before_dispatch")
            return proof
        old, new, selected = _bound(before["state"], after["state"], parent, allow_rename=action in {"combine", "combine_ai"})
        if action == "select_submask":
            old_root, new_root = copy.deepcopy(old["structure"]), copy.deepcopy(new["structure"])
            old_root["metadata"].pop("SelectedSubmaskIndex", None)
            new_root["metadata"].pop("SelectedSubmaskIndex", None)
            _require(old_root == new_root and old["content_sha256"] == new["content_sha256"]
                     and live.get("submask_index") == patch["index"], "Native child selection or unchanged mask contents did not verify")
            proof.update(selected_index=patch["index"], selection_verified=True, selection_source="native_selectedSubmaskIndex")
            return proof
        ai = action == "combine_ai"
        previous = (_children(old["structure"]) if old["structure"].get("mask_type") == 255 else [old["structure"]]) if ai else _combine_destination(old["structure"])
        children = _children(new["structure"])
        _require(len(children) == len(previous) + 1 and receipt.get("appended_index") == len(previous)
                 and live.get("submask_index") == (-1 if ai else len(previous)), "Native combination did not append one component with the expected selection")
        wrapped = old["structure"].get("mask_type") != 255
        for original, current in zip(previous, children[:-1]):
            _require(original["content_sha256"] == current["content_sha256"]
                     and _copy_component_content(original, omit_name=wrapped) == _copy_component_content(current, omit_name=wrapped),
                     "Native combination changed an existing component")
        wrapper = _copy_component_content(new["structure"]); wrapper.pop("children")
        expected_wrapper = {"mask_type": 255, "composition_raw": [1, 1.0, 0, 0], "metadata": {}}
        if not wrapped:
            expected_wrapper = _copy_component_content(old["structure"]); expected_wrapper.pop("children")
        _require(wrapper == expected_wrapper, "Native combination changed wrapper semantics")
        if ai:
            proof.update(verify_combine_ai(before["state"], after["state"], parent, patch["mode"], patch["kind"]))
            _require(children[-1].get("metadata", {}).get("SubmaskName") == receipt.get("submask_name")
                     and isinstance(receipt.get("submask_name"), str) and bool(receipt["submask_name"]),
                     "The appended AI component name differs from the native receipt")
            return proof
        source = next(mask for mask in before["state"]["masks"] if mask["uuid"] == patch["source_layer"]["native_id"])
        expected = _copy_component_content(source["structure"])
        expected["composition_raw"] = [1, 1.0, {"add": 0, "subtract": 5, "intersect": 2}[patch["mode"]], 0]
        name = receipt.get("submask_name")
        _require(isinstance(name, str) and bool(name), "Native new component name is unavailable")
        expected["metadata"]["SubmaskName"] = name
        _require(_copy_component_content(children[-1]) == expected, "The appended mask differs from the observed source or requested mode")
        proof.update(source_structure_verified=True, source_metadata_verified=True, appended_index=len(previous),
                     source_ref=patch["source_layer"], mode=patch["mode"], source_selection_point_copied=False)
        return proof

    def _layer_action_result(self, after, action, saved_snapshot):
        if action not in {"duplicate", "move"}:
            return after
        result = self.observe_module("layers")
        if (result["target"] != after["target"] or result["native_context"]["layer_action_baseline"]["state"] != after["state"]
                or result["native_context"]["duplicate_snapshot"] != saved_snapshot):
            raise Refused("layer_state_changed", "Complete saved layer state changed after native verification")
        return result

    def _adjust_native_layer_action(self, patch, observation):
        before = self._layer_action_before(observation)
        arguments = self._layer_action_arguments(before, patch)
        action = patch["action"]
        def apply():
            wrote, receipt = False, None
            try:
                current = self.observe_module("masks")
                if (current["target"] != before["target"] or current["state"] != before["state"]
                        or current["native_context"].get("bridge_target") != before["native_context"].get("bridge_target")
                        or current["native_context"]["mask_ax_parameters"] != before["native_context"]["mask_ax_parameters"]
                        or current["native_context"]["mask_file_snapshot"] != before["native_context"]["mask_file_snapshot"]):
                    raise Refused("stale_observation", "Native layer target, selection or complete saved state changed")
                if action in {"duplicate", "move"} and self._saved_layer_digest(
                        self._color_editor_untouched(current, current, exclude_color=False)) != before["native_context"]["duplicate_snapshot"]:
                    raise Refused("stale_observation", "Complete saved layer parameters changed before dispatch")
                try:
                    receipt = self.native_bridge().apply_layer(current["target"], current["native_context"]["bridge_target"],
                        self.action_context["operation_id"], action, **arguments)
                except Exception as error:
                    wrote = getattr(error, "dispatched", True)
                    raise
                wrote = receipt.get("dispatched") is not False
                after = self.observe_module("masks")
                proof = self._native_layer_proof(before, after, patch, receipt, self.action_context["operation_id"])
                result = self._layer_action_result(after, action, proof.get("saved_layer_snapshot"))
                self.native_bridge().call(result["target"], "mask_release", operation_id=self.action_context["operation_id"])
                return {"status": "control_confirmed", "requested_result_verified": True, "after": result,
                        "evidence": proof, "target_transition": {"before": before["target"], "after": result["target"],
                            "evidence": {"selected_parent_rename": {"combine": "native_combine_existing", "combine_ai": "native_combine_ai"}.get(action),
                                         "before_layers": before["state"]["layers"], "after_layers": result["state"]["layers"],
                                         "selected_layer": result["state"]["selected_layer"]}}}
            except Exception as error:
                error.dispatched = wrote
                if receipt is not None:
                    error.details = receipt
                raise
        module = "layers" if action in {"duplicate", "move"} else "masks"
        return self.action_hook(module + "." + action, apply, {"patch": patch})

    def _duplicate_layer(self, patch, observation):
        if self._native_layer_route(patch["action"], observation):
            return self._adjust_native_layer_action(patch, observation)
        def apply():
            wrote = False
            try:
                from .macos_ax import run_ax
                before = self.observe_module("masks")
                native_state = {k: v for k, v in before["state"].items() if k != "masks"}
                saved = self._color_editor_untouched(before, before, exclude_color=False)
                if (before["target"] != observation["target"] or native_state != observation["state"]
                        or self._saved_layer_digest(saved) != observation.get("native_context", {}).get("duplicate_snapshot")):
                    raise Refused("stale_observation", "Read complete current layer settings before duplicating")
                def guard():
                    current = self._native("layers_read", [])
                    if (current["observation"]["target"] != before["target"]
                            or current["result"]["state"] != native_state
                            or self._color_editor_untouched(before, before, exclude_color=False) != saved):
                        raise Refused("stale_observation", "Layer or complete saved contents changed during menu lookup")
                try:
                    receipt = run_ax(self.state_dir, before["target"], "menu", before_dispatch=guard,
                                    menu_path=[["Layer", "图层"], ["Duplicate Selected Layer", "复制已选图层"]])
                except Exception as error:
                    wrote = getattr(error, "dispatched", True)
                    raise
                wrote = receipt.get("dispatched") is not False
                after = self.observe_module("masks")
                saved_after = self._color_editor_untouched(after, after, exclude_color=False)
                self._verify_layer_duplicate(before, after, saved, saved_after)
                if "name" in patch and patch["name"] != after["state"]["selected_layer"]["name"]:
                    rename_from = {**after, "state": after["native_context"]["layer_state"]}
                    self._native("layers_adjust", [], rename_from, patch={"action": "rename",
                        "layer_ref": after["state"]["selected_layer"], "name": patch["name"]})
                    after = self.observe_module("masks")
                    saved_after = self._color_editor_untouched(after, after, exclude_color=False)
                    self._verify_layer_duplicate(before, after, saved, saved_after)
                    if after["state"]["selected_layer"]["name"] != patch["name"]:
                        raise Refused("layer_copy_mismatch", "Copied layer name differs from the request")
                result = {**after, "module": "layers", "state": after["native_context"]["layer_state"]}
                result["values"] = {"layers": result["state"]}
                result["native_context"]["duplicate_snapshot"] = self._saved_layer_digest(saved_after)
                return {"status": "control_confirmed", "requested_result_verified": True, "after": result,
                        "evidence": {"model_verified": True, "render_verified": False, "atomic": False,
                                     "interface": "official_menu_via_accessibility", "complete_layer_and_mask_copy_verified": True},
                        "target_transition": {"before": observation["target"], "after": result["target"],
                            "evidence": {"before_layers": observation["state"]["layers"],
                                         "after_layers": result["state"]["layers"], "selected_layer": result["state"]["selected_layer"]}}}
            except Exception as error:
                error.dispatched = wrote
                raise
        return self.action_hook("layers.duplicate", apply, {"patch": patch})

    def _mask_ax_parameter_snapshot(self, observation, *, saved_layers=None):
        selected = observation["state"]["selected_layer"]
        if saved_layers is None:
            content = self._mask_ax_parameters(observation, allow_rename=not selected["is_image_layer"])
        else:
            content = copy.deepcopy(saved_layers)
            if not selected["is_image_layer"]:
                content[int(observation["target"]["variant"]["id"])]["local:" + selected["native_id"].lower()].pop("ZNAME")
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"),
                             default=lambda value: {"blob_hex": value.hex()} if isinstance(value, bytes) else value)
        return {"scope": "complete_saved_real_layer_parameters_except_selected_local_name_v1",
                "layer_native_id": selected["native_id"],
                "sha256": hashlib.sha256(encoded.encode()).hexdigest()}

    def _create_viewer_gradient(self, patch, observation):
        from .macos_mask_actions import preflight_viewer_gradient, verify_viewer_gradient
        parent = observation["state"]["selected_layer"]
        preflight_viewer_gradient(observation["state"], parent, patch)
        view = observation.get("gradient_view", {})
        if view.get("available") is not True:
            raise Refused("gradient_view_unavailable", "Read masks with a single fit-to-window photo and explicit mouse configuration")
        def apply():
            wrote = False
            try:
                from .macos_ax import run_ax
                before = self.observe_module("masks")
                saved = self._color_editor_untouched(before, before, exclude_color=False)
                if (before["target"] != observation["target"] or before["state"] != observation["state"]
                        or before.get("gradient_view") != view
                        or before["native_context"]["mask_ax_parameters"] != observation["native_context"]["mask_ax_parameters"]
                        or self._mask_ax_parameter_snapshot(before, saved_layers=saved["layers"]) != before["native_context"]["mask_ax_parameters"]
                        or saved["masks"] != before["native_context"]["mask_file_snapshot"]):
                    raise Refused("stale_observation", "Photo, empty mask, viewing geometry or complete saved settings changed")
                def guard():
                    bound = self._native("layers_read", [])
                    if (bound["observation"]["target"] != before["target"]
                            or bound["result"]["state"] != before["native_context"]["layer_state"]
                            or self._color_editor_untouched(before, before, exclude_color=False) != saved):
                        raise Refused("stale_observation", "Native photo or empty layer changed before drawing")
                try:
                    receipt = run_ax(self.state_dir, before["target"], "draw_gradient",
                        expected_view=view["view"], start=patch["start"], stop=patch["stop"], before_dispatch=guard)
                except Exception as error:
                    wrote = getattr(error, "dispatched", True)
                    raise
                wrote = receipt.get("dispatched") is not False
                after = self.observe_module("masks")
                if after["target"] != before["target"]:
                    raise Refused("target_changed", "Drawing changed the native photo or layer identity")
                proof = verify_viewer_gradient(before["state"], after["state"], parent, patch)
                expected = copy.deepcopy(saved["layers"])
                actual = self._mask_ax_parameters(after)
                vid = int(before["target"]["variant"]["id"])
                role = "local:" + parent["native_id"].lower()
                point = actual[vid][role].get("ZLAYERSELECTPOINT")
                try:
                    coordinates = [float(value) for value in point.split(";")]
                    if len(coordinates) != 2 or not all(math.isfinite(value) for value in coordinates):
                        raise ValueError()
                except (AttributeError, ValueError):
                    raise Refused("mask_readback_mismatch", "The new gradient selection marker is invalid")
                expected[vid][role]["ZLAYERSELECTPOINT"] = point
                if actual != expected:
                    raise Refused("mask_readback_mismatch", "Drawing changed saved parameters beyond the gradient selection marker")
                return {"status": "partial", "requested_result_verified": False, "after": after,
                    "evidence": {**proof, "interface": "explicit_viewer_mouse_gesture_with_native_readback",
                                 "gesture": receipt, "atomic": False, "model_verified": False,
                                 "viewer_gesture_dispatched": True, "raw_points_matched_to_request": False},
                    "target_transition": {"before": observation["target"], "after": after["target"],
                        "evidence": {"before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                     "selected_layer": after["state"]["selected_layer"]}}}
            except Exception as error:
                error.dispatched = wrote
                raise
        return self.action_hook("masks.linear_gradient", apply, {"patch": patch})

    def _create_native_gradient(self, patch, observation):
        from .macos_mask_actions import native_gradient_parameters, verify_native_gradient, verify_gradient_selection_point
        parent = observation["state"]["selected_layer"]
        parameters = native_gradient_parameters(observation["state"], parent, patch)
        def apply():
            wrote = False
            try:
                before = self.observe_module("masks")
                saved = self._color_editor_untouched(before, before, exclude_color=False)
                if (before["target"] != observation["target"] or before["state"] != observation["state"]
                        or before["native_context"]["mask_ax_parameters"] != observation["native_context"]["mask_ax_parameters"]
                        or self._mask_ax_parameter_snapshot(before, saved_layers=saved["layers"]) != observation["native_context"]["mask_ax_parameters"]
                        or saved["masks"] != before["native_context"]["mask_file_snapshot"]):
                    raise Refused("stale_observation", "Empty mask or complete saved layer parameters changed")
                native = self.native_bridge().inspect(before["target"])
                if (native["target"] != before["native_context"].get("bridge_target")
                        or native["raw_dimensions"] != {"Width": next(m for m in before["state"]["masks"] if m["uuid"] == parent["native_id"])["width"],
                                                       "Height": next(m for m in before["state"]["masks"] if m["uuid"] == parent["native_id"])["height"]}):
                    raise Refused("stale_observation", "Native target, revision or RAW dimensions changed")
                if self._color_editor_untouched(before, before, exclude_color=False) != saved:
                    raise Refused("stale_observation", "Saved state changed immediately before native gradient creation")
                try:
                    receipt = self.native_bridge().create_gradient(before["target"], native["target"],
                        self.action_context["operation_id"], parent["native_id"], patch["action"], parameters)
                except Exception as error:
                    wrote = getattr(error, "dispatched", True)
                    raise
                wrote = True
                after = self.observe_module("masks")
                if after["target"] != before["target"]:
                    raise Refused("target_changed", "Native gradient changed the photo or layer identity")
                proof = verify_native_gradient(before["state"], after["state"], parent, patch, parameters)
                expected = copy.deepcopy(saved["layers"])
                actual = self._mask_ax_parameters(after)
                vid, role = int(before["target"]["variant"]["id"]), "local:" + parent["native_id"].lower()
                point = actual[vid][role].get("ZLAYERSELECTPOINT")
                if expected[vid][role].get("ZLAYERSELECTPOINT") != point:
                    verify_gradient_selection_point(point, patch["action"], parameters)
                    expected[vid][role]["ZLAYERSELECTPOINT"] = point
                if actual != expected:
                    raise Refused("mask_readback_mismatch", "Gradient changed unrelated saved layer parameters")
                return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                        "evidence": {**proof, "native_receipt": receipt, "model_verified": True,
                                     "interface": "c1_native_gradient_command_and_undo", "mouse_used": False,
                                     "other_parameters_and_masks_unchanged": True, "atomic": False},
                        "target_transition": {"before": before["target"], "after": after["target"],
                            "evidence": {"before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                         "selected_layer": after["state"]["selected_layer"]}}}
            except Exception as error:
                error.dispatched = wrote
                raise
        return self.action_hook("masks." + patch["action"], apply, {"patch": patch})

    def _native_mask_proof(self, before, after, patch, receipt):
        """One saved-state proof for the bridge's AI/root-mask commands."""
        from .macos_mask_actions import verify_parameter_invert, verify_raster_invert
        action = patch["action"]
        if any(before["target"][key] != after["target"][key] for key in ("instance", "document", "image", "variant")):
            raise Refused("target_changed", "Native mask command changed photo identity")
        old, new = before["state"], after["state"]
        selected = new["selected_layer"]
        if selected["is_image_layer"] or after["target"]["layer"] != selected:
            raise Refused("mask_readback_mismatch", "Native mask must bind the resulting selected local layer")
        actual = self._mask_ax_parameters(after)
        new_masks = {m["uuid"]: m for m in new["masks"]}
        mask = new_masks[selected["native_id"]]
        create = action in {"create_subject", "create_background"}
        if create:
            if (len(new["layers"]) != len(old["layers"]) + 1 or selected in old["layers"]
                    or selected["native_id"] != receipt.get("created_mask_uuid")
                    or [r for r in new["layers"] if r != selected] != old["layers"]
                    or [d for r, d in zip(new["layers"], new["details"]) if r != selected] != old["details"]
                    or mask["structure"].get("metadata", {}).get("MaskGenerator") !=
                       ("AISubject" if action == "create_subject" else "AIBackground")
                    or "map" not in mask["structure"]):
                raise Refused("mask_readback_mismatch", "Native AI layer/generator or existing layer state changed")
            del actual[int(after["target"]["variant"]["id"])]["local:" + selected["native_id"].lower()]
        else:
            if before["target"] != after["target"] or old["layers"] != new["layers"]:
                raise Refused("mask_readback_mismatch", "Root mask operation changed layer identity/order")
            details = copy.deepcopy(new["details"])
            index = new["layers"].index(selected)
            # C1 derives layer kind from a filled/empty mask. Clearing the whole
            # root also removes its Luma components, not adjustment values.
            details[index]["kind"] = old["details"][index]["kind"]
            if action in {"fill", "clear"}:
                details[index]["luma_range"] = old["details"][index]["luma_range"]
            if details != old["details"]:
                raise Refused("mask_readback_mismatch", "Mask operation changed other exposed layer properties")
            previous = next(m for m in old["masks"] if m["uuid"] == selected["native_id"])
            if action in {"fill", "clear"} or previous["constant"]["verified"]:
                desired = (255 if action == "fill" else 0 if action == "clear" else 255 - previous["constant"]["value_8bit"])
                if not mask["constant"]["verified"] or mask["constant"]["value_8bit"] != desired:
                    raise Refused("mask_readback_mismatch", "Native constant mask result differs from the requested coverage")
            elif previous["mask_type"] in {1, 3, 4}:
                verify_parameter_invert(old, new, old["selected_layer"])
            else:
                verify_raster_invert(old, new, old["selected_layer"])
        for old_mask in old["masks"]:
            if create or old_mask["uuid"] != selected["native_id"]:
                if new_masks.get(old_mask["uuid"], {}).get("content_sha256") != old_mask["content_sha256"]:
                    raise Refused("mask_readback_mismatch", "Native command changed a non-target mask")
        if self._mask_ax_parameter_snapshot(before, saved_layers=actual) != before["native_context"]["mask_ax_parameters"]:
            raise Refused("mask_readback_mismatch", "Native mask command changed other complete saved layer parameters")
        return {"interface": "c1_native_mask_command", "native_receipt": receipt,
                "whole_parent": not create, "other_parameters_and_masks_unchanged": True,
                "model_verified": True, "mouse_used": False, "atomic": False}

    def _adjust_native_mask_command(self, patch, observation):
        from .macos_mask_actions import parameter_invert_kind, raster_invert_evidence
        action = patch["action"]
        if action not in observation["native_context"].get("bridge_mask_actions", []):
            raise Refused("native_bridge_unavailable", "Load the matching native mask bridge and read masks again")
        if action == "fill" and patch["density"] != 1:
            raise Refused("unsupported", "Native full-parent fill currently accepts density=1")
        if action == "invert":
            parent = observation["state"]["selected_layer"]
            mask = next((m for m in observation["state"]["masks"] if m["uuid"] == parent["native_id"]), None)
            if mask is None or mask["mask_type"] not in {0, 1, 3, 4}:
                raise Refused("unsupported", "C1 cannot invert a whole composite/Luma parent without changing its structure")
            if not mask["constant"]["verified"]:
                if mask["mask_type"] in {1, 3, 4}:
                    parameter_invert_kind(observation["state"], parent)
                else:
                    raster_invert_evidence(observation["state"], parent)
        def apply():
            wrote, receipt = False, None
            try:
                before = self.observe_module("masks")
                if (before["target"] != observation["target"] or before["state"] != observation["state"]
                        or before["native_context"]["mask_ax_parameters"] != observation["native_context"]["mask_ax_parameters"]
                        or before["native_context"].get("bridge_target") != observation["native_context"].get("bridge_target")):
                    raise Refused("stale_observation", "Mask target, revision or complete saved state changed")
                try:
                    receipt = self.native_bridge().apply_mask(before["target"], before["native_context"]["bridge_target"],
                        self.action_context["operation_id"], action)
                except Exception as error:
                    wrote = getattr(error, "dispatched", True)
                    raise
                wrote = True
                after = self.observe_module("masks")
                proof = self._native_mask_proof(before, after, patch, receipt)
                self.native_bridge().call(after["target"], "mask_release", operation_id=self.action_context["operation_id"])
                return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                        "evidence": proof, "target_transition": {"before": before["target"], "after": after["target"],
                            "evidence": {"before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                         "selected_layer": after["state"]["selected_layer"]}}}
            except Exception as error:
                error.dispatched = wrote
                if receipt is not None:
                    error.details = receipt
                raise
        return self.action_hook("masks." + action, apply, {"patch": patch})

    def _move_layer(self, patch, observation):
        if self._native_layer_route(patch["action"], observation):
            return self._adjust_native_layer_action(patch, observation)
        def apply():
            wrote = False
            try:
                from .macos_ax import run_ax
                before = self.observe_module("masks")
                native_state = {k: v for k, v in before["state"].items() if k != "masks"}
                saved = self._color_editor_untouched(before, before, exclude_color=False)
                if (before["target"] != observation["target"] or native_state != observation["state"]
                        or self._saved_layer_digest(saved) != observation.get("native_context", {}).get("duplicate_snapshot")
                        or saved["masks"] != before["native_context"]["mask_file_snapshot"]):
                    raise Refused("stale_observation", "Read complete layer settings before reordering")
                old = before["state"]["layers"]
                selected = before["state"]["selected_layer"]
                index = patch["index"]
                if selected != patch["layer_ref"] or selected["is_image_layer"] or index >= len(old) - 1:
                    raise Refused("invalid_layer_index", "Use a selected local layer and an existing zero-based local index")
                expected = [row for row in old if row != selected]
                expected.insert(index + 1, selected)
                if expected != old:
                    live = self._mask_ax_layout(before, run_ax(self.state_dir, before["target"], "read_layers"))
                    if live["submask_index"] != -1 or any(row["AXDisclosureLevel"] != 0 for row in live["rows"]):
                        raise Refused("whole_parent_required", "Select the whole parent and collapse child rows before reordering")
                    # The UI is top-to-bottom; the public index is bottom-to-top
                    # among local layers. Always drop before a remaining row.
                    visible = list(reversed(old))
                    remaining = [row for row in visible if row != selected]
                    anchor = remaining[len(old) - 2 - index]
                    insertion = visible.index(anchor)
                    def guard():
                        bound = self._native("layers_read", [])
                        if (bound["observation"]["target"] != before["target"]
                                or bound["result"]["state"] != before["native_context"]["layer_state"]
                                or self._color_editor_untouched(before, before, exclude_color=False) != saved):
                            raise Refused("stale_observation", "Native layer order or saved contents changed before the drag")
                    try:
                        receipt = run_ax(self.state_dir, before["target"], "move_layer",
                            row_index=live["row_index"], insertion_row_index=insertion,
                            expected_rows=live["rows"], before_dispatch=guard)
                    except Exception as error:
                        wrote = getattr(error, "dispatched", True)
                        raise
                    wrote = receipt.get("dispatched") is not False
                after = self.observe_module("masks")
                saved_after = self._color_editor_untouched(after, after, exclude_color=False)
                self._verify_layer_move(before, after, saved, saved_after, expected)
                result_after = self.observe_module("layers")
                if (result_after["target"] != after["target"]
                        or result_after["state"] != {k: v for k, v in after["state"].items() if k != "masks"}
                        or result_after["native_context"]["duplicate_snapshot"] != self._saved_layer_digest(saved_after)):
                    raise Refused("layer_move_mismatch", "Layer contents changed after order verification")
                return {"status": "control_confirmed", "requested_result_verified": True, "after": result_after,
                        "evidence": {"model_verified": True, "atomic": False, "render_verified": False,
                                     "interface": "explicit_mouse_opt_in_native_layer_drag",
                                     "complete_saved_parameters_and_masks_preserved": True,
                                     "no_change": expected == old},
                        "target_transition": {"before": observation["target"], "after": result_after["target"],
                            "evidence": {"before_layers": old, "after_layers": expected, "selected_layer": selected}}}
            except Exception as error:
                error.dispatched = wrote
                raise
        return self.action_hook("layers.move", apply, {"patch": patch})

    @staticmethod
    def _verify_layer_move(before, after, saved_before, saved_after, expected):
        if before["target"] != after["target"]:
            raise Refused("target_changed", "Reordering changed the photo, Session or selected layer")
        old, new = before["state"], after["state"]
        details = dict(zip((row["native_id"] for row in old["layers"]), old["details"]))
        masks = lambda state: {mask["uuid"]: mask for mask in state["masks"]}
        if (new["layers"] != expected or new["selected_layer"] != old["selected_layer"]
                or new["details"] != [details[row["native_id"]] for row in expected]
                or masks(old) != masks(new)):
            raise Refused("layer_move_mismatch", "Only the requested order may change; layer properties and masks must remain intact")
        wanted = copy.deepcopy(saved_before)
        vid = int(before["target"]["variant"]["id"])
        for index, layer in enumerate(expected[1:]):
            wanted["layers"][vid]["local:" + layer["native_id"].lower()]["ZINDEX"] = index
        if saved_after != wanted:
            raise Refused("layer_move_mismatch", "Reordering changed complete saved settings or mask bytes beyond layer indices")

    def _combine_existing(self, patch, observation):
        if self._native_layer_route(patch["action"], observation):
            return self._adjust_native_layer_action(patch, observation)
        from .macos_mask_actions import preflight_combine_existing, verify_append_blank, verify_combine_existing
        parent, source = observation["state"]["selected_layer"], patch["source_layer"]
        mode = patch["mode"]
        preflight_combine_existing(observation["state"], parent, source, mode)
        # The official submenu identifies its source by its exact visible name.
        # UUID identity still comes from the complete native inventory.
        if sum(layer["name"] == source["name"] for layer in observation["state"]["layers"]) != 1:
            raise Refused("ambiguous_mask_source", "Give the source layer a unique name before using the native copy submenu")

        def apply():
            wrote = False
            try:
                from .macos_ax import run_ax
                before = self.observe_module("masks")
                if (before["target"] != observation["target"] or before["state"] != observation["state"]
                        or before["native_context"]["mask_ax_parameters"] != observation.get("native_context", {}).get("mask_ax_parameters")):
                    raise Refused("stale_observation", "Read both complete masks and saved parameters before combining")
                saved = self._color_editor_untouched(before, before, exclude_color=False)
                if saved["masks"] != before["native_context"]["mask_file_snapshot"]:
                    raise Refused("stale_observation", "Saved masks changed before combination dispatch")
                if self._mask_ax_parameter_snapshot(before, saved_layers=saved["layers"]) != before["native_context"]["mask_ax_parameters"]:
                    raise Refused("stale_observation", "Complete saved parameters changed before combination dispatch")
                unchanged = copy.deepcopy(saved["layers"])
                vid = int(before["target"]["variant"]["id"])
                parent_role, source_role = "local:" + parent["native_id"].lower(), "local:" + source["native_id"].lower()
                point = unchanged[vid][source_role].get("ZLAYERSELECTPOINT")
                if not isinstance(point, str) or len(point.split(";")) != 2:
                    raise Refused("unsupported_mask_source", "The native source needs an explicit saved layer selection point")
                try:
                    if not all(math.isfinite(float(value)) for value in point.split(";")):
                        raise ValueError()
                except ValueError as error:
                    raise Refused("unsupported_mask_source", "The source selection point is invalid") from error
                unchanged[vid][parent_role].pop("ZNAME")
                live = self._mask_ax_layout(before, run_ax(self.state_dir, before["target"], "read_layers"))
                if live["submask_index"] != -1:
                    raise Refused("whole_parent_required", "Select the whole parent before combining an existing source")

                def dispatch_menu(observed, snapshot, child, path):
                    nonlocal wrote
                    def guard():
                        current = self._mask_ax_layout(observed, run_ax(self.state_dir, observed["target"], "read_layers"))
                        if current["submask_index"] != child:
                            raise Refused("submask_not_selected", "The native copy menu must operate on the verified child")
                        # AX traversal can be slow; bind the native photo and
                        # full saved state after it, immediately before press.
                        bound = self._native("layers_read", [])
                        if (bound["observation"]["target"] != observed["target"]
                                or bound["result"]["state"] != observed["native_context"]["layer_state"]
                                or self._color_editor_untouched(observed, observed, exclude_color=False) != snapshot):
                            raise Refused("stale_observation", "Source, parent or saved contents changed during menu lookup")
                    try:
                        receipt = run_ax(self.state_dir, observed["target"], "menu", menu_path=path, before_dispatch=guard)
                    except Exception as error:
                        wrote = wrote or getattr(error, "dispatched", True)
                        raise
                    wrote = wrote or receipt.get("dispatched") is not False

                labels = {"add": ["Add", "加"], "subtract": ["Subtract", "减去"], "intersect": ["Intersect", "相交"]}
                dispatch_menu(before, saved, -1, [["Layer", "图层"], labels[mode], ["Brush", "刷"]])
                middle = self.observe_module("masks")
                if any(middle["target"][key] != before["target"][key] for key in ("instance", "document", "image", "variant")):
                    raise Refused("target_changed", "Creating the combination child changed photo or Session")
                added = verify_append_blank(before["state"], middle["state"], parent, mode)
                saved_middle = self._color_editor_untouched(middle, middle, exclude_color=False)
                if saved_middle["masks"] != middle["native_context"]["mask_file_snapshot"]:
                    raise Refused("stale_observation", "Saved masks changed before copying the source")
                middle_parameters = copy.deepcopy(saved_middle["layers"])
                middle_parameters[vid][parent_role].pop("ZNAME")
                if middle_parameters != unchanged:
                    raise Refused("mask_readback_mismatch", "Creating the empty child changed saved adjustments")
                # Unlike AppleScript copy mask, this official UI command copies
                # into the selected child and preserves its combination mode.
                dispatch_menu(middle, saved_middle, added["appended_index"],
                              [["Layer", "图层"], ["Copy Mask From", "从...复制遮罩"], [source["name"]]])
                after = self.observe_module("masks")
                if any(after["target"][key] != before["target"][key] for key in ("instance", "document", "image", "variant")):
                    raise Refused("target_changed", "Combining masks changed photo or Session")
                proof = verify_combine_existing(before["state"], middle["state"], after["state"], parent, source, mode)
                # C1 copies the source's editable selection marker as well as
                # its mask; compare this exact native value, never ignore it.
                unchanged[vid][parent_role]["ZLAYERSELECTPOINT"] = point
                if self._mask_ax_parameters(after, allow_rename=True) != unchanged:
                    raise Refused("mask_readback_mismatch", "Combination changed unrelated complete saved parameters")
                return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                        "evidence": {**proof, "model_verified": True, "atomic": False,
                                     "interface": "official_child_copy_menu_via_accessibility",
                                     "source_selection_point_copied": True},
                        "target_transition": {"before": observation["target"], "after": after["target"],
                            "evidence": {"selected_parent_rename": "native_combine_existing",
                                         "before_layers": observation["state"]["layers"], "after_layers": after["state"]["layers"],
                                         "selected_layer": after["state"]["selected_layer"]}}}
            except Exception as error:
                error.dispatched = wrote
                raise
        return self.action_hook("masks.combine", apply, {"patch": patch})

    def _adjust_mask_ax(self, patch, observation):
        if self._native_layer_route(patch["action"], observation):
            return self._adjust_native_layer_action(patch, observation)
        from .macos_mask_actions import verify_combine_ai, verify_select_submask, verify_remove_submask
        selected = observation["state"]["selected_layer"]
        if selected["is_image_layer"]:
            raise Refused("adjustment_layer_required", "Select a local parent layer")
        action = patch["action"]
        root = next(mask for mask in observation["state"]["masks"] if mask["uuid"] == selected["native_id"])["structure"]
        children = root.get("children", [])
        if action != "combine_ai" and (type(patch.get("index")) is not int or patch["index"] < -1
                or patch["index"] >= len(children) or action == "delete_submask" and (patch["index"] < 0 or len(children) < 2)):
            raise Refused("submask_missing", "Read an existing component index; deletion requires a surviving component")
        if (root.get("composition_raw") != [1, 1.0, 0, 0] or root.get("mask_type") not in {0, 255}
                or any("children" in child for child in children)):
            raise Refused("unsupported", "This AX route requires a neutral simple raster or flat composite parent")
        if (action == "delete_submask" and len(children) == 2
                and children[1 - patch["index"]].get("mask_type") != 0):
            raise Refused("unsupported", "Collapsing to a parameter mask changes additional saved state; only raster survivors are qualified")

        def apply():
            wrote = False
            try:
                from .macos_ax import run_ax
                before = self.observe_module("masks")
                if before["target"] != observation["target"] or before["state"] != observation["state"]:
                    raise Refused("stale_observation", "Mask content or parent identity changed before UI dispatch")
                if before["native_context"]["mask_ax_parameters"] != observation.get("native_context", {}).get("mask_ax_parameters"):
                    raise Refused("stale_observation", "Read complete saved layer parameters before this UI mask action")
                dispatch_snapshot = self._color_editor_untouched(before, before, exclude_color=False)
                if self._mask_ax_parameter_snapshot(before, saved_layers=dispatch_snapshot["layers"]) != before["native_context"]["mask_ax_parameters"]:
                    raise Refused("stale_observation", "Saved parameters changed before the UI mask dispatch snapshot")
                untouched = copy.deepcopy(dispatch_snapshot["layers"])
                if action == "combine_ai":
                    untouched[int(before["target"]["variant"]["id"])]["local:" + selected["native_id"].lower()].pop("ZNAME")
                live = None
                if children or action != "combine_ai":
                    live = self._mask_ax_layout(before, run_ax(self.state_dir, before["target"], "read_layers"))
                    if action == "combine_ai" and live["submask_index"] != -1:
                        raise Refused("whole_parent_required", "Select the whole parent before combining another AI component")
                    if action == "delete_submask" and live["submask_index"] != patch["index"]:
                        raise Refused("submask_not_selected", "Select the observed child index before deleting it")
                if action == "combine_ai":
                    mode = {"add": ["Add", "加"], "subtract": ["Subtract", "减去"], "intersect": ["Intersect", "相交"]}[patch["mode"]]
                    kind = {"subject": ["Subject", "主题"], "background": ["Background", "背景"]}[patch["kind"]]
                    native_action, arguments = "menu", {"menu_path": [["Layer", "图层"], mode, kind]}
                elif action == "select_submask":
                    if patch["index"] not in live["row_map"]:
                        raise Refused("submask_not_visible", "Expand the parent in Capture One before selecting a child")
                    native_action, arguments = "select_row", {"row_index": live["row_map"][patch["index"]], "expected_rows": live["rows"]}
                else:
                    native_action, arguments = "delete_submask", {"expected_rows": live["rows"]}
                def before_mask_dispatch():
                    current = self.observe_module("masks")
                    if current["target"] != before["target"] or current["state"] != before["state"]:
                        raise Refused("stale_observation", "Native photo, parent or complete saved mask changed during UI lookup")
                    if action == "combine_ai" and live is not None:
                        selected_now = self._mask_ax_layout(current, run_ax(self.state_dir, current["target"], "read_layers"))
                        if selected_now != live or selected_now["submask_index"] != -1:
                            raise Refused("whole_parent_required", "Whole-parent UI selection changed during menu lookup")
                    bound = self._native("layers_read", [])
                    if (bound["observation"]["target"] != before["target"] or
                            bound["result"]["state"] != before["native_context"]["layer_state"]):
                        raise Refused("target_changed", "Native mask target changed immediately before UI dispatch")
                    if self._color_editor_untouched(before, before, exclude_color=False) != dispatch_snapshot:
                        raise Refused("mask_state_changed", "Saved adjustments or full mask file changed during UI lookup")
                try:
                    receipt = run_ax(self.state_dir, before["target"], native_action,
                                     before_dispatch=before_mask_dispatch, **arguments)
                except Exception as error:
                    wrote = getattr(error, "dispatched", True)
                    raise
                wrote = receipt.get("dispatched") is not False
                deadline = time.monotonic() + 10
                while True:
                    after = self.observe_module("masks")
                    if any(before["target"][key] != after["target"][key] for key in ("instance", "document", "image", "variant")):
                        raise Refused("target_changed", "Mask UI action changed photo or Session")
                    try:
                        if action == "combine_ai":
                            proof = verify_combine_ai(before["state"], after["state"], selected, patch["mode"], patch["kind"])
                        elif action == "select_submask":
                            current = self._mask_ax_layout(after, run_ax(self.state_dir, after["target"], "read_layers"))
                            proof = verify_select_submask(before["state"], after["state"], selected, patch["index"], live_selection=current)
                        else:
                            proof = verify_remove_submask(before["state"], after["state"], selected, patch["index"])
                        break
                    except Refused:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.25)  # Poll evidence only; never repeat the UI command.
                if self._mask_ax_parameters(after, allow_rename=action == "combine_ai") != untouched:
                    raise Refused("mask_readback_mismatch", "UI mask action changed unrelated saved adjustments")
                return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                        "evidence": {**proof, "model_verified": False, "atomic": False,
                                     "interface": "explicit_opt_in_accessibility_with_native_identity_and_saved_structure"},
                        "target_transition": {"before": observation["target"], "after": after["target"],
                            "evidence": {"selected_parent_rename": "native_combine_ai" if action == "combine_ai" else None,
                                         "before_layers": observation["state"]["layers"], "after_layers": after["state"]["layers"],
                                         "selected_layer": after["state"]["selected_layer"]}}}
            except Exception as error:
                error.dispatched = wrote
                raise
        return self.action_hook("masks." + action, apply, {"patch": patch})

    def adjust_module(self, module, patch, observation):
        if module in {"layers", "masks"}:
            if module == "masks" and patch.get("action") in {"linear_gradient", "radial_gradient"}:
                if not self._qualified(observation):
                    raise Refused("unqualified_candidate", "Gradient creation requires a registered Mac candidate")
                if patch.get("coordinate_space", "raw") == "viewer":
                    return self._create_viewer_gradient(patch, observation)
                return self._create_native_gradient(patch, observation)
            if module == "masks" and patch.get("action") == "combine":
                if not self._qualified(observation):
                    raise Refused("unqualified_candidate", "Existing mask combination requires a registered Mac candidate")
                return self._combine_existing(patch, observation)
            if module == "layers" and patch.get("action") == "move":
                if not self._qualified(observation):
                    raise Refused("unqualified_candidate", "Layer reordering requires a registered Mac candidate")
                return self._move_layer(patch, observation)
            if module == "layers" and patch.get("action") == "duplicate":
                if not self._qualified(observation):
                    raise Refused("unqualified_candidate", "Layer duplication requires a registered Mac candidate")
                return self._duplicate_layer(patch, observation)
            if module == "masks" and patch.get("action") in {"combine_ai", "select_submask", "delete_submask"}:
                if not self._qualified(observation):
                    raise Refused("unqualified_candidate", "Mask UI actions require a registered Mac candidate")
                return self._adjust_mask_ax(patch, observation)
            if module == "masks" and patch.get("action") in {"create_subject", "create_background", "invert", "fill", "clear"}:
                from .macos_bridge import configured
                enabled = configured(self.state_dir)
                if not enabled and patch["action"] in observation.get("native_context", {}).get("bridge_mask_actions", []):
                    raise Refused("stale_observation", "Native bridge configuration changed; read masks again before choosing a route")
                if enabled:
                    if not self._qualified(observation):
                        raise Refused("unqualified_candidate", "Native masks require a registered candidate")
                    return self._adjust_native_mask_command(patch, observation)
            actions = {"select", "rename", "create_empty", "set_enabled", "set_opacity", "remove_created"} if module == "layers" else {"luma_range", "clear_luma_range", "create_subject", "create_background", "fill", "clear", "copy", "invert", "feather", "refine"}
            if not self._qualified(observation) or patch.get("action") not in actions:
                raise Refused("unsupported", "This layer/mask action is not qualified on registered Mac candidates")
            if module == "layers" and patch["action"] == "set_opacity":
                value = patch.get("opacity")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 100 or int(value) != value:
                    raise Refused("out_of_range", "Mac layer opacity requires an integer 1..100; disable the layer with enabled=false")
                patch = {**patch, "opacity": int(value)}
            if module == "layers" and patch["action"] == "set_enabled" and type(patch.get("enabled")) is not bool:
                raise Refused("invalid_patch", "Layer enabled must be boolean")
            if module == "masks" and patch["action"] == "fill" and patch["density"] != 1:
                raise Refused("unsupported", "Mac native Fill Mask supports density 1 only")
            if module == "masks" and patch["action"] == "clear_luma_range":
                from .macos_mask_actions import clear_luma_range_index
                clear_luma_range_index(observation["state"], observation["state"]["selected_layer"])
            invert_parameter_kind = None
            if module == "masks" and patch["action"] == "invert":
                selected = observation["state"]["selected_layer"]["native_id"]
                mask = next(m for m in observation["state"]["masks"] if m["uuid"] == selected)
                if not mask["constant"]["verified"] or mask["constant"]["density"] not in (0, 1):
                    from .macos_mask_actions import parameter_invert_kind, raster_invert_evidence
                    if mask["mask_type"] in {1, 3, 4}:
                        if mask["structure"].get("composition_raw") != [1, 1.0, 0, 0]:
                            raise Refused("unsupported", "Parameter inversion requires a neutral simple mask")
                        invert_parameter_kind = parameter_invert_kind(observation["state"], observation["state"]["selected_layer"])
                    else:
                        raster_invert_evidence(observation["state"], observation["state"]["selected_layer"])
            if module == "masks" and patch["action"] in {"feather", "refine"}:
                from .macos_mask_actions import raster_invert_evidence
                radius = patch.get("radius")
                limit = 100 if patch["action"] == "feather" else 300
                if (type(radius) not in (int, float) or not math.isfinite(radius) or not 0 <= radius <= limit
                        or patch["action"] == "refine" and radius == 0):
                    raise Refused("out_of_range", "Native Feather requires 0..100; Refine requires greater than 0 and at most 300")
                raster_invert_evidence(observation["state"], observation["state"]["selected_layer"])
            def change_layer():
                if module == "masks":
                    try:
                        fresh = self.observe_module("masks")
                        if fresh["target"] != observation["target"] or fresh["state"] != observation["state"]:
                            raise Refused("stale_observation", "Layer mask content changed before dispatch")
                    except Exception as error:
                        error.dispatched = False
                        raise
                payload = self._native(module + "_adjust", [], observation, patch=patch)
                native_mask_ack = payload.get("result", {}).get("native_mask_command_succeeded")
                mask_evidence = {}
                after = self._module_observation(module, payload)
                if module == "masks":
                    bound_target, bound_state = after["target"], after["state"]
                    def check_bound(current):
                        state = {k: v for k, v in current["state"].items() if k != "masks"}
                        if current["target"] != bound_target or state != bound_state:
                            raise Refused("target_changed", "Native layer or adjustment changed while waiting for saved mask data")
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            after = self._mask_observation(payload)
                            check_bound(after)
                            break
                        except Refused as error:
                            if error.code not in {"mask_state_unavailable", "mask_state_changed"} or time.monotonic() >= deadline:
                                raise
                            time.sleep(.25)
                            payload = self._native("masks_read", [])
                    selected = after["state"]["selected_layer"]["native_id"]
                    def verified(current):
                        nonlocal mask_evidence
                        mask = next(m for m in current["state"]["masks"] if m["uuid"] == selected)
                        if patch["action"] in {"fill", "clear"}:
                            return mask["constant"]["verified"] and mask["constant"]["density"] == (1 if patch["action"] == "fill" else 0)
                        if patch["action"] == "invert":
                            old = next(m for m in observation["state"]["masks"] if m["uuid"] == selected)
                            if old["constant"]["verified"] and old["constant"]["density"] in (0, 1):
                                return mask["constant"]["verified"] and mask["constant"]["density"] == 1 - old["constant"]["density"]
                            from .macos_mask_actions import verify_parameter_invert, verify_raster_invert
                            try:
                                verify_invert = verify_parameter_invert if invert_parameter_kind is not None else verify_raster_invert
                                mask_evidence = verify_invert(observation["state"], current["state"], observation["state"]["selected_layer"])
                                return True
                            except Refused:
                                return False
                        if patch["action"] in {"feather", "refine"}:
                            from .macos_mask_actions import verify_native_raster_filter
                            try:
                                mask_evidence = verify_native_raster_filter(observation["state"], current["state"],
                                    observation["state"]["selected_layer"], patch["action"], patch["radius"],
                                    native_command_succeeded=native_mask_ack == {"action": patch["action"], "amount": patch["radius"]})
                                return True
                            except Refused:
                                return False
                        if patch["action"] in {"create_subject", "create_background"}:
                            generator = "AISubject" if patch["action"] == "create_subject" else "AIBackground"
                            return mask["structure"].get("metadata", {}).get("MaskGenerator") == generator and "map" in mask["structure"]
                        if patch["action"] == "copy":
                            return self._verified_mask_copy(observation, current, patch)
                        if patch["action"] == "clear_luma_range":
                            from .macos_mask_actions import verify_clear_luma_range
                            try:
                                verify_clear_luma_range(observation["state"], current["state"], observation["state"]["selected_layer"])
                                return True
                            except Refused:
                                return False
                        return True  # Native creation / all seven luma values already checked.
                    deadline = time.monotonic() + 5
                    while not verified(after) and time.monotonic() < deadline:
                        time.sleep(.25)
                        after = self.observe_module("masks")  # Read only; never repeat the command.
                        check_bound(after)
                    original_id = observation["state"]["selected_layer"]["native_id"]
                    unchanged = {m["uuid"]: m["content_sha256"] for m in observation["state"]["masks"]
                                 if patch["action"].startswith("create_") or m["uuid"] != original_id}
                    actual = {m["uuid"]: m["content_sha256"] for m in after["state"]["masks"]}
                    if not verified(after) or any(actual.get(k) != v for k, v in unchanged.items()):
                        raise Refused("mask_readback_mismatch", "Saved mask content does not verify the requested change")
                if module == "layers" and after["target"]["instance"].get("platform") == "macos":
                    after["native_context"]["duplicate_snapshot"] = self._duplicate_snapshot(after)
                return {"status": "control_confirmed", "after": after, "requested_result_verified": True,
                        "evidence": {"model_verified": True, "render_verified": False, **mask_evidence},
                        "target_transition": {"before": observation["target"], "after": after["target"],
                            "evidence": {"copy_identity": "native_mask_uuid_replaced_by_copy" if module == "masks" and patch["action"] == "copy" else None,
                                "before_layers": observation["state"]["layers"],
                                "after_layers": after["state"]["layers"], "selected_layer": after["state"]["selected_layer"]}}}
            return self.action_hook(module + "." + patch["action"], change_layer, {"patch": patch})
        if module not in IMPLEMENTED_MODULES or not self._qualified(observation):
            raise Refused("unqualified_candidate", "Structured writes require a registered current-instance Mac candidate")
        if module == "curves":
            points = observation["state"]["channels"][patch["channel"]]
            if len(points) < 2 or points[0][0] != 0 or points[-1][0] != 1:
                raise Refused("unsupported_curve_endpoints", "Native replacement requires existing endpoints at 0 and 1")
        if module == "levels":
            expected = {**observation["state"]["channels"][patch["channel"]], **patch}
            if expected["input_black"] >= expected["input_white"] or expected["output_black"] > expected["output_white"]:
                raise Refused("invalid_levels", "Black and white levels must remain ordered")
        if module == "color_mixer":
            if patch.get("mode") in {"basic", "skin", "advanced"} and self._color_route(observation):
                return self._adjust_native_color(patch, observation)
            if patch.get("mode") == "skin":
                return self._adjust_skin(patch, observation)
            if patch["mode"] not in {"basic", "advanced"} or "inverted" in patch:
                raise Refused("unsupported", "Skin Tone and Advanced inversion are not exposed by the official Mac scripting interface")
            if "rgb" in patch and any(int(v) != v for v in patch["rgb"]):
                raise Refused("unsupported_precision", "The official Mac range creation API requires integer RGB components")
            if patch["mode"] == "advanced" and patch.get("action") != "remove":
                rows = observation["state"]["advanced"]["ranges"]
                if patch.get("action") == "add":
                    row = {"rgb": patch["rgb"], "hue_low": 0, "hue_high": 0,
                           "saturation_low": 0, "saturation_high": 0}
                else:
                    if patch["index"] >= len(rows):
                        raise Refused("range_missing", "Read an existing Advanced range before editing")
                    row = rows[patch["index"]]
                expected = {**row, **patch}
                maximum, minimum = max(expected["rgb"]), min(expected["rgb"])
                origin_saturation = 100 * (maximum - minimum) / maximum if maximum else 0
                if (expected["saturation_low"] < -origin_saturation - 0.0001
                        or expected["saturation_high"] > 100 - origin_saturation + 0.0001
                        or expected["saturation_low"] > expected["saturation_high"]):
                    raise Refused("out_of_range", "Advanced saturation offsets must keep the absolute range within 0..100 for this RGB origin")
                if "rgb" in patch and patch.get("action") != "add":
                    # A properties-set is one Apple Event, not a promised
                    # atomic RGB replacement. Reject before dispatch if an old
                    # offset could clamp while C1 updates the RGB components.
                    for mask in range(1, 8):
                        rgb = [patch["rgb"][i] if mask & (1 << i) else row["rgb"][i] for i in range(3)]
                        high, low = max(rgb), min(rgb)
                        saturation = 100 * (high - low) / high if high else 0
                        if (row["saturation_low"] < -saturation - 0.0001
                                or row["saturation_high"] > 100 - saturation + 0.0001):
                            raise Refused("out_of_range", "Narrow the existing saturation range before changing its RGB origin; intermediate values could be clamped")
                if expected["hue_high"] - expected["hue_low"] > 359:
                    raise Refused("out_of_range", "Advanced hue range cannot exceed 359 degrees")

        def apply():
            old_saved, old_skin, untouched = None, None, None
            if module == "color_mixer":
                try:
                    self._check_color_editor_current(observation)
                    old_saved = observation.get("native_context", {}).get("color_editor_snapshot", {})
                    if old_saved.get("available"):
                        old_skin = [row for row in old_saved["rows"] if row[2] == 2]
                        untouched = self._color_editor_untouched(observation, observation)
                except Exception as error:
                    error.dispatched = False
                    raise
            payload = self._native("local_module_set" if self._local(observation) else "module_set", [], observation, module=module,
                                   expected_state=observation["state"], patch=patch)
            after = self._module_observation(module, payload)
            if module == "color_mixer":
                try:
                    after = self._color_editor_observation(after)
                    if old_skin is not None:
                        saved = after["native_context"]["color_editor_snapshot"]
                        if (not saved.get("available") or saved["source"] != old_saved["source"]
                                or [row for row in saved["rows"] if row[2] == 2] != old_skin
                                or self._color_editor_untouched(observation, observation) != untouched):
                            raise Refused("color_editor_readback_mismatch", "Basic/Advanced update changed hidden Skin Tone or unrelated parameters")
                except Exception as error:
                    error.dispatched = True
                    raise
            return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                    "evidence": {"source": "c1_applescript", "model_verified": True,
                                 **({"hidden_skin_preservation_verified": old_skin is not None} if module == "color_mixer" else {}),
                                 "complete_module_readback": True, "render_verified": False}}
        return self.action_hook(module + ".set", apply, {"patch": patch})

    def apply_module_action(self, module, patch, before, source):
        if not self._qualified(before):
            raise Refused("unqualified_candidate", "Module actions require a registered current-instance Mac candidate")
        neutral_reset = patch["action"] == "reset"
        if neutral_reset:
            # Explicit Mac contract: fixed neutral values, not camera/user
            # defaults. Do not reset the entire variant to emulate a module.
            if module in {"exposure", "hdr"}:
                source = {**before, "values": {field: 0 for field in MODULES[module]}}
            elif module == "curves":
                source = {**before, "state": {"channels": {channel: [[0, 0], [1, 1]]
                          for channel in ("rgb", "red", "green", "blue", "luma")}}}
            elif module == "levels":
                source = {**before, "state": {"channels": {channel: {"input_black": 0, "input_white": 255,
                          "midtone": 0, "output_black": 0, "output_white": 255}
                          for channel in ("rgb", "red", "green", "blue")}}}
            elif module == "color_balance":
                source = {**before, "state": {**before["state"],
                          "values": {field: 0 for field in before["state"]["values"]}}}
            elif module == "color_mixer":
                if self._color_route(before):
                    return self._adjust_native_color({"action": "reset"}, before)
                # Skin Tone is not exposed. Report this as an explicitly
                # limited Basic/Advanced reset, never a full Color Editor reset.
                if before["state"]["colors"]["all"]["lightness"] != 0:
                    raise Refused("unsupported", "The all-colors lightness field has no qualified setter")
                after = before
                for index in range(len(before["state"]["advanced"]["ranges"]) - 1, -1, -1):
                    after = self.adjust_module(module, {"mode": "advanced", "action": "remove", "index": index}, after)["after"]
                changed = after is not before
                for color in ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "magenta", "all"):
                    part = {"mode": "basic", "color": color, "hue": 0, "saturation": 0}
                    if color != "all":
                        part["lightness"] = 0
                    if any(after["state"]["colors"][color][key] != value for key, value in part.items() if key not in {"mode", "color"}):
                        after = self.adjust_module(module, part, after)["after"]
                        changed = True
                if not changed:
                    # Even an already-neutral reset must execute the native
                    # stale-state guard; do not confirm an old snapshot.
                    after = self.adjust_module(module, {"mode": "basic", "color": "red", "hue": 0}, after)["after"]
                return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                        "evidence": {"source": "c1_applescript", "model_verified": True, "render_verified": False,
                                     "reset_kind": "neutral_values", "reset_scope": "basic_advanced_only",
                                     "skin_tone_reset": False, "reset_to_c1_defaults": False, "atomic": False}}
            else:
                raise Refused("unsupported", "Mac neutral reset excludes White Balance; its camera defaults are not fixed")
        if (source["target"]["instance"] != before["target"]["instance"]
                or source["target"]["document"] != before["target"]["document"]
                or not (source["target"]["layer"] == "Background" or self._local(source))):
            raise Refused("wrong_source", "Read the source in the same C1 instance and Session before pasting")
        engine = before.get("native_context", {}).get("processing_engine")
        if not isinstance(engine, int) or source.get("native_context", {}).get("processing_engine") != engine:
            raise Refused("engine_mismatch", "Read source and destination with the same native processing engine before pasting")
        if module == "color_mixer":
            return self._paste_color_editor(patch, before, source)
        if module in MODULES:
            fields = MODULES[module]
            if not all(field in item.get("values", {}) for item in (before, source) for field in fields):
                raise Refused("incomplete_module", "Read the complete source and destination modules before pasting")
            values = {field: source["values"][field] for field in fields}
            if any(value is None for value in values.values()):
                raise Refused("unset_source_values", "A complete numeric paste cannot reproduce unset local values; read explicit source values or use reset to write neutral values")
            def paste_numeric():
                result = self.set_values(values, before)
                expected = {**before["values"], **values}
                for field, value in expected.items():
                    actual = result["observation"]["values"][field]
                    matches = actual is None if value is None else (
                        isinstance(actual, (int, float)) and not isinstance(actual, bool) and
                        math.isclose(actual, value, rel_tol=0, abs_tol=FIELD_RANGES[field][2]))
                    if not matches:
                        raise Refused("paste_readback_mismatch", "Native module paste differs from the requested source values")
                return result
            intent = ({"reset_kind": "neutral_values"} if neutral_reset else
                      {"source_observation_token": patch["source_observation_token"]})
            after = self.action_hook(module + "." + patch["action"], paste_numeric, intent)["observation"]
        else:
            if module not in {"curves", "levels", "color_balance"} or source.get("module") != module:
                raise Refused("wrong_source", "Read the same complete source module before pasting")
            state = source["state"]
            if module == "curves":
                patches = [{"channel": channel, "points": points} for channel, points in state["channels"].items()]
            elif module == "levels":
                patches = [{"channel": channel, **values} for channel, values in state["channels"].items()]
            else:
                patches = [{"wheel": wheel, **{axis: state["values"][prefix + "_" + axis]
                            for axis in (("x", "y") if wheel == "master" else ("x", "y", "lightness"))}}
                           for wheel, prefix in (("master", "master"), ("shadows", "shadow"),
                                                 ("midtones", "midtone"), ("highlights", "highlight"))]
            # Validate the complete plan before the first write; each existing
            # native setter retains its stale-state guard and durable action.
            for part in patches:
                validate_structured_patch({"module": module, "patch": part})
            if module in {"curves", "levels"}:
                changed = [part for part in patches if (part["points"] != before["state"]["channels"][part["channel"]]
                    if module == "curves" else any(before["state"]["channels"][part["channel"]][key] != value
                        for key, value in part.items() if key != "channel"))]
                patches = changed or patches[:1]  # Retain a native guard even for a no-op.
            after = before
            for part in patches:
                after = self.adjust_module(module, part, after)["after"]
        return {"status": "control_confirmed", "requested_result_verified": True, "after": after,
                "evidence": {"source": "c1_applescript", "model_verified": True, "render_verified": False,
                             **({"reset_kind": "neutral_values", "reset_to_c1_defaults": False,
                                 "custom_camera_defaults_used": False} if neutral_reset else
                                {"paste_scope": "complete_exposed_module_from_observation"}), "atomic": False}}

    @staticmethod
    def _verified_mask_copy(before, after, patch):
        a, b = before["state"], after["state"]
        if any(before["target"][k] != after["target"][k] for k in ("instance", "document", "image", "variant")):
            return False
        index = a["layers"].index(a["selected_layer"])
        source = a["layers"].index(patch["source_layer"])
        if len(a["layers"]) != len(b["layers"]) or b["selected_layer"] != b["layers"][index]:
            return False
        stable = lambda ref: {k: v for k, v in ref.items() if k not in {"native_id", "runtime_id"}}
        for i, ref in enumerate(a["layers"]):
            if i != index:
                if ref != b["layers"][i] or a["details"][i] != b["details"][i]:
                    return False
            else:
                expected = {**a["details"][i], "kind": a["details"][source]["kind"],
                            "luma_range": a["details"][source]["luma_range"]}
                if stable(ref) != stable(b["layers"][i]) or expected != b["details"][i]:
                    return False
        old = {m["uuid"]: m["content_sha256"] for m in a["masks"]}
        new = {m["uuid"]: m["content_sha256"] for m in b["masks"]}
        old_selected, new_selected = a["selected_layer"]["native_id"], b["selected_layer"]["native_id"]
        return (len(old) == len(new) and new.get(new_selected) == old[patch["source_layer"]["native_id"]]
                and {k: v for k, v in old.items() if k != old_selected} == {k: v for k, v in new.items() if k != new_selected})

    def _reconcile_native_layer_action(self, record):
        before = self._layer_action_before(record["before"])
        patch = record["request"]["patch"]
        after = self.observe_module("masks")
        if any(before["target"][key] != after["target"][key] for key in ("instance", "document", "image", "variant")):
            raise Refused("target_changed", "Return to the original candidate before reconciling its native layer action")
        receipt = ((record.get("reconciliation") or {}).get("evidence", {}).get("native_receipt")
                   or (record.get("error") or {}).get("details"))
        if not isinstance(receipt, dict):
            receipt = None
        try:
            native = self.native_bridge().inspect(after["target"])
            live = native.get("mask_operation_present") is True
            if live:
                receipt = self.native_bridge().call(after["target"], "mask_status", operation_id=record["operation_id"])
            arguments = self._layer_action_arguments(before, patch)
            if (receipt is None or receipt.get("operation_id") != record["operation_id"]
                    or receipt.get("target") != before["native_context"].get("bridge_target")
                    or receipt.get("kind") != patch["action"] or receipt.get("temporal_open") is not False
                    or receipt.get("callback_received") is not True
                    or any(receipt.get("arguments", {}).get(key) != value for key, value in arguments.items())):
                raise Refused("native_layer_unresolved", "The original terminal native layer receipt is unavailable")
            if receipt.get("state") == "committed":
                proof = self._native_layer_proof(before, after, patch, receipt, record["operation_id"])
                outcome = "applied_not_restored"
            elif ((receipt.get("state") == "rejected" and receipt.get("dispatched") is False)
                  or (receipt.get("state") == "cancelled" and patch["action"] == "combine_ai")):
                native_after = after["native_context"].get("bridge_target", {})
                native_before = before["native_context"].get("bridge_target", {})
                if (before["target"] != after["target"] or before["state"] != after["state"]
                        or before["native_context"]["mask_file_snapshot"] != after["native_context"]["mask_file_snapshot"]
                        or before["native_context"]["mask_ax_parameters"] != self._mask_ax_parameter_snapshot(after)
                        or any(native_after.get(key) != native_before.get(key) for key in ("layer_uuid", "submask_index"))):
                    raise Refused("native_layer_unresolved", "Unapplied native action lacks complete unchanged state and live selection")
                proof, outcome = {"native_receipt": receipt, "complete_saved_state_unchanged": True,
                                  "saved_layer_snapshot": before["native_context"].get("duplicate_snapshot")}, "not_applied"
            else:
                raise Refused("native_layer_unresolved", "Native layer action has not reached a verified terminal state")
            # Core requires the original public module shape for both applied
            # and not-applied outcomes; the mask baseline stays private.
            result = self._layer_action_result(after, patch["action"], proof.get("saved_layer_snapshot"))
            if live:
                self.native_bridge().call(result["target"], "mask_release", operation_id=record["operation_id"])
            return {"target": result["target"], "outcome": outcome,
                    "evidence": {**proof, "observation": result, "replayed": False},
                    "target_transition": {"before": before["target"], "after": result["target"],
                        "evidence": {"selected_parent_rename": {"combine": "native_combine_existing", "combine_ai": "native_combine_ai"}.get(patch["action"]),
                                     "before_layers": before["state"]["layers"], "after_layers": result["state"]["layers"],
                                     "selected_layer": result["state"]["selected_layer"]}}}
        except (Refused, OSError) as error:
            return {"target": after["target"], "outcome": "unknown",
                    "evidence": {"observation": after, "replayed": False, "native_receipt": receipt,
                                 "reason": getattr(error, "code", type(error).__name__)}}

    def _reconcile_native_mask_command(self, record):
        before, patch = record["before"], record["request"]["patch"]
        after = self.observe_module("masks")
        if any(before["target"][k] != after["target"][k] for k in ("instance", "document", "image", "variant")):
            raise Refused("target_changed", "Return to the original candidate to reconcile its native mask command")
        receipt = ((record.get("reconciliation") or {}).get("evidence", {}).get("native_receipt")
                   or (record.get("error") or {}).get("details"))
        if not isinstance(receipt, dict):
            receipt = None
        try:
            native = self.native_bridge().inspect(after["target"])
            live = native.get("mask_operation_present") is True
            if live:
                receipt = self.native_bridge().call(after["target"], "mask_status", operation_id=record["operation_id"])
            if (receipt is None or receipt.get("operation_id") != record["operation_id"]
                    or receipt.get("target") != before["native_context"].get("bridge_target")
                    or receipt.get("kind") != patch["action"] or receipt.get("temporal_open") is not False
                    or receipt.get("callback_received") is not True):
                raise Refused("native_mask_unresolved", "Original terminal native mask receipt is unavailable")
            if receipt.get("state") == "committed":
                proof = self._native_mask_proof(before, after, patch, receipt)
                outcome = "applied_not_restored"
            elif receipt.get("state") in {"cancelled", "rejected"}:
                if (before["target"] != after["target"] or before["state"] != after["state"]
                        or before["native_context"]["mask_file_snapshot"] != after["native_context"]["mask_file_snapshot"]
                        or before["native_context"]["mask_ax_parameters"] != self._mask_ax_parameter_snapshot(after)):
                    raise Refused("native_mask_unresolved", "Cancelled mask command changed saved state")
                proof, outcome = {"native_receipt": receipt, "complete_saved_state_unchanged": True}, "not_applied"
            else:
                raise Refused("native_mask_unresolved", "Native mask command has not reached a terminal state")
            if live:
                self.native_bridge().call(after["target"], "mask_release", operation_id=record["operation_id"])
            return {"target": after["target"], "outcome": outcome,
                    "evidence": {**proof, "observation": after, "replayed": False},
                    "target_transition": {"before": before["target"], "after": after["target"],
                        "evidence": {"before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                     "selected_layer": after["state"]["selected_layer"]}}}
        except (Refused, OSError) as error:
            return {"target": after["target"], "outcome": "unknown",
                    "evidence": {"observation": after, "replayed": False, "native_receipt": receipt,
                                 "reason": getattr(error, "code", type(error).__name__)}}

    def _reconcile_native_color(self, record):
        from .macos_native_color import verify_saved_change
        before, patch = record["before"], record["request"]["patch"]
        after = self.observe_module("color_mixer")
        receipt = ((record.get("reconciliation") or {}).get("evidence", {}).get("native_receipt")
                   or (record.get("error") or {}).get("details"))
        try:
            native = self.native_bridge().inspect(after["target"])
            live = native.get("color_operation_present") is True
            if live:
                receipt = self.native_bridge().call(after["target"], "color_status", operation_id=record["operation_id"])
            context = before["native_context"]
            if (not isinstance(receipt, dict) or receipt.get("operation_id") != record["operation_id"]
                    or receipt.get("target") != context.get("bridge_target") or receipt.get("patch") != patch
                    or receipt.get("after_target") != after["native_context"].get("bridge_target")
                    or before["target"] != after["target"]
                    or context.get("color_native_guard") != after["native_context"].get("color_native_guard")
                    or context["layer_state"] != after["native_context"]["layer_state"]):
                raise Refused("native_color_unresolved", "Original native Color Editor receipt or complete target proof unavailable")
            proof = verify_saved_change(context["color_editor_snapshot"],
                after["native_context"]["color_editor_snapshot"], receipt, patch)
            if live:
                self.native_bridge().call(after["target"], "color_release", operation_id=record["operation_id"])
            return {"target": after["target"], "outcome": "applied_not_restored",
                    "evidence": {**proof, "observation": after, "replayed": False, "native_receipt": receipt}}
        except (Refused, OSError) as error:
            return {"target": after["target"], "outcome": "unknown",
                    "evidence": {"observation": after, "replayed": False, "native_receipt": receipt,
                                 "reason": getattr(error, "code", type(error).__name__)}}

    def reconcile(self, record):
        module = record.get("request", {}).get("module")
        color_patch = record.get("request", {}).get("patch", {})
        if (module == "color_mixer" and record.get("before", {}).get("native_context", {}).get("native_color_snapshot")
                and (color_patch.get("mode") in {"basic", "advanced", "skin"} or color_patch.get("action") == "reset")):
            return self._reconcile_native_color(record)
        if (module in {"layers", "masks"} and record["request"]["patch"]["action"] in
                record.get("before", {}).get("native_context", {}).get("bridge_layer_actions", [])):
            return self._reconcile_native_layer_action(record)
        if (module == "masks" and record["request"]["patch"]["action"] in
                record.get("before", {}).get("native_context", {}).get("bridge_mask_actions", [])):
            return self._reconcile_native_mask_command(record)
        if (module == "masks" and record["request"]["patch"]["action"] in {"linear_gradient", "radial_gradient"}
                and record["request"]["patch"].get("coordinate_space", "raw") == "raw"):
            from .macos_mask_actions import native_gradient_parameters, verify_native_gradient, verify_gradient_selection_point
            before, patch = record["before"], record["request"]["patch"]
            after = self.observe_module("masks")
            if before["target"] != after["target"]:
                raise Refused("target_changed", "Return to the original photo and layer to reconcile the native gradient")
            parent = before["state"]["selected_layer"]
            status = None
            for receipt in ((record.get("reconciliation") or {}).get("evidence", {}).get("native_receipt"),
                            (record.get("error") or {}).get("details")):
                if isinstance(receipt, dict) and receipt.get("state") in {"cancelled", "rejected"}:
                    status = receipt
                    break
            try:
                parameters = native_gradient_parameters(before["state"], parent, patch)
                native = self.native_bridge().inspect(after["target"])
                if native["gradient_operation_present"]:
                    status = self.native_bridge().call(after["target"], "gradient_status", operation_id=record["operation_id"])
                # Release may complete before its reply is lost. In that case
                # only this operation's durable terminal receipt can prove it.
                if status is not None:
                    if status["state"] in {"cancelled", "rejected"}:
                        if (status.get("operation_id") != record["operation_id"]
                                or status.get("target") != before["native_context"].get("bridge_target")
                                or status.get("kind") != patch["action"]
                                or status.get("parameters") != parameters
                                or status.get("mask_uuid") != parent["native_id"]
                                or status.get("temporal_open") is not False
                                or (status.get("dispatched") is not False and status.get("callback_received") is not True)
                                or after["state"] != before["state"]
                                or after["native_context"]["mask_file_snapshot"] != before["native_context"]["mask_file_snapshot"]
                                or self._mask_ax_parameter_snapshot(after) != before["native_context"]["mask_ax_parameters"]):
                            raise Refused("native_gradient_unresolved", "Cancelled gradient lacks complete unchanged-state evidence")
                        # This only drops retained native bookkeeping; it does
                        # not cancel, undo, or repeat any host command.
                        if native["gradient_operation_present"]:
                            self.native_bridge().call(after["target"], "gradient_release", operation_id=record["operation_id"])
                        return {"target": after["target"], "outcome": "not_applied",
                                "evidence": {"observation": after, "native_receipt": status,
                                             "complete_saved_state_unchanged": True, "replayed": False}}
                    if status["state"] != "committed":
                        raise Refused("native_gradient_unresolved", "Native gradient has not reached a terminal state")
                proof = verify_native_gradient(before["state"], after["state"], parent, patch, parameters)
                snapshot = self._mask_ax_parameters(after)
                baseline = before["native_context"]["mask_ax_parameters"]
                if self._mask_ax_parameter_snapshot(after, saved_layers=snapshot) != baseline:
                    row = snapshot[int(after["target"]["variant"]["id"])]["local:" + parent["native_id"].lower()]
                    verify_gradient_selection_point(row.get("ZLAYERSELECTPOINT"), patch["action"], parameters)
                    row["ZLAYERSELECTPOINT"] = before["native_context"].get("gradient_selection_point")
                    # Older observations did not retain the marker. Derive a
                    # candidate from their exact old float geometry, then still
                    # require the entire original saved-parameter hash below.
                    if "gradient_selection_point" not in before["native_context"]:
                        old_mask = next(m for m in before["state"]["masks"] if m["uuid"] == parent["native_id"])
                        old = old_mask["structure"].get("linear_gradient_raw")
                        if old:
                            import struct
                            f32 = lambda v: struct.unpack("<f", struct.pack("<f", v))[0]
                            row["ZLAYERSELECTPOINT"] = ";".join(f"{f32(a + (b - a) * old[4]):.6f}" for a, b in zip(old[:2], old[2:4]))
                    if self._mask_ax_parameter_snapshot(after, saved_layers=snapshot) != baseline:
                        raise Refused("mask_readback_mismatch", "Other saved layer parameters differ from the original snapshot")
                if status is not None:
                    self.native_bridge().call(after["target"], "gradient_release", operation_id=record["operation_id"])
                return {"target": after["target"], "outcome": "applied_not_restored",
                        "evidence": {**proof, "observation": after, "replayed": False,
                                     "complete_saved_parameters_unchanged_except_native_selection_marker": True},
                        "target_transition": {"before": before["target"], "after": after["target"],
                            "evidence": {"before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                         "selected_layer": after["state"]["selected_layer"]}}}
            except (Refused, OSError) as error:
                return {"target": after["target"], "outcome": "unknown",
                        "evidence": {"observation": after, "replayed": False,
                                     "reason": getattr(error, "code", type(error).__name__),
                                     **({"native_receipt": status} if status is not None and
                                        status.get("state") in {"cancelled", "rejected"} else {})}}
        if module == "masks" and record["request"]["patch"]["action"] in {"combine_ai", "select_submask", "delete_submask"}:
            before, patch = record["before"], record["request"]["patch"]
            after = self.observe_module("masks")
            if any(after["target"][key] != record["target"][key] for key in ("instance", "document", "image", "variant")):
                raise Refused("target_changed", "Return to the original photo before inspecting the UI mask action")
            if patch["action"] == "combine_ai":
                baseline = before.get("native_context", {}).get("mask_ax_parameters")
                reason = "original_complete_saved_parameter_snapshot_unavailable"
                if baseline:
                    try:
                        from .macos_ax import run_ax
                        from .macos_mask_actions import verify_combine_ai
                        if baseline != after["native_context"]["mask_ax_parameters"]:
                            raise Refused("mask_readback_mismatch", "Saved non-target layer parameters differ from the original observation")
                        live = self._mask_ax_layout(after, run_ax(self.state_dir, after["target"], "read_layers"))
                        if live["submask_index"] != -1:
                            raise Refused("whole_parent_required", "Select the original whole parent before reconciling its combination")
                        stable = self.observe_module("masks")
                        if (stable["target"] != after["target"] or stable["state"] != after["state"] or
                                stable["native_context"]["mask_ax_parameters"] != baseline):
                            raise Refused("mask_state_changed", "Mask processing or saved parameters changed during reconciliation")
                        proof = verify_combine_ai(before["state"], stable["state"], before["state"]["selected_layer"],
                                                  patch["mode"], patch["kind"])
                        final_live = self._mask_ax_layout(stable, run_ax(self.state_dir, stable["target"], "read_layers"))
                        final = self.observe_module("masks")
                        if (final_live != live or final["target"] != stable["target"] or final["state"] != stable["state"] or
                                final["native_context"]["mask_ax_parameters"] != baseline):
                            raise Refused("mask_state_changed", "Whole-parent selection or saved content did not remain stable")
                        return {"target": final["target"], "outcome": "applied_not_restored",
                                "evidence": {**proof, "observation": final, "replayed": False, "atomic": False,
                                             "complete_saved_parameters_unchanged": True, "live_whole_parent_verified": True,
                                             "scope": "original_saved_state_and_current_stable_native_and_ax_readback"},
                                "target_transition": {"before": before["target"], "after": final["target"],
                                    "evidence": {"selected_parent_rename": "native_combine_ai",
                                                 "before_layers": before["state"]["layers"], "after_layers": final["state"]["layers"],
                                                 "selected_layer": final["state"]["selected_layer"]}}}
                    except Refused as error:
                        reason = error.code
                return {"target": after["target"], "outcome": "unknown",
                        "evidence": {"observation": after, "replayed": False, "reason": reason,
                                     "scope": "complete_original_and_stable_current_evidence_required; unchanged_does_not_prove_not_applied"}}
            return {"target": after["target"], "outcome": "unknown",
                    "evidence": {"observation": after, "replayed": False,
                                 "scope": "current_saved_structure_only; no_live_selection_or_command_reconstruction"}}
        if module == "masks" and record["request"]["patch"]["action"] in {"feather", "refine"}:
            after = self.observe_module("masks")
            if after["target"] != record["target"]:
                raise Refused("target_changed", "Return to the original layer before inspecting the mask filter")
            # A native filter may legitimately produce identical pixels. Saved
            # pixels encode neither its amount nor whether the command ran.
            return {"target": after["target"], "outcome": "unknown",
                    "evidence": {"observation": after, "replayed": False,
                                 "state_unchanged": after["state"] == record["before"]["state"],
                                 "amount_readback_verified": False, "algorithm_result_verified": False,
                                 "scope": "saved_mask_state_only; native_filter_acknowledgement_unavailable"}}
        if module == "masks" and record["request"]["patch"]["action"] == "clear_luma_range":
            from .macos_mask_actions import verify_clear_luma_range
            before, after = record["before"], self.observe_module("masks")
            if after["target"] != record["target"]:
                raise Refused("target_changed", "Return to the original layer before inspecting Clear Luma Range")
            outcome = "not_applied" if after["state"] == before["state"] else "unknown"
            if outcome == "unknown":
                try:
                    verify_clear_luma_range(before["state"], after["state"], before["state"]["selected_layer"])
                    outcome = "applied_not_restored"
                except Refused:
                    pass
            return {"target": after["target"], "outcome": outcome,
                    "evidence": {"observation": after, "replayed": False,
                                 "scope": "sole_direct_luma_removed_and_surviving_masks_unchanged"}}
        if module == "masks" and record["request"]["patch"]["action"] == "luma_range":
            after = self.observe_module("masks")
            if after["target"] != record["target"]:
                raise Refused("target_changed", "Return to the original layer before inspecting Luma Range")
            return {"target": after["target"],
                    "outcome": "not_applied" if after["state"] == record["before"]["state"] else "unknown",
                    "evidence": {"observation": after, "replayed": False, "scope": "complete_unchanged_mask_state_only"}}
        if module == "masks" and record["request"]["patch"]["action"] == "copy":
            before, patch = record["before"], record["request"]["patch"]
            after = self.observe_module("masks")
            if not self._verified_mask_copy(before, after, patch):
                raise Refused("copy_not_verified", "Saved state cannot verify this original mask copy; no command was replayed")
            return {"target": after["target"], "outcome": "applied_not_restored",
                    "evidence": {"observation": after, "replayed": False, "scope": "bounded_mask_uuid_replacement_and_equal_saved_content"},
                    "target_transition": {"before": before["target"], "after": after["target"],
                        "evidence": {"copy_identity": "native_mask_uuid_replaced_by_copy",
                                     "before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                     "selected_layer": after["state"]["selected_layer"]}}}
        if module == "masks" and record["request"].get("patch", {}).get("action") in {"fill", "clear", "invert"}:
            before, patch = record["before"], record["request"]["patch"]
            after = self.observe_module("masks")
            if before["target"] != after["target"]:
                raise Refused("target_changed", "Return to the originally observed layer before inspecting this mask")
            selected = before["state"]["selected_layer"]["native_id"]
            old = {m["uuid"]: m for m in before["state"]["masks"]}
            new = {m["uuid"]: m for m in after["state"]["masks"]}
            desired = 1 if patch["action"] == "fill" else 0
            if patch["action"] == "invert":
                if not old[selected]["constant"]["verified"] or old[selected]["constant"]["density"] not in (0, 1):
                    from .macos_mask_actions import verify_parameter_invert, verify_raster_invert
                    parameter_mask = old[selected]["mask_type"] in {1, 3, 4}
                    unchanged = before["state"] == after["state"]
                    # A half-density ConstantFill can invert to itself. Without
                    # the original command receipt unchanged parameters prove
                    # neither dispatch nor non-dispatch.
                    outcome = "not_applied" if unchanged and not parameter_mask else "unknown"
                    evidence = {}
                    if not unchanged:
                        try:
                            if parameter_mask and old[selected]["structure"].get("composition_raw") != [1, 1.0, 0, 0]:
                                raise Refused("unsupported", "Parameter inversion requires a neutral simple mask")
                            verify_invert = verify_parameter_invert if parameter_mask else verify_raster_invert
                            evidence = verify_invert(before["state"], after["state"], before["state"]["selected_layer"])
                            outcome = "applied_not_restored"
                        except Refused:
                            pass
                    return {"target": after["target"], "outcome": outcome,
                            "evidence": {"observation": after, "replayed": False,
                                         "scope": ("saved_native_parameter_inversion_and_unchanged_layers" if parameter_mask
                                                   else "saved_raster_complement_and_unchanged_layers"), **evidence},
                            "target_transition": {"before": before["target"], "after": after["target"],
                                "evidence": {"before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                             "selected_layer": after["state"]["selected_layer"]}}}
                desired = 1 - old[selected]["constant"]["density"]
            native = lambda state: {k: v for k, v in state.items() if k != "masks"}
            expected_native = json.loads(json.dumps(native(before["state"])))
            selected_index = before["state"]["layers"].index(before["state"]["selected_layer"])
            # C1 derives adjustment/filled kind from the resulting constant mask.
            if after["state"]["details"][selected_index]["kind"] == ("filled" if desired else "adjustment"):
                expected_native["details"][selected_index]["kind"] = "filled" if desired else "adjustment"
            confirmed = (expected_native == native(after["state"]) and old.keys() == new.keys()
                         and all(new[k]["content_sha256"] == old[k]["content_sha256"] for k in old if k != selected)
                         and new[selected]["constant"]["verified"] and new[selected]["constant"]["density"] == desired)
            outcome = ("not_applied" if before["state"] == after["state"] else
                       "applied_not_restored" if confirmed else "unknown")
            return {"target": after["target"], "outcome": outcome,
                    "evidence": {"observation": after, "replayed": False, "scope": "saved_constant_mask_and_unchanged_layers"},
                    "target_transition": {"before": before["target"], "after": after["target"],
                        "evidence": {"before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                     "selected_layer": after["state"]["selected_layer"]}}}
        if module == "layers" and record["request"].get("patch", {}).get("action") in {"select", "rename"}:
            after = self.observe_module("layers")
            before = record["before"]
            if any(before["target"][key] != after["target"][key] for key in ("instance", "document", "image", "variant")):
                raise Refused("target_changed", "Return to the original candidate before reconciling layers")
            expected = json.loads(json.dumps(before["state"]))
            patch = record["request"]["patch"]
            index = expected["layers"].index(patch["layer_ref"])
            if patch["action"] == "rename":
                expected["layers"][index]["name"] = patch["name"]
                expected["details"][index]["name"] = patch["name"]
            expected["selected_layer"] = expected["layers"][index]
            outcome = ("not_applied" if after["state"] == before["state"] and after["target"] == before["target"] else
                       "applied_not_restored" if after["state"] == expected else "unknown")
            return {"target": after["target"], "outcome": outcome,
                    "evidence": {"observation": after, "replayed": False, "scope": "current_layer_state_only"},
                    "target_transition": {"before": before["target"], "after": after["target"],
                        "evidence": {"before_layers": before["state"]["layers"], "after_layers": after["state"]["layers"],
                                     "selected_layer": after["state"]["selected_layer"]}}}
        if module not in IMPLEMENTED_MODULES and module not in MODULES:
            raise Refused("unsupported", "Mac reconciliation currently covers implemented structured modules only")
        after = self.observe(list(record["before"]["values"])) if module in MODULES else self.observe_module(module)
        if after["target"] != record["target"]:
            raise Refused("target_changed", "Select the original candidate before inspecting the uncertain result")
        key = "values" if module in MODULES else "state"
        if module == "color_mixer":
            before_context = record["before"].get("native_context", {})
            after_context = after.get("native_context", {})
            old, new = (context.get("color_editor_snapshot", {}) for context in (before_context, after_context))
            unchanged = (old.get("available") is True and new.get("available") is True
                         and old.get("source") == new.get("source") and bool(old.get("sha256"))
                         and old["sha256"] == new.get("sha256")
                         and before_context.get("layer_state") == after_context.get("layer_state")
                         and after[key] == record["before"][key])
            return {"target": after["target"], "outcome": "not_applied" if unchanged else "unknown",
                    "evidence": {"observation": after, "source": "c1_applescript", "replayed": False,
                                 "scope": "complete_saved_color_editor_and_layer_state; exposed_values_alone_are_insufficient"}}
        return {"target": after["target"],
                "outcome": "not_applied" if after[key] == record["before"][key] else "unknown",
                "evidence": {"observation": after, "source": "c1_applescript", "replayed": False,
                             "scope": "current_complete_module_state; no claim about intermediate events"}}
