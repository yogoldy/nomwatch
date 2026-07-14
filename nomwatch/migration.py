"""Conservative legacy snapshot/import coordinator for the first host cutover."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .state import LocalState

LEGACY_LABEL = "com.nomwatch.run"


@dataclass(frozen=True)
class CutoverResult:
    snapshot: Path | None
    legacy_plist: Path | None
    legacy_booted_out: bool
    imported_now: bool


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
        suffix = 1
        while snapshot.exists():
            suffix += 1
            snapshot = self.state.paths.migrations / f"{stamp}-{suffix}"
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

    @staticmethod
    def _validate_legacy_plist(legacy_plist: Path) -> None:
        try:
            parsed = plistlib.loads(legacy_plist.read_bytes())
        except Exception as exc:
            raise RuntimeError("legacy LaunchAgent is malformed; refusing to modify it") from exc
        if parsed.get("Label") != LEGACY_LABEL:
            raise RuntimeError("plist is not the exact legacy NomWatch service; refusing to modify it")

    def atomic_cutover(self, config_path: Path, events_path: Path,
                       legacy_plist: Path | None = None, *, runner=subprocess.run,
                       system: str | None = None) -> CutoverResult | None:
        """Quiesce exact legacy ownership, import, verify, or restart it on failure."""
        already_complete = self.already_complete()
        legacy_present = bool(legacy_plist and legacy_plist.exists())
        if already_complete and not legacy_present:
            return None
        system = system or platform.system()
        booted_out = False
        if legacy_present:
            self._validate_legacy_plist(legacy_plist)
            if system == "Darwin":
                result = runner(
                    ["launchctl", "bootout", f"gui/{os.getuid()}", str(legacy_plist)],
                    capture_output=True, text=True,
                )
                booted_out = result.returncode == 0

        from . import monitorlock
        lock = monitorlock.run_loop_lock()
        acquired = lock.__enter__()
        if not acquired:
            lock.__exit__(None, None, None)
            if booted_out:
                runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(legacy_plist)],
                       capture_output=True, text=True)
            raise RuntimeError("a legacy/manual monitor still owns the writer lock; cutover aborted")
        try:
            snapshot = None if already_complete else self.snapshot_and_import(config_path, events_path, legacy_plist)
            return CutoverResult(snapshot, legacy_plist if legacy_present else None,
                                 booted_out, snapshot is not None)
        except Exception:
            if booted_out:
                runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(legacy_plist)],
                       capture_output=True, text=True)
            raise
        finally:
            lock.__exit__(None, None, None)

    def rollback_failed_cutover(self, result: CutoverResult, *, runner=subprocess.run,
                                system: str | None = None) -> None:
        """Make a failed new-host start retryable and restore the exact old service."""
        if result.imported_now:
            with self.state.connect() as conn:
                conn.execute("DELETE FROM settings WHERE namespace='migration.cutover'")
        system = system or platform.system()
        if result.legacy_booted_out and result.legacy_plist and result.legacy_plist.exists() and system == "Darwin":
            self._validate_legacy_plist(result.legacy_plist)
            runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(result.legacy_plist)],
                   capture_output=True, text=True)

    def finalize_cutover(self, result: CutoverResult, *, runner=subprocess.run,
                         system: str | None = None) -> None:
        """Retire only the snapshotted exact legacy manifest after new-host readiness."""
        legacy_plist = result.legacy_plist
        if not legacy_plist or not legacy_plist.exists():
            return
        self._validate_legacy_plist(legacy_plist)
        system = system or platform.system()
        if system == "Darwin":
            runner(["launchctl", "bootout", f"gui/{os.getuid()}", str(legacy_plist)],
                   capture_output=True, text=True)
        legacy_plist.unlink()
