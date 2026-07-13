"""
Auto-start service wiring so `nomwatch run` survives reboots/logouts without
anyone needing to leave a terminal open.

macOS: generates and loads a launchd user agent (~/Library/LaunchAgents).
Linux: generates a systemd --user unit (not yet wired into the CLI - stub
below, tracked in docs/ROADMAP.md).
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional
from xml.sax.saxutils import escape as _xml_escape

LAUNCHD_LABEL = "com.nomwatch.run"


def _service_path() -> str:
    """
    A PATH for the launchd agent that actually contains the binaries NomWatch
    shells out to (ffmpeg, mediamtx). launchd gives agents a bare
    /usr/bin:/bin PATH, so a Homebrew ffmpeg in /opt/homebrew/bin is invisible
    and every frame capture fails silently - the auto-start loop would run but
    never see the camera. We pin the dirs of the tools we can locate now, plus
    the usual Homebrew/system locations and the installing shell's PATH.
    """
    dirs: List[str] = []

    def add(d: Optional[str]) -> None:
        if d and d not in dirs:
            dirs.append(d)

    for tool in ("ffmpeg", "mediamtx"):
        found = shutil.which(tool)
        if found:
            add(str(Path(found).parent))
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        add(d)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        add(d)
    return os.pathsep.join(dirs)


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _nomwatch_command() -> List[str]:
    """
    The argv to launch `nomwatch run`, as a proper list so each element stays
    intact even when a path contains spaces (e.g. a venv under a directory
    like 'Documents (local)' - splitting the string on whitespace produced a
    broken plist that launchd couldn't run). Prefers the installed console
    script; falls back to `<python> -m nomwatch.cli` when it's not on PATH.
    """
    found = shutil.which("nomwatch")
    if found:
        return [found, "run"]
    return [sys.executable, "-m", "nomwatch.cli", "run"]


def render_launchd_plist(log_dir: Path) -> str:
    args = _nomwatch_command()
    # Each argv element is its own <string>; XML-escape in case a path contains
    # &, < or >. Spaces inside a single <string> are fine - that was the bug.
    program_args = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in args)
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
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{_xml_escape(_service_path())}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(stdout_log))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(stderr_log))}</string>
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
