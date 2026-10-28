"""Bounded PrintWindow sampling of the qualified C1 Viewer, without input.

This is a display sample, not a C1 export or proof of completed adjustment
rendering. No screen DC, foreground activation, window restoration, or export
fallback is used. The blocking native call runs in a disposable child process.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from .native_images import checked_call, fail
from .native_preview import _assert_observation, _plain_path

VIEWER = {"automation_id": "Viewer", "class_name": "VariantGridItemWpf", "control_type": "Custom"}
MAX_PIXELS = 32_000_000
MAX_PNG_BYTES = 32 * 1024 * 1024


def _refresh_observation(backend, reference):
    module = reference.get("module")
    if module is not None:
        if not isinstance(module, str) or not isinstance(reference.get("state"), dict):
            fail("VIEWER_OBSERVATION_INVALID", "结构工具预览需要完整的模块状态。")
        current = backend.observe_module(module)
        if (not isinstance(current, dict) or current.get("module") != module
                or current.get("target") != reference.get("target") or current.get("state") != reference["state"]):
            fail("PREVIEW_TARGET_CHANGED", "捕获关联的目标或结构工具状态发生变化。")
        # Core uses normalized values while native module observations carry
        # their complete state separately. Compare the state, then normalize.
        return {**current, "values": {module: copy.deepcopy(current["state"])}}
    current = backend.observe(list(reference.get("values", {})))
    _assert_observation(reference, current)
    return current


def _rectangle(value):
    if not isinstance(value, dict):
        fail("VIEWER_GEOMETRY_UNVERIFIED", "查看器没有实际矩形。")
    coordinates = [value.get(key) for key in ("x", "y", "width", "height")]
    if any(type(item) not in (int, float) or not math.isfinite(item) for item in coordinates):
        fail("VIEWER_GEOMETRY_UNVERIFIED", "查看器矩形包含无效坐标。")
    x, y, width, height = coordinates
    if width < 32 or height < 32 or width * height > MAX_PIXELS:
        fail("VIEWER_GEOMETRY_UNVERIFIED", "查看器大小超出已限定捕获范围。")
    return [math.floor(x), math.floor(y), math.ceil(x + width), math.ceil(y + height)]


def _viewer(backend, observation, selector):
    if not isinstance(selector, dict) or any(selector.get(key) != value for key, value in VIEWER.items()):
        fail("VIEWER_SELECTOR_UNQUALIFIED", "仅支持实际 Viewer / VariantGridItemWpf 控件。")
    # Exact fixed selector is already supplied; rebuilding eight ancestor
    # selectors adds round trips but no identity evidence used by this path.
    if observation.get('evidence',{}).get('source')=='c1_internal_model':
        node=backend._model_call({'command':'viewer'},observation)['viewer']
    else:
        node = checked_call(backend, observation, 'read', selector=selector, profile='compact')['node']
    if (any(node.get(key) != value for key, value in VIEWER.items()) or node.get("offscreen") is not False
            or node.get("enabled") is not True or not isinstance(node.get("runtime_id"), list)
            or not node["runtime_id"] or any(type(value) is not int for value in node["runtime_id"])):
        fail("VIEWER_UNAVAILABLE", "查看器身份或可见状态不可核验。")
    return node, _rectangle(node.get("rect"))


def _window_identity(observation):
    window = observation.get("raw_state", {}).get("window", {})
    instance = observation.get("target", {}).get("instance", {})
    started = instance.get("started_at")
    if (type(window.get("hwnd")) is not int or window["hwnd"] <= 0
            or type(window.get("pid")) is not int or window["pid"] <= 0
            or instance.get("pid") != window["pid"] or not str(started).isdigit()
            or not isinstance(window.get("title"), str) or not window["title"]
            or not isinstance(window.get("exe"), str) or not window["exe"]):
        fail("VIEWER_WINDOW_UNVERIFIED", "查看器捕获缺少应用窗口及进程启动身份。")
    if window.get("minimized") is True or window.get("visible") is False:
        fail("VIEWER_WINDOW_UNAVAILABLE", "不捕获最小化或不可见窗口，也不会恢复或激活窗口。")
    return {"hwnd": window["hwnd"], "pid": window["pid"], "started_at": str(started),
            "title": window["title"], "exe": window["exe"]}




def _run_worker(request, timeout):
    if os.name != "nt":
        fail("VIEWER_CAPTURE_UNSUPPORTED", "PrintWindow 仅用于已核验的 Windows 环境。")
    try:
        completed = subprocess.run([sys.executable, "-m", "c1ai.fast_preview", "--capture-worker"],
                                   input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                                   creationflags=subprocess.CREATE_NO_WINDOW, check=False,
                                   cwd=str(Path(__file__).resolve().parent.parent))
    except subprocess.TimeoutExpired:
        fail("VIEWER_CAPTURE_TIMEOUT", "窗口捕获超时；已停止捕获子进程，不重试或转为导出。")
    if completed.returncode != 0 or len(completed.stdout) > MAX_PNG_BYTES * 2:
        fail("VIEWER_CAPTURE_FAILED", "窗口捕获子进程没有返回有效结果。")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeError):
        fail("VIEWER_CAPTURE_FAILED", "窗口捕获响应无法解析。")
    if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("result"), dict):
        error = result.get("error", {}) if isinstance(result, dict) else {}
        fail("VIEWER_CAPTURE_FAILED", "指定窗口没有返回可核验的查看器图像。", error)
    return result["result"]


def _decode_capture(result, request):
    from PIL import Image, ImageStat
    if not isinstance(result, dict):
        fail("VIEWER_CAPTURE_UNVERIFIED", "捕获响应不是可核验对象。")
    identity = request["window"]
    before, after = result.get("window_before", {}), result.get("window_after", {})
    if (not isinstance(before, dict) or before != after
            or any(before.get(key) != value for key, value in identity.items())
            or result.get("viewer_rect") != request["viewer_rect"]
            or result.get("print_window_flags") != request["print_window_flags"]
            or result.get("print_window_success") is not True or result.get("geometry_verified") is not True):
        fail("VIEWER_CAPTURE_UNVERIFIED", "捕获结果不属于同一窗口、进程或查看器区域。")
    try:
        encoded = result["png_base64"]
        if not isinstance(encoded, str) or len(encoded) > MAX_PNG_BYTES * 2:
            raise ValueError("capture exceeds bound")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > MAX_PNG_BYTES:
            raise ValueError("capture exceeds bound")
        with Image.open(io.BytesIO(data)) as frame:
            if frame.format != "PNG":
                raise ValueError("not PNG")
            rect = request["viewer_rect"]
            if frame.size != (rect[2] - rect[0], rect[3] - rect[1]):
                raise ValueError("wrong crop size")
            frame.load()
            rgb = frame.convert("RGB")
            # Test the centre, excluding UI borders which can make a black or
            # unpainted GPU surface look superficially non-uniform.
            inset_x, inset_y = max(1, rgb.width // 10), max(1, rgb.height // 10)
            middle = rgb.crop((inset_x, inset_y, rgb.width - inset_x, rgb.height - inset_y))
            stats = ImageStat.Stat(middle)
            spans = [high - low for low, high in middle.getextrema()]
            histogram = middle.convert("L").histogram()
            usable = sum(histogram[9:247]) / (middle.width * middle.height)
            if max(stats.var) < 2 or max(spans) < 8 or usable < 0.01:
                fail("VIEWER_CAPTURE_EMPTY", "查看器捕获为空白、近黑或无可用图像；不作为成功预览。")
            return data, {"format": "PNG", "width": rgb.width, "height": rgb.height, "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(),
                          "pixel_sha256": hashlib.sha256(rgb.tobytes()).hexdigest(), "decoded": True,
                          "icc_profile": None, "color_space_verified": False}, before
    except Exception as error:
        if getattr(error, "code", None):
            raise
        fail("VIEWER_CAPTURE_INVALID", "窗口捕获图像未通过完整解码和尺寸校验。", {"reason": str(error)})


def capture_viewer(backend, observation, *, viewer_selector=None, timeout=5, print_window_flags=0,
                   verified_preflight=False):
    """Sample Viewer; only the internal backend may reuse a Core preflight.

    ``verified_preflight`` is not a public request option. The backend may set
    it only for the observation freshly verified by Core under its owner lock.
    Native window identity and the complete ending observation remain fresh.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5:
        fail("INVALID_TIMEOUT", "窗口捕获子进程的超时必须大于 0 且不超过 5 秒。")
    if type(print_window_flags) is not int or print_window_flags not in (0, 2):
        fail("VIEWER_CAPTURE_UNSUPPORTED", "只允许显式选择 PrintWindow 模式 0 或 2；不会自动切换模式。")
    if type(verified_preflight) is not bool:
        fail("VIEWER_OBSERVATION_INVALID", "内部预检查标记必须是布尔值。")
    selector = copy.deepcopy(viewer_selector if viewer_selector is not None else VIEWER)
    before = copy.deepcopy(observation) if verified_preflight else _refresh_observation(backend, observation)
    identity = _window_identity(before)
    node, rectangle = _viewer(backend, before, selector)
    current_view = {"sampling": "current_view", "metadata_sampled": False,
                    "zoom_text": None, "before_after_enabled": None,
                    "whole_image_verified": False, "may_include_zoom_crop_or_before_after": True,
                    "capture_synchronized": False}
    request = {"window": identity, "viewer_rect": rectangle, "print_window_flags": print_window_flags}
    started = time.time()
    result = _run_worker(request, timeout)
    data, image, native_window = _decode_capture(result, request)
    after = _refresh_observation(backend, before)
    if _window_identity(after) != identity:
        fail("VIEWER_CAPTURE_UNVERIFIED", "捕获后应用窗口身份发生变化。")
    after_node, after_rectangle = _viewer(backend, after, selector)
    if after_node["runtime_id"] != node["runtime_id"] or after_rectangle != rectangle or after_node["rect"] != node["rect"]:
        fail("VIEWER_GEOMETRY_CHANGED", "捕获期间查看器控件或位置发生变化。")
    directory = _plain_path(_plain_path(backend.state_dir, directory=True) / "viewer-captures")
    directory.mkdir(exist_ok=True)
    directory = _plain_path(directory, directory=True)
    path = _plain_path(directory / (image["sha256"] + ".png"))
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.stat().st_nlink != 1 or hashlib.sha256(path.read_bytes()).hexdigest() != image["sha256"]:
            fail("VIEWER_ARCHIVE_CONFLICT", "已有捕获归档与本次哈希不匹配。")
    return {"status": "completed", "source": "viewer_capture", "capture_backend": "print_window",
            "print_window_flags": print_window_flags, "path": str(path), "target": after["target"], "image": image,
            "current_view": current_view,
            "capture_verified": True, "render_verified": False, "quality_approved": False,
            "sampled_at": started, "completed_at": time.time(),
            "evidence": {"target_and_observed_values_unchanged": True, "observed_fields": list(before.get("values", {})),
                         "observed_module": before.get("module"),
                         "verified_preflight_reused": verified_preflight,
                         "viewer_runtime_id": node["runtime_id"], "viewer_rect": rectangle,
                         "native_window_rect": native_window.get("rect"), "window_process_verified": True,
                         "input_sent": False, "desktop_capture": False, "adjustment_frame_freshness_verified": False},
            "limits": "Sampled current Viewer; it may show a zoomed crop or before/after comparison. Zoom and before/after metadata are not sampled. No verified ICC or guarantee that the current adjustment has finished rendering; not an exported render."}


def _native_capture(request):
    """Child-only Win32 implementation. Never acquires a desktop or screen DC."""
    import ctypes as c
    from ctypes import wintypes as w
    from PIL import Image
    if os.name != "nt":
        raise RuntimeError("Windows required")
    user, gdi, kernel = c.WinDLL("user32", use_last_error=True), c.WinDLL("gdi32", use_last_error=True), c.WinDLL("kernel32", use_last_error=True)
    def api(dll, name, result, *args):
        method = getattr(dll, name); method.restype = result; method.argtypes = list(args); return method
    is_window = api(user, "IsWindow", w.BOOL, w.HWND)
    is_visible = api(user, "IsWindowVisible", w.BOOL, w.HWND)
    is_iconic = api(user, "IsIconic", w.BOOL, w.HWND)
    get_pid = api(user, "GetWindowThreadProcessId", w.DWORD, w.HWND, c.POINTER(w.DWORD))
    get_rect = api(user, "GetWindowRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
    get_text = api(user, "GetWindowTextW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
    set_dpi = api(user, "SetThreadDpiAwarenessContext", c.c_void_p, c.c_void_p)
    open_process = api(kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    close_handle = api(kernel, "CloseHandle", w.BOOL, w.HANDLE)
    get_times = api(kernel, "GetProcessTimes", w.BOOL, w.HANDLE, *([c.POINTER(w.FILETIME)] * 4))
    get_exe = api(kernel, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD))
    create_dc = api(gdi, "CreateCompatibleDC", w.HDC, w.HDC)
    delete_dc = api(gdi, "DeleteDC", w.BOOL, w.HDC)
    select_object = api(gdi, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
    delete_object = api(gdi, "DeleteObject", w.BOOL, w.HANDLE)
    print_window = api(user, "PrintWindow", w.BOOL, w.HWND, w.HDC, w.UINT)
    class BITMAPINFOHEADER(c.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                    ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                    ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]
    class BITMAPINFO(c.Structure):
        _fields_ = [("header", BITMAPINFOHEADER), ("colors", w.DWORD * 3)]
    create_bitmap = api(gdi, "CreateDIBSection", w.HBITMAP, w.HDC, c.POINTER(BITMAPINFO), w.UINT,
                        c.POINTER(c.c_void_p), w.HANDLE, w.DWORD)
    expected = request["window"]; hwnd = expected["hwnd"]
    def window_state():
        if not is_window(hwnd) or not is_visible(hwnd) or is_iconic(hwnd):
            raise RuntimeError("Window is missing, invisible or minimized")
        pid, rectangle = w.DWORD(), w.RECT()
        get_pid(hwnd, c.byref(pid))
        if pid.value != expected["pid"] or not get_rect(hwnd, c.byref(rectangle)):
            raise RuntimeError("Window process or rectangle changed")
        handle = open_process(0x1000, False, pid.value)
        if not handle:
            raise RuntimeError("Process identity unavailable")
        try:
            times = [w.FILETIME() for _ in range(4)]
            if not get_times(handle, *(c.byref(value) for value in times)):
                raise RuntimeError("Process start time unavailable")
            started = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            exe, size = c.create_unicode_buffer(32768), w.DWORD(32768)
            if not get_exe(handle, 0, exe, c.byref(size)):
                raise RuntimeError("Process executable unavailable")
            title = c.create_unicode_buffer(4096); get_text(hwnd, title, len(title))
            if (started != expected["started_at"] or title.value != expected["title"]
                    or os.path.normcase(exe.value) != os.path.normcase(expected["exe"])):
                raise RuntimeError("Window belongs to another process instance or document")
            return {**expected, "rect": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom]}
        finally:
            close_handle(handle)
    previous_dpi = set_dpi(c.c_void_p(-4))
    if not previous_dpi:
        raise RuntimeError("Physical-pixel DPI context unavailable")
    dc = bitmap = old_bitmap = None
    try:
        before = window_state(); bounds = before["rect"]; viewer = request["viewer_rect"]
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        if (width <= 0 or height <= 0 or width * height > MAX_PIXELS
                or len(viewer) != 4 or not bounds[0] <= viewer[0] < viewer[2] <= bounds[2]
                or not bounds[1] <= viewer[1] < viewer[3] <= bounds[3]):
            raise RuntimeError("Viewer is outside the native window")
        flags = request["print_window_flags"]
        if type(flags) is not int or flags not in (0, 2):
            raise RuntimeError("Unsupported PrintWindow flags")
        dc = create_dc(None)
        if not dc:
            raise RuntimeError("Memory DC unavailable")
        info = BITMAPINFO(); info.header.biSize = c.sizeof(BITMAPINFOHEADER)
        info.header.biWidth, info.header.biHeight = width, -height
        info.header.biPlanes, info.header.biBitCount = 1, 32
        bits = c.c_void_p()
        bitmap = create_bitmap(dc, c.byref(info), 0, c.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise RuntimeError("Memory bitmap unavailable")
        old_bitmap = select_object(dc, bitmap)
        if not old_bitmap or old_bitmap == c.c_void_p(-1).value:
            old_bitmap = None
            raise RuntimeError("Memory bitmap selection failed")
        c.memset(bits, 0, width * height * 4)
        if not print_window(hwnd, dc, flags):
            raise RuntimeError("PrintWindow did not paint the requested window")
        after = window_state()
        if before != after:
            raise RuntimeError("Window geometry or identity changed during capture")
        frame = Image.frombytes("RGB", (width, height), c.string_at(bits, width * height * 4), "raw", "BGRX")
        crop = frame.crop((viewer[0] - bounds[0], viewer[1] - bounds[1], viewer[2] - bounds[0], viewer[3] - bounds[1]))
        output = io.BytesIO(); crop.save(output, format="PNG")
        data = output.getvalue()
        if len(data) > MAX_PNG_BYTES:
            raise RuntimeError("Viewer capture exceeds its byte limit")
        return {"window_before": before, "window_after": after, "viewer_rect": viewer,
                "print_window_flags": flags, "print_window_success": True, "geometry_verified": True,
                "png_base64": base64.b64encode(data).decode("ascii")}
    finally:
        if old_bitmap and dc:
            select_object(dc, old_bitmap)
        if bitmap:
            delete_object(bitmap)
        if dc:
            delete_dc(dc)
        set_dpi(previous_dpi)


def _worker_main():
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ValueError("request exceeds bound")
        result = _native_capture(json.loads(raw.decode("utf-8")))
        response = {"ok": True, "result": result}
    except Exception as error:
        response = {"ok": False, "error": {"code": "PRINT_WINDOW_FAILED", "message": str(error)}}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__" and sys.argv[1:] == ["--capture-worker"]:
    _worker_main()
