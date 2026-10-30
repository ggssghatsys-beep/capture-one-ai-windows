"""Opt-in Capture Pilot preview; official sharing controls, local native JPEGs.

Wire protocol checked against the installed C1 client and LiveLegacy
c6b8422f22ebe712b1a5731d8acf96e84ed395ae. No third-party code is copied.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from .contracts import Refused, digest
from .macos_ipc import secure_directory

SUPPORTED_PILOT_VERSION = "16.8.6.25"


class PilotClient:
    def __init__(self, port: int, password: str):
        if not 1 <= port <= 65535:
            raise ValueError("Invalid local port")
        self.port, self.session = port, None
        # Protected mobile serving also requires HTTP Basic authentication.
        # C1's installed server registers its configured password under Guest.
        self.authorization = "Basic " + base64.b64encode(("Guest:" + password).encode()).decode()
        protocol = self.request("getProtocolVersion").decode().strip()
        if protocol != "2.1":
            raise ValueError("Unexpected Capture Pilot protocol")
        session = self.request("connectToService", {
            "protocolVersion": "2.4", "password": hashlib.sha1(password.encode()).hexdigest()
        }, method="POST").decode().strip()
        if not session or session == "0" or len(session) > 256:
            raise ValueError("Capture Pilot did not grant a session")
        self.session = session

    def request(self, endpoint, params=None, *, method="GET", limit=8 * 1024 * 1024):
        allowed = {"getProtocolVersion", "connectToService", "getServerState",
                   "getServerChanges", "getImageMetadata", "getImage"}
        if endpoint not in allowed:
            raise ValueError("Not a read-only probe endpoint")
        query = dict(params or {})
        query["timestamp"] = str(time.time_ns())
        if self.session is not None:
            query["sessionID"] = self.session
        # http.client avoids proxies and redirects: credentials stay on loopback.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, "/" + endpoint + "?" + urlencode(query),
                               body=b"" if method == "POST" else None,
                               headers={"Cache-Control": "no-cache", "Authorization": self.authorization})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"Capture Pilot returned HTTP {response.status}")
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("Capture Pilot response exceeds probe limit")
            return body
        finally:
            connection.close()

    def state(self):
        result = json.loads(self.request("getServerState"))
        if not isinstance(result, dict) or not isinstance(result.get("variants"), list):
            raise ValueError("Unexpected Capture Pilot state")
        return result

    def image(self, identifier, size=1200):
        if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
            raise ValueError("Invalid Pilot variant identifier")
        if not 128 <= size <= 1600:
            raise ValueError("Probe size must be 128..1600")
        data = self.request("getImage", {"id": identifier, "width": size, "height": size,
                                         "left": 0, "top": 0, "right": 0, "bottom": 0})
        # The bundled C1 web client receives base64; the public mobile client
        # receives JPEG bytes. Accept only these two encodings of the same call.
        if data.startswith(b"/9j/"):
            data = base64.b64decode(data, validate=True)
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or min(image.size) <= 0 or max(image.size) > size + 8:
                raise ValueError("Unexpected Capture Pilot image format or dimensions")
            image.verify()
        return data


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Pilot configuration must be an owner-only regular file")
        data = os.read(fd, 65537)
        if len(data) > 65536:
            raise ValueError("Pilot configuration is too large")
        return json.loads(data)
    finally:
        os.close(fd)


def load_config(root, target):
    config = private_json(root / "macos-pilot.json")
    fixed_scope = (config.get("version") == 1 and isinstance(config.get("collection_id"), str)
                   and isinstance(config.get("images"), dict) and 1 <= len(config["images"]) <= 32
                   and config["images"].get(target["image"]["id"]) == target["image"]["path"])
    selected_scope = (config.get("version") == 2 and config.get("scope") == "selected_photo_in_collection"
                      and isinstance(config.get("source_collection"), dict)
                      and set(config["source_collection"]) == {"id", "name", "kind", "folder"}
                      and all(isinstance(v, str) for v in config["source_collection"].values())
                      and bool(config["source_collection"]["id"]))
    if (not (fixed_scope or selected_scope) or config.get("document") != target["document"]
            or target["instance"]["version"] != SUPPORTED_PILOT_VERSION
            or config.get("capture_one_version") != target["instance"]["version"]
            or not isinstance(config.get("password"), str) or len(config["password"]) < 32):
        raise Refused("pilot_not_configured", "This Session/photo is outside the approved Pilot preview scope")
    return config


def available(root, target):
    try:
        load_config(root, target)
        return not (root / "macos-pilot-active.json").exists()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def control(command, receipt):
    # Only the config path goes in argv. Never the password or HTTP session ID.
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript",
        str(Path(__file__).parent / "native" / "macos_pilot.js"),
        json.dumps({**receipt, "command": command}, ensure_ascii=False)],
        capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise Refused("pilot_control_failed", "Capture One Pilot control failed; inspect its current service state")
    payload = json.loads(result.stdout)
    if payload.get("ok") is not True:
        raise Refused("pilot_control_failed", payload.get("error", "Pilot control failed"))
    return payload["result"]


def save_private(path, value):
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        json.dump(value, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())


def cleanup(root):
    """Explicit recovery of our recorded server only; never replays a render."""
    path = root / "macos-pilot-active.json"
    receipt = private_json(path)
    result = control("stop", receipt)
    path.unlink()
    return result


def check_port(port, pid):
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-iTCP:" + str(port),
                             "-sTCP:LISTEN", "-Fp"], capture_output=True, text=True, timeout=5)
    pids = {line[1:] for line in result.stdout.splitlines() if line.startswith("p")}
    if result.returncode or pids != {str(pid)}:
        raise Refused("pilot_wrong_process", "Pilot port is not owned by the observed Capture One process")


def match_variant(state, rows, target):
    # This installed build sends '<native script row ID>/<runtime object UUID>'.
    # MOVariant.uuid lazily creates this UUID; it is NOT the persisted
    # MOVariant.variantUUID in the Session/COS, nor MOImage.imageUUID.
    # Bind every row to the approved native collection, not a filename guess.
    native = {row["id"]: row for row in rows}
    variants = {}
    for variant in state["variants"]:
        identifier = variant.get("kVariantKey_VariantID", "")
        parts = identifier.split("/")
        if len(parts) != 2 or parts[0] not in native:
            raise Refused("pilot_identity_mismatch", "Pilot returned a variant outside the observed collection")
        uuid.UUID(parts[1])
        if variant.get("kVariantKey_ChangeType") in {"deleted", "removed"}:
            raise Refused("pilot_collection_changed", "Pilot collection changed during enumeration")
        row = native[parts[0]]
        if (variant.get("kVariantKey_VariantNo") != row["position"] - 1
                or variant.get("kVariantKey_VariantName") != Path(row["path"]).name):
            raise Refused("pilot_identity_mismatch", "Pilot and native variant identity disagree")
        previous = variants.get(parts[0])
        if previous is not None and previous["kVariantKey_VariantID"] != identifier:
            raise Refused("pilot_identity_mismatch", "Pilot row ID maps to multiple variant UUIDs")
        variants[parts[0]] = variant
    if variants.keys() != native.keys():
        raise Refused("pilot_collection_changed", "Pilot and native collection membership disagree")
    selected = variants[target["variant"]["id"]]
    flags = selected.get("kVariantKey_Properties", {})
    if any(flags.get("kVariantProperty_" + key) is not False for key in ["Missing", "Corrupted", "IsOffline"]):
        raise Refused("pilot_image_unavailable", "Pilot reports an unavailable source photo")
    return selected


def _sidecar_bytes(path, limit, *, optional=False):
    """Read C1-owned data without following a replaced file or accepting a torn read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if optional:
            return None
        raise Refused("saved_state_unavailable", "Capture One has not saved the expected Session sidecar")
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise Refused("saved_state_unavailable", "Session sidecar type or size is outside the qualified scope")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(data) > limit or signature(before) != signature(after) or signature(after) != signature(current):
            raise Refused("saved_state_changed", "Session sidecar changed while reading")
        return data


def _saved_database_content(variant, layers, metadata):
    """Replace rebuildable storage links with roles/UUIDs, retaining their contents."""
    roles = {"ZDEFAULTLAYER": "default", "ZCOMBINEDSETTINGS": "combined", "ZADJUSTMENTLAYER": "background"}
    identities = {}
    for column, role in roles.items():
        row_id = variant[column]
        if row_id is None or row_id in identities:
            raise ValueError("Missing or ambiguous saved layer role")
        identities[row_id] = role
    by_id = {row["Z_PK"]: row for row in layers}
    if len(by_id) != len(layers) or not set(identities).issubset(by_id):
        raise ValueError("Saved layer role does not resolve")
    for row in layers:
        if row["ZVARIANT"] != variant["Z_PK"]:
            raise ValueError("Saved layer belongs to another variant")
        if row["Z_PK"] not in identities:
            if row["ZINDEX"] is None:
                raise ValueError("Unknown saved layer role")
            identities[row["Z_PK"]] = "local:" + str(uuid.UUID(row["ZMASKUUID"]))
    if len(set(identities.values())) != len(identities):
        raise ValueError("Ambiguous saved layer UUID")
    by_metadata_id = {row["Z_PK"]: row for row in metadata}
    if len(by_metadata_id) != len(metadata):
        raise ValueError("Ambiguous saved metadata link")
    normalized_layers = {}
    used_metadata = set()
    for row in layers:
        role = identities[row["Z_PK"]]
        item = {key: value for key, value in row.items() if key != "Z_PK"}
        item["ZVARIANT"] = str(uuid.UUID(variant["ZVARIANTUUID"]))
        metadata_id = row["ZMETADATA"]
        if metadata_id is not None:
            value = by_metadata_id.get(metadata_id)
            if value is None or value["ZLAYER"] != row["Z_PK"] or metadata_id in used_metadata:
                raise ValueError("Saved metadata does not resolve to its layer")
            used_metadata.add(metadata_id)
            item["ZMETADATA"] = {key: val for key, val in value.items() if key != "Z_PK"}
            item["ZMETADATA"]["ZLAYER"] = role
        normalized_layers[role] = item
    if used_metadata != set(by_metadata_id):
        raise ValueError("Unbound saved metadata record")
    normalized_variant = {key: value for key, value in variant.items() if key != "Z_PK"}
    for column, role in roles.items():
        normalized_variant[column] = role
    return {"variant": normalized_variant, "layers": normalized_layers}


def saved_snapshot(backend, observation):
    """Bind a saved Session composite, not decoded mask coverage or an atomic render revision.

    Only C1 saves its data. SQLite (including its normal WAL) and sidecars are read-only.
    Whole-photo sidecar hashes deliberately reject changes to sibling variants too.
    """
    target = observation["target"]
    local = isinstance(target.get("layer"), dict)
    exposed = backend._native("local_read" if local else "snapshot", [],
                              {**observation, "module": "layers"} if local else observation)
    layers = backend._native("layers_read", [])
    if layers["observation"]["target"] != target:
        raise Refused("target_changed", "Selected photo or layer changed before preview")
    layer_state = layers["result"]["state"]
    raw = Path(target["image"]["path"])
    if raw.suffix.lower() == ".eip" or target["instance"]["version"] != SUPPORTED_PILOT_VERSION:
        raise Refused("saved_state_unavailable", "Saved preview binding requires the qualified unpacked Session")
    folder = raw.parent / "CaptureOne" / "Settings1680"
    paths = [folder / (raw.name + ".cos"), folder / (raw.name + ".comask")]
    def files():
        return [_sidecar_bytes(paths[0], 16 * 1024 * 1024),
                _sidecar_bytes(paths[1], 256 * 1024 * 1024, optional=len(layer_state["layers"]) == 1)]
    before = files()
    try:
        connection = sqlite3.connect(Path(target["document"]["path"]).as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            variant = [dict(row) for row in connection.execute("SELECT * FROM ZVARIANT WHERE Z_PK=?", (int(target["variant"]["id"]),))]
            rows = [dict(row) for row in connection.execute("SELECT * FROM ZVARIANTLAYER WHERE ZVARIANT=? ORDER BY Z_PK", (int(target["variant"]["id"]),))]
            metadata = [dict(row) for row in connection.execute(
                "SELECT m.* FROM ZVARIANTMETADATA m JOIN ZVARIANTLAYER l ON m.Z_PK=l.ZMETADATA "
                "WHERE l.ZVARIANT=? ORDER BY m.Z_PK", (int(target["variant"]["id"]),))]
        finally:
            connection.close()
        if len(variant) != 1:
            raise ValueError("Variant disappeared")
        v = variant[0]
        database_content = _saved_database_content(v, rows, metadata)
        variant_uuid = str(uuid.UUID(v["ZVARIANTUUID"]))
        expected = layer_state["layers"]
        visible = sorted([row for row in rows if row["ZINDEX"] is not None or row["Z_PK"] == v["ZADJUSTMENTLAYER"]],
                         key=lambda row: -1 if row["ZINDEX"] is None else row["ZINDEX"])
        if (v["ZIMAGE"] != int(target["image"]["id"]) or
                v["ZPROCESSINGENGINE"] != layers["observation"]["native_context"]["processing_engine"] or
                len(visible) != len(expected)):
            raise ValueError("Saved variant binding differs")
        for row, ref in zip(visible, expected):
            if ((ref["is_image_layer"] and row["Z_PK"] != v["ZADJUSTMENTLAYER"]) or
                    str(uuid.UUID(ref["variant_uuid"])) != variant_uuid or
                    (not ref["is_image_layer"] and str(uuid.UUID(row["ZMASKUUID"])) != str(uuid.UUID(ref["native_id"])))):
                raise ValueError("Saved layer binding differs")
        xml = ET.fromstring(before[0])
        variants = [node for node in xml.findall("VAR") if any(
            item.get("K") == "UUID" and str(item.get("V", "")).lower() == variant_uuid for item in node.findall("E"))]
        if xml.tag != "IMG" or len(variants) != 1:
            raise ValueError("Sidecar variant UUID missing or ambiguous")
        saved = variants[0]
        fields = {item.get("K"): item.get("V") for item in saved.findall("E")}
        if int(saved.get("Engine")) != v["ZPROCESSINGENGINE"]:
            raise ValueError("Sidecar engine differs")
        for key, column in [("HighResProcessCounter", "ZHIGHRESPROCESSCOUNTER"),
                            ("LowResProcessCounter", "ZLOWRESPROCESSCOUNTER"),
                            ("SyncProcessCounter", "ZSYNCPROCESSCOUNTER")]:
            value = int(fields[key]) if key in fields else None
            if value != v[column]:
                raise ValueError("Sidecar processing counter differs")
        mask_ids = []
        for layer in saved.findall("LA"):
            ids = [item.get("V") for item in layer.findall("E") if item.get("K") == "MaskUUID"]
            if len(ids) != 1:
                raise ValueError("Unknown saved mask structure")
            mask_ids.append(str(uuid.UUID(ids[0])))
        # COS serializes local layers in the opposite order to this build's
        # scripting/SQLite order. Join by UUID; order is guarded by the native
        # layer state, all DB rows and the complete unchanged COS bytes.
        expected_masks = [str(uuid.UUID(ref["native_id"])) for ref in expected if not ref["is_image_layer"]]
        if len(set(mask_ids)) != len(mask_ids) or sorted(mask_ids) != sorted(expected_masks):
            raise ValueError("Sidecar mask identities differ")
        if mask_ids and (before[1] is None or not before[1].startswith(b"\x04XCon")):
            raise ValueError("Unknown saved mask container")
    except (sqlite3.Error, ValueError, KeyError, TypeError, ET.ParseError) as error:
        raise Refused("saved_state_unavailable", "Live, Session and sidecar state do not agree: " + str(error)) from error
    after = files()
    if after != before:
        raise Refused("saved_state_changed", "Session sidecars changed across the database read")
    # A second native read detects selection/layer changes after the file reads;
    # it also asks C1 to flush any intervening edits before the final byte check.
    final_layers = backend._native("layers_read", [])
    if final_layers["observation"]["target"] != target or final_layers["result"]["state"] != layer_state or files() != before:
        raise Refused("saved_state_changed", "Capture One changed during saved-state binding")
    encoded = lambda value: {"blob_hex": value.hex()} if isinstance(value, bytes) else value
    state = {"variant_uuid": variant_uuid, "database_digest": digest(json.loads(json.dumps(
                 database_content, default=encoded))),
             "sidecars": [hashlib.sha256(data).hexdigest() if data is not None else None for data in before],
             "layers": layer_state,
             "exposed": exposed["result"].get("render_state", exposed["observation"]["values"])}
    return {"state": state, "observation": exposed["observation"]}


def render(backend, request, observation):
    if request.get("kind", "native") != "native" or request.get("action", "render") != "render":
        raise Refused("unsupported", "Mac Pilot supports native render only, not masks, point mapping or exports")
    root, target = backend.state_dir, observation["target"]
    config = load_config(root, target)
    receipt_path = root / "macos-pilot-active.json"
    if receipt_path.exists():
        raise Refused("pilot_cleanup_required", "A prior Pilot request needs explicit cleanup; do not replay it")
    baseline = saved_snapshot(backend, observation)["state"]
    layer_state = baseline["layers"]
    receipt = {"target": target, "config_path": str(root / "macos-pilot.json"),
               "name": "C1 AI Preview " + uuid.uuid4().hex,
               "selected_index": layer_state["layers"].index(layer_state["selected_layer"]),
               "selected_layer": layer_state["selected_layer"], "layer_details": layer_state["details"]}
    if config.get("version") == 2:
        receipt.update(source_collection=config["source_collection"], temporary_album=True,
                       images={target["image"]["id"]: target["image"]["path"]})
    else:
        receipt.update(collection_id=config["collection_id"], images=config["images"])
    initial = control("inspect", receipt)
    receipt["before"] = initial["settings"]
    if "tool_tab_id" in initial:
        receipt["before_tool_tab_id"] = initial["tool_tab_id"]
    if receipt["before"]["mobile"] or receipt["before"]["web"]:
        raise Refused("pilot_already_running", "Stop the existing Pilot sharing session before managed previews")
    save_private(receipt_path, receipt)
    try:
        started = backend.action_hook("pilot.start", lambda: control("start", receipt),
                                      {"temporary_sharing": True,
                                       "selected_photo_only": receipt.get("temporary_album", False)})
        port = started["settings"]["port"]
        check_port(port, target["instance"]["pid"])
        client = PilotClient(port, config["password"])
        state = client.state()
        selected = match_variant(state, started["rows"], target)
        # The runtime UUID is bound by C1's _variantWithID: lookup to the native
        # row ID. Keep the entire identifier unchanged across the image request;
        # saved_snapshot separately binds that same row to its persistent UUID.
        pilot_identity = {"native_row_id": target["variant"]["id"],
                          "runtime_uuid": str(uuid.UUID(selected["kVariantKey_VariantID"].split("/")[1])),
                          "saved_variant_uuid": baseline["variant_uuid"]}
        data = client.image(selected["kVariantKey_VariantID"], request.get("max_size", 1600))
        final_native = control("inspect", receipt)
        if final_native != started:
            raise Refused("pilot_state_changed", "Pilot sharing settings or collection changed during rendering")
        selected_after = match_variant(client.state(), final_native["rows"], target)
        if selected_after != selected:
            # Full state emits new/modified entries; compare actual identity and
            # metadata below, not the delivery event type or stream revision.
            clean = lambda v: {k: value for k, value in v.items() if k != "kVariantKey_ChangeType"}
            if clean(selected_after) != clean(selected):
                raise Refused("pilot_state_changed", "Pilot image metadata changed during rendering")
        after = saved_snapshot(backend, observation)
        if after["state"] != baseline:
            raise Refused("stale_preview", "Saved Capture One adjustments or masks changed during rendering")
    finally:
        cleanup_called = False
        def recorded_cleanup():
            nonlocal cleanup_called
            cleanup_called = True
            return cleanup(root)
        try:
            backend.action_hook("pilot.stop", recorded_cleanup, {"restore_sharing_settings": True})
        except Exception as error:
            # Stopping network sharing must not depend on a writable journal.
            # Never replay image work. Only retry entry into guarded cleanup
            # when the action hook never called it (e.g. SQLite/disk failure).
            stopped = not receipt_path.exists()
            cleanup_error = None
            if not cleanup_called:
                try:
                    cleanup(root)
                    stopped = True
                except Exception as stop_error:
                    cleanup_error = str(stop_error)
            return {"status": "outcome_unknown", "target": target, "model_verified": False,
                    "error": {"code": "pilot_cleanup_or_journal_failed", "message": str(error)},
                    "cleanup": {"sharing_stopped_and_settings_restored": stopped,
                                "receipt_retained": receipt_path.exists(), "error": cleanup_error},
                    "replay_allowed": False}
    # Album deletion can change C1's browser. Verify the final selected target
    # and exposed settings after cleanup, not only while Pilot is serving.
    after = saved_snapshot(backend, observation)
    if after["state"] != baseline:
        raise Refused("stale_preview", "Capture One settings changed during preview cleanup")
    from PIL import Image
    with Image.open(io.BytesIO(data)) as picture:
        width, height = picture.size
        has_icc = bool(picture.info.get("icc_profile"))
    directory = root / "native-previews"
    secure_directory(directory)
    preview_id = uuid.uuid4().hex
    path = directory / (preview_id + ".jpg")
    with open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        output.write(data)
    # Bounded cache, consistent with Windows's eight retained native previews.
    files = sorted(directory.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[8:]:
        old.unlink()
    return {"status": "completed", "source": "capture_one_pilot_render", "path": str(path),
            "preview_id": preview_id, "target": target, "after": after["observation"],
            "image": {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                      "width": width, "height": height, "format": "JPEG", "icc_present": has_icc},
            "render_state_digest": digest(baseline), "render_verified": True,
            "evidence": {"transport": "capture_pilot_loopback", "interface": "native_service_undocumented_wire_protocol",
                         "identity": "native_row_runtime_uuid_and_saved_variant_uuid_with_collection_position_and_path",
                         "variant_identity": pilot_identity,
                         "settings_checked_before_after": True, "sharing_stopped": True, "export_used": False,
                         "saved_state_checked_before_after": True, "mask_pixels_verified": False,
                         "freshness_scope": "native composite plus saved Session, COS and COMASK stability",
                         "atomic": False},
            "limits": "Composite preview, not a mask bitmap. Saved state stability is not an atomic host render revision "
                      "or proof of mask coverage. Avoid concurrent manual edits and pending AI/brush operations. "
                      "Pilot JPEG is returned unchanged; its color profile is not independently qualified."}
