"""Current-user Unix socket owner; the same Core and six-tool protocol as Windows."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time

from .owner import (MAX_FRAME, PROTOCOL, PROTOCOL_VERSION, OwnerAlreadyRunning,
                    PipeError, PipeTimeout, ProtocolMismatch)


def state_directory():
    root = Path.home() / "Library" / "Application Support" / "CaptureOneAI"
    secure_directory(root)
    return root


def secure_directory(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PipeError("Mac owner state directory must be a real current-user directory with mode 0700")


def _peer_uid(connection):
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    libc.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    libc.getpeereid.restype = ctypes.c_int
    if libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), "Cannot authenticate local socket peer")
    return uid.value


def _timeout(connection, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PipeTimeout("IPC deadline exceeded")
    connection.settimeout(remaining)


def send_frame(connection, value, deadline):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise PipeError("IPC frame exceeds 4 MiB")
    _timeout(connection, deadline)
    connection.sendall(struct.pack("<I", len(data)) + data)


def receive_frame(connection, deadline):
    def exact(count):
        data = bytearray()
        while len(data) < count:
            _timeout(connection, deadline)
            block = connection.recv(count - len(data))
            if not block:
                raise PipeError("Socket closed during read")
            data.extend(block)
        return data

    count = struct.unpack("<I", exact(4))[0]
    if not 0 < count <= MAX_FRAME:
        raise PipeError("Invalid IPC frame length")
    value = json.loads(exact(count), parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    if not isinstance(value, dict):
        raise PipeError("IPC messages must be JSON objects")
    return value


def send_request(payload, *, timeout, autostart, address, connect_timeout):
    from .client import RequestUncertain, launch_owner

    path = Path(address) if address is not None else state_directory() / "owner.sock"
    secure_directory(path.parent)
    deadline, started = time.monotonic() + connect_timeout, False
    while True:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            _timeout(connection, deadline)
            connection.connect(str(path))
            break
        except OSError as error:
            connection.close()
            if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
            if autostart and not started:
                launch_owner()
                started = True
            if time.monotonic() >= deadline:
                raise PipeTimeout("Mac owner did not become available") from error
            time.sleep(0.05)
    dispatched = False
    try:
        if _peer_uid(connection) != os.getuid():
            raise ProtocolMismatch("Mac owner belongs to another user")
        deadline = time.monotonic() + timeout
        hello = {"protocol": PROTOCOL, "version": PROTOCOL_VERSION, "kind": "hello"}
        send_frame(connection, hello, deadline)
        reply = receive_frame(connection, deadline)
        # Darwin SOL_LOCAL/LOCAL_PEERPID authenticates the hello's process identity.
        owner_pid = connection.getsockopt(0, 2)
        if reply != {**hello, "owner_pid": owner_pid}:
            raise ProtocolMismatch("Mac owner protocol/identity mismatch")
        dispatched = True
        send_frame(connection, {"protocol": PROTOCOL, "version": PROTOCOL_VERSION,
                                "kind": "request", "request": payload}, deadline)
        result = receive_frame(connection, deadline)
        if (result.get("protocol") != PROTOCOL or result.get("version") != PROTOCOL_VERSION
                or result.get("kind") != "result" or not isinstance(result.get("result"), dict)):
            raise ProtocolMismatch("Invalid Mac owner result envelope")
        return result["result"]
    except (OSError, PipeError, ValueError, UnicodeError) as error:
        if dispatched:
            raise RequestUncertain(payload["request_id"], error) from error
        raise
    finally:
        connection.close()


class UnixSocketServer:
    def __init__(self, core_factory, root=None):
        self.root = Path(root) if root is not None else state_directory()
        self.core_factory = core_factory
        self.ready = threading.Event()
        self.failure = None
        self._clients = threading.BoundedSemaphore(32)
        self._queued = threading.BoundedSemaphore(16)
        self._display_queued = threading.BoundedSemaphore(4)

    def _handle(self, connection, core, executor, display_executor):
        try:
            if _peer_uid(connection) != os.getuid():
                return
            hello = {"protocol": PROTOCOL, "version": PROTOCOL_VERSION, "kind": "hello"}
            if receive_frame(connection, time.monotonic() + 10) != hello:
                raise ProtocolMismatch("Invalid hello")
            send_frame(connection, {**hello, "owner_pid": os.getpid()}, time.monotonic() + 5)
            envelope = receive_frame(connection, time.monotonic() + 10)
            if (envelope.get("protocol") != PROTOCOL or envelope.get("version") != PROTOCOL_VERSION
                    or envelope.get("kind") != "request" or not isinstance(envelope.get("request"), dict)):
                raise ProtocolMismatch("Invalid request envelope")
            request = envelope["request"]
            display = getattr(core, 'is_display_request', lambda request: False)(request)
            queue = self._display_queued if display else self._queued
            if request.get("command") == "operations" and request.get("action", "get") in {"get", "list"}:
                result = core.peek(request)
            elif not queue.acquire(blocking=False):
                result = {"status": "refused", "error": {"code": "OWNER_BUSY", "message": "Request was not dispatched"}}
            else:
                try:
                    # A disconnected client cannot cancel or replay the accepted work.
                    selected_executor = display_executor if display else executor
                    result = selected_executor.submit(core.dispatch, request).result()
                except Exception as error:
                    result = {"status": "outcome_unknown", "request_id": request.get("request_id"),
                              "error": {"code": "OWNER_DISPATCH_FAILED", "message": str(error)}, "replay_allowed": False}
                finally:
                    queue.release()
            send_frame(connection, {"protocol": PROTOCOL, "version": PROTOCOL_VERSION,
                                    "kind": "result", "result": result}, time.monotonic() + 10)
        except (OSError, PipeError, ValueError, UnicodeError):
            pass
        finally:
            connection.close()
            self._clients.release()

    def serve(self, stop=None):
        stop = stop or threading.Event()
        secure_directory(self.root)
        lock = os.open(self.root / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        listener = core = None
        bound = False
        handlers = []
        path = self.root / "owner.sock"
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PipeError("Unsafe Mac owner lock")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OwnerAlreadyRunning("The per-user Mac owner is already running") from error
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise PipeError("Refusing to replace a non-socket or foreign owner endpoint")
                path.unlink()
            core = self.core_factory()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            bound = True
            path.chmod(0o600)
            listener.listen(32)
            listener.settimeout(0.2)
            with (ThreadPoolExecutor(max_workers=1, thread_name_prefix="c1ai-dispatcher") as executor,
                  ThreadPoolExecutor(max_workers=1, thread_name_prefix="c1ai-display") as display_executor):
                self.ready.set()
                try:
                    while not stop.is_set():
                        try:
                            connection, _ = listener.accept()
                        except socket.timeout:
                            continue
                        if not self._clients.acquire(blocking=False):
                            connection.close()
                            continue
                        thread = threading.Thread(target=self._handle, args=(connection, core, executor, display_executor))
                        handlers = [h for h in handlers if h.is_alive()]
                        handlers.append(thread)
                        thread.start()
                finally:
                    for thread in handlers:
                        thread.join()
        except Exception as error:
            self.failure = error
            raise
        finally:
            if listener is not None:
                listener.close()
            if core is not None:
                core.close()
            if bound:
                path.unlink(missing_ok=True)
            os.close(lock)


def serve_owner():
    from .core import Core
    from .macos_backend import MacOSBackend

    root = state_directory()
    try:
        UnixSocketServer(lambda: Core(MacOSBackend(state_dir=root), state_dir=root), root).serve()
    except OwnerAlreadyRunning:
        pass
    return 0
