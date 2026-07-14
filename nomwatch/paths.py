"""Private filesystem layout for a NomWatch installation."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


def default_home() -> Path:
    override = os.environ.get("NOMWATCH_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "nomwatch"


@dataclass(frozen=True)
class NomWatchPaths:
    home: Path

    @classmethod
    def from_environment(cls) -> "NomWatchPaths":
        return cls(default_home())

    @property
    def database(self) -> Path:
        return self.home / "nomwatch.sqlite3"

    @property
    def secrets(self) -> Path:
        return self.home / "secrets.json"

    @property
    def runtime(self) -> Path:
        override = os.environ.get("NOMWATCH_RUNTIME_DIR")
        return Path(override) if override else self.home / "run"

    @property
    def migrations(self) -> Path:
        return self.home / "migration-backups"

    def ensure_private(self) -> None:
        old_umask = os.umask(0o077)
        try:
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        finally:
            os.umask(old_umask)
        os.chmod(self.home, stat.S_IRWXU)
        os.chmod(self.runtime, stat.S_IRWXU)


PATHS = NomWatchPaths.from_environment()
