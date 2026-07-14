"""
Bridge setup: MediaMTX config generation + Tailscale serve wiring.

This module generates config and shells out to `mediamtx`/`tailscale` binaries
that must already be installed (via brew, apt, or the official installers).
It deliberately does not manage installation of those binaries itself in v1 -
see cli.py's `doctor` command for install-detection and guidance.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple

import os

from .config import CONFIG_DIR, NomWatchConfig, clean_user_path


def rtsp_url(cfg: NomWatchConfig) -> str:
    """
    Builds the camera RTSP URL with the username/password percent-encoded.
    Without encoding, a password containing '@', '/', ':' or '#' silently
    produces a malformed URL that ffmpeg/MediaMTX fail to parse - and the
    resulting "could not connect" error looks exactly like a wrong-password
    problem, which is miserable to debug.
    """
    user = urllib.parse.quote(cfg.camera.username, safe="")
    pw = urllib.parse.quote(cfg.camera.password, safe="")
    return (
        f"rtsp://{user}:{pw}@{cfg.camera.ip}:{cfg.camera.rtsp_port}/{cfg.camera.stream_path}"
    )

MEDIAMTX_CONFIG_TEMPLATE = """\
rtspAddress: 127.0.0.1:{rtsp_port}
hlsAddress: 127.0.0.1:{hls_port}
authMethod: internal
authInternalUsers:
- user: {read_user}
  pass: {read_password}
  permissions:
  - action: read
    path: cam
webrtc: false
rtmp: false
srt: false
api: false
metrics: false
playback: false

paths:
  cam:
    source: {source_url}
    rtspTransport: tcp
{recording_block}"""

RECORDING_BLOCK_TEMPLATE = """\
    record: yes
    recordPath: {recordings_dir}/%path/%Y-%m-%d_%H-%M-%S-%f
    recordSegmentDuration: {segment_seconds}s
    recordDeleteAfter: {retention_seconds}s
"""
# NOTE: recordPath deliberately has NO trailing extension. MediaMTX appends
# its own extension based on recordFormat (default "fmp4" -> ".mp4") - a
# hardcoded ".mp4" here produces "....mp4.mp4" filenames, which breaks
# clip.py's SEGMENT_FILENAME_RE and makes find_segments_covering() silently
# return [] for every real segment (100% pre-roll clip failure). Found via
# a real live test against actual MediaMTX output - a hand-named synthetic
# test file wouldn't have caught this.


def _internal_media_credentials() -> tuple[str, str]:
    user = os.environ.get("NOMWATCH_MEDIAMTX_READ_USER", "nomwatch")
    password = os.environ.get("NOMWATCH_MEDIAMTX_READ_PASSWORD")
    if not password:
        # Config generation outside the host is development-only. The host
        # always injects an installation-scoped random value.
        password = "development-loopback-only"
    return user, password


def render_mediamtx_config(cfg: NomWatchConfig, *, read_user: Optional[str] = None,
                           read_password: Optional[str] = None) -> str:
    recording_block = ""
    if cfg.detection.pre_roll_seconds > 0:
        recordings_dir = clean_user_path(cfg.bridge.recordings_dir) or str(CONFIG_DIR / "recordings")
        recording_block = RECORDING_BLOCK_TEMPLATE.format(
            recordings_dir=recordings_dir,
            segment_seconds=cfg.bridge.record_segment_seconds,
            # Keep enough history for pre-roll plus the full post-confirm
            # clip window, with some headroom.
            retention_seconds=max(
                cfg.bridge.record_retention_seconds,
                cfg.detection.pre_roll_seconds + cfg.detection.clip_post_confirm_seconds + 30,
            ),
        )

    default_user, default_password = _internal_media_credentials()
    return MEDIAMTX_CONFIG_TEMPLATE.format(
        rtsp_port=cfg.bridge.mediamtx_rtsp_port,
        hls_port=cfg.bridge.mediamtx_hls_port,
        source_url=rtsp_url(cfg),
        read_user=read_user or default_user,
        read_password=read_password or default_password,
        recording_block=recording_block,
    )


def local_mediamtx_rtsp_url(cfg: NomWatchConfig) -> str:
    """Credential-bearing loopback URL passed only through the child environment."""
    user, password = _internal_media_credentials()
    return (
        f"rtsp://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
        f"@127.0.0.1:{cfg.bridge.mediamtx_rtsp_port}/cam"
    )


def binary_available(name: str) -> bool:
    return shutil.which(name) is not None


def _brew_install(package: str, on_output=None) -> bool:
    """
    Shared Homebrew-install helper. macOS only - fails clearly if brew
    itself isn't installed (we don't try to install brew itself, that's a
    bigger ask than NomWatch should make unprompted). Other platforms:
    not attempted, caller should show manual instructions.
    """
    if platform.system() != "Darwin":
        if on_output:
            on_output(f"Automatic {package} install is only wired up for macOS (via Homebrew) right now.")
        return False

    if not binary_available("brew"):
        if on_output:
            on_output(
                f"Homebrew isn't installed, so NomWatch can't install {package} automatically. "
                "Install Homebrew first (https://brew.sh), or install it some other way, then recheck."
            )
        return False

    try:
        process = subprocess.Popen(
            ["brew", "install", package],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return False

    for line in process.stdout:
        if on_output:
            on_output(line.rstrip())
    process.wait()
    return process.returncode == 0


def install_ffmpeg(on_output=None) -> bool:
    """Attempts to install ffmpeg automatically. See _brew_install."""
    return _brew_install("ffmpeg", on_output) and binary_available("ffmpeg")


def install_mediamtx(on_output=None) -> bool:
    """Attempts to install MediaMTX automatically. See _brew_install."""
    return _brew_install("mediamtx", on_output) and binary_available("mediamtx")


MEDIAMTX_PID_PATH = CONFIG_DIR / "mediamtx.pid"


def write_pid_file(path: Path, pid: int) -> None:
    """
    Records a managed process as JSON {"pid": ..., "started_at": ...}.
    The start timestamp lets the UI tell whether the config file changed
    AFTER the process started (i.e. it's running with stale settings) -
    the exact failure mode behind the "settings saved but nothing changed"
    bug class.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "started_at": time.time()}))


def read_pid_info(path: Path) -> Tuple[Optional[int], Optional[float]]:
    """
    Returns (pid, started_at) for a live tracked process, or (None, None).
    Tolerates the older plain-integer pid file format (started_at None).
    """
    if not path.exists():
        return None, None
    try:
        text = path.read_text().strip()
        if text.startswith("{"):
            info = json.loads(text)
            pid = int(info["pid"])
            started_at = info.get("started_at")
        else:
            pid, started_at = int(text), None
        os.kill(pid, 0)  # signal 0: check existence without actually killing it
        return pid, started_at
    except (ValueError, KeyError, ProcessLookupError, PermissionError):
        return None, None


def _wait_for_exit(pid: int, timeout: float = 5.0) -> bool:
    """Waits for a process to actually terminate. True if it's gone."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.1)
    return False


def mediamtx_pid_running() -> Optional[int]:
    """Returns the PID if our tracked MediaMTX process is still alive, else None."""
    return read_pid_info(MEDIAMTX_PID_PATH)[0]


def start_mediamtx(config_path: Path, restart: bool = True) -> Optional[int]:
    """
    Starts MediaMTX as a detached background process using the given config,
    tracking its PID so we can check/stop it later. Returns the PID on
    success, or None if the binary isn't installed or launch failed.
    Does NOT set up auto-start-on-login - that's a separate concern (see
    nomwatch/service.py for the launchd pattern used for `nomwatch run`).

    `restart=True` (the default) always stops any already-running instance
    first. This matters a lot in practice: the web UI rewrites
    mediamtx.yml (e.g. to enable/disable pre-roll recording) every time
    detection settings are saved, but MediaMTX only reads its config file
    once, at process start - it does not hot-reload. Without restarting,
    an instance started before a settings change silently keeps running
    with the STALE config forever (e.g. recording disabled even though the
    UI now says pre-roll is on), and there was previously no way to fix
    that short of manually killing the process. `restart=False` preserves
    the old "leave it alone if already up" behavior for callers that only
    care whether *something* is serving the stream.
    """
    if not binary_available("mediamtx"):
        return None

    existing = mediamtx_pid_running()
    if existing:
        if not restart:
            return existing
        stop_mediamtx()
        # SIGTERM is asynchronous - if we launch the replacement before the
        # old process has actually released its ports, the new one fails to
        # bind and dies instantly, leaving NOTHING running after a
        # "restart". Wait for real termination first.
        _wait_for_exit(existing)

    try:
        process = subprocess.Popen(
            ["mediamtx", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach so it survives this process exiting
            # Run from the config dir, not whatever CWD this server happens
            # to have: MediaMTX drops files into its CWD (observed live: it
            # generated auto.crt/auto.key inside the NomWatch source repo
            # because the web UI launched it from there).
            cwd=str(CONFIG_DIR),
        )
    except FileNotFoundError:
        return None

    write_pid_file(MEDIAMTX_PID_PATH, process.pid)
    return process.pid


def stop_mediamtx() -> bool:
    pid = mediamtx_pid_running()
    if not pid:
        return False
    try:
        os.kill(pid, 15)  # SIGTERM
    except ProcessLookupError:
        pass
    MEDIAMTX_PID_PATH.unlink(missing_ok=True)
    return True


def write_mediamtx_config(cfg: NomWatchConfig, path: Path, *, read_user: Optional[str] = None,
                          read_password: Optional[str] = None) -> Path:
    path.write_text(render_mediamtx_config(cfg, read_user=read_user, read_password=read_password))
    # The rendered config embeds the camera's RTSP credentials in plaintext -
    # it must be owner-only, same as config.yml (was previously left at the
    # default 644, world-readable).
    os.chmod(path, 0o600)
    return path


def enable_tailscale_serve(hls_port: int) -> subprocess.CompletedProcess:
    """
    Publishes the local-only HLS endpoint over the tailnet.
    Deliberately uses `serve`, never `funnel` - funnel makes it public,
    which defeats the entire point of this project.
    """
    return subprocess.run(
        ["tailscale", "serve", "--bg", f"http://127.0.0.1:{hls_port}"],
        capture_output=True,
        text=True,
    )


def tailscale_status() -> Optional[str]:
    if not binary_available("tailscale"):
        return None
    result = subprocess.run(["tailscale", "status"], capture_output=True, text=True)
    return result.stdout
