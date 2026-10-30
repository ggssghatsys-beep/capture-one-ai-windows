"""One current-user native channel for C1 preview and mask operations.

Frida only loads our version-qualified dylib into a normally running host, then
detaches. Native callbacks and receipts remain alive independently of Python.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import time
import threading
import uuid

from .contracts import Refused
from .macos_ipc import secure_directory


def _private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused("invalid_bridge_file", "Native bridge files must be private current-user regular files")
    return path.read_bytes()


def configured(root):
    path = Path(root) / "macos-bridge.json"
    if not path.exists():
        return False
    return json.loads(_private_file(path)).get("enabled") is True


class NativeBridge:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "native-bridge" / "v2"
        self._pending_preview = None
        self._channel_lock = threading.RLock()
        self._render_lock = threading.RLock()
        self._artifact_lock = threading.RLock()

    def _ready(self):
        try:
            return json.loads(_private_file(self.root / "ready.json"))
        except FileNotFoundError:
            return {}

    def ensure(self, target):
        config = json.loads(_private_file(self.state_dir / "macos-bridge.json"))
        if config.get("enabled") is not True:
            raise Refused("native_bridge_disabled", "Enable the Mac native bridge before use")
        instance = target["instance"]
        if instance["version"] != config["capture_one_version"]:
            raise Refused("unsupported_bridge_version", "Native bridge does not match this Capture One build")
        secure_directory(self.root.parent); secure_directory(self.root)
        ready = self._ready()
        if ready.get("pid") == instance["pid"]:
            if ready.get("build_id") != config["build_id"] or ready.get("protocol") != 2:
                raise Refused("bridge_restart_required", "Restart Capture One normally to use the updated native bridge")
            return
        library = Path(config["path"])
        if library.resolve().parent != self.root.resolve() or hashlib.sha256(_private_file(library)).hexdigest() != config["sha256"]:
            raise Refused("bridge_build_changed", "Native bridge build path or hash changed; rebuild explicitly")
        # Verify the executable before asking macOS for debugger authorization.
        import ctypes
        proc = ctypes.CDLL("/usr/lib/libproc.dylib")
        buffer = ctypes.create_string_buffer(4096)
        if proc.proc_pidpath(instance["pid"], buffer, len(buffer)) <= 0 or os.fsdecode(buffer.value) != "/Applications/Capture One.app/Contents/MacOS/Capture One":
            raise Refused("bridge_target_changed", "Capture One process no longer matches")
        try:
            import frida
        except ImportError as error:
            raise Refused("bridge_dependency_missing", "Install the project's mac extra to load the native bridge") from error
        session = None
        try:
            with frida.Cancellable() as cancel:
                import threading
                timer = threading.Timer(15, cancel.cancel); timer.daemon = True; timer.start()
                try:
                    session = frida.get_local_device().attach(instance["pid"])
                    script = session.create_script("if (Process.id !== " + str(instance["pid"]) +
                        ") throw new Error('Host changed'); rpc.exports.load = () => { Module.load(" +
                        json.dumps(str(library)) + "); return true; };")
                    script.load(); script.exports_sync.load()
                finally:
                    timer.cancel()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                ready = self._ready()
                if ready.get("pid") == instance["pid"] and ready.get("build_id") == config["build_id"]:
                    return
                time.sleep(.1)
            raise Refused("native_bridge_not_ready", "Native bridge has not returned its ready handshake")
        except (frida.PermissionDeniedError, frida.ProcessNotFoundError, frida.TransportError,
                frida.OperationCancelledError, frida.RPCException, frida.InvalidOperationError) as error:
            raise Refused("native_bridge_load_failed", str(error)) from error
        finally:
            if session is not None:
                with frida.Cancellable() as cancel:
                    timer = threading.Timer(3, cancel.cancel); timer.daemon = True; timer.start()
                    try:
                        session.detach()
                    finally:
                        timer.cancel()

    def call(self, public_target, action, **fields):
        if not self._channel_lock.acquire(timeout=.05):
            error = Refused("bridge_request_pending", "Native channel is busy; retry the read shortly")
            error.dispatched = False
            raise error
        try:
            return self._call(public_target, action, **fields)
        finally:
            self._channel_lock.release()

    def _call(self, public_target, action, **fields):
        sent = False
        lock = None
        try:
            self.ensure(public_target)
            identifier = uuid.uuid4().hex
            request = {"request_id": identifier, "pid": public_target["instance"]["pid"], "action": action, **fields}
            lock = os.open(self.root / "channel.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refused("bridge_request_pending", "Native channel is busy; retry the read shortly") from error
            slot = self.root / "request.json"
            if slot.exists():
                raise Refused("bridge_request_pending", "A native request is pending; do not replace or replay it")
            temporary = self.root / (identifier + ".tmp")
            with open(temporary, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
                json.dump(request, stream, allow_nan=False)
            os.replace(temporary, slot); sent = True
            response = self.root / (identifier + ".json")
            deadline = time.monotonic() + 8
            while not response.exists() and time.monotonic() < deadline:
                time.sleep(.002)
            if not response.exists():
                raise Refused("bridge_response_pending", "Native reply timed out; query the original operation, never replay")
            result = json.loads(_private_file(response)); response.unlink()
            if result.get("request_id") != identifier:
                raise Refused("bridge_response_mismatch", "Native response belongs to another request")
            if result.get("ok") is not True:
                error = Refused("native_bridge_refused", result.get("error", "Native request failed"))
                # Creation catches errors after its first mutation. Failure to
                # encode a completed response cannot certify no dispatch.
                error.dispatched = action in {"gradient_create", "mask_apply", "color_apply", "layer_apply"} and result.get("response_encoding_failed") is True
                raise error
            return result["result"]
        except Exception as error:
            if not hasattr(error, "dispatched"):
                error.dispatched = sent and action in {"gradient_create", "mask_apply", "color_apply", "layer_apply"}
            raise
        finally:
            if lock is not None:
                os.close(lock)

    def inspect(self, target):
        result = self.call(target, "inspect")
        native = result["target"]
        layer = target["layer"]
        expected_layer = layer["native_id"] if isinstance(layer, dict) else "Background"
        if (native["pid"] != target["instance"]["pid"] or native["document_path"] != target["document"]["path"]
                or native["variant_id"] != target["variant"]["id"] or native["image_path"] != target["image"]["path"]
                or native["layer_uuid"].lower() != expected_layer.lower()):
            raise Refused("bridge_target_changed", "Native bridge target differs from the official photo/layer observation")
        return result

    def render(self, request, observation):
        if request.get("live_sample") is True and request.get("pixel_buffer") is True and request.get("kind", "native") == "native":
            started = time.monotonic()
            target = observation["target"]
            native = self.inspect(target)["target"]
            if native != observation.get("native_context", {}).get("bridge_target"):
                raise Refused("stale_observation", "Native display target changed")
            cached = self.call(target, "display_cache", target=native, max_size=request.get("max_size", 640))
            if cached.get("phase") == "ready":
                return self._deliver(cached, native, target, "native", True, started)
        if not self._render_lock.acquire(timeout=.05):
            raise Refused("native_render_pending", "C1 is still rendering another native bitmap")
        try:
            return self._render(request, observation)
        finally:
            self._render_lock.release()

    def _render(self, request, observation):
        started = time.monotonic()
        target = observation["target"]
        if request.get("action") == "map_points":
            raise Refused("unsupported", "Mac preview coordinate mapping has not been qualified")
        size, kind = request.get("max_size", 1600), request.get("kind", "native")
        native = self.inspect(target)["target"]
        saved = observation.get("native_context", {}).get("bridge_target")
        if saved is not None and saved != native:
            raise Refused("stale_observation", "Native preview revision changed; read again")
        raw = request.get("pixel_buffer") is True
        key = [native, size, kind, raw]
        if self._pending_preview and self._pending_preview[0] == key:
            result = self.call(target, "preview_result", preview_id=self._pending_preview[1])
        else:
            result = self.call(target, "preview_start", target=native, max_size=size, kind=kind, pixel_buffer=raw)
            self._pending_preview = (key, result["preview_id"])
        while result["phase"] != "ready":
            if time.monotonic() - started > 25:
                raise Refused("native_render_pending", "C1 is still rendering; a subsequent render reuses the same job")
            time.sleep(.005)
            result = self.call(target, "preview_result", preview_id=result["preview_id"])
        completed = self._deliver(result, native, target, kind, raw, started)
        self._pending_preview = None
        return completed

    def _deliver(self, result, native, target, kind, raw, started):
        with self._artifact_lock:
            return self._deliver_artifact(result, native, target, kind, raw, started)

    def _deliver_artifact(self, result, native, target, kind, raw, started):
        source = Path(result["artifact"]["path"])
        extension = ".bgra" if raw else ".png"
        if source.parent.resolve() != self.root.resolve() or source.name != result["preview_id"] + extension:
            raise Refused("invalid_native_preview_path", "Native image is outside its private cache")
        data = _private_file(source)
        if raw:
            import numpy as np
            artifact = result["artifact"]
            if (artifact.get("format") != "BGRA8" or artifact.get("stride") != artifact["width"] * 4
                    or artifact.get("row_order") != "top-down" or artifact.get("bit_depth") != 8
                    or len(data) != artifact["width"] * artifact["height"] * 4):
                raise Refused("native_bitmap_mismatch", "Native RGB buffer differs from its receipt")
            if kind == "mask":
                pixels = np.frombuffer(data, dtype=np.uint8).reshape(-1, 4)
                if not (np.array_equal(pixels[:, 0], pixels[:, 1]) and np.array_equal(pixels[:, 1], pixels[:, 2])
                        and np.all(pixels[:, 3] == 255)):
                    raise Refused("native_mask_unqualified", "Native mask buffer is not opaque grayscale")
        else:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.size != (result["artifact"]["width"], result["artifact"]["height"]):
                    raise Refused("native_bitmap_mismatch", "Native image dimensions or format differ from its receipt")
                if kind == "mask":
                    from PIL import ImageChops
                    red, green, blue, alpha = image.convert("RGBA").split()
                    if (ImageChops.difference(red, green).getbbox() or ImageChops.difference(green, blue).getbbox()
                            or alpha.getextrema() != (255, 255)):
                        raise Refused("native_mask_unqualified", "Native mask renderer did not return opaque grayscale coverage")
        sha = hashlib.sha256(data).hexdigest()
        archive = self.state_dir / "native-previews"; secure_directory(archive)
        path = archive / (sha + extension)
        if not path.exists():
            with open(path, "xb", opener=lambda p, f: os.open(p, f, 0o600)) as output:
                output.write(data)
        elif hashlib.sha256(_private_file(path)).hexdigest() != sha:
            raise Refused("native_preview_changed", "Cached native image hash mismatch")
        if self.inspect(target)["target"] != native:
            raise Refused("stale_preview", "Native photo, layer or revision changed during delivery")
        old = sorted((p for p in archive.glob("*" + extension) if p != path), key=lambda p: p.stat().st_mtime, reverse=True)
        for obsolete in old[7:]:
            if not obsolete.is_symlink():
                obsolete.unlink(missing_ok=True)
        source.unlink(missing_ok=True)
        for obsolete in self.root.glob("*" + extension):
            if not obsolete.is_symlink() and obsolete.stat().st_mtime < time.time() - 60:
                obsolete.unlink(missing_ok=True)
        artifact = {**result["artifact"], "path": str(path), "sha256": sha}
        return {"status": "completed", "source": "capture_one_native_mask" if kind == "mask" else "capture_one_native_render",
                "path": str(path), "image": artifact, "target": target, "revision": result["revision"],
                "preview_id": result["preview_id"], "geometry": result["geometry"], "render_verified": True,
                "model_verified": True, "current_adjustments_verified": True, "export_required": False,
                "focus_used": False, "elapsed_seconds": round(time.monotonic() - started, 4),
                **({"sample_source": result["sample_source"], "sample_limitation": result.get("limitation"),
                    "viewer_stamp": result.get("viewer_stamp")} if result.get("sample_source") else {}),
                "color_profile": ("C1 display/proofed Viewer cache converted to sRGB" if result.get("sample_source") == "viewer_cache"
                                  else "C1 native sRGB output"),
                "crop_evidence": "C1 VariantGeometryTransformer.canvasCropRect + native bitmap job"}

    def create_gradient(self, target, native, operation_id, mask_uuid, kind, parameters):
        result = self.call(target, "gradient_create", target=native, operation_id=operation_id,
                           mask_uuid=mask_uuid, kind=kind, parameters=parameters)
        try:
            return self._wait_gradient(target, operation_id, result)
        except Exception as error:
            # Even a pre-write rejection may retain native bookkeeping when
            # its release reply is lost. Keep the operation reconcilable then.
            error.dispatched = result.get("dispatched", True) or getattr(error, "receipt_cleanup_pending", False)
            raise

    def apply_layer(self, target, native, operation_id, kind, **arguments):
        result = self.call(target, "layer_apply", target=native, operation_id=operation_id,
                           kind=kind, **arguments)
        try:
            return self._wait_gradient(target, operation_id, result, prefix="mask", release_committed=False)
        except Exception as error:
            error.dispatched = result.get("dispatched", True) or getattr(error, "receipt_cleanup_pending", False)
            raise

    def apply_mask(self, target, native, operation_id, kind):
        result = self.call(target, "mask_apply", target=native, operation_id=operation_id, kind=kind)
        try:
            return self._wait_gradient(target, operation_id, result, prefix="mask", release_committed=False)
        except Exception as error:
            error.dispatched = result.get("dispatched", True) or getattr(error, "receipt_cleanup_pending", False)
            raise

    def apply_color(self, target, native, operation_id, patch, expected_groups):
        result = self.call(target, "color_apply", target=native, operation_id=operation_id,
                           patch=patch, expected_groups=expected_groups)
        try:
            return self._wait_gradient(target, operation_id, result, prefix="color", release_committed=False)
        except Exception as error:
            error.dispatched = result.get("dispatched", True) or getattr(error, "receipt_cleanup_pending", False)
            raise

    def _wait_gradient(self, target, operation_id, result, *, prefix="gradient", release_committed=True):
        deadline = time.monotonic() + 25
        while result["state"] in {"prepared", "beginning", "pending", "executing", "applying", "committing"}:
            if time.monotonic() >= deadline:
                break
            time.sleep(.1)
            result = self.call(target, prefix + "_status", operation_id=operation_id)
        # A proven pre-write rejection can drop bookkeeping immediately. All
        # dispatched cancellations stay until unchanged-state reconciliation.
        safe_rejection = (result["state"] == "rejected" and result.get("dispatched") is False
                          and result.get("temporal_open") is False)
        if (result["state"] == "committed" and release_committed) or safe_rejection:
            # The native manager can remain busy for a short time after callback.
            for attempt in range(20):
                try:
                    self.call(target, prefix + "_release", operation_id=operation_id)
                    break
                except Exception as error:
                    if attempt == 19:
                        error.receipt_cleanup_pending = True
                        error.details = result
                        raise
                    time.sleep(.1)
        if result["state"] != "committed":
            error = Refused("native_" + prefix + "_unresolved", "Native mask operation did not commit; query original operation")
            error.dispatched = result.get("dispatched", True)
            error.details = result
            raise error
        return result
