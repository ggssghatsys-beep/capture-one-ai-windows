"""Synthetic TUBE peer and real local IPC; never attaches to Capture One."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import struct
import threading
import time
import uuid

import pytest

from c1ai import tangent_transport as tube


def response(parameter_id, value):
    return tube.frame(struct.pack(">IIfI", 0x82, parameter_id, value, 0))


def definition(path, app="Capture One"):
    parts = [struct.pack(">I", 0x81)]
    for text in (app, str(path), ""):
        raw = text.encode("utf-8")
        parts.extend((struct.pack(">I", len(raw)), raw))
    return tube.frame(b"".join(parts))


class ApplicationDouble:
    def __init__(self, sock, controls_path):
        self.sock, self.controls_path = sock, controls_path
        self.values = {parameter_id: 0.0 for parameter_id in tube.FIELDS.values()}
        self.changes, self.commands, self.errors = [], [], []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def exact(self, length):
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def run(self):
        try:
            while True:
                size = struct.unpack(">I", self.exact(4))[0]
                payload = self.exact(size)
                code = struct.unpack(">I", payload[:4])[0]
                self.commands.append(code)
                if code == 1:
                    wire = definition(self.controls_path)
                    # Exercise stream framing rather than one-recv assumptions.
                    self.sock.sendall(wire[:2])
                    self.sock.sendall(wire[2:9])
                    self.sock.sendall(wire[9:])
                elif code == 4:
                    parameter_id = struct.unpack(">I", payload[4:8])[0]
                    self.sock.sendall(response(parameter_id, self.values[parameter_id]))
                elif code == 2:
                    parameter_id, increment = struct.unpack(">If", payload[4:])
                    self.changes.append((parameter_id, increment))
                    self.values[parameter_id] += increment
                    self.sock.sendall(response(parameter_id, self.values[parameter_id]))
                else:
                    raise AssertionError(f"Unexpected synthetic command {code}")
        except (EOFError, OSError):
            pass
        except Exception as error:
            self.errors.append(error)

    def close(self):
        self.sock.close()
        self.thread.join(2)


@pytest.fixture
def transport(tmp_path):
    executable = tmp_path / "CaptureOne.exe"
    executable.write_bytes(b"synthetic executable placeholder; never loaded")
    controls = tmp_path / "TangentSettings" / "controls.xml"
    controls.parent.mkdir()
    controls.write_text("<Controls><Group>" + "".join(
        f'<Parameter id="0x{parameter_id:x}"><MinValue>-50</MinValue><MaxValue>50</MaxValue></Parameter>'
        for parameter_id in tube.FIELDS.values()) + "</Group></Controls>", encoding="utf-8")
    config = {"executable": str(executable), "controls_sha256": hashlib.sha256(controls.read_bytes()).hexdigest()}
    identity = {"pid": 123, "started_at": "456", "executable": str(executable.resolve())}
    service = tube.TangentTransport(config, tmp_path / "state", identity_reader=lambda pid: deepcopy(identity),
                                    peer_reader=lambda port: 123)
    service.test_identity, service.test_config = identity, config
    yield service
    service.close()


def attach_double(service):
    client, app_socket = socket.socketpair()
    app = ApplicationDouble(app_socket, service.controls_path.parent)
    service.attach(client, ("127.0.0.1", 11111))
    return app


def request(**overrides):
    return {"command": "delta", "request_id": "request-1", "operation_id": "operation-1", "action_id": "action-1",
            "expected_instance": {"pid": 123, "started_at": "456"}, "field": "master_x",
            "expected_before": 0.0, "delta": 0.5, **overrides}


def read_request():
    return {"command": "read", "fields": ["master_x", "shadow_lightness"],
            "expected_instance": {"pid": 123, "started_at": "456"}}


def test_official_increment_frame_and_unavailable_parameter_decoding():
    assert tube.parameter_change(14, 0.5) == bytes.fromhex("0000000c000000020000000e3f000000")
    unavailable = tube.decode(response(14, math.nan)[4:])[0]
    assert unavailable["value"] is None and unavailable["available"] is False
    assert tube.decode(response(14, 0.5)[4:] + response(15, -0.5)[4:])[1]["value"] == -0.5


@pytest.mark.parametrize("payload", [b"", b"\x00", struct.pack(">I", 999),
    struct.pack(">IIfI", 0x82, 14, math.inf, 0), struct.pack(">IIfI", 0x82, 14, 0.0, 2)])
def test_decoder_refuses_invalid_messages(payload):
    if not payload:
        assert tube.decode(payload) == []
    else:
        with pytest.raises((tube.TransportError, UnicodeError)):
            tube.decode(payload)


def test_real_framing_handshake_read_and_increment_do_not_multiply_xml_step(transport):
    app = attach_double(transport)
    try:
        read = transport.dispatch(read_request())
        assert read["status"] == "ok" and read["values"] == {"master_x": 0.0, "shadow_lightness": 0.0}
        assert read["photo_identity_verified"] is False and read["model_verified"] is False
        result = transport.dispatch(request())
        assert result["status"] == "completed", result
        assert result["after"]["value"] == 0.5
        assert result["parameter_readback_verified"] is True
        assert result["model_verified"] is False and result["render_verified"] is False
        assert app.changes == [(14, 0.5)]
        assert result["planned_wire"] == tube.parameter_change(14, 0.5).hex()
        assert any(row["hex"] == result["planned_wire"] for row in result["wire"])
        assert result["operation_id"] == "operation-1" and result["action_id"] == "action-1"
        replay = transport.dispatch(request())
        assert replay["replayed"] is False and app.changes == [(14, 0.5)]
        assert transport.dispatch(request(delta=0.75))["error"]["code"] == "REQUEST_CONFLICT"
        assert transport.dispatch(request(request_id="different"))["error"]["code"] == "ACTION_CONFLICT"
        queried = transport.dispatch({"command": "query", "request_id": "request-1"})
        assert queried["after"]["value"] == 0.5 and not app.errors
    finally:
        app.close()


@pytest.mark.parametrize("change", [{"delta": 0}, {"delta": 5.01}, {"delta": math.nan}, {"delta": True},
    {"field": "opacity"}, {"expected_before": 49, "delta": 2}, {"steps": ["invoke"]},
    {"expected_instance": {"pid": 123, "started_at": "456", "variant": "fake"}}])
def test_invalid_writes_never_send_anything(transport, change):
    app = attach_double(transport)
    try:
        result = transport.dispatch(request(**change))
        assert result["status"] == "refused" and result["dispatched"] is False
        assert app.changes == []
    finally:
        app.close()


def test_target_or_controls_change_blocks_before_wire_send(transport):
    app = attach_double(transport)
    try:
        result = transport.dispatch(request(expected_instance={"pid": 123, "started_at": "789"}))
        assert result["status"] == "not_dispatched" and result["error"]["code"] == "PEER_CHANGED"
        assert app.changes == []
        transport.controls_path.write_text("changed", encoding="utf-8")
        result = transport.dispatch(request(request_id="two", action_id="two"))
        assert result["status"] == "not_dispatched" and result["error"]["code"] == "CONTROLS_MISMATCH"
        assert app.changes == []
    finally:
        app.close()


def test_stale_parameter_is_durable_not_dispatched_and_not_retried(transport):
    app = attach_double(transport)
    app.values[14] = 1.0
    try:
        result = transport.dispatch(request())
        assert result["status"] == "not_dispatched" and result["error"]["code"] == "STALE_PARAMETER"
        count = len(app.commands)
        assert transport.dispatch(request())["status"] == "not_dispatched"
        assert len(app.commands) == count and app.changes == []
    finally:
        app.close()


def test_post_send_failure_preserves_unknown_and_never_replays(transport, monkeypatch):
    app = attach_double(transport)
    original = transport.session.read
    reads = 0

    def failing_read(parameter_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise tube.TransportError("TUBE_TIMEOUT", "synthetic post-send timeout")
        return original(parameter_id)

    monkeypatch.setattr(transport.session, "read", failing_read)
    try:
        result = transport.dispatch(request())
        assert result["status"] == "outcome_unknown" and result["dispatched"] is True
        assert result["accepted"] is True and result["parameter_readback_verified"] is False
        assert transport.dispatch(request())["status"] == "outcome_unknown"
        deadline = time.monotonic() + 1
        while not app.changes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app.changes == [(14, 0.5)]
    finally:
        app.close()


def test_persist_failure_before_send_never_sends_and_after_send_is_not_refused(transport, monkeypatch):
    app = attach_double(transport)
    real_save = transport._save

    def fail_before(record):
        if record["phase"] == "change":
            raise OSError("synthetic write-ahead failure")
        real_save(record)

    try:
        monkeypatch.setattr(transport, "_save", fail_before)
        result = transport.dispatch(request())
        assert app.changes == [] and result["status"] == "outcome_unknown"

        def fail_after(record):
            if record["phase"] == "readback":
                raise OSError("synthetic final receipt failure")
            real_save(record)

        monkeypatch.setattr(transport, "_save", fail_after)
        result = transport.dispatch(request(request_id="second", action_id="second"))
        assert result["status"] == "outcome_unknown" and result["dispatched"] is True
        assert result["persistence_error"]["code"] == "RECEIPT_PERSIST_FAILED"
        assert transport.dispatch(request(request_id="second", action_id="second"))["status"] == "dispatching"
        assert app.changes == [(14, 0.5)]
    finally:
        app.close()


def test_timeout_keeps_stream_but_quarantines_further_reads_and_changes():
    client, app = socket.socketpair()
    session = tube.TubeSession(client, {"pid": 1}, timeout=0.03)
    try:
        with pytest.raises(tube.TransportError, match="timed out"):
            session.read(14)
        assert session.synchronized is False and client.fileno() >= 0
        with pytest.raises(tube.TransportError) as error:
            session.change(14, 0.5)
        assert error.value.code == "TUBE_DESYNCHRONIZED"
        with pytest.raises(tube.TransportError):
            session.read(14)
    finally:
        client.close()
        app.close()


def test_stream_fragment_is_preserved_after_timeout():
    client, app = socket.socketpair()
    session = tube.TubeSession(client, {"pid": 1}, timeout=0.02)
    wire = response(14, 0.25)
    try:
        app.sendall(wire[:7])
        with pytest.raises(tube.TransportError):
            session._receive(time.monotonic() + 0.02)
        assert bytes(session.buffer) == wire[:7]
        app.sendall(wire[7:])
        assert session._receive(time.monotonic() + 1)[0]["value"] == 0.25
    finally:
        client.close()
        app.close()


def test_restart_with_dispatching_receipt_returns_original_without_read_or_send(transport):
    record = {"request_id": "request-1", "operation_id": "operation-1", "action_id": "action-1",
              "status": "dispatching", "dispatched": True, "replay_allowed": False}
    transport.db.execute("INSERT INTO receipts VALUES (?,?,?,?)", ("request-1", "action-1", tube.canonical(request()), tube.canonical(record)))
    transport.db.commit()
    replacement = tube.TangentTransport(transport.test_config, transport.root,
                                        identity_reader=lambda pid: deepcopy(transport.test_identity))
    try:
        # Deliberately no session: a duplicate must not touch the application.
        result = replacement.dispatch(request())
        assert result["status"] == "dispatching" and result["replayed"] is False
        assert result["operation_id"] == "operation-1"
    finally:
        replacement.close()


def test_wrong_peer_or_handshake_is_rejected_without_replacing_live_connection(transport):
    app = attach_double(transport)
    original = transport.session
    bad_client, bad_app = socket.socketpair()
    try:
        with pytest.raises(tube.TransportError) as error:
            transport.attach(bad_client, ("192.0.2.1", 5555))
        assert error.value.code == "PEER_UNVERIFIED" and transport.session is original
        transport.test_identity["executable"] = str(transport.executable.parent / "other.exe")
        second, second_app = socket.socketpair()
        try:
            with pytest.raises(tube.TransportError) as error:
                transport.attach(second, ("127.0.0.1", 5555))
            assert error.value.code == "PEER_UNVERIFIED" and transport.session is original
        finally:
            second_app.close()
    finally:
        bad_app.close()
        app.close()


@pytest.mark.skipif(os.name != "nt", reason="Uses actual current-user Windows pipe")
def test_separate_pipe_survives_client_timeout_and_accepts_new_owner_clients(transport, monkeypatch):
    from c1ai.client import RequestUncertain, send_request
    from c1ai.owner import NamedPipeServer

    app = attach_double(transport)
    entered, release = threading.Event(), threading.Event()
    original = transport.session.read
    reads = 0

    def slow_read(parameter_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            entered.set()
            assert release.wait(5)
        return original(parameter_id)

    monkeypatch.setattr(transport.session, "read", slow_read)
    suffix = uuid.uuid4().hex
    server = NamedPipeServer(transport.dispatch, address=rf"\\.\pipe\C1AI.Tangent.Test.{suffix}",
                             name=rf"Local\C1AI.Tangent.Test.{suffix}")
    stop, errors = threading.Event(), []

    def serve():
        try:
            server.serve(stop)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    assert server.ready.wait(5)
    try:
        with pytest.raises(RequestUncertain):
            send_request(request(), address=server.address, autostart=False, timeout=0.15)
        assert entered.wait(2)
        release.set()
        result = send_request({"command": "query", "request_id": "request-1"}, address=server.address, autostart=False)
        assert result["status"] == "completed"
        with ThreadPoolExecutor(max_workers=3) as pool:
            replies = list(pool.map(lambda _: send_request(request(), address=server.address, autostart=False), range(3)))
        assert all(reply["status"] == "completed" and reply["replayed"] is False for reply in replies)
        assert app.changes == [(14, 0.5)] and not app.errors
        assert transport.session.client.fileno() >= 0
    finally:
        release.set()
        stop.set()
        worker.join(10)
        app.close()
        assert not worker.is_alive() and not errors


@pytest.mark.skipif(os.name != "nt", reason="Windows user SID")
def test_tangent_pipe_and_mutex_are_distinct_from_business_owner():
    from c1ai import owner
    assert tube.endpoint() != owner.endpoint()
    assert tube.mutex_name() != owner.mutex_name()
    assert "CaptureOneAI.Tangent." in tube.endpoint()
    assert tube.mutex_name().startswith("Global\\CaptureOneAI.Tangent.")
