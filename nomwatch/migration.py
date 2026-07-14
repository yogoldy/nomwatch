"""Conservative legacy snapshot/import coordinator for the first host cutover."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path

from .state import LocalState

LEGACY_LABEL = "com.nomwatch.run"


class MigrationCoordinator:
    def __init__(self, state: LocalState, *, clock=time.time):
        self.state = state
        self.clock = clock

    def already_complete(self) -> bool:
        with self.state.connect() as conn:
            return conn.execute("SELECT 1 FROM settings WHERE namespace='migration.cutover'").fetchone() is not None

    def snapshot_and_import(self, config_path: Path, events_path: Path,
                            legacy_plist: Path | None = None) -> Path | None:
        if self.already_complete() or (not config_path.exists() and not events_path.exists()):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        snapshot = self.state.paths.migrations / stamp
        snapshot.mkdir(parents=True, mode=0o700)
        manifest = {"created_at": self.clock(), "files": {}}
        for source in (config_path, events_path, legacy_plist):
            if source is None or not source.exists():
                continue
            target = snapshot / source.name
            shutil.copy2(source, target)
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            manifest["files"][source.name] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest_path = snapshot / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2))
        os.chmod(manifest_path, stat.S_IRUSR | stat.S_IWUSR)
        try:
            result = self.state.import_legacy_shadow(config_path, events_path)
            with self.state.connect() as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {integrity}")
            self.state.put_setting("migration.cutover", {"snapshot": snapshot.name, "imported": result})
        except Exception:
            # The legacy sources and snapshot remain untouched. Database work
            # is additive/idempotent and host startup refuses to retire the old service.
            raise
        return snapshot
