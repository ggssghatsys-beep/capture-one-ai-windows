"""Synthetic pixels and Win32 shims only: never call a live capture API."""
import base64
import copy
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from c1ai import fast_preview as fast
from c1ai.native_images import NativeAdapterError


class ViewerBackend:
    def __init__(self, directory):
        self.state_dir = Path(directory)
        self.target = {"instance": {"pid": 7, "started_at": "123456", "version": "16.8.6"},
                       "document": {"path": "synthetic.cosessiondb"}, "image": {"path": "a.NEF"},
                       "variant": {"id": "synthetic-uuid", "label": "a.NEF [2]"}, "layer": "Background"}
        self.window = {"hwnd": 123, "pid": 7, "title": "synthetic", "exe": r"C:\fake\CaptureOne.exe",
                       "visible": True, "minimized": False, "version": "16.8.6"}
        self.node = {**fast.VIEWER, "enabled": True, "offscreen": False, "runtime_id": [7, 8, 9],
                     "rect": {"x": 110, "y": 110, "width": 100, "height": 100}}
        self.values = {"exposure": 0.2}
        self.view = {"zoom": {"automation_id": "ZoomText", "control_type": "Text", "name": "100%"},
                     "before_after": {"automation_id": "BeforeAfterOnOff", "control_type": "DataItem", "name": "synthetic toolbar item"}}
        self.calls = []

    def observe(self, fields):
        self.calls.append(("observe", fields))
        return {"target": copy.deepcopy(self.target), "values": copy.deepcopy(self.values),
                "raw_state": {"window": copy.deepcopy(self.window)}}

    def call(self, action, **payload):
        self.calls.append((action, payload))
        if action == "snapshot":
            return {"window": copy.deepcopy(self.window), "values": copy.deepcopy(self.view),
                    "missing": list(set(fast.VIEW_BINDINGS) - self.view.keys()), "atomic": False}
        if action != "read":
            raise AssertionError("Unexpected native operation: " + action)
        return {"window": copy.deepcopy(self.window), "node": copy.deepcopy(self.node)}

    def action(self, *args, **kwargs):
        raise AssertionError("Viewer sampling cannot dispatch a C1 mutation")


def png_bytes(color=None):
    frame = Image.new("RGB", (100, 100), color or (0, 0, 0))
    if color is None:
        frame.putdata([(x * 2, y * 2, (x + y) % 256) for y in range(100) for x in range(100)])
    stream = io.BytesIO(); frame.save(stream, format="PNG")
    return stream.getvalue()


def fake_result(request, data=None):
    window = {**request["window"], "rect": [100, 100, 220, 220]}
    return {"window_before": window, "window_after": copy.deepcopy(window),
            "viewer_rect": request["viewer_rect"], "print_window_flags": request["print_window_flags"],
            "print_window_success": True, "geometry_verified": True,
            "png_base64": base64.b64encode(data if data is not None else png_bytes()).decode("ascii")}


class FastPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.backend = ViewerBackend(self.temp.name)
        self.before = self.backend.observe(["exposure"])
        self.worker = patch.object(fast, "_run_worker", side_effect=lambda request, timeout: fake_result(request)).start()
        self.addCleanup(patch.stopall)

    def capture(self, **kwargs):
        return fast.capture_viewer(self.backend, self.before, **kwargs)

    def test_capture_binds_target_and_physical_viewer_crop_without_render_claim(self):
        result = self.capture()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source"], "viewer_capture")
        self.assertEqual(result["image"]["format"], "PNG")
        self.assertEqual((result["image"]["width"], result["image"]["height"]), (100, 100))
        self.assertEqual(result["target"], self.before["target"])
        self.assertIsNone(result["image"]["icc_profile"])
        self.assertFalse(result["render_verified"])
        self.assertFalse(result["evidence"]["adjustment_frame_freshness_verified"])
        self.assertFalse(result["evidence"]["input_sent"])
        self.assertFalse(result["evidence"]["desktop_capture"])
        path = Path(result["path"])
        self.assertEqual(path.parent, self.backend.state_dir / "viewer-captures")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result["image"]["sha256"])
        self.assertEqual(len(result["image"]["pixel_sha256"]), 64)
        self.assertEqual({name for name, _ in self.backend.calls}, {"read", "observe", "snapshot"})
        self.worker.assert_called_once()

    def test_current_view_records_actual_zoom_without_guessing_comparison_state(self):
        result = self.capture()
        view = result["current_view"]
        self.assertEqual(view["zoom_text"], "100%")
        self.assertTrue(view["before_after_control_observed"])
        self.assertIsNone(view["before_after_enabled"])
        self.assertFalse(view["before_after_state_verified"])
        self.assertFalse(view["whole_image_verified"])
        self.assertTrue(view["may_include_zoom_crop_or_before_after"])
        self.assertFalse(view["capture_synchronized"])
        self.assertEqual(view["observation_timing"], "before_capture")
        self.assertFalse(view["view_state_stability_verified"])
        snapshots = [payload for name, payload in self.backend.calls if name == "snapshot"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["bindings"], fast.VIEW_BINDINGS)
        self.assertNotIn("name", snapshots[0]["bindings"]["zoom"])

    def test_unavailable_view_controls_are_unknown_not_invented_values(self):
        self.backend.view = {}
        result = self.capture()
        self.assertIsNone(result["current_view"]["zoom_text"])
        self.assertIsNone(result["current_view"]["before_after_enabled"])
        self.assertEqual(set(result["current_view"]["missing_controls"]), set(fast.VIEW_BINDINGS))

    def test_verified_preflight_reuses_before_but_keeps_ending_observation_and_two_viewer_reads(self):
        for reuse, observations in ((False, 2), (True, 1)):
            with self.subTest(reuse=reuse):
                self.backend.calls.clear()
                result = self.capture(verified_preflight=reuse)
                calls = [name for name, _ in self.backend.calls]
                self.assertEqual(calls.count("observe"), observations)
                self.assertEqual(calls.count("read"), 2)
                self.assertEqual(calls.count("snapshot"), 1)
                self.assertEqual(result["evidence"]["verified_preflight_reused"], reuse)

    def test_verified_preflight_does_not_hide_changed_target_or_values(self):
        for during, field in ((False, "target"), (True, "target"), (True, "values")):
            with self.subTest(during=during, field=field):
                backend = ViewerBackend(self.temp.name)
                before = backend.observe([])
                def change():
                    if field == "target": backend.target["variant"]["id"] = "other"
                    else: backend.values["exposure"] = 1
                if not during: change()
                def worker(request, timeout):
                    if during: change()
                    return fake_result(request)
                self.worker.side_effect = worker
                with self.assertRaises(NativeAdapterError) as raised:
                    fast.capture_viewer(backend, before, verified_preflight=True)
                self.assertEqual(raised.exception.code, "PREVIEW_TARGET_CHANGED")
        self.assertFalse((self.backend.state_dir / "viewer-captures").exists())

    def test_printwindow_mode_is_explicit_and_never_falls_back(self):
        self.assertEqual(self.capture(print_window_flags=2)["print_window_flags"], 2)
        self.worker.reset_mock()
        self.worker.side_effect = NativeAdapterError("VIEWER_CAPTURE_FAILED", "black GPU surface")
        with self.assertRaises(NativeAdapterError):
            self.capture()
        self.worker.assert_called_once()

    def test_rejects_uniform_black_white_and_unpainted_colour(self):
        for color in ((0, 0, 0), (255, 255, 255), (127, 127, 127), (255, 0, 255)):
            with self.subTest(color=color):
                self.worker.side_effect = lambda request, timeout: fake_result(request, png_bytes(color))
                with self.assertRaises(NativeAdapterError) as raised:
                    self.capture()
                self.assertEqual(raised.exception.code, "VIEWER_CAPTURE_EMPTY")
        self.assertFalse((self.backend.state_dir / "viewer-captures").exists())

    def test_white_border_does_not_validate_black_viewer_surface(self):
        frame = Image.new("RGB", (100, 100), "white")
        frame.paste((0, 0, 0), (5, 5, 95, 95))
        data = io.BytesIO(); frame.save(data, format="PNG")
        self.worker.side_effect = lambda request, timeout: fake_result(request, data.getvalue())
        with self.assertRaises(NativeAdapterError) as raised:
            self.capture()
        self.assertEqual(raised.exception.code, "VIEWER_CAPTURE_EMPTY")

    def test_wrong_window_process_or_geometry_in_worker_output_is_rejected(self):
        for field in ("pid", "hwnd", "started_at", "title", "exe", "rect"):
            with self.subTest(field=field):
                def wrong(request, timeout):
                    result = fake_result(request)
                    result["window_after"][field] = "wrong"
                    return result
                self.worker.side_effect = wrong
                with self.assertRaises(NativeAdapterError) as raised:
                    self.capture()
                self.assertEqual(raised.exception.code, "VIEWER_CAPTURE_UNVERIFIED")

    def test_identity_values_or_viewer_changes_after_capture_never_save_artifact(self):
        for field in ("target", "values", "runtime", "rect"):
            with self.subTest(field=field):
                backend = ViewerBackend(self.temp.name)
                before = backend.observe([])
                def change(request, timeout):
                    if field == "target": backend.target["variant"]["id"] = "other"
                    elif field == "values": backend.values["exposure"] = 1
                    elif field == "runtime": backend.node["runtime_id"] = [42]
                    else: backend.node["rect"]["width"] = 99
                    return fake_result(request)
                self.worker.side_effect = change
                with self.assertRaises(NativeAdapterError):
                    fast.capture_viewer(backend, before)
        self.assertFalse((self.backend.state_dir / "viewer-captures").exists())

    def test_minimized_offscreen_wrong_viewer_or_bad_rect_stop_before_worker(self):
        for condition in ("minimized", "offscreen", "wrong_viewer", "nan", "large"):
            with self.subTest(condition=condition):
                backend = ViewerBackend(self.temp.name)
                if condition == "minimized": backend.window["minimized"] = True
                elif condition == "offscreen": backend.node["offscreen"] = True
                elif condition == "wrong_viewer": backend.node["automation_id"] = "CompareView"
                elif condition == "nan": backend.node["rect"]["x"] = float("nan")
                else: backend.node["rect"]["width"] = 1e8
                self.worker.reset_mock()
                with self.assertRaises(NativeAdapterError):
                    fast.capture_viewer(backend, backend.observe([]))
                self.worker.assert_not_called()

    def test_invalid_timeout_flags_or_selector_never_call_worker(self):
        for arguments in ({"timeout": 6}, {"timeout": float("nan")}, {"timeout": True},
                          {"print_window_flags": 3}, {"print_window_flags": True},
                          {"verified_preflight": 1},
                          {"viewer_selector": {"control_type": "Window"}}):
            with self.subTest(arguments=arguments), self.assertRaises(NativeAdapterError):
                self.capture(**arguments)
        self.worker.assert_not_called()

    def test_archive_does_not_follow_symlink(self):
        external = self.backend.state_dir / "external"; external.mkdir()
        try:
            (self.backend.state_dir / "viewer-captures").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        with self.assertRaises(NativeAdapterError):
            self.capture()
        self.assertEqual(list(external.iterdir()), [])

    def test_invalid_png_or_dimensions_are_rejected(self):
        def invalid(request, timeout):
            result = fake_result(request)
            result["png_base64"] = base64.b64encode(b"not PNG").decode()
            return result
        self.worker.side_effect = invalid
        with self.assertRaises(NativeAdapterError) as raised:
            self.capture()
        self.assertEqual(raised.exception.code, "VIEWER_CAPTURE_INVALID")

    def module_observation(self):
        self.backend.module_state = {"layers": [{"runtime_id": [42], "name": "Background"}],
                                     "selected_layer": {"runtime_id": [42], "name": "Background"}}
        def observe_module(module):
            self.backend.calls.append(("observe_module", module))
            return {"module": module, "state": copy.deepcopy(self.backend.module_state), "values": {},
                    "target": copy.deepcopy(self.backend.target), "raw_state": {"window": copy.deepcopy(self.backend.window)}}
        self.backend.observe_module = observe_module
        raw = observe_module("layers")
        return {**raw, "values": {"layers": copy.deepcopy(raw["state"])}}

    def test_structured_token_reads_same_module_and_normalizes_backend_state(self):
        observation = self.module_observation()
        self.backend.calls.clear()
        with patch.object(self.backend, "observe", side_effect=AssertionError("numeric read forbidden for module token")):
            result = fast.capture_viewer(self.backend, observation)
        self.assertEqual(result["evidence"]["observed_module"], "layers")
        self.assertEqual([name for name, _ in self.backend.calls].count("observe_module"), 2)
        self.assertFalse(result["render_verified"])

    def test_structured_state_drift_during_capture_refuses_artifact(self):
        observation = self.module_observation()
        def change(request, timeout):
            self.backend.module_state["selected_layer"]["name"] = "Other layer"
            return fake_result(request)
        self.worker.side_effect = change
        with self.assertRaises(NativeAdapterError) as raised:
            fast.capture_viewer(self.backend, observation)
        self.assertEqual(raised.exception.code, "PREVIEW_TARGET_CHANGED")
        self.assertFalse((self.backend.state_dir / "viewer-captures").exists())

    def test_verified_structured_preflight_still_observes_module_after_capture(self):
        observation = self.module_observation()
        self.backend.calls.clear()
        with patch.object(self.backend, "observe", side_effect=AssertionError("numeric read forbidden for module token")):
            result = fast.capture_viewer(self.backend, observation, verified_preflight=True)
        self.assertEqual(result["evidence"]["observed_module"], "layers")
        self.assertEqual([name for name, _ in self.backend.calls].count("observe_module"), 1)


class WorkerBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows subprocess options")
    def test_child_timeout_is_bounded_hidden_and_has_no_fallback(self):
        with patch.object(fast.subprocess, "run", side_effect=subprocess.TimeoutExpired("capture", 5)) as run:
            with self.assertRaises(NativeAdapterError) as raised:
                fast._run_worker({"request": "synthetic"}, 5)
        self.assertEqual(raised.exception.code, "VIEWER_CAPTURE_TIMEOUT")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertNotIn("shell", run.call_args.kwargs)


class NativeShim:
    """ctypes-shaped functions backed only by Python memory, no PInvoke."""
    def __init__(self, request, *, wrong_pid=False, change_after=False, paint=True):
        self.request, self.calls, self.buffer = request, [], None
        self.wrong_pid, self.change_after, self.paint = wrong_pid, change_after, paint
        self.printed = False

    def dll(self, name, **kwargs):
        return self

    def __getattr__(self, name):
        def call(*args):
            self.calls.append(name)
            if name in ("IsWindow", "IsWindowVisible"): return 1
            if name == "IsIconic": return 0
            if name == "SetThreadDpiAwarenessContext": return 1
            if name == "GetWindowThreadProcessId":
                args[1]._obj.value = 8 if self.wrong_pid or (self.change_after and self.printed) else 7
                return 1
            if name == "GetWindowRect":
                rectangle = args[1]._obj
                rectangle.left, rectangle.top, rectangle.right, rectangle.bottom = 100, 100, 220, 220
                return 1
            if name == "OpenProcess": return 11
            if name == "GetProcessTimes":
                args[1]._obj.dwLowDateTime = 123456
                args[1]._obj.dwHighDateTime = 0
                return 1
            if name == "QueryFullProcessImageNameW":
                args[2].value = self.request["window"]["exe"]; return 1
            if name == "GetWindowTextW": args[1].value = "synthetic"; return 9
            if name == "CreateCompatibleDC":
                if args[0] is not None: raise AssertionError("Only private memory DC allowed")
                return 12
            if name == "CreateDIBSection":
                self.buffer = ctypes.create_string_buffer(120 * 120 * 4)
                ctypes.cast(args[3], ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(self.buffer)
                return 13
            if name == "SelectObject": return 14
            if name == "PrintWindow":
                self.printed = True
                if self.paint:
                    pixels = bytes(component for y in range(120) for x in range(120)
                                   for component in ((x + y) % 256, y * 2, x * 2, 0))
                    ctypes.memmove(self.buffer, pixels, len(pixels))
                return 1
            if name in ("CloseHandle", "DeleteObject", "DeleteDC"): return 1
            raise AssertionError("Unexpected native function " + name)
        return call


@unittest.skipUnless(os.name == "nt", "Windows ctypes type declarations")
class NativeMemoryShimTests(unittest.TestCase):
    def request(self):
        return {"window": {"hwnd": 123, "pid": 7, "started_at": "123456", "title": "synthetic", "exe": r"C:\fake\CaptureOne.exe"},
                "viewer_rect": [110, 110, 210, 210], "print_window_flags": 0}

    def test_printwindow_uses_only_memory_dc_and_crops_the_bound_viewer(self):
        request = self.request(); shim = NativeShim(request)
        with patch.object(ctypes, "WinDLL", side_effect=shim.dll):
            result = fast._native_capture(request)
        data, image, window = fast._decode_capture(result, request)
        self.assertEqual((image["width"], image["height"]), (100, 100))
        self.assertEqual(shim.calls.count("PrintWindow"), 1)
        self.assertIn("DeleteDC", shim.calls)
        self.assertIn("DeleteObject", shim.calls)
        self.assertFalse(set(shim.calls) & {"GetDC", "BitBlt", "SetForegroundWindow", "ShowWindow", "SendInput"})

    def test_other_process_stops_before_print_and_changed_process_rejects_after(self):
        for before in (True, False):
            with self.subTest(before=before):
                request = self.request(); shim = NativeShim(request, wrong_pid=before, change_after=not before)
                with patch.object(ctypes, "WinDLL", side_effect=shim.dll), self.assertRaises(RuntimeError):
                    fast._native_capture(request)
                self.assertEqual(shim.calls.count("PrintWindow"), 0 if before else 1)

    def test_native_success_without_painted_gpu_pixels_is_rejected(self):
        request = self.request(); shim = NativeShim(request, paint=False)
        with patch.object(ctypes, "WinDLL", side_effect=shim.dll):
            result = fast._native_capture(request)
        with self.assertRaises(NativeAdapterError) as raised:
            fast._decode_capture(result, request)
        self.assertEqual(raised.exception.code, "VIEWER_CAPTURE_EMPTY")


if __name__ == "__main__":
    unittest.main()
