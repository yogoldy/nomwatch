"""Local-only backup, restore, retention, and redacted recovery diagnostics."""
from __future__ import annotations

import hashlib
import contextlib
import json
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path

from .paths import NomWatchPaths
from .state import LocalState, SCHEMA_VERSION, StateError

MIN_DISK_FREE_BYTES = 1_000_000_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_operational_backup(state: LocalState) -> Path:
    """Create a consistent owner-only SQLite backup; secrets/media are excluded."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(state.clock()))
    root = state.paths.home / "backups" / stamp
    root.mkdir(parents=True, mode=0o700)
    destination = root / "nomwatch.sqlite3"
    source_conn = state.connect()
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
    os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    with contextlib.closing(sqlite3.connect(destination)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        schema = conn.execute("SELECT COALESCE(MAX(migration_id),0) FROM schema_migrations").fetchone()[0]
    if integrity != "ok" or schema > SCHEMA_VERSION:
        raise StateError("backup verification failed")
    manifest = {
        "format": 1, "created_at": state.clock(), "database": destination.name,
        "sha256": _sha256(destination), "schema_version": schema,
        "contains": "local operational database; excludes secrets and media bytes",
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2))
    os.chmod(manifest_path, stat.S_IRUSR | stat.S_IWUSR)
    return root


def restore_operational_backup(paths: NomWatchPaths, backup_dir: Path) -> Path:
    """Restore while the host is offline; preserve the previous database for rollback."""
    if (paths.runtime / "control.sock").exists():
        raise StateError("stop the NomWatch host before restoring a database")
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise StateError("backup manifest is missing")
    manifest = json.loads(manifest_path.read_text())
    source = backup_dir / str(manifest.get("database", ""))
    if not source.is_file() or _sha256(source) != manifest.get("sha256"):
        raise StateError("backup checksum does not match")
    with contextlib.closing(sqlite3.connect(source)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StateError("backup database failed integrity_check")
        schema = conn.execute("SELECT COALESCE(MAX(migration_id),0) FROM schema_migrations").fetchone()[0]
    if schema > SCHEMA_VERSION:
        raise StateError("backup schema is newer than this NomWatch version")
    rollback = paths.home / "recovery-rollback" / time.strftime("%Y%m%d-%H%M%S")
    rollback.mkdir(parents=True, mode=0o700)
    if paths.database.exists():
        shutil.copy2(paths.database, rollback / paths.database.name)
        os.chmod(rollback / paths.database.name, 0o600)
    temp = paths.database.with_suffix(".restore.tmp")
    source_conn = sqlite3.connect(source)
    destination_conn = sqlite3.connect(temp)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
    os.chmod(temp, 0o600)
    temp.replace(paths.database)
    for suffix in ("-wal", "-shm"):
        Path(str(paths.database) + suffix).unlink(missing_ok=True)
    return rollback


def prune_migration_backups(paths: NomWatchPaths, *, older_than_days: int = 30,
                            clock=time.time) -> int:
    if not paths.migrations.exists():
        return 0
    cutoff = clock() - older_than_days * 86400
    removed = 0
    for child in paths.migrations.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child)
            removed += 1
    return removed


def compact_state(state: LocalState, *, completed_job_days: int = 7,
                  audit_days: int = 90) -> dict[str, int]:
    now = state.clock()
    with state.connect() as conn:
        jobs = conn.execute(
            "DELETE FROM jobs WHERE status='complete' AND updated_at<?", (now - completed_job_days * 86400,)
        ).rowcount
        audits = conn.execute(
            "DELETE FROM audit_log WHERE created_at<?", (now - audit_days * 86400,)
        ).rowcount
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return {"jobs_removed": jobs, "audit_rows_removed": audits}


def diagnostics(paths: NomWatchPaths) -> dict:
    result = {
        "status": "ok", "home_private": False, "database_private": False,
        "database_integrity": "missing", "schema_version": None,
        "disk_free_bytes": shutil.disk_usage(paths.home).free if paths.home.exists() else None,
        "disk_floor_bytes": MIN_DISK_FREE_BYTES, "disk_floor_ok": None,
        "control_socket_present": (paths.runtime / "control.sock").exists(),
        "remote_access": {"status": "unknown", "cleanup_required": False},
        "legacy_artifacts": [], "migration_backups": 0,
    }
    if paths.home.exists():
        result["home_private"] = stat.S_IMODE(paths.home.stat().st_mode) == 0o700
    if result["disk_free_bytes"] is not None:
        result["disk_floor_ok"] = result["disk_free_bytes"] >= MIN_DISK_FREE_BYTES
    if paths.database.exists():
        result["database_private"] = stat.S_IMODE(paths.database.stat().st_mode) == 0o600
        try:
            with contextlib.closing(sqlite3.connect(f"file:{paths.database}?mode=ro", uri=True)) as conn:
                result["database_integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
                result["schema_version"] = conn.execute("SELECT COALESCE(MAX(migration_id),0) FROM schema_migrations").fetchone()[0]
                remote = conn.execute("SELECT status,desired_enabled FROM remote_access WHERE id=1").fetchone()
                if remote:
                    result["remote_access"] = {
                        "status": remote[0], "cleanup_required": remote[0] == "cleanup_required",
                        "desired_enabled": bool(remote[1]),
                    }
        except sqlite3.Error as exc:
            result["database_integrity"] = f"error: {type(exc).__name__}"
    for name in ("run.pid", "mediamtx.pid", "heartbeat.json"):
        if (paths.home / name).exists():
            result["legacy_artifacts"].append(name)
    if paths.migrations.exists():
        result["migration_backups"] = sum(1 for item in paths.migrations.iterdir() if item.is_dir())
    if (not result["home_private"] or not result["database_private"] or
            result["database_integrity"] != "ok" or result["disk_floor_ok"] is False or
            result["remote_access"]["cleanup_required"]):
        result["status"] = "degraded"
    return result
