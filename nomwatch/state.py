"""Transactional local state, migrations, and private secret references."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from .paths import NomWatchPaths

SCHEMA_VERSION = 1
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id INTEGER PRIMARY KEY, application_version TEXT NOT NULL,
  applied_at REAL NOT NULL, checksum TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS installation (
  id TEXT PRIMARY KEY, display_name TEXT NOT NULL, mdns_slug TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('viewer','operator','owner')),
  disabled_at REAL, session_version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, token_digest TEXT NOT NULL UNIQUE, csrf_digest TEXT NOT NULL,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  user_session_version INTEGER NOT NULL, origin_class TEXT NOT NULL,
  issued_at REAL NOT NULL, last_seen REAL NOT NULL, idle_expires_at REAL NOT NULL,
  absolute_expires_at REAL NOT NULL, reauthenticated_at REAL,
  revoked_at REAL, client_meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS activation_tokens (
  id TEXT PRIMARY KEY, token_digest TEXT NOT NULL UNIQUE, purpose TEXT NOT NULL,
  role TEXT, target_user_id TEXT, expires_at REAL NOT NULL, consumed_at REAL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, source TEXT NOT NULL,
  attempted_at REAL NOT NULL, success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(username, source, attempted_at);
CREATE TABLE IF NOT EXISTS secret_refs (
  id TEXT PRIMARY KEY, purpose TEXT NOT NULL, created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cameras (
  id TEXT PRIMARY KEY, display_name TEXT NOT NULL, host TEXT NOT NULL,
  port INTEGER NOT NULL, stream_path TEXT NOT NULL, username TEXT NOT NULL,
  password_secret_ref TEXT REFERENCES secret_refs(id), enabled INTEGER NOT NULL DEFAULT 1,
  revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
  namespace TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
  value_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, legacy_source_id TEXT UNIQUE, camera_id TEXT REFERENCES cameras(id),
  occurred_at REAL NOT NULL, confidence REAL, reason TEXT, status TEXT NOT NULL,
  error_summary TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media_files (
  id TEXT PRIMARY KEY, event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
  kind TEXT NOT NULL, relative_path TEXT NOT NULL, mime_type TEXT, size INTEGER,
  checksum TEXT, retention_state TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  next_run_at REAL NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  terminal_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_access (
  id INTEGER PRIMARY KEY CHECK(id=1), desired_enabled INTEGER NOT NULL DEFAULT 0,
  owned_https_port INTEGER, owned_target TEXT, node_identity TEXT,
  tailnet_identity TEXT, status_fingerprint TEXT, observed_url TEXT,
  status TEXT NOT NULL DEFAULT 'not_installed', error TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id TEXT, action TEXT NOT NULL,
  object_id TEXT, result TEXT NOT NULL, source_class TEXT NOT NULL,
  created_at REAL NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


class StateError(RuntimeError):
    pass


class ClosingConnection(sqlite3.Connection):
    """sqlite3's context manager commits but does not close; this one does both."""

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class LocalState:
    def __init__(self, paths: NomWatchPaths, *, clock=time.time):
        self.paths = paths
        self.clock = clock
        self.paths.ensure_private()
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.paths.database, timeout=5.0, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        db_existed = self.paths.database.exists()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if existing:
                current = conn.execute("SELECT COALESCE(MAX(migration_id),0) FROM schema_migrations").fetchone()[0]
                if current > SCHEMA_VERSION:
                    raise StateError(f"database schema {current} is newer than supported {SCHEMA_VERSION}")
            conn.executescript(SCHEMA_SQL)
            checksum = hashlib.sha256(SCHEMA_SQL.encode()).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?,?)",
                (1, self.application_version(), self.clock(), checksum),
            )
            row = conn.execute("SELECT id FROM installation LIMIT 1").fetchone()
            if not row:
                install_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO installation VALUES (?,?,?,?)",
                    (install_id, "NomWatch", f"nomwatch-{install_id[:4]}", self.clock()),
                )
            conn.execute(
                "INSERT OR IGNORE INTO remote_access(id,updated_at) VALUES (1,?)", (self.clock(),)
            )
        os.chmod(self.paths.database, stat.S_IRUSR | stat.S_IWUSR)
        if not db_existed:
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.paths.database) + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def application_version() -> str:
        from . import __version__
        return __version__

    def _load_secrets(self) -> dict[str, str]:
        if not self.paths.secrets.exists():
            return {}
        return json.loads(self.paths.secrets.read_text())

    def _save_secrets(self, values: dict[str, str]) -> None:
        tmp = self.paths.secrets.with_suffix(".tmp")
        tmp.write_text(json.dumps(values, sort_keys=True))
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(self.paths.secrets)
        os.chmod(self.paths.secrets, stat.S_IRUSR | stat.S_IWUSR)

    def secret_get_or_create(self, purpose: str, *, nbytes: int = 32) -> str:
        values = self._load_secrets()
        if purpose not in values:
            values[purpose] = secrets.token_hex(nbytes)
            self._save_secrets(values)
        return values[purpose]

    def put_secret(self, purpose: str, value: str) -> str:
        ref_id = uuid.uuid4().hex
        values = self._load_secrets()
        values[ref_id] = value
        self._save_secrets(values)
        now = self.clock()
        with self.connect() as conn:
            conn.execute("INSERT INTO secret_refs VALUES (?,?,?,?)", (ref_id, purpose, now, now))
        return ref_id

    def get_secret(self, ref_id: str) -> Optional[str]:
        return self._load_secrets().get(ref_id)

    def put_setting(self, namespace: str, value: dict[str, Any], *, expected_revision: Optional[int] = None) -> int:
        now = self.clock()
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT revision FROM settings WHERE namespace=?", (namespace,)).fetchone()
            if row:
                if expected_revision is not None and row[0] != expected_revision:
                    raise StateError("settings revision conflict")
                revision = row[0] + 1
                conn.execute(
                    "UPDATE settings SET value_json=?,revision=?,updated_at=? WHERE namespace=?",
                    (encoded, revision, now, namespace),
                )
            else:
                if expected_revision not in (None, 0):
                    raise StateError("settings revision conflict")
                revision = 1
                conn.execute("INSERT INTO settings VALUES (?,?,?,?,?)", (namespace, 1, encoded, revision, now))
        return revision

    def audit(self, action: str, result: str, *, actor: Optional[str] = None,
              object_id: Optional[str] = None, source: str = "local", detail: Optional[dict] = None) -> None:
        safe = {k: v for k, v in (detail or {}).items() if not any(x in k.lower() for x in ("password", "token", "secret", "body"))}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(actor_user_id,action,object_id,result,source_class,created_at,detail_json) VALUES (?,?,?,?,?,?,?)",
                (actor, action, object_id, result, source, self.clock(), json.dumps(safe, sort_keys=True)),
            )

    def persist_event_with_job(self, *, timestamp: float, confidence: float, reason: str,
                               payload: dict[str, Any], run_at: Optional[float] = None) -> str:
        """Persist the event before atomically creating its idempotent outbox job."""
        event_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        now = self.clock()
        with self.transaction(immediate=True) as conn:
            camera = conn.execute("SELECT id FROM cameras ORDER BY id LIMIT 1").fetchone()
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (event_id, None, camera[0] if camera else None, timestamp, confidence,
                 reason[:1000], "pending", None, now, now),
            )
            body = dict(payload)
            body["event_id"] = event_id
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, "finalize_event", json.dumps(body, sort_keys=True), "pending", 0,
                 run_at if run_at is not None else now, f"finalize:{event_id}", None, now, now),
            )
        return event_id

    def claim_job(self) -> Optional[dict[str, Any]]:
        now = self.clock()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('pending','retry') AND next_run_at<=? ORDER BY next_run_at,id LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND status IN ('pending','retry')",
                (now, row["id"]),
            )
            claimed = dict(row)
            claimed["attempts"] += 1
            return claimed

    def finish_job(self, job_id: str, *, event_id: Optional[str] = None) -> None:
        now = self.clock()
        with self.transaction(immediate=True) as conn:
            conn.execute("UPDATE jobs SET status='complete',updated_at=? WHERE id=?", (now, job_id))
            if event_id:
                conn.execute("UPDATE events SET status='complete',updated_at=? WHERE id=?", (now, event_id))

    def retry_job(self, job_id: str, error: str, attempts: int, *, event_id: Optional[str] = None) -> None:
        now = self.clock()
        terminal = attempts >= 5
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE jobs SET status=?,next_run_at=?,terminal_error=?,updated_at=? WHERE id=?",
                ("failed" if terminal else "retry", now + min(300, 2 ** attempts), error[:1000], now, job_id),
            )
            if event_id:
                conn.execute(
                    "UPDATE events SET status=?,error_summary=?,updated_at=? WHERE id=?",
                    ("failed" if terminal else "pending", error[:1000], now, event_id),
                )

    def add_media(self, event_id: str, path: Path, kind: str = "clip") -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.paths.home.resolve())
        except ValueError as exc:
            raise StateError("media must live under NOMWATCH_HOME") from exc
        media_id = uuid.uuid4().hex
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO media_files VALUES (?,?,?,?,?,?,?,?,?)",
                (media_id, event_id, kind, str(relative), "video/mp4", path.stat().st_size,
                 digest, "active", self.clock()),
            )
        return media_id

    def import_legacy_shadow(self, config_path: Path, events_path: Path) -> dict[str, int]:
        """Idempotently imports copies of legacy data; callers must quiesce before real cutover."""
        imported_settings = imported_events = 0
        if config_path.exists():
            import yaml
            raw = yaml.safe_load(config_path.read_text()) or {}
            if isinstance(raw, dict):
                for namespace in ("bridge", "detection", "notify", "storage"):
                    value = raw.get(namespace)
                    if isinstance(value, dict):
                        redacted = {k: v for k, v in value.items() if not any(s in k.lower() for s in ("password", "token", "credential", "secret"))}
                        if namespace == "notify" and value.get("pushover_app_token"):
                            redacted["pushover_app_token_ref"] = self.put_secret("pushover_app_token", str(value["pushover_app_token"]))
                        if namespace == "storage":
                            for key in ("drive_credentials_path", "drive_token_path"):
                                if value.get(key):
                                    redacted[key + "_ref"] = self.put_secret(key, str(value[key]))
                        with self.connect() as conn:
                            exists = conn.execute("SELECT 1 FROM settings WHERE namespace=?", (namespace,)).fetchone()
                        if not exists:
                            self.put_setting(namespace, redacted)
                            imported_settings += 1
                camera = raw.get("camera")
                if isinstance(camera, dict) and camera.get("ip"):
                    source_hash = hashlib.sha256(json.dumps(camera, sort_keys=True).encode()).hexdigest()
                    camera_id = "legacy-" + source_hash[:24]
                    with self.connect() as conn:
                        exists = conn.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone()
                        if not exists:
                            secret_ref = self.put_secret("camera_password", str(camera.get("password", ""))) if camera.get("password") else None
                            conn.execute(
                                "INSERT INTO cameras VALUES (?,?,?,?,?,?,?,?,1)",
                                (camera_id, "Camera", str(camera.get("ip")), int(camera.get("rtsp_port", 554)),
                                 str(camera.get("stream_path", "stream1")), str(camera.get("username", "")), secret_ref, 1),
                            )
        if events_path.exists():
            with self.connect() as conn:
                camera = conn.execute("SELECT id FROM cameras ORDER BY id LIMIT 1").fetchone()
                camera_id = camera[0] if camera else None
                for raw_line in events_path.read_text(errors="replace").splitlines():
                    try:
                        event = json.loads(raw_line)
                    except (TypeError, ValueError):
                        continue
                    source = hashlib.sha256(raw_line.encode()).hexdigest()
                    event_id = "legacy-" + source[:24]
                    occurred = float(event.get("timestamp", event.get("ts", self.clock())))
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (event_id, source, camera_id, occurred, event.get("confidence"),
                         str(event.get("reason", event.get("reasoning", "")))[:1000], "complete", None,
                         self.clock(), self.clock()),
                    )
                    imported_events += cur.rowcount
        return {"settings": imported_settings, "events": imported_events}
