"""Durable per-user receipts. SQLite commits precede every dispatched action."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from .contracts import Refused, canonical, digest

PENDING = {"dispatching", "partial", "outcome_unknown"}


def default_state_dir():
    if sys.platform == "darwin":
        from .macos_ipc import state_directory
        return state_directory()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "CaptureOneAI"


def scope(target):
    def path(part):
        value = (target or {}).get(part, {}).get("path")
        if not value:
            return None
        if (target or {}).get("platform") == "macos" or (target or {}).get("instance", {}).get("platform") == "macos":
            return str(value)
        return str(value).replace("/", "\\").casefold()
    return path("document"), path("image")


def same_scope(left, right):
    return all(scope(left)) and scope(left) == scope(right)


def affects_image(record):
    """Only unresolved photo edits, candidate creation and exports block writes."""
    if record.get("legacy"):
        return True
    request = record.get("request", {})
    command, action = request.get("command"), request.get("action")
    if command == "adjust":
        return True
    if command == "images":
        return action in {"clone", "create_candidate"}
    if command == "preview":
        return request.get("kind", "render") == "render" and action in {None, "render"}
    return command not in {"context", "read", "operations"}


class Journal:
    def __init__(self, state_dir=None):
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.state_dir / "operations.sqlite3", timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                request_hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (token TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        self.db.commit()

    @staticmethod
    def _observation_identity(observation):
        identity = {"target": observation["target"], "values": observation["values"]}
        if observation.get("module"):
            identity["module"] = observation["module"]
        if (observation.get("module") in {"layers", "masks", "color_mixer"}
                and observation["target"].get("instance", {}).get("platform") == "macos"):
            context = observation.get("native_context", {})
            identity["mac_bridge"] = {key: context.get(key) for key in
                ("bridge_target", "bridge_mask_actions", "bridge_layer_actions", "bridge_color_actions")}
        if (observation.get("module") == "color_mixer"
                and observation["target"].get("instance", {}).get("platform") == "macos"):
            # Skin Tone is absent from the public Basic/Advanced values. Bind
            # its complete saved value so a new read cannot overwrite an older
            # source token while retaining the same visible adjustment values.
            context = observation.get("native_context", {})
            saved = context.get("color_editor_snapshot", {})
            identity["mac_color_editor"] = {
                "saved": {key: saved.get(key) for key in ("source", "sha256", "available")},
                "layer_state": context.get("layer_state"),
                "native": context.get("native_color_snapshot"),
                "guard": context.get("color_native_guard"),
                "apple_event_state": context.get("apple_event_color_state"),
            }
        if (observation.get("module") == "masks"
                and observation["target"].get("instance", {}).get("platform") == "macos"):
            identity["mac_mask_parameters"] = observation.get("native_context", {}).get("mask_ax_parameters")
            identity["mac_gradient_view"] = observation.get("gradient_view")
            identity["mac_gradient_selection_point"] = observation.get("native_context", {}).get("gradient_selection_point")
        if (observation.get("module") == "layers"
                and observation["target"].get("instance", {}).get("platform") == "macos"):
            identity["mac_layer_parameters"] = observation.get("native_context", {}).get("duplicate_snapshot")
        return identity

    def observe(self, observation):
        token = digest(self._observation_identity(observation))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?)", (token, canonical(observation)))
        return token

    def observation(self, token):
        row = self.db.execute("SELECT payload FROM observations WHERE token=?", (token,)).fetchone()
        if row is None:
            raise Refused("unknown_observation", "Read current values before requesting an edit")
        result = json.loads(row[0])
        identity = self._observation_identity(result)
        if "mac_color_editor" in identity and token != digest(identity):
            # Older Mac tokens omitted Skin Tone and could already have been
            # overwritten by another read. They cannot safely name a source.
            raise Refused("legacy_color_editor_observation", "Read Color Editor again; this older token did not bind its complete saved value")
        if "mac_mask_parameters" in identity and (identity["mac_mask_parameters"] is None or token != digest(identity)):
            raise Refused("legacy_mask_observation", "Read masks again; this older token did not bind the complete saved parameters")
        if "mac_layer_parameters" in identity and (identity["mac_layer_parameters"] is None or token != digest(identity)):
            raise Refused("legacy_layer_observation", "Read layers again; this older token did not bind complete layer and mask contents")
        return result

    def get(self, operation_id=None, request_id=None):
        return self._get(self.db, operation_id, request_id)

    @staticmethod
    def _get(connection, operation_id=None, request_id=None):
        column, value = ("operation_id", operation_id) if operation_id else ("request_id", request_id)
        row = connection.execute(f"SELECT payload FROM operations WHERE {column}=?", (value,)).fetchone()
        if row is None:
            raise Refused("not_found", "Operation not found")
        record = json.loads(row[0])
        record["actions"] = [json.loads(item[0]) for item in connection.execute(
            "SELECT payload FROM actions WHERE operation_id=? ORDER BY sequence", (record["operation_id"],))]
        return record

    def list(self):
        return self._list(self.db)

    @staticmethod
    def _list(connection):
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM operations ORDER BY rowid DESC")]

    def peek(self, action, operation_id=None, request_id=None):
        """A short, consistent WAL snapshot; never use the writer connection."""
        if action not in {"get", "list"}:
            raise Refused("readonly_only", "Journal peek only supports get/list")
        path = (self.state_dir / "operations.sqlite3").resolve()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as reader:
            reader.execute("BEGIN")  # Both get queries see the same committed snapshot.
            return self._list(reader) if action == "list" else self._get(reader, operation_id, request_id)

    def pending(self, target, exclude=None):
        return [record for record in self.list() if record["operation_id"] != exclude
                and record["status"] in PENDING and affects_image(record)
                and (not scope(record.get("target"))[0] or not scope(target)[0]
                     or scope(record.get("target"))[0] == scope(target)[0])]

    def existing(self, request):
        row = self.db.execute("SELECT request_hash, payload FROM operations WHERE request_id=?",
                              (request.get("request_id"),)).fetchone()
        if row is None:
            return None
        if row[0] != digest(request):
            raise Refused("request_id_conflict", "This request_id already names different content")
        return self.get(operation_id=json.loads(row[1])["operation_id"])

    def begin(self, request, target, before):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.existing(request)
            if existing:
                self.db.commit()
                return existing, False
            mac_native=before.get('evidence',{}).get('source') == 'c1_applescript'
            native=(before.get('evidence',{}).get('source') in {'c1_internal_model', 'c1_applescript'}
                    and before.get('evidence',{}).get('model_verified') is True)
            def is_structural(req):
                # Returning to an observed Mac layer is needed for read-only
                # inspection of uncertain writes; selection changes no content.
                if (mac_native and req.get('command') == 'adjust'
                        and (req.get('module'), req.get('patch', {}).get('action'))
                        in {('layers', 'select'), ('masks', 'select_submask')}):
                    return False
                return req.get('command') == 'adjust' and (
                    req.get('module') in {'layers','masks'} and req.get('patch',{}).get('action') != 'set_opacity'
                    or mac_native and req.get('module') == 'color_mixer'
                    and (req.get('patch',{}).get('mode') == 'advanced'
                         and req.get('patch',{}).get('action') in {'add','remove'}
                         or req.get('patch',{}).get('action') in {'reset','paste'}))
            structural=is_structural(request)
            selection = mac_native and (
                request.get('command') == 'images' and request.get('action') in {'select', 'next', 'previous'}
                or request.get('command') == 'adjust'
                and (request.get('module'), request.get('patch', {}).get('action'))
                in {('layers', 'select'), ('masks', 'select_submask')})
            pending=self.pending(target) if selection or affects_image({'request':request}) else []
            if selection:
                # An unfinished native mask/AI action may still attach its
                # result to the active layer. Selecting elsewhere is unsafe
                # until that operation is reconciled, even though selection
                # itself changes no saved content.
                for item in pending:
                    req = item.get('request', {})
                    if (item.get('target', {}).get('instance') == target.get('instance')
                            and req.get('command') == 'adjust'
                            and req.get('module') == 'masks'
                            and req.get('patch', {}).get('action') in {'create_subject', 'create_background', 'combine_ai'}):
                        raise Refused('pending_operation', 'Resolve the unfinished mask action before changing the selected photo, layer or submask')
                if request.get('command') == 'images':
                    pending = []  # Ordinary unresolved edits do not block navigation.
            if native and request.get('command')=='adjust':
                pending=[r for r in pending if structural and r.get('target',{}).get('instance')==target.get('instance')
                    and r.get('target',{}).get('variant',{}).get('id')==target.get('variant',{}).get('id')
                    and is_structural(r.get('request',{}))]
            elif native and request.get('command')=='images' and request.get('action') in {'clone','create_candidate'}:
                pending=[r for r in pending if r.get('target',{}).get('instance')==target.get('instance')
                    and r.get('target',{}).get('image')==target.get('image')
                    and ((r.get('request',{}).get('command')=='images'
                          and r.get('request',{}).get('action') in {'clone','create_candidate'})
                         or (r.get('target',{}).get('variant',{}).get('id')==target.get('variant',{}).get('id')
                             and is_structural(r.get('request',{}))))]
            if pending:raise Refused('pending_operation','Resolve the unfinished structural action on this photo before creating another candidate or layer')
            record = {"operation_id": uuid.uuid4().hex, "request_id": request["request_id"],
                      "request_hash": digest(request), "request": request, "target": target,
                      "before": before, "after": before, "status": "dispatching", "created_at": time.time()}
            self.db.execute("INSERT INTO operations VALUES (?, ?, ?, ?)",
                            (record["operation_id"], record["request_id"], record["request_hash"], canonical(record)))
            self.db.commit()
            return record, True
        except BaseException:
            self.db.rollback()
            raise

    def update(self, operation_id, **changes):
        record = self.get(operation_id=operation_id)
        record.pop("actions", None)
        record.update(changes)
        with self.db:
            self.db.execute("UPDATE operations SET payload=? WHERE operation_id=?", (canonical(record), operation_id))
        return self.get(operation_id=operation_id)

    def start_action(self, operation_id, name, intent):
        action = {"action_id": uuid.uuid4().hex, "operation_id": operation_id,
                  "name": name, "intent": intent, "status": "dispatching", "started_at": time.time()}
        with self.db:
            sequence = self.db.execute("SELECT COUNT(*) FROM actions WHERE operation_id=?", (operation_id,)).fetchone()[0]
            self.db.execute("INSERT INTO actions VALUES (?, ?, ?, ?)",
                            (action["action_id"], operation_id, sequence, canonical(action)))
        return action

    def finish_action(self, action, **changes):
        action.update(changes)
        with self.db:
            self.db.execute("UPDATE actions SET payload=? WHERE action_id=?", (canonical(action), action["action_id"]))

    def import_legacy(self, record, source_path, source_sha256=None):
        raw = Path(source_path).read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if source_sha256 and source_sha256.casefold() != actual_hash:
            raise Refused("source_hash_mismatch", "Legacy source hash does not match")
        operation_id = record["operation_id"]
        try:
            return self.get(operation_id=operation_id)
        except Refused as error:
            if error.code != "not_found":
                raise
        imported = dict(record, operation_id=operation_id, request_id="legacy:" + operation_id,
                        request_hash=digest(record), status="outcome_unknown", legacy=True,
                        source_path=str(Path(source_path).resolve()), source_sha256=actual_hash)
        with self.db:
            self.db.execute("INSERT INTO operations VALUES (?, ?, ?, ?)",
                            (operation_id, imported["request_id"], imported["request_hash"], canonical(imported)))
        return self.get(operation_id=operation_id)

    def close(self):
        self.db.close()
