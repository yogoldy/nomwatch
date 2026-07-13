"""
Control of the `nomwatch run` monitoring loop as a supervised child process,
shared by every front-end that can start/stop monitoring: the web dashboard
(webui.py), the native macOS menu-bar app (macapp.py), and potentially the
CLI. Centralizing it here means all three agree on the same pid file, the same
duplicate-detection, and the same "restart to apply new config" behavior.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .bridge import _wait_for_exit, read_pid_info, write_pid_file
from .config import CONFIG_DIR, CONFIG_PATH

RUN_PID_PATH = CONFIG_DIR / "run.pid"
HEARTBEAT_PATH = CONFIG_DIR / "heartbeat.json"


def run_loop_pid() -> Optional[int]:
    """The pid of the monitoring loop this app started (from its pid file), or None."""
    return read_pid_info(RUN_PID_PATH)[0]


def run_loop_started_at() -> Optional[float]:
    return read_pid_info(RUN_PID_PATH)[1]


def external_run_pids() -> list:
    """
    `nomwatch run` processes NOT tracked by our pid file - e.g. one started by
    the launchd auto-start service or a stray terminal. Lets a front-end avoid
    starting a DUPLICATE loop (which would double every notification/clip).
    pgrep is available on both macOS and Linux.
    """
    tracked = run_loop_pid()
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"nomwatch(\.cli)? run"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    pids = []
    for line in out.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid != tracked and pid != os.getpid():
            pids.append(pid)
    return pids


def monitoring_alive() -> bool:
    """True if any monitoring loop is running (ours or an external/launchd one)."""
    return run_loop_pid() is not None or bool(external_run_pids())


def read_heartbeat() -> Optional[dict]:
    """The monitoring loop's last-poll heartbeat plus its age in seconds, or None."""
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        data = json.loads(HEARTBEAT_PATH.read_text())
        data["age_seconds"] = round(time.time() - float(data.get("ts", 0)), 1)
        return data
    except (ValueError, OSError):
        return None


def config_changed_since(started_at: Optional[float]) -> bool:
    """
    True if config.yml was saved AFTER the given process start time - i.e. the
    process is running with settings older than what the user sees. This exact
    staleness (settings saved, nothing restarted, old behavior continues
    silently) caused real bug reports.
    """
    if started_at is None:
        return False
    try:
        return CONFIG_PATH.stat().st_mtime > started_at
    except OSError:
        return False


def start_run_loop() -> Optional[int]:
    """
    Start `nomwatch run` fresh, restarting it if it's already running. Restart
    matters because `nomwatch run` loads config ONCE at startup and never
    reloads - restarting on every explicit Start guarantees the latest saved
    config is actually in effect. Returns the new pid, or None if it couldn't
    be launched.
    """
    existing = run_loop_pid()
    if existing:
        stop_run_loop()
        _wait_for_exit(existing)

    exe = shutil.which("nomwatch")
    cmd = [exe, "run"] if exe else [sys.executable, "-m", "nomwatch.cli", "run"]
    log_dir = CONFIG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "run.out.log", "a")
    try:
        process = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True,
            cwd=str(CONFIG_DIR),
        )
    except FileNotFoundError:
        return None
    write_pid_file(RUN_PID_PATH, process.pid)
    return process.pid


def stop_run_loop() -> bool:
    """SIGTERM the tracked monitoring loop and clear its pid file."""
    pid = run_loop_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 15)  # SIGTERM
    except ProcessLookupError:
        pass
    RUN_PID_PATH.unlink(missing_ok=True)
    return True
