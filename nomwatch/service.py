"""
Auto-start service wiring so `nomwatch run` survives reboots/logouts without
anyone needing to leave a terminal open.

macOS: generates and loads a launchd user agent (~/Library/LaunchAgents).
Linux: generates a systemd --user unit (not yet wired into the CLI - stub
below, tracked in docs/ROADMAP.md).
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

LAUNCHD_LABEL = "com.nomwatch.run"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _nomwatch_executable() -> str:
    """
    Resolves the actual path to the installed `nomwatch` console script,
    falling back to `python3 -m nomwatch.cli` if it's not on PATH (e.g. the
    pip user-install-bin-not-on-PATH situation we've hit before).
    """
    found = shutil.which("nomwatch")
    if found:
        return found
    return f"{sys.executable} -m nomwatch.cli"


def render_launchd_plist(log_dir: Path) -> str:
    exe = _nomwatch_executable()
    parts = exe.split()
    program_args = "\n".join(f"        <string>{p}</string>" for p in [*parts, "run"])
    stdout_log = log_dir / "nomwatch.out.log"
    stderr_log = log_dir / "nomwatch.err.log"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
</dict>
</plist>
"""


def install_launchd_service(log_dir: Path) -> Optional[str]:
    """
    Writes the plist and loads it via launchctl, so `nomwatch run` starts on
    login and restarts automatically if it crashes (KeepAlive). Returns an
    error message string on failure, or None on success.
    """
    if platform.system() != "Darwin":
        return "launchd auto-start is only supported on macOS."

    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(render_launchd_plist(log_dir))

    # Unload first in case a previous version is already loaded, then load fresh.
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0:
        return f"launchctl load failed: {result.stderr.strip() or result.stdout.strip()}"
    return None


def uninstall_launchd_service() -> Optional[str]:
    if platform.system() != "Darwin":
        return "launchd auto-start is only supported on macOS."

    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        return "No NomWatch launchd service found."

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink()
    return None


def launchd_service_status() -> str:
    if platform.system() != "Darwin":
        return "launchd is macOS-only."

    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        return "Not installed."

    result = subprocess.run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True, text=True)
    if result.returncode == 0:
        return f"Installed and loaded:\n{result.stdout}"
    return "Plist exists but not currently loaded (may need `launchctl load` again)."
