"""Narrow opt-in Capture One AX access through Apple's system frameworks.

No injected code, permission changes or database writes. Physical gestures and
the qualified gradient shortcut additionally require explicit mouse opt-in.
The caller binds version, RAW and layer with the official native interface;
AX process/window checks are additional guards and are not an atomic lock.
"""
from __future__ import annotations

import ctypes as C
import json
import math
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import time

from .contracts import Refused
from .macos_skin import FIELDS


_IDS = {
    "smoothness": "SmoothnessSkin", "hue": "HueSkinAmount",
    "saturation": "SaturationSkinAmount", "lightness": "LightnessSkinAmount",
    "uniformity_hue": "HueSkinUniform", "uniformity_saturation": "SaturationSkinUniform",
    "uniformity_lightness": "LightnessSkinUniform",
}


class _Element:
    def __init__(self, pointer):
        self.pointer = pointer


class _Pair(C.Structure):
    _fields_ = [("a", C.c_double), ("b", C.c_double)]


class _Rect(C.Structure):
    _fields_ = [("origin", _Pair), ("size", _Pair)]


def _fail(code, message, dispatched=False):
    error = Refused(code, message)
    error.dispatched = dispatched
    raise error


def _enabled(state_dir):
    path = Path(state_dir) / "macos-ui.json"
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 65536):
            raise ValueError("UI configuration must be a current-user regular file without shared write access")
        config = json.loads(path.read_text())
        if not isinstance(config, dict) or config.get("enabled") is not True:
            raise ValueError("Direct AX access is not explicitly enabled")
    except (OSError, ValueError) as exc:
        _fail("macos_ui_disabled", "Enable direct AX explicitly in local macos-ui.json first: " + str(exc))
    return config


class _AX:
    def __init__(self, pid, executable, document_uri, before_dispatch=None):
        self.dispatched = False
        self.before_dispatch = before_dispatch
        self.deadline = time.monotonic() + 20
        self.owned = []
        self.strings = {}
        self.pid, self.executable, self.document_uri = pid, executable, document_uri
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ax = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self.proc = C.CDLL("/usr/lib/libproc.dylib")
        def bind(library, name, result, *args):
            fn = getattr(library, name); fn.restype = result; fn.argtypes = list(args)
        p, n = C.c_void_p, C.c_long
        bind(self.proc, "proc_pidpath", C.c_int, C.c_int, p, C.c_uint32)
        bind(self.cf, "CFRelease", None, p)
        bind(self.cf, "CFGetTypeID", C.c_ulong, p)
        bind(self.cf, "CFHash", C.c_ulong, p)
        bind(self.cf, "CFEqual", C.c_ubyte, p, p)
        for name in ("CFString", "CFNumber", "CFBoolean", "CFArray", "CFURL"):
            bind(self.cf, name + "GetTypeID", C.c_ulong)
        bind(self.cf, "CFStringCreateWithCString", p, p, C.c_char_p, C.c_uint32)
        bind(self.cf, "CFStringGetLength", n, p)
        bind(self.cf, "CFStringGetMaximumSizeForEncoding", n, n, C.c_uint32)
        bind(self.cf, "CFStringGetCString", C.c_ubyte, p, p, n, C.c_uint32)
        bind(self.cf, "CFNumberGetValue", C.c_ubyte, p, C.c_int, p)
        bind(self.cf, "CFNumberCreate", p, p, C.c_int, p)
        bind(self.cf, "CFBooleanGetValue", C.c_ubyte, p)
        bind(self.cf, "CFArrayGetCount", n, p)
        bind(self.cf, "CFArrayGetValueAtIndex", p, p, n)
        bind(self.cf, "CFArrayCreate", p, p, C.POINTER(p), n, p)
        bind(self.cf, "CFURLGetString", p, p)
        bind(self.ax, "AXUIElementGetTypeID", C.c_ulong)
        bind(self.ax, "AXValueGetTypeID", C.c_ulong)
        bind(self.ax, "AXValueGetType", C.c_uint32, p)
        bind(self.ax, "AXValueGetValue", C.c_ubyte, p, C.c_uint32, p)
        bind(self.ax, "AXIsProcessTrusted", C.c_ubyte)
        bind(self.ax, "AXUIElementCreateApplication", p, C.c_int)
        bind(self.ax, "AXUIElementSetMessagingTimeout", C.c_int, p, C.c_float)
        bind(self.ax, "AXUIElementGetPid", C.c_int, p, C.POINTER(C.c_int))
        bind(self.ax, "AXUIElementCopyAttributeValue", C.c_int, p, p, C.POINTER(p))
        bind(self.ax, "AXUIElementIsAttributeSettable", C.c_int, p, p, C.POINTER(C.c_ubyte))
        bind(self.ax, "AXUIElementSetAttributeValue", C.c_int, p, p, p)
        bind(self.ax, "AXUIElementPerformAction", C.c_int, p, p)
        self.types = {name: getattr(self.cf, name + "GetTypeID")()
                      for name in ("CFString", "CFNumber", "CFBoolean", "CFArray", "CFURL")}
        self.types["AXUIElement"] = self.ax.AXUIElementGetTypeID()
        self.types["AXValue"] = self.ax.AXValueGetTypeID()
        self.app = None

    def own(self, pointer):
        if not pointer:
            self.fail("ax_allocation_failed", "System AX allocation failed")
        self.owned.append(pointer)
        return pointer

    def close(self):
        for pointer in reversed(self.owned):
            self.cf.CFRelease(pointer)
        self.owned.clear()

    def fail(self, code, message):
        _fail(code, message, self.dispatched)

    def before_write(self):
        # This trusted in-process callback rechecks the native photo/layer after
        # AX lookup. It cannot make the cross-interface operation atomic.
        if not callable(self.before_dispatch):
            self.fail("ax_native_guard_required", "AX writes require a final native target check")
        try:
            self.before_dispatch()
        except Exception as error:
            error.dispatched = self.dispatched
            raise
        self.guard()

    def check_time(self):
        if time.monotonic() > self.deadline:
            self.fail("ax_timeout", "Bounded AX operation timed out; do not replay a dispatched write")

    def string(self, value):
        if value not in self.strings:
            self.strings[value] = self.own(self.cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100))
        return self.strings[value]

    def decode(self, pointer):
        kind = self.cf.CFGetTypeID(pointer)
        if kind == self.types["AXUIElement"]:
            return _Element(pointer)
        if kind == self.types["CFString"]:
            count = self.cf.CFStringGetMaximumSizeForEncoding(self.cf.CFStringGetLength(pointer), 0x08000100) + 1
            if count > 65536:
                self.fail("ax_value_too_large", "AX text exceeds the bounded inspection size")
            buffer = C.create_string_buffer(count)
            if not self.cf.CFStringGetCString(pointer, buffer, count, 0x08000100):
                self.fail("ax_value_invalid", "AX text could not be decoded")
            return buffer.value.decode("utf-8")
        if kind == self.types["CFBoolean"]:
            return bool(self.cf.CFBooleanGetValue(pointer))
        if kind == self.types["CFNumber"]:
            number = C.c_double()
            if not self.cf.CFNumberGetValue(pointer, 13, C.byref(number)) or not math.isfinite(number.value):
                self.fail("ax_value_invalid", "AX numeric value is unavailable or non-finite")
            return number.value
        if kind == self.types["CFArray"]:
            count = self.cf.CFArrayGetCount(pointer)
            if not 0 <= count <= 10000:
                self.fail("ax_tree_too_large", "AX child count exceeds the bounded inspection scope")
            # The copied array stays owned until close(), retaining its children.
            return [self.decode(self.cf.CFArrayGetValueAtIndex(pointer, i)) for i in range(count)]
        if kind == self.types["CFURL"]:
            return self.decode(self.cf.CFURLGetString(pointer))
        if kind == self.types["AXValue"]:
            value_type = self.ax.AXValueGetType(pointer)
            if value_type in (1, 2, 3):
                value = _Rect() if value_type == 3 else _Pair()
                if not self.ax.AXValueGetValue(pointer, value_type, C.byref(value)):
                    self.fail("ax_value_invalid", "AX geometry could not be decoded")
                components = (value.origin.a, value.origin.b, value.size.a, value.size.b) if value_type == 3 else (value.a, value.b)
                if not all(math.isfinite(number) for number in components):
                    self.fail("ax_value_invalid", "AX geometry is non-finite")
                if value_type == 1:
                    return {"x": value.a, "y": value.b}
                if value_type == 2:
                    return {"width": value.a, "height": value.b}
                return {"x": value.origin.a, "y": value.origin.b,
                        "width": value.size.a, "height": value.size.b}
        return None

    def attr(self, element, name, required=False):
        self.check_time()
        value = C.c_void_p()
        code = self.ax.AXUIElementCopyAttributeValue(element.pointer, self.string(name), C.byref(value))
        if code != 0:
            if required:
                self.fail("ax_read_failed", "Required AX attribute unavailable: " + name + " (" + str(code) + ")")
            return None
        return self.decode(self.own(value.value))

    def children(self, element):
        values = self.attr(element, "AXChildren")
        if values is None:
            return []
        if not isinstance(values, list) or any(not isinstance(v, _Element) for v in values):
            self.fail("ax_tree_invalid", "AX children are not elements")
        return values

    def descendants(self, root, *, limit=6000, max_depth=24, prune=None):
        result, queue, seen = [], [(root, 0)], {}
        while queue:
            self.check_time()
            node, depth = queue.pop()
            key = self.cf.CFHash(node.pointer)
            bucket = seen.setdefault(key, [])
            if any(self.cf.CFEqual(node.pointer, p) for p in bucket):
                continue
            bucket.append(node.pointer)
            if len(result) >= limit or depth > max_depth:
                self.fail("ax_tree_too_large", "AX traversal exceeds the bounded Capture One window scope")
            result.append(node)
            if prune is not None and prune(node):
                continue
            queue.extend((child, depth + 1) for child in self.children(node))
        return result

    def unique(self, values, description):
        if len(values) != 1:
            self.fail("ax_target_ambiguous", "Expected exactly one " + description)
        return values[0]

    def guard(self):
        self.check_time()
        buffer = C.create_string_buffer(4096)
        if self.proc.proc_pidpath(self.pid, buffer, len(buffer)) <= 0:
            self.fail("ax_process_changed", "Capture One process no longer exists")
        if Path(os.fsdecode(buffer.value)).resolve() != self.executable:
            self.fail("ax_process_changed", "PID does not belong to the bound Capture One executable")
        if self.app is None:
            if not self.ax.AXIsProcessTrusted():
                self.fail("ax_permission_required", "macOS Accessibility access has not been granted")
            self.app = _Element(self.own(self.ax.AXUIElementCreateApplication(self.pid)))
            if self.ax.AXUIElementSetMessagingTimeout(self.app.pointer, 1.0) != 0:
                self.fail("ax_timeout_unavailable", "Cannot bound AX messaging time")
        window = self.attr(self.app, "AXMainWindow", True)
        if not isinstance(window, _Element):
            self.fail("ax_window_unavailable", "Capture One has no unambiguous main window")
        owner = C.c_int()
        if self.ax.AXUIElementGetPid(window.pointer, C.byref(owner)) != 0 or owner.value != self.pid:
            self.fail("ax_process_changed", "AX window belongs to another process")
        if self.attr(window, "AXDocument", True) != self.document_uri:
            self.fail("ax_document_changed", "Main window document URL differs from the bound Session")
        return window

    def tool(self, identifier):
        window = self.guard()
        # Native AX exposes both fixed and scrolling tool areas, including
        # an empty fixed area that the rendered accessibility tree omits.
        # A different layout must be qualified explicitly, never replaced by a
        # whole-window search that can reach browsers, viewers and menus.
        areas = [node for node in self.children(window) if self.attr(node, "AXIdentifier") == "toolsArea"]
        if len(areas) != 1:
            self.fail("ax_layout_unsupported", "Expected one direct toolsArea in the bound main window")
        scrolls = [node for node in self.children(areas[0]) if self.attr(node, "AXRole") == "AXScrollArea"]
        if not 1 <= len(scrolls) <= 2:
            self.fail("ax_layout_unsupported", "Expected one or two direct tool scroll areas")
        identifiers = {}
        def stop_at_tool(node):
            name = self.attr(node, "AXIdentifier")
            identifiers[node.pointer] = name
            # Check every sibling tool header for uniqueness, but never walk
            # another tool's controls (or the target's controls at this stage).
            return isinstance(name, str) and name.startswith("Tools_")
        nodes = [node for scroll in scrolls
                 for node in self.descendants(scroll, limit=256, max_depth=6, prune=stop_at_tool)]
        return self.unique([node for node in nodes if identifiers[node.pointer] == identifier], identifier)

    def skin(self):
        tool = self.tool("Tools_ColorEditor")
        identifiers = {}
        # These sibling identifiers are defined in the installed Color Editor
        # nib. They contain Basic/Advanced controls, never Skin Tone controls.
        other = {"PreslicedTab", "CustomTab", "HueBasic", "SaturationBasic", "LightnessBasic",
                 "SmoothnessAdvanced", "HueAdvanced", "SaturationAdvanced", "LightnessAdvanced"}
        def stop_at_control(node):
            name = self.attr(node, "AXIdentifier")
            identifiers[node.pointer] = name
            return name in other or name in _IDS.values()
        nodes = self.descendants(tool, limit=384, max_depth=14, prune=stop_at_control)
        controls = {}
        for field, identifier in _IDS.items():
            container = self.unique([node for node in nodes if identifiers[node.pointer] == identifier], identifier)
            controls[field] = self.unique([node for node in self.descendants(container, limit=24, max_depth=4)
                                           if self.attr(node, "AXRole") == "AXSlider"], identifier + " slider")
        return controls

    def number(self, element):
        value = self.attr(element, "AXValue", True)
        if type(value) not in (int, float) or not math.isfinite(value):
            self.fail("ax_value_invalid", "Skin Tone slider has no finite numeric AX value")
        return value

    def layer_rows(self, *, outline=None, tool=None):
        if outline is None:
            tool = self.tool("Tools_LocalAdjustments")
            outline = self.unique([node for node in self.descendants(tool, limit=384, max_depth=12)
                                   if self.attr(node, "AXRole") == "AXOutline"], "Layers outline")
        elements = self.attr(outline, "AXRows", True)
        if (not isinstance(elements, list) or not 0 < len(elements) <= 256
                or any(not isinstance(row, _Element) for row in elements)):
            self.fail("ax_layout_unsupported", "Layers outline has no bounded AXRows list")
        rows = []
        for index, row in enumerate(elements):
            result = {name: self.attr(row, name, name in {"AXDisclosureLevel", "AXSelected"})
                      for name in ("AXDisclosureLevel", "AXDisclosing", "AXSelected", "AXDescription",
                                   "AXValue", "AXPosition", "AXSize")}
            level = result["AXDisclosureLevel"]
            if type(level) not in (int, float) or not 0 <= level <= 24 or int(level) != level or type(result["AXSelected"]) is not bool:
                self.fail("ax_layout_unsupported", "Layers row hierarchy or selection is unavailable")
            result["AXDisclosureLevel"] = int(level)
            # Do not return opaque AX elements or recursively expose unrelated UI.
            if result["AXValue"] is not None and type(result["AXValue"]) not in (str, int, float, bool):
                self.fail("ax_layout_unsupported", "Layers row AXValue has an unsupported representation")
            row_children = self.children(row)
            if len(row_children) > 32:
                self.fail("ax_layout_unsupported", "Layers row exceeds the qualified direct child count")
            cell = self.unique([node for node in row_children if self.attr(node, "AXRole") == "AXCell"],
                               "direct Layers row cell")
            cell_children = self.children(cell)
            if len(cell_children) > 32:
                self.fail("ax_layout_unsupported", "Layers cell exceeds the qualified direct child count")
            texts = []
            for node in cell_children:
                if self.attr(node, "AXRole") == "AXStaticText":
                    value = self.attr(node, "AXValue", True)
                    if not isinstance(value, str) or len(value) > 1024 or len(texts) >= 8:
                        self.fail("ax_layout_unsupported", "Layers cell has unsupported static text")
                    texts.append(value)
            # Native rows have no label value of their own. Keep these separate
            # and ordered, including opacity text; the caller matches the full
            # native layer inventory. Image descriptions are deliberately not
            # read: their selected/unselected wording is transient UI state.
            result["cell_texts"] = texts
            rows.append({"index": index, **result})
        selected = self.attr(outline, "AXSelectedRows", True)
        if not isinstance(selected, list) or any(not isinstance(row, _Element) for row in selected):
            self.fail("ax_layout_unsupported", "Layers outline selected rows are unavailable")
        indices = []
        for selected_row in selected:
            matches = [i for i, row in enumerate(elements) if self.cf.CFEqual(row.pointer, selected_row.pointer)]
            indices.append(self.unique(matches, "selected row in the current outline"))
        if sorted(indices) != [i for i, row in enumerate(rows) if row["AXSelected"]]:
            self.fail("ax_state_changed", "Layers outline and row selection disagree")
        self.guard()
        return {"rows": rows, "selected_rows": sorted(indices)}, outline, elements, tool

    def select_row(self, row_index, expected_rows):
        view, outline, elements, _ = self.layer_rows()
        if view["rows"] != expected_rows or not 0 <= row_index < len(elements):
            self.fail("ax_stale_rows", "Complete Layers rows changed before selection")
        attribute, settable = self.string("AXSelectedRows"), C.c_ubyte()
        if self.ax.AXUIElementIsAttributeSettable(outline.pointer, attribute, C.byref(settable)) != 0 or not settable.value:
            self.fail("ax_control_readonly", "Layers outline cannot directly select AX rows")
        self.before_write()
        fresh, outline, elements, _ = self.layer_rows(outline=outline)
        if fresh["rows"] != expected_rows:
            self.fail("ax_stale_rows", "Complete Layers rows changed before dispatch")
        if self.ax.AXUIElementIsAttributeSettable(outline.pointer, attribute, C.byref(settable)) != 0 or not settable.value:
            self.fail("ax_control_readonly", "Layers outline became read-only before dispatch")
        values = (C.c_void_p * 1)(elements[row_index].pointer)
        # Source AXRows remains retained until close(), so null array callbacks
        # are safe for this one synchronous setter and do not leak child retains.
        array = self.own(self.cf.CFArrayCreate(None, values, 1, None))
        self.dispatched = True
        code = self.ax.AXUIElementSetAttributeValue(outline.pointer, attribute, array)
        if code != 0:
            self.fail("ax_write_failed", "AX row selection returned " + str(code) + "; do not replay")
        after, _, _, _ = self.layer_rows()
        # Compare stable row/cell data and permit only selection to change.
        # cell_texts excludes the transient AXImage selection descriptions.
        wanted = [{**row, "AXSelected": i == row_index} for i, row in enumerate(expected_rows)]
        if after["rows"] != wanted or after["selected_rows"] != [row_index]:
            self.fail("ax_readback_mismatch", "Layers row selection changed an unexpected row property")
        return after

    def delete_submask(self, expected_rows):
        view, outline, _, tool = self.layer_rows()
        if view["rows"] != expected_rows or len(view["selected_rows"]) != 1:
            self.fail("ax_stale_rows", "Deletion needs the unchanged complete row snapshot and exactly one selected child")
        selected = view["selected_rows"][0]
        if view["rows"][selected]["AXDisclosureLevel"] <= 0:
            self.fail("ax_parent_delete_refused", "Only an explicitly selected submask row may be deleted")
        button = self.unique([node for node in self.descendants(tool, limit=384, max_depth=12)
                              if self.attr(node, "AXIdentifier") == "Delete Layer"
                              and self.attr(node, "AXRole") == "AXButton"], "Delete Layer button")
        if self.attr(button, "AXEnabled", True) is not True:
            self.fail("ax_control_disabled", "Delete Layer button is disabled")
        self.before_write()
        fresh, _, _, _ = self.layer_rows(outline=outline)
        if fresh != view or self.attr(button, "AXEnabled", True) is not True:
            self.fail("ax_stale_rows", "Layers rows or delete availability changed before dispatch")
        self.dispatched = True
        code = self.ax.AXUIElementPerformAction(button.pointer, self.string("AXPress"))
        if code != 0:
            self.fail("ax_write_failed", "Delete submask AXPress returned " + str(code) + "; do not replay")
        self.guard()
        return {"selected_row_before": selected, "native_result_verified": False}

    def move_layer(self, row_index, insertion_row_index, expected_rows):
        """One foreground drag between qualified parent-row insertion boundaries.

        The caller proves the complete AX/native UUID mapping and later verifies
        the saved layer permutation. A foreground flag alone does not prove that
        the window receives physical input: both endpoints are hit-tested.
        """
        view, outline, elements, _ = self.layer_rows()
        rows = view["rows"]
        if (rows != expected_rows or view["selected_rows"] != [row_index]
                or len(rows) < 3 or any(row["AXDisclosureLevel"] != 0 for row in rows)
                or not 0 <= row_index < len(rows) - 1
                or not 0 <= insertion_row_index < len(rows)
                or insertion_row_index in (row_index, row_index + 1)):
            self.fail("ax_stale_rows", "Move requires unchanged collapsed parent rows and a different insertion boundary")
        image_labels = {"Image Layer", "图像图层"}
        if [i for i, row in enumerate(rows) if image_labels.intersection(row["cell_texts"])] != [len(rows) - 1]:
            self.fail("ax_layout_unsupported", "The last row must be the uniquely identified Image Layer")

        def geometry(row):
            position, size = row.get("AXPosition"), row.get("AXSize")
            if not isinstance(position, dict) or not isinstance(size, dict):
                self.fail("ax_layout_unsupported", "Layer row geometry is unavailable")
            values = [position.get("x"), position.get("y"), size.get("width"), size.get("height")]
            if (any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
                    or values[2] < 160 or values[3] < 20):
                self.fail("ax_layout_unsupported", "Layer row geometry is outside the qualified layout")
            return values

        rectangles = [geometry(row) for row in rows]
        if any(right[1] < left[1] + left[3] - 1 for left, right in zip(rectangles, rectangles[1:])):
            self.fail("ax_layout_unsupported", "Layer rows overlap or are out of screen order")
        x, y, width, _ = rectangles[row_index]
        target_x, target_y, target_width, _ = rectangles[insertion_row_index]
        start = (x + min(130, width / 2), y + 16)
        stop = (start[0], target_y + (1 if insertion_row_index == 0 else -1))
        if not target_x < stop[0] < target_x + target_width:
            self.fail("ax_layout_unsupported", "Layer insertion boundary is not aligned with the source row")

        def unchanged():
            if self.layer_rows(outline=outline)[0] != view:
                self.fail("ax_stale_rows", "Layers rows changed during foreground preparation")
        result = self._mouse_drag(start, stop, unchanged,
                                  (elements[row_index], outline),
                                  (elements[max(0, insertion_row_index - 1)], outline))
        return {**result, "row_index": row_index, "insertion_row_index": insertion_row_index}

    def gradient_view(self):
        """Read the single fitted photo rectangle in global screen points."""
        window = self.guard()
        photo = self.unique([node for node in self.children(window)
                             if self.attr(node, "AXIdentifier") == "VariantCropLayerRect"],
                            "direct single-photo rectangle")
        def rectangle(node):
            position, size = self.attr(node, "AXPosition", True), self.attr(node, "AXSize", True)
            if not isinstance(position, dict) or not isinstance(size, dict):
                self.fail("ax_layout_unsupported", "The native viewer rectangle is unavailable")
            result = {"x": position.get("x"), "y": position.get("y"),
                      "width": size.get("width"), "height": size.get("height")}
            if (any(type(value) not in (int, float) or not math.isfinite(value) for value in result.values())
                    or result["width"] <= 0 or result["height"] <= 0):
                self.fail("ax_layout_unsupported", "The native viewer rectangle is invalid")
            return result
        photo_rect, window_rect = rectangle(photo), rectangle(window)
        if not (window_rect["x"] <= photo_rect["x"]
                and window_rect["y"] <= photo_rect["y"]
                and photo_rect["x"] + photo_rect["width"] <= window_rect["x"] + window_rect["width"]
                and photo_rect["y"] + photo_rect["height"] <= window_rect["y"] + window_rect["height"]):
            self.fail("ax_layout_unsupported", "The complete photo is not inside the bound window")
        # Viewer toolbar only: do not descend into photo, tools or browser rows.
        def stop(node):
            return (self.attr(node, "AXIdentifier") in {"toolsArea", "VariantCropLayerRect"}
                    or self.attr(node, "AXRole") in {"AXOutline", "AXTable", "AXList"})
        nodes = self.descendants(window, limit=256, max_depth=7, prune=stop)
        zoom = self.unique([node for node in nodes if self.attr(node, "AXIdentifier") == "zoomSlider"
                            and self.attr(node, "AXRole") == "AXSlider"], "viewer zoom slider")
        indicator = self.unique([node for node in self.children(zoom)
                                 if self.attr(node, "AXRole") == "AXValueIndicator"],
                                "direct viewer zoom value indicator")
        value = self.attr(indicator, "AXValue", True)
        if type(value) not in (int, float) or value != 0:
            self.fail("gradient_fit_required", "Gradient gestures require the existing Fit viewer zoom")
        self.guard()
        return {"photo_rect": photo_rect, "window_rect": window_rect, "zoom_value": value}, photo, window

    def _linear_shortcut(self):
        """Qualify only the installed, explicitly observed unmodified L binding."""
        carbon = C.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource.restype = C.c_void_p
        carbon.TISGetInputSourceProperty.restype = C.c_void_p
        carbon.TISGetInputSourceProperty.argtypes = [C.c_void_p, C.c_void_p]
        source = self.own(carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource())
        key = C.c_void_p.in_dll(carbon, "kTISPropertyInputSourceID")
        pointer = carbon.TISGetInputSourceProperty(source, key)
        layout = self.decode(pointer) if pointer else None
        if layout not in {"com.apple.keylayout.US", "com.apple.keylayout.ABC"}:
            self.fail("gradient_shortcut_unverified", "Gradient key input requires the qualified US or ABC underlying keyboard layout")
        self.cf.CFPreferencesCopyAppValue.restype = C.c_void_p
        self.cf.CFPreferencesCopyAppValue.argtypes = [C.c_void_p, C.c_void_p]
        info = plistlib.loads((self.executable.parents[1] / "Info.plist").read_bytes())
        domain = info.get("CFBundleIdentifier")
        if not isinstance(domain, str) or not domain:
            self.fail("gradient_shortcut_unverified", "The bound application preference domain is unavailable")
        pointer = self.cf.CFPreferencesCopyAppValue(self.string("KeyboardShortcutSet"), self.string(domain))
        name = self.decode(self.own(pointer)) if pointer else None
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or Path(name).name != name or "/" in name or "\\" in name):
            self.fail("gradient_shortcut_unverified", "An explicit safe user keyboard shortcut set is required")
        path = Path.home() / "Library/Application Support/Capture One/KeyboardShortcuts" / (name + ".plist")
        bundled = False
        if not path.exists():
            path = self.executable.parents[1] / "Resources/KeyboardShortcuts" / (name + ".plist")
            bundled = True
        try:
            file_info = path.lstat()
            if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid not in ({0, os.getuid()} if bundled else {os.getuid()})
                    or file_info.st_mode & 0o022 or not 0 < file_info.st_size <= 262144):
                raise ValueError("Unsafe shortcut file")
            raw = path.read_bytes()
            config = plistlib.loads(raw)
            keyboard, advanced, speed = (config[key] for key in
                                          ("KeyboardShortcuts", "AdvancedShortcuts", "GodModeShortcuts"))
            def contains_l(value):
                if isinstance(value, dict):
                    return any(contains_l(item) for item in value.values())
                if isinstance(value, list):
                    return any(contains_l(item) for item in value)
                return value == "l"
            if (not all(isinstance(item, dict) for item in (keyboard, advanced, speed))
                    or keyboard.get("GradientMask") != "l"
                    or sum(value == "l" for value in keyboard.values()) != 1
                    or contains_l(advanced) or contains_l(speed)):
                raise ValueError("L is not an unambiguous linear-gradient shortcut")
        except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
            self.fail("gradient_shortcut_unverified", "The active user shortcut set must bind only Linear Gradient to unmodified L")
        return str(path), raw, layout

    def draw_gradient(self, expected_view, start, stop):
        view, photo, window = self.gradient_view()
        if view != expected_view:
            self.fail("ax_stale_view", "The fitted viewer changed since the observation")
        shortcut = self._linear_shortcut()
        rect = view["photo_rect"]
        points = [(rect["x"] + point[0] * rect["width"],
                   rect["y"] + point[1] * rect["height"]) for point in (start, stop)]
        if math.dist(*points) < 4:
            self.fail("invalid_ax_request", "A gradient gesture must span at least four screen points")
        def unchanged():
            current, current_photo, current_window = self.gradient_view()
            if (current != view or not self.cf.CFEqual(photo.pointer, current_photo.pointer)
                    or not self.cf.CFEqual(window.pointer, current_window.pointer)):
                self.fail("ax_stale_view", "The fitted photo rectangle changed during gradient preparation")
        def prepare(quartz):
            if self._linear_shortcut() != shortcut:
                self.fail("gradient_shortcut_unverified", "The active shortcut configuration changed")
            focused = self.attr(self.app, "AXFocusedUIElement", True)
            if not isinstance(focused, _Element):
                self.fail("gradient_text_focus", "Cannot verify the focused Capture One control")
            owner = C.c_int()
            role = self.attr(focused, "AXRole", True)
            if (self.ax.AXUIElementGetPid(focused.pointer, C.byref(owner)) != 0 or owner.value != self.pid
                    or role in {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXWebArea"}
                    or self.attr(focused, "AXEditable") is True):
                self.fail("gradient_text_focus", "Leave text editing before selecting the gradient tool")
            quartz.CGEventCreateKeyboardEvent.restype = C.c_void_p
            quartz.CGEventCreateKeyboardEvent.argtypes = [C.c_void_p, C.c_uint16, C.c_bool]
            quartz.CGEventSourceKeyState.restype = C.c_bool
            quartz.CGEventSourceKeyState.argtypes = [C.c_int32, C.c_uint16]
            if any(quartz.CGEventSourceKeyState(state, 37) for state in (0, 1)):
                self.fail("mouse_input_busy", "Release the gradient shortcut key before creating a mask")
            down = quartz.CGEventCreateKeyboardEvent(None, 37, True)
            up = quartz.CGEventCreateKeyboardEvent(None, 37, False)
            if not down or not up:
                if down:
                    self.cf.CFRelease(down)
                if up:
                    self.cf.CFRelease(up)
                self.fail("mouse_event_failed", "Could not allocate both gradient shortcut events")
            try:
                self.dispatched = True
                quartz.CGEventPost(0, down)
                time.sleep(.05)
            finally:
                try:
                    quartz.CGEventPost(0, up)
                finally:
                    self.cf.CFRelease(down)
                    self.cf.CFRelease(up)
            time.sleep(.1)
        result = self._mouse_drag(*points, unchanged, (photo, window), (photo, window), prepare=prepare)
        return {**result, "requested_viewer_gesture": {
                    "kind": "linear", "start": list(start), "stop": list(stop), "midpoint": .5},
                "actual_screen_points": [list(point) for point in points],
                "gradient_shortcut_verified": True, "raw_coordinates_verified": False}

    def _mouse_drag(self, start, stop, unchanged, start_ancestors, stop_ancestors, *, prepare=None):
        """One opt-in gesture, bound to exact AX ancestry at both endpoints."""
        quartz = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        def bind(library, name, result, *args):
            fn = getattr(library, name); fn.restype = result; fn.argtypes = list(args)
        bind(quartz, "CGPreflightPostEventAccess", C.c_bool)
        bind(quartz, "CGEventSourceButtonState", C.c_bool, C.c_int32, C.c_uint32)
        bind(quartz, "CGEventSourceFlagsState", C.c_uint64, C.c_int32)
        bind(quartz, "CGEventCreateMouseEvent", C.c_void_p, C.c_void_p, C.c_uint32, _Pair, C.c_uint32)
        bind(quartz, "CGEventPost", None, C.c_uint32, C.c_void_p)
        bind(self.ax, "AXUIElementCreateSystemWide", C.c_void_p)
        bind(self.ax, "AXUIElementCopyElementAtPosition", C.c_int, C.c_void_p,
             C.c_float, C.c_float, C.POINTER(C.c_void_p))

        def input_idle():
            if not quartz.CGPreflightPostEventAccess():
                self.fail("mouse_permission_required", "macOS has not granted event posting access")
            if any(quartz.CGEventSourceButtonState(state, button) for state in (0, 1) for button in range(32)):
                self.fail("mouse_input_busy", "Release all mouse buttons before the native gesture")
            if any(quartz.CGEventSourceFlagsState(state) & 0x00fe0000 for state in (0, 1)):
                self.fail("mouse_input_busy", "Release keyboard modifiers before the native gesture")

        input_idle()
        self.guard()
        # Fixed public AppKit call; the PID was bound to the executable by guard.
        # No other application is hidden, minimized or otherwise controlled.
        script = ("ObjC.import('AppKit'); var app = $.NSRunningApplication."
                  "runningApplicationWithProcessIdentifier(" + str(self.pid) + "); "
                  "if (!app.activateWithOptions(3)) throw Error('Activation failed');")
        try:
            subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
                           check=True, capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            self.fail("ax_activation_failed", "Could not normally activate the bound Capture One process")
        unchanged()
        self.before_write()
        window = self.guard()
        system = self.own(self.ax.AXUIElementCreateSystemWide())

        def hit_test(point, expected_ancestors):
            pointer = C.c_void_p()
            if self.ax.AXUIElementCopyElementAtPosition(system, *point, C.byref(pointer)) != 0 or not pointer.value:
                self.fail("mouse_target_unverified", "Cannot verify the physical mouse target")
            hit = _Element(self.own(pointer.value))
            pid = C.c_int()
            if self.ax.AXUIElementGetPid(hit.pointer, C.byref(pid)) != 0 or pid.value != self.pid:
                self.fail("mouse_target_occluded", "The physical input point is covered by another application")
            hit_window = hit if self.attr(hit, "AXRole") == "AXWindow" else self.attr(hit, "AXWindow", True)
            if not isinstance(hit_window, _Element) or not self.cf.CFEqual(hit_window.pointer, window.pointer):
                self.fail("mouse_target_unverified", "The physical input point belongs to another window")
            # Window identity alone does not exclude clipped rows or an overlay
            # covering the requested photo. Require the exact observed ancestry.
            node, remaining = hit, list(expected_ancestors)
            for _ in range(20):
                if self.cf.CFEqual(node.pointer, remaining[0].pointer):
                    remaining.pop(0)
                    if not remaining:
                        return
                node = self.attr(node, "AXParent")
                if not isinstance(node, _Element):
                    break
            self.fail("mouse_target_unverified", "The input point does not hit its verified visible AX target")

        input_idle()
        hit_test(start, start_ancestors)
        hit_test(stop, stop_ancestors)
        if prepare is not None:
            prepare(quartz)
            unchanged()
            self.before_write()
            input_idle()
            hit_test(start, start_ancestors)
            hit_test(stop, stop_ancestors)
        sent = []
        def event(kind, point):
            pointer = quartz.CGEventCreateMouseEvent(None, kind, _Pair(*point), 0)
            if not pointer:
                self.fail("mouse_event_failed", "Could not allocate the single drag event sequence")
            try:
                self.dispatched = True
                quartz.CGEventPost(0, pointer)
                sent.append(kind)
            finally:
                self.cf.CFRelease(pointer)

        # Allocate release before pressing; an allocation failure mid-drag
        # must not leave the global button held down.
        release = quartz.CGEventCreateMouseEvent(None, 2, _Pair(*stop), 0)
        if not release:
            self.fail("mouse_event_failed", "Could not allocate the drag release event")
        down_attempted = False
        try:
            event(5, start)
            time.sleep(.15)
            input_idle()
            hit_test(start, start_ancestors)
            hit_test(stop, stop_ancestors)
            down_attempted = True
            event(1, start)
            time.sleep(.2)
            for index in range(1, 31):
                event(6, (start[0] + (stop[0] - start[0]) * index / 30,
                          start[1] + (stop[1] - start[1]) * index / 30))
                time.sleep(.025)
        finally:
            try:
                if down_attempted:
                    quartz.CGEventPost(0, release)
                    sent.append(2)
            finally:
                self.cf.CFRelease(release)
        return {"start": list(start), "stop": list(stop), "events_posted": len(sent),
                "foreground_activated": True, "endpoint_window_verified": True,
                "physical_pointer_moved": True, "native_result_verified": False}

    def set_skin(self, field, value, expected_old):
        controls = self.skin()
        slider = controls[field]
        before = self.number(slider)
        if not math.isclose(before, expected_old, abs_tol=0.0001, rel_tol=0):
            self.fail("ax_stale_value", "Skin Tone slider changed before dispatch")
        if self.attr(slider, "AXEnabled", True) is not True:
            self.fail("ax_control_disabled", "Skin Tone slider is disabled")
        attribute, settable = self.string("AXValue"), C.c_ubyte()
        if self.ax.AXUIElementIsAttributeSettable(slider.pointer, attribute, C.byref(settable)) != 0 or not settable.value:
            self.fail("ax_control_readonly", "Skin Tone slider does not support direct AX values")
        self.before_write()
        if not math.isclose(self.number(slider), before, abs_tol=0.0001, rel_tol=0):
            self.fail("ax_stale_value", "Skin Tone slider changed before dispatch")
        if self.attr(slider, "AXEnabled", True) is not True:
            self.fail("ax_control_disabled", "Skin Tone slider became disabled")
        if self.ax.AXUIElementIsAttributeSettable(slider.pointer, attribute, C.byref(settable)) != 0 or not settable.value:
            self.fail("ax_control_readonly", "Skin Tone slider became read-only before dispatch")
        number = C.c_double(value)
        encoded = self.own(self.cf.CFNumberCreate(None, 13, C.byref(number)))
        self.dispatched = True
        code = self.ax.AXUIElementSetAttributeValue(slider.pointer, attribute, encoded)
        if code != 0:
            self.fail("ax_write_failed", "AX setter returned " + str(code) + "; do not replay")
        self.guard()
        after = self.number(self.skin()[field])
        if not math.isclose(after, value, abs_tol=0.0001, rel_tol=0):
            self.fail("ax_readback_mismatch", "AX value did not match the requested value; inspect saved native state")
        return {"field": field, "before_value": before, "value": after}

    def menu(self, path):
        self.guard()
        current = self.attr(self.app, "AXMenuBar", True)
        if not isinstance(current, _Element):
            self.fail("ax_menu_unavailable", "Capture One menu bar is unavailable")
        opened_menu = None
        try:
            for index, titles in enumerate(path):
                children = self.children(current)
                matches = [node for node in children if self.attr(node, "AXTitle") in titles
                           and self.attr(node, "AXRole") in ("AXMenuBarItem", "AXMenuItem")]
                current = self.unique(matches, "menu item in the explicit title path")
                if self.attr(current, "AXEnabled", True) is not True:
                    self.fail("ax_control_disabled", "Requested menu item is disabled")
                if index != len(path) - 1:
                    submenu = self.unique([node for node in self.children(current)
                                           if self.attr(node, "AXRole") == "AXMenu"], "submenu")
                    if index == 0:
                        # Opening refreshes AppKit's cached enabled states.
                        if self.attr(current, "AXRole") != "AXMenuBarItem":
                            self.fail("ax_menu_unavailable", "Expected a top-level menu bar item")
                        self.guard()
                        opened_menu = submenu
                        if self.ax.AXUIElementPerformAction(current.pointer, self.string("AXPress")) != 0:
                            self.fail("ax_menu_unavailable", "Could not open the exact top-level menu")
                    current = submenu
        finally:
            # C1 pauses Apple Events while tracking an open menu. Always close
            # it before the native target check, including failed lookups.
            if opened_menu is not None:
                if self.ax.AXUIElementPerformAction(opened_menu.pointer, self.string("AXCancel")) != 0:
                    self.fail("ax_menu_unavailable", "Could not close the validated menu")
        self.before_write()
        if self.attr(current, "AXEnabled", True) is not True:
            self.fail("ax_control_disabled", "Requested menu item became disabled")
        self.dispatched = True
        code = self.ax.AXUIElementPerformAction(current.pointer, self.string("AXPress"))
        if code != 0:
            self.fail("ax_write_failed", "Menu AXPress returned " + str(code) + "; do not replay")
        # An AI menu command can temporarily stop answering AX messages. The
        # caller must verify native target and saved results after dispatch;
        # this receipt acknowledges only the single successful AXPress.
        return {"menu_path": path, "native_result_verified": False}


def run_ax(state_dir, target, action, *, field=None, value=None, expected_old=None,
           menu_path=None, app_path="/Applications/Capture One.app", tool="Tools_ColorEditor",
           row_index=None, insertion_row_index=None, expected_rows=None, before_dispatch=None,
           expected_view=None, start=None, stop=None):
    """One bounded direct AX operation; all accesses require explicit local opt-in.

    menu_path is a list of exact title alternatives, e.g.
    [["Layer", "图层"], ["Add", "加"], ["Subject", "主题"]]. No fuzzy matching or
    fallback clicking is performed. The exact top-level menu is opened then
    closed to refresh AppKit validation before the final native target check.
    Writes require a trusted Python before_dispatch callback that checks the
    native photo/layer immediately after AX lookup and raises on any mismatch.
    JSON callers cannot supply this callback and therefore cannot write.
    draw_gradient uses explicit normalized Viewer coordinates, only a linear
    gesture with midpoint 0.5, and the verified current user L shortcut. It does
    not infer or promise a matching RAW-coordinate transformation.
    """
    config = _enabled(state_dir)
    if sys.platform != "darwin":
        _fail("unsupported_platform", "Direct AX is available only on macOS")
    if action not in {"read_skin", "set_skin", "menu", "inspect", "read_layers", "select_row", "delete_submask", "move_layer", "read_gradient_view", "draw_gradient"}:
        _fail("invalid_ax_request", "Unsupported bounded AX action")
    if action in {"set_skin", "menu", "select_row", "delete_submask", "move_layer", "draw_gradient"} and not callable(before_dispatch):
        _fail("ax_native_guard_required", "AX writes require a trusted final native target check")
    if action in {"move_layer", "read_gradient_view", "draw_gradient"} and config.get("mouse_enabled") is not True:
        _fail("macos_mouse_disabled", "Enable mouse_enabled explicitly in local macos-ui.json before physical input")
    if action == "draw_gradient":
        if (not isinstance(expected_view, dict)
                or any(not isinstance(point, (list, tuple)) or len(point) != 2
                       or any(type(number) not in (int, float) or not math.isfinite(number)
                              or not 0 < number < 1 for number in point) for point in (start, stop))
                or list(start) == list(stop)):
            _fail("invalid_ax_request", "Provide the observed view and two distinct normalized interior viewer points")
    if action in {"select_row", "delete_submask", "move_layer"}:
        if (not isinstance(expected_rows, list) or not 0 < len(expected_rows) <= 256
                or any(not isinstance(row, dict) for row in expected_rows)):
            _fail("invalid_ax_request", "The complete previously observed Layers rows are required")
        if action in {"select_row", "move_layer"} and (type(row_index) is not int or not 0 <= row_index < len(expected_rows)):
            _fail("invalid_ax_request", "Select one observed row index")
        if action == "move_layer" and (type(insertion_row_index) is not int
                                      or not 0 <= insertion_row_index < len(expected_rows)):
            _fail("invalid_ax_request", "Provide an observed parent row insertion boundary")
    if action == "inspect" and tool not in {"Tools_ColorEditor", "Tools_LocalAdjustments"}:
        _fail("invalid_ax_request", "Inspection is limited to the Color Editor or Local Adjustments tool")
    if action == "set_skin":
        if field not in FIELDS:
            _fail("invalid_ax_request", "Unknown Skin Tone field")
        _, _, low, high = FIELDS[field]
        if (type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value)
                or type(expected_old) not in (int, float) or not low <= expected_old <= high or not math.isfinite(expected_old)):
            _fail("invalid_ax_request", "Skin Tone value and expected old value must be finite and within the native range")
    if action == "menu":
        if (not isinstance(menu_path, list) or not 1 <= len(menu_path) <= 5
                or any(not isinstance(titles, list) or not 1 <= len(titles) <= 3
                       or any(not isinstance(title, str) or not 1 <= len(title) <= 100
                              or any(ord(c) < 32 for c in title) for title in titles) for titles in menu_path)):
            _fail("invalid_ax_request", "Provide a bounded explicit menu title path")
    try:
        pid = target["instance"]["pid"]
        document = Path(target["document"]["path"])
        app = Path(app_path)
        if type(pid) is not int or pid <= 0 or not document.is_absolute() or not app.is_absolute() or app.name != "Capture One.app":
            raise ValueError("Invalid Capture One process, application or Session path")
        if document.suffix.lower() != ".cosessiondb":
            raise ValueError("Direct AX requires the bound Session document")
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        executable_name = info["CFBundleExecutable"]
        if not isinstance(executable_name, str) or Path(executable_name).name != executable_name:
            raise ValueError("Invalid Capture One executable name")
        executable = (app / "Contents/MacOS" / executable_name).resolve(strict=True)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        _fail("invalid_ax_target", str(exc))
    session = None
    try:
        session = _AX(pid, executable, document.as_uri(), before_dispatch=before_dispatch)
        if action == "read_skin":
            result = {"values": {key: session.number(slider) for key, slider in session.skin().items()},
                      "skin_tab_selection_verified": False}
            session.guard()
        elif action == "set_skin":
            result = session.set_skin(field, value, expected_old)
        elif action == "menu":
            result = session.menu(menu_path)
        elif action == "read_layers":
            result = session.layer_rows()[0]
        elif action == "select_row":
            result = session.select_row(row_index, expected_rows)
        elif action == "delete_submask":
            result = session.delete_submask(expected_rows)
        elif action == "move_layer":
            result = session.move_layer(row_index, insertion_row_index, expected_rows)
        elif action == "read_gradient_view":
            result = {"view": session.gradient_view()[0]}
        elif action == "draw_gradient":
            result = session.draw_gradient(expected_view, start, stop)
        else:
            nodes = session.descendants(session.tool(tool))
            if len(nodes) > 512:
                session.fail("ax_tree_too_large", "Requested tool exceeds bounded inspection output")
            attributes = ("AXRole", "AXIdentifier", "AXTitle", "AXEnabled", "AXValue",
                          "AXMinValue", "AXMaxValue", "AXPosition", "AXSize", "AXSelected")
            def inspection_value(node, attribute):
                value = session.attr(node, attribute)
                return value if value is None or type(value) in (str, bool, int, float, dict) else None
            result = {"tool": tool, "elements": [{attribute: inspection_value(node, attribute)
                        for attribute in attributes} for node in nodes]}
            session.guard()
        return {**result, "dispatched": session.dispatched, "atomic": False, "source": "macos_accessibility",
                "document_url_verified": True, "layer_identity_verified": False}
    except Refused:
        raise
    except Exception as exc:
        _fail("ax_operation_failed", str(exc), session.dispatched if session else False)
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            _fail("invalid_ax_request", "AX request exceeds size limit")
        request = json.loads(raw)
        result = run_ax(**request)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
    except Exception as error:
        print(json.dumps({"ok": False, "error": {"code": getattr(error, "code", "invalid_ax_request"),
                          "message": str(error), "dispatched": getattr(error, "dispatched", False)}}, ensure_ascii=False))
