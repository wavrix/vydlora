"""Account-scoped, offline-capable MEGA sync using immutable device snapshots.

Each field has a logical-clock revision; unrelated edits merge independently.
Deletions have durable tombstones. Only this device's old snapshots are pruned,
after a replacement has uploaded. Credentials for MEGA itself and local paths
never travel. MEGAcmd encrypts the uploaded application data in the account.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Float, Integer, String, event, select

from .. import models, repositories as repo
from ..core.config import settings as cfg
from ..core.database import SessionLocal
from ..core.events import publish
from ..core.logging_config import get_logger
from . import cookie_service, mega_service as mega, settings_service

log = get_logger("cloud-sync")
from ..core.compat import CLOUD_SNAPSHOT_FOLDER
REMOTE_FOLDER = CLOUD_SNAPSHOT_FOLDER
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
SNAPSHOT_NAME = re.compile(r"([a-f0-9]{32})-([0-9]{20})-([a-f0-9]{8})\.json\.gz\Z")
BOUND_ACCOUNT = mega.BOUND_ACCOUNT_KEY
DEVICE_KEY = "_cloud_device"
KINDS = {
    "users": models.User, "posts": models.Post, "downloads": models.Download,
    "queue": models.QueueJob, "activity": models.ActivityEvent,
    "telegram": models.TelegramMessage, "settings": models.Setting,
    "cookies": models.CookieProfile, "logs": models.LogEntry,
    "notifications": models.Notification,
}
ORDER = ("users", "settings", "cookies", "posts", "downloads", "queue",
         "activity", "telegram", "logs", "notifications", "files")
SYNC_SETTINGS = set(settings_service.DEFAULTS) | {"bot_token", "telegram_allowed_ids"}
FILE_KEYS = {"shortcuts", "instagram-cookies", "x-cookies"}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def data(payload: dict) -> dict | None:
    if not payload["alive"]["v"]:
        return None
    return {name: field["v"] for name, field in payload["fields"].items()}


def merge(left: dict, right: dict) -> dict:
    result = copy.deepcopy(left)
    if tuple(right["alive"]["r"]) > tuple(left["alive"]["r"]):
        result["alive"] = copy.deepcopy(right["alive"])
    for name, field in right["fields"].items():
        if name not in result["fields"] or tuple(field["r"]) > tuple(result["fields"][name]["r"]):
            result["fields"][name] = copy.deepcopy(field)
    return result


def fields_for(kind: str) -> set[str]:
    if kind == "files":
        return {"name", "content"}
    result = {column.name for column in KINDS[kind].__table__.columns} - {"id", "user_id", "file_path", "updated_at"}
    if "user_id" in KINDS[kind].__table__.columns:
        result.add("user_key")
    if kind == "cookies":
        result.add("content")
    if kind == "queue":
        result.add("owner")
    return result


def validate_document(document: dict, account: str) -> dict:
    """Validate the entire remote snapshot before changing any local data."""
    if not isinstance(document, dict) or document.get("version") != 1 or document.get("account") != account:
        raise ValueError("This cloud backup has an unsupported format or belongs to another account.")
    records = document.get("records")
    if not isinstance(records, dict):
        raise ValueError("This cloud backup is incomplete.")
    for key, item in records.items():
        if not isinstance(key, str) or not isinstance(item, dict) or item.get("kind") not in ORDER:
            raise ValueError("This cloud backup contains an invalid record.")
        kind, payload = item["kind"], item.get("payload")
        if not key.startswith(kind + ":") or not isinstance(payload, dict):
            raise ValueError("This cloud backup contains an invalid key.")
        if not isinstance(payload.get("alive"), dict) or not isinstance(payload.get("fields"), dict):
            raise ValueError("This cloud backup contains an invalid revision.")
        if type(payload["alive"].get("v")) is not bool:
            raise ValueError("This cloud backup contains an invalid deletion.")
        for field in [payload["alive"], *payload["fields"].values()]:
            revision = field.get("r") if isinstance(field, dict) else None
            if not isinstance(revision, list) or len(revision) != 2 or type(revision[0]) is not int or not 0 <= revision[0] < 2**63 or not isinstance(revision[1], str) or not re.fullmatch(r"[a-f0-9]{32}", revision[1]):
                raise ValueError("This cloud backup contains an invalid revision.")
        if set(payload["fields"]) != fields_for(kind):
            raise ValueError("This cloud backup contains unsupported fields. Update Vydlora first.")
        values = {name: field["v"] for name, field in payload["fields"].items()}
        if kind == "settings" and values.get("key") not in SYNC_SETTINGS:
            raise ValueError("This cloud backup contains a device-only setting.")
        if kind == "files" and values.get("name") not in FILE_KEYS:
            raise ValueError("This cloud backup contains an unsupported file.")
        natural = {"users": "state_key", "settings": "key", "cookies": "name", "files": "name"}.get(kind)
        expected = kind + ":" + values[natural] if natural else (
            "posts:" + canonical([values["user_key"], values["video_id"]]) if kind == "posts" else key)
        if expected != key or (natural is None and kind != "posts" and not re.fullmatch(kind + r":[a-f0-9]{32}", key)):
            raise ValueError("This cloud backup contains a mismatched record key.")
        if kind == "files" and values["name"] == "shortcuts":
            shortcuts = json.loads(values.get("content", ""))
            if not isinstance(shortcuts, list):
                raise ValueError("This cloud backup contains invalid shortcuts.")
        for name, value in values.items():
            if kind == "files" or name in ("content", "owner", "user_key"):
                if value is not None and not isinstance(value, str):
                    raise ValueError("This cloud backup contains invalid text.")
                continue
            column = KINDS[kind].__table__.columns[name]
            if value is None:
                if not column.nullable:
                    raise ValueError("This cloud backup is missing a required value.")
            elif isinstance(column.type, DateTime):
                datetime.fromisoformat(value)
            elif isinstance(column.type, Boolean):
                if type(value) is not bool:
                    raise ValueError("This cloud backup contains an invalid switch.")
            elif isinstance(column.type, Integer):
                if type(value) is not int or not -(2**63) <= value < 2**63:
                    raise ValueError("This cloud backup contains an invalid number.")
            elif isinstance(column.type, Float):
                if type(value) not in (int, float):
                    raise ValueError("This cloud backup contains an invalid number.")
            elif isinstance(column.type, String) and not isinstance(value, str):
                raise ValueError("This cloud backup contains invalid text.")
        if kind == "settings":
            value = json.loads(values["value"])
            from ..schemas import SettingsUpdate
            if values["key"] in SettingsUpdate.model_fields:
                SettingsUpdate.model_validate({values["key"]: value}, strict=True)
            elif values["key"] in settings_service.DEFAULTS and type(value) is not type(settings_service.DEFAULTS[values["key"]]):
                raise ValueError("This cloud backup contains an invalid preference.")
            elif values["key"] in ("bot_token", "telegram_allowed_ids") and not isinstance(value, str):
                raise ValueError("This cloud backup contains invalid account settings.")
    return records


class CloudSync:
    def __init__(self, session_factory=SessionLocal, *, data_dir: Path | None = None,
                 cookies_dir: Path | None = None):
        self.sessions = session_factory
        self.data_dir = data_dir or Path(os.environ.get("LORA1_DATA_DIR") or cfg.cookies_path.parent)
        self.cookies_dir = cookies_dir or cookie_service.COOKIES_DIR
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._startup_thread = None
        self._seen: set[str] = set()
        self._uploaded = ""
        self._clock = 0
        self._state = {"state": "disconnected", "last_synced": None, "error": "", "pending_uploads": 0, "primary_device": True}

    @property
    def disabled(self) -> bool:
        return os.environ.get("LORA1_CLOUD_SYNC_DISABLED") == "1"

    def status(self) -> dict:
        return dict(self._state)

    def _status(self, state: str, **values) -> None:
        self._state = {**self._state, "state": state, **values}
        publish("cloud_sync", self.status())

    def request(self) -> dict:
        self._wake.set()
        return self.status()

    def can_automate(self, db) -> bool:
        from .account_profile_service import switching
        if switching():
            return False
        primary = repo.Settings.get(db, "automation_device", "")
        device = repo.Settings.get(db, DEVICE_KEY, "")
        return not primary or primary == device

    def take_over(self) -> dict:
        with self.sessions() as db:
            _, device = self._identity(db)
            repo.Settings.set(db, "automation_device", device)
            db.commit()
        self._status(self._state["state"], primary_device=True)
        self._apply_runtime(telegram_changed=True)
        return self.request()

    def start(self) -> None:
        self._stop.clear()
        self._start()

    def start_background(self, on_ready) -> None:
        """Keep the API responsive while restoring, before starting workers."""
        self._stop.clear()
        self._startup_thread = threading.Thread(
            target=self._start, args=(on_ready,), name="cloud-restore", daemon=True)
        self._startup_thread.start()

    def _start(self, on_ready=None) -> None:
        if self._stop.is_set():
            return
        if self.disabled:
            self._status("disabled")
            if on_ready and not self._stop.is_set():
                on_ready()
            return
        # Restore before workers start scanning/enqueuing against an empty DB.
        # Large media transfers belong to the later background cycles.
        self.sync_once(include_media=False)
        if self._stop.is_set():
            return
        if not event.contains(SessionLocal.class_, "after_flush", _mark_changes):
            event.listen(SessionLocal.class_, "after_flush", _mark_changes)
            event.listen(SessionLocal.class_, "after_commit", _request_sync)
            event.listen(SessionLocal.class_, "after_rollback", _discard_changes)
        self._thread = threading.Thread(target=self._loop, name="cloud-sync", daemon=True)
        self._thread.start()
        if on_ready and not self._stop.is_set():
            on_ready()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in (self._thread, self._startup_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(30)
            self._wake.clear()
            if self._stop.wait(2):
                return
            if not self._stop.is_set():
                self.sync_once()

    def _revision(self) -> list:
        self._clock = max(self._clock + 1, time.time_ns() // 1000)
        return [self._clock, self.device]

    def _identity(self, db) -> tuple[str, str]:
        account = repo.Settings.get(db, BOUND_ACCOUNT, "")
        device = repo.Settings.get(db, DEVICE_KEY, "")
        if not re.fullmatch(r"[a-f0-9]{32}", device):
            device = uuid.uuid4().hex
            repo.Settings.set(db, DEVICE_KEY, device)
        return account, device

    @contextmanager
    def _db(self, *, freeze=False):
        with self.sessions() as db:
            db.info["cloud_sync"] = True
            if freeze and db.bind.dialect.name == "sqlite":
                # Local edits cannot commit between capture and merge, otherwise
                # a field edited in that gap could be overwritten by an import.
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield db

    @contextmanager
    def _file_transaction(self, db, entries, changed):
        backups = {}
        for key in changed:
            entry = entries[key]
            values = data(json.loads(entry.payload))
            if entry.kind == "files":
                target = self._file_path(key.split(":", 1)[1])
            elif entry.kind == "cookies":
                if values is not None:
                    target = self.cookies_dir / ("cloud-" + hashlib.sha256(values["name"].encode()).hexdigest()[:24] + ".txt")
                    row = self._row(db, entry, values)
                    if row and self.cookies_dir.resolve() in Path(row.file_path).resolve().parents:
                        old = Path(row.file_path)
                        backups[old] = old.read_bytes() if old.is_file() else None
                else:
                    row = self._row(db, entry, None)
                    if not row or self.cookies_dir.resolve() not in Path(row.file_path).resolve().parents:
                        continue
                    target = Path(row.file_path)
            else:
                continue
            backups[target] = target.read_bytes() if target.is_file() else None
        try:
            yield
        except Exception:
            for target, original in backups.items():
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(original)
            raise

    def _file_path(self, name: str) -> Path:
        return self.data_dir / "keyboard-shortcuts.json" if name == "shortcuts" else self.cookies_dir / ("x.txt" if name == "x-cookies" else "instagram.txt")

    def _snapshot(self, db, entries: dict) -> dict:
        snapshot = {}
        users = {row.id: row.state_key for row in db.scalars(select(models.User))}
        by_local = {(entry.kind, entry.local_id): entry for entry in entries.values() if entry.local_id is not None}
        for kind, model in KINDS.items():
            for row in db.scalars(select(model)):
                if kind == "settings" and row.key not in SYNC_SETTINGS:
                    continue
                values = {}
                for column in model.__table__.columns:
                    if column.name in ("id", "user_id", "file_path", "updated_at"):
                        continue
                    value = getattr(row, column.name)
                    values[column.name] = value.isoformat() if isinstance(value, datetime) else value
                if "user_id" in model.__table__.columns:
                    values["user_key"] = users.get(row.user_id)
                if kind == "users":
                    key = kind + ":" + row.state_key
                elif kind == "posts":
                    key = kind + ":" + canonical([users.get(row.user_id), row.video_id])
                elif kind == "settings":
                    key = kind + ":" + row.key
                elif kind == "cookies":
                    key = kind + ":" + row.name
                    path = Path(row.file_path)
                    values["content"] = path.read_text(encoding="utf-8") if path.is_file() else ""
                else:
                    known = by_local.get((kind, row.id))
                    # SQLite can reuse the largest deleted id. A different creation
                    # time is a new item, not an edit of the deleted cloud record.
                    if known and known.baseline and json.loads(known.baseline).get("created_at") != values.get("created_at"):
                        known = None
                    key = known.key if known else kind + ":" + uuid.uuid4().hex
                if kind == "queue":
                    previous = json.loads(entries[key].baseline) if key in entries and entries[key].baseline else {}
                    values["owner"] = previous.get("owner", self.device)
                    if previous.get("status") == "paused" and values["status"] == "pending":
                        values["owner"] = self.device
                snapshot[key] = (kind, row.id, values)
        for name in FILE_KEYS:
            path = self._file_path(name)
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                if name == "shortcuts":
                    content = canonical(json.loads(content))
                snapshot["files:" + name] = ("files", None, {"name": name, "content": content})
        return snapshot

    def _entries(self, db, account: str) -> dict:
        entries = {entry.key: entry for entry in db.scalars(select(models.CloudRecord).where(models.CloudRecord.account == account))}
        for entry in entries.values():
            payload = json.loads(entry.payload)
            self._clock = max(self._clock, payload["alive"]["r"][0], *(field["r"][0] for field in payload["fields"].values()))
        return entries

    def capture(self, db, account: str) -> dict:
        entries = self._entries(db, account)
        snapshot = self._snapshot(db, entries)
        for key, (kind, local_id, values) in snapshot.items():
            entry = entries.get(key)
            if entry is None:
                # Fresh-device seeded defaults yield to the saved account values.
                seeded = kind == "settings" and json.loads(values["value"]) == settings_service.DEFAULTS.get(values["key"], object())
                revision = [0, self.device] if seeded else self._revision()
                payload = {"alive": {"v": True, "r": revision},
                           "fields": {name: {"v": value, "r": revision} for name, value in values.items()}}
                entry = models.CloudRecord(account=account, key=key, kind=kind, local_id=local_id,
                                           payload=canonical(payload), baseline=canonical(values))
                db.add(entry)
                entries[key] = entry
            elif entry.baseline != canonical(values):
                payload = json.loads(entry.payload)
                baseline = json.loads(entry.baseline) if entry.baseline else {}
                revision = self._revision()
                if not payload["alive"]["v"]:
                    payload["alive"] = {"v": True, "r": revision}
                for name, value in values.items():
                    if name not in baseline or value != baseline[name]:
                        payload["fields"][name] = {"v": value, "r": revision}
                entry.payload = canonical(payload)
                entry.baseline = canonical(values)
                entry.local_id = local_id
        for key, entry in entries.items():
            if key not in snapshot and entry.baseline is not None:
                payload = json.loads(entry.payload)
                payload["alive"] = {"v": False, "r": self._revision()}
                entry.payload, entry.baseline, entry.local_id = canonical(payload), None, None
        db.flush()
        return entries

    def _row(self, db, entry, values):
        model = KINDS[entry.kind]
        row = db.get(model, entry.local_id) if entry.local_id is not None else None
        if row is None and values is not None:
            if entry.kind == "users":
                row = db.scalar(select(model).where(model.state_key == values["state_key"]))
            elif entry.kind == "settings":
                row = db.scalar(select(model).where(model.key == values["key"]))
            elif entry.kind == "cookies":
                row = db.scalar(select(model).where(model.name == values["name"]))
            elif entry.kind == "posts":
                user = db.scalar(select(models.User).where(models.User.state_key == values["user_key"]))
                if user:
                    row = db.scalar(select(model).where(model.user_id == user.id, model.video_id == values["video_id"]))
        return row

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".cloud-tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def apply(self, db, entries: dict, changed: set[str]) -> bool:
        # Child deletions precede their parent; parent insertions precede children.
        for kind in reversed(ORDER):
            for key in changed:
                entry = entries[key]
                payload = json.loads(entry.payload)
                if entry.kind != kind or payload["alive"]["v"]:
                    continue
                if kind == "files":
                    self._file_path(key.split(":", 1)[1]).unlink(missing_ok=True)
                else:
                    row = self._row(db, entry, None)
                    if row:
                        if kind == "cookies":
                            path = Path(row.file_path).resolve()
                            if self.cookies_dir.resolve() in path.parents:
                                path.unlink(missing_ok=True)
                        db.delete(row)
                entry.local_id, entry.baseline = None, None
            db.flush()
        for kind in ORDER:
            for key in changed:
                entry = entries[key]
                values = data(json.loads(entry.payload))
                if entry.kind != kind or values is None:
                    continue
                if kind == "files":
                    self._write(self._file_path(values["name"]), values["content"])
                    entry.baseline = canonical(values)
                    continue
                row = self._row(db, entry, values)
                if row is None:
                    row = KINDS[kind]()
                    db.add(row)
                for name, value in values.items():
                    if name in ("user_key", "content", "owner"):
                        continue
                    column = KINDS[kind].__table__.columns[name]
                    setattr(row, name, datetime.fromisoformat(value) if value is not None and isinstance(column.type, DateTime) else value)
                if "user_id" in KINDS[kind].__table__.columns:
                    user = db.scalar(select(models.User).where(models.User.state_key == values.get("user_key")))
                    if not user and kind in ("posts", "downloads"):
                        db.expunge(row) if row in db.new else db.delete(row)
                        entry.local_id, entry.baseline = None, None
                        continue
                    row.user_id = user.id if user else None
                if kind == "cookies":
                    old = Path(row.file_path) if row.file_path else None
                    path = self.cookies_dir / ("cloud-" + hashlib.sha256(values["name"].encode()).hexdigest()[:24] + ".txt")
                    self._write(path, values["content"])
                    row.file_path = str(path)
                    if old and old.resolve() != path.resolve() and self.cookies_dir.resolve() in old.resolve().parents:
                        old.unlink(missing_ok=True)
                if kind == "queue" and values["owner"] != self.device and row.status in ("pending", "processing"):
                    # A restored queue is recoverable without executing another
                    # device's in-flight downloads a second time.
                    row.status, row.worker, row.progress = "paused", None, 0
                    values = {**values, "status": "paused", "worker": None, "progress": 0}
                db.flush()
                entry.local_id, entry.baseline = row.id, canonical(values)
        # Multiple offline devices can each select a different cookie. Resolve
        # the active profile by revision rather than leaving two jars active.
        active = [entry for entry in entries.values() if entry.kind == "cookies" and data(json.loads(entry.payload)) and data(json.loads(entry.payload)).get("active")]
        if active:
            selected = max(active, key=lambda entry: tuple(json.loads(entry.payload)["fields"]["active"]["r"])).local_id
            for row in db.scalars(select(models.CookieProfile)):
                row.active = row.id == selected
            for entry in active:
                if entry.baseline:
                    baseline = json.loads(entry.baseline)
                    baseline["active"] = entry.local_id == selected
                    entry.baseline = canonical(baseline)
        db.flush()
        return bool(changed)

    def _remote_documents(self, account: str, directory: Path) -> tuple[list[dict], list[str]]:
        result = mega._run(["ls", REMOTE_FOLDER], timeout=30)
        if not result.ok:
            # MEGAcmd can return "Folder already exists" for mkdir -p on a
            # repeated sync. List first, and verify after creation so another
            # device creating the same folder concurrently is harmless too.
            mega._run(["mkdir", "-p", REMOTE_FOLDER], timeout=30)
            result = mega._run(["ls", REMOTE_FOLDER], timeout=30)
        if not result.ok:
            raise mega.MegaError(result.message)
        names = [line.strip() for line in result.out.splitlines() if SNAPSHOT_NAME.fullmatch(line.strip())]
        latest = {}
        for name in names:
            device = SNAPSHOT_NAME.fullmatch(name)[1]
            latest[device] = max(name, latest.get(device, ""))
        documents = []
        for name in latest.values():
            if account + ":" + name in self._seen:
                continue
            result = mega._run(["get", REMOTE_FOLDER + "/" + name, str(directory)], timeout=120)
            if not result.ok:
                raise mega.MegaError(result.message)
            path = directory / name
            if not path.is_file() or path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise ValueError("The cloud backup is missing or too large to read.")
            with gzip.open(path, "rb") as stream:
                expanded = stream.read(MAX_EXPANDED_BYTES + 1)
            if len(expanded) > MAX_EXPANDED_BYTES:
                raise ValueError("The cloud backup is too large to restore.")
            document = json.loads(expanded)
            validate_document(document, account)
            documents.append(document)
        return documents, names

    def _backup_media(self, account: str, *, upload=True) -> tuple[int, int]:
        with self._db() as db:
            rows = db.scalars(select(models.Download).where(models.Download.remote_path.is_(None), models.Download.status == "completed").order_by(models.Download.id)).all()
            available = [row for row in rows if row.file_path and Path(row.file_path).exists()]
            uploaded = 0
            for row in available[:3] if upload else []:
                if self._stop.is_set():
                    break
                path = Path(row.file_path)
                remote = mega.upload(db, path, mega.remote_dir_for(db, path.parent))
                row.remote_path = remote
                db.commit()
                uploaded += 1
            return len(available) - uploaded, len(rows) - len(available)

    def sync_once(self, *, include_media=True) -> dict:
        if self.disabled:
            self._status("disabled")
            return self.status()
        with self._lock, mega._lock:
            from .account_profile_service import switching
            if switching():
                return self.status()
            try:
                with self._db() as db:
                    bound, self.device = self._identity(db)
                    if not mega.ensure_session(db):
                        if bound:
                            self.capture(db, bound)
                        db.commit()
                        self._status("disconnected", error="")
                        return self.status()
                    account = mega.whoami(cached=False).strip().lower()
                    if not account:
                        raise mega.MegaError("Unable to read the connected MEGA account.")
                    if bound and bound != account:
                        self._status("error", error="Reconnect the MEGA account used to sync this device's data.")
                        return self.status()
                    repo.Settings.set(db, BOUND_ACCOUNT, account)
                    db.commit()
                self._status("syncing", error="")
                self.data_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="cloud-sync-", dir=self.data_dir) as temporary:
                    directory = Path(temporary)
                    documents, names = self._remote_documents(account, directory)
                    with self._db(freeze=True) as db:
                        entries = self.capture(db, account)
                        changed = set()
                        for document in documents:
                            for key, item in document["records"].items():
                                entry = entries.get(key)
                                if entry:
                                    combined = canonical(merge(json.loads(entry.payload), item["payload"]))
                                    if combined != entry.payload:
                                        entry.payload = combined
                                        changed.add(key)
                                else:
                                    entry = models.CloudRecord(account=account, key=key, kind=item["kind"], payload=canonical(item["payload"]))
                                    db.add(entry)
                                    entries[key] = entry
                                    changed.add(key)
                        changed.update(key for key, entry in entries.items() if entry.baseline is None and data(json.loads(entry.payload)) is not None)
                        with self._file_transaction(db, entries, changed):
                            self.apply(db, entries, changed)
                            for minimum, maximum in (("scan_spacing_min", "scan_spacing_max"), ("tikdownloader_min_interval", "tikdownloader_max_interval")):
                                low, high = settings_service.get(db, minimum), settings_service.get(db, maximum)
                                if high < low:
                                    repo.Settings.set(db, maximum, low)
                            if not repo.Settings.get(db, "automation_device", ""):
                                repo.Settings.set(db, "automation_device", self.device)
                            primary = self.can_automate(db)
                            db.commit()
                    if changed:
                        publish("cloud_data", {})
                        if any(key.startswith("settings:") for key in changed):
                            self._apply_runtime(telegram_changed=any(key in changed for key in ("settings:bot_token", "settings:telegram_allowed_ids", "settings:automation_device")))
                    # Save metadata before slower media transfers. A failed media
                    # upload must not hold the creator/settings backup hostage.
                    with self._db(freeze=True) as db:
                        entries = self.capture(db, account)
                        encoded = canonical({"version": 1, "account": account,
                            "records": {key: {"kind": entry.kind, "payload": json.loads(entry.payload)} for key, entry in entries.items()}})
                        db.commit()
                    names = self._upload_document(account, encoded, directory, names)
                    self._seen = {account + ":" + name for name in names}
                    # Upload prior local-only media as well as new downloads. This
                    # backup retains local files; the existing storage preference
                    # still controls immediate offloading after a download.
                    pending, waiting = self._backup_media(account, upload=include_media)
                    with self._db(freeze=True) as db:
                        entries = self.capture(db, account)
                        document = {"version": 1, "account": account,
                                    "records": {key: {"kind": entry.kind, "payload": json.loads(entry.payload)} for key, entry in entries.items()}}
                        encoded = canonical(document)
                        db.commit()
                    names = self._upload_document(account, encoded, directory, names)
                    self._seen = {account + ":" + name for name in names}
                self._status("uploading" if pending else "waiting" if waiting else "synced", error="", pending_uploads=pending + waiting,
                             last_synced=datetime.now(timezone.utc).isoformat(), primary_device=primary)
                if pending:
                    self._wake.set()
            except Exception as exc:
                # Failed/corrupt downloads never result in a replacing upload.
                # The local journal survives, so offline edits retry next cycle.
                log.warning("Cloud sync will retry: %s", exc)
                self._status("error", error="Couldn't sync with MEGA. Your local data is safe; we'll retry automatically.")
            return self.status()

    def _upload_document(self, account, encoded, directory, names):
        if len(encoded.encode("utf-8")) > MAX_EXPANDED_BYTES:
            raise ValueError("The application backup exceeds the supported size.")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if account + digest == self._uploaded:
            return names
        name = f"{self.device}-{self._revision()[0]:020d}-{uuid.uuid4().hex[:8]}.json.gz"
        path = directory / name
        compressed = gzip.compress(encoded.encode("utf-8"), mtime=0)
        if len(compressed) > MAX_DOCUMENT_BYTES:
            raise ValueError("The application backup exceeds the supported size.")
        path.write_bytes(compressed)
        result = mega._run(["put", "-c", str(path), REMOTE_FOLDER + "/"], timeout=120)
        if not result.ok:
            raise mega.MegaError(result.message)
        self._uploaded = account + digest
        names = [*names, name]
        own = sorted((value for value in names if value.startswith(self.device + "-")), reverse=True)
        for old in own[3:]:
            mega._run(["rm", "-f", REMOTE_FOLDER + "/" + old], timeout=30)
        return names

    def _apply_runtime(self, telegram_changed=False) -> None:
        from ..workers.manager import manager_instance
        if not manager_instance._running:
            return
        with self.sessions() as db:
            values = settings_service.get_all(db)
        manager_instance.resize(int(values["download_workers"]))
        manager_instance.reschedule(int(values["scan_interval"]))
        manager_instance.reschedule_stories(int(values["story_scan_interval"]))
        manager_instance.reschedule_instagram(int(values["instagram_scan_interval"]))
        manager_instance.reschedule_x(int(values["x_scan_interval"]))
        from ..tiktok import downloader
        downloader.set_min_interval(float(values["tikdownloader_min_interval"]))
        downloader.set_max_interval(float(values["tikdownloader_max_interval"]))
        downloader.set_max_retries(int(values["tikdownloader_max_retries"]))
        if telegram_changed:
            from . import telegram_service
            telegram_service.restart()


def _mark_changes(db, _):
    if db.info.get("cloud_sync"):
        return
    for row in db.new | db.dirty | db.deleted:
        if type(row) in KINDS.values() and (not isinstance(row, models.Setting) or row.key in SYNC_SETTINGS):
            db.info["cloud_dirty"] = True
            break


def _request_sync(db):
    if db.info.pop("cloud_dirty", False):
        sync.request()


def _discard_changes(db):
    db.info.pop("cloud_dirty", None)


sync = CloudSync()
