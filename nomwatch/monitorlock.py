"""Cross-process ownership for the one NomWatch monitoring loop."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - NomWatch's supported hosts are POSIX
    fcntl = None

from .config import CONFIG_DIR

RUN_LOCK_PATH = CONFIG_DIR / "run.lock"


@contextmanager
def run_loop_lock() -> Iterator[bool]:
    """Hold the cross-process lock that permits exactly one monitor loop.

    PID files are useful for dashboard status but cannot prevent races between
    the web UI, launchd, and an interactive terminal. The lock belongs to the
    monitor itself and the OS releases it if that process exits or crashes.
    """
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(RUN_LOCK_PATH, "a+")
    acquired = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        else:
            # NomWatch's supported hosts are macOS/Linux. On another platform
            # remain functional without falsely claiming a lock guarantee.
            acquired = True
        yield acquired
    finally:
        if acquired and fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def run_loop_locked() -> bool:
    """Return whether another monitor process currently owns the lock."""
    with run_loop_lock() as acquired:
        return not acquired
