"""
Local web UI - full 6-screen setup wizard. See docs/UI_SPEC.md for the spec.

This is NOT a replacement for `nomwatch setup` - it's the primary first-run/
reconfigure path per product decision, with the CLI wizard kept underneath
for power users/automation/headless devices.

Run via `nomwatch ui` (requires the `ui` extra: `pip install nomwatch[ui]`).
Binds to 127.0.0.1 only by default, plain HTTP, no encryption of its own -
same "loopback-only" philosophy as MediaMTX. If you want it reachable from
another device, expose it manually via `tailscale serve` (same pattern used
for the HLS stream) - there's no automatic Tailscale wiring for the UI yet.

Known limitations, noted rather than hidden:
- Model pulls block the request until `ollama pull` finishes rather than
  streaming real progress.
- The camera "test connection" preview is a single still-frame snapshot
  (via ffmpeg), not a live video stream - live preview would need MediaMTX/
  HLS already running, which isn't wired into the UI yet.
- The QR code on the notifications screen is rendered by calling the free
  public api.qrserver.com image service, which means the ntfy topic name is
  sent to that third party to generate the image. The topic itself is
  already effectively public (anyone with it can subscribe on ntfy.sh), so
  this is a minor concern, not a secrets leak - but it's an external network
  call worth knowing about. A self-hosted/client-side QR generator would
  remove this dependency; not built yet.
- No host-vs-client/multi-device delegation concept exists anywhere in
  NomWatch yet - whichever machine runs `nomwatch run`/`nomwatch ui` is
  simply "the bridge" by virtue of running it there.
"""
from __future__ import annotations

import base64
import datetime
import html
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from .bridge import (
    MEDIAMTX_PID_PATH,
    binary_available,
    install_ffmpeg,
    install_mediamtx,
    read_pid_info,
    rtsp_url,
    start_mediamtx,
    write_pid_file,
    write_mediamtx_config,
)
from .config import (
    CONFIG_DIR,
    CONFIG_PATH,
    BridgeConfig,
    CameraConfig,
    DetectionConfig,
    NomWatchConfig,
    NotifyConfig,
    StorageConfig,
    clean_user_path,
    config_from_dict,
    load_config,
    save_config,
)
from .detection import (
    DEFAULT_VISION_MODEL,
    capture_frame_with_error,
    list_local_models,
    model_installed,
    pick_vision_model,
    probe_local_model_server,
    pull_model,
)
from .notify import NtfyNotifier
from .service import (
    install_launchd_service,
    launchd_service_status,
    uninstall_launchd_service,
)
from .storage import find_google_drive_sync_folder
from . import monitorlock

# --- "Start monitoring right now" process management -----------------------
# Separate from the optional launchd auto-start-on-login service (see
# service.py) - this is the "click a button in the browser and the detection
# loop actually starts running" path, tracked by PID the same way bridge.py
# tracks the MediaMTX process.

RUN_PID_PATH = CONFIG_DIR / "run.pid"
EVENTS_LOG_PATH = CONFIG_DIR / "events.jsonl"
HEARTBEAT_PATH = CONFIG_DIR / "heartbeat.json"
CLIPS_DIR = CONFIG_DIR / "clips"
THUMBNAILS_DIR = CONFIG_DIR / "thumbnails"


def _get_or_init_config() -> NomWatchConfig:
    cfg = load_config()
    if cfg is None:
        cfg = NomWatchConfig(camera=CameraConfig(ip=""))
    return cfg


def _run_loop_pid_running() -> Optional[int]:
    return read_pid_info(RUN_PID_PATH)[0]


def _external_run_pids() -> list:
    """
    Finds `nomwatch run` processes that this UI does NOT track via its pid
    file - e.g. one started by the launchd auto-start service or a stray
    terminal. Without this, the dashboard would say "monitoring is NOT
    running" while a launchd-managed loop is happily polling away, and
    clicking Start would spin up a DUPLICATE loop (double notifications,
    double clips). pgrep is available on both macOS and Linux.
    """
    tracked = _run_loop_pid_running()
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


def _read_heartbeat() -> Optional[dict]:
    """Returns the monitoring loop's last-poll heartbeat plus its age, or None."""
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        data = json.loads(HEARTBEAT_PATH.read_text())
        data["age_seconds"] = round(time.time() - float(data.get("ts", 0)), 1)
        return data
    except (ValueError, OSError):
        return None


def _config_changed_since(started_at: Optional[float]) -> bool:
    """
    True if config.yml was saved AFTER the given process start time - i.e.
    the process is running with settings older than what the user sees in
    the UI. This exact staleness (settings saved, nothing restarted, old
    behavior continues silently) caused the last two real bug reports.
    """
    if started_at is None:
        return False
    try:
        return CONFIG_PATH.stat().st_mtime > started_at
    except OSError:
        return False


def _start_run_loop() -> Optional[int]:
    """
    Starts `nomwatch run` fresh, always restarting it if it's already
    running. This matters because `nomwatch run` loads config ONCE at
    startup and never reloads - if it were left running across a
    settings change (e.g. a new local_save_dir, or new pre-roll timing),
    it would silently keep acting on the OLD config forever with no way
    to fix that short of manually killing the process. Restarting on every
    explicit "Start monitoring" click guarantees the latest saved config
    is actually the one in effect.
    """
    existing = _run_loop_pid_running()
    if existing:
        _stop_run_loop()
        from .bridge import _wait_for_exit

        if not _wait_for_exit(existing):
            return None

    # ``pgrep`` is useful status information but is unavailable in some
    # sandboxed macOS contexts. The monitor-owned lock is authoritative.
    if monitorlock.run_loop_locked():
        return None

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


def _stop_run_loop() -> bool:
    pid = _run_loop_pid_running()
    if not pid:
        return False
    try:
        os.kill(pid, 15)  # SIGTERM
    except ProcessLookupError:
        pass
    RUN_PID_PATH.unlink(missing_ok=True)
    return True


def _read_recent_events(limit: int = 50) -> list:
    if not EVENTS_LOG_PATH.exists():
        return []
    events = []
    with open(EVENTS_LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    events = list(reversed(events[-limit:]))
    # Enrich with what the gallery needs: does the clip file still exist in
    # the canonical clips dir, and by what name can it be streamed/deleted.
    for event in events:
        clip_file = None
        clip_path = event.get("clip_path")
        if clip_path:
            candidate = Path(clip_path)
            if candidate.name and (CLIPS_DIR / candidate.name).exists():
                clip_file = candidate.name
        event["clip_file"] = clip_file
    return events


_CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.mp4$")


def _safe_clip_path(name: str) -> Optional[Path]:
    """
    Maps a client-supplied clip filename to a real file inside CLIPS_DIR,
    or None. Whitelist-style validation (single path component, .mp4 only)
    so the clip-serving/delete endpoints can't be used to read or remove
    arbitrary files.
    """
    if not name or not _CLIP_NAME_RE.match(name) or name.startswith("."):
        return None
    path = (CLIPS_DIR / name).resolve()
    if path.parent != CLIPS_DIR.resolve() or not path.exists():
        return None
    return path


def _thumbnail_for(clip: Path) -> Optional[Path]:
    """
    Returns a cached poster-frame JPEG for a clip, generating it with ffmpeg
    on first request. Uses a frame ~1s in (the pre-roll makes frame 0 an
    empty scene most of the time).
    """
    THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    thumb = THUMBNAILS_DIR / (clip.stem + ".jpg")
    if thumb.exists() and thumb.stat().st_mtime >= clip.stat().st_mtime:
        return thumb
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", "1", "-i", str(clip),
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
                str(thumb),
            ],
            capture_output=True, timeout=20, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        # Frame at 1s can fail on very short clips - try the first frame.
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(clip), "-frames:v", "1",
                 "-vf", "scale=320:-2", "-q:v", "5", str(thumb)],
                capture_output=True, timeout=20, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return None
    return thumb if thumb.exists() else None


def _camera_failure_hints(ip: str, port: int, ffmpeg_error: str) -> list:
    """
    Turns a failed camera test into concrete, copy-paste-able next steps by
    combining a raw TCP probe of the RTSP port with ffmpeg's error output.
    "Could not connect" alone leaves people guessing between wrong IP,
    wrong port, RTSP disabled, and wrong password - these are the four
    failure modes that actually distinguish them.
    """
    hints = []
    err = (ffmpeg_error or "").lower()

    tcp_result = None
    try:
        with socket.create_connection((ip, port), timeout=3):
            tcp_result = "open"
    except socket.timeout:
        tcp_result = "timeout"
    except ConnectionRefusedError:
        tcp_result = "refused"
    except OSError:
        tcp_result = "unreachable"

    if tcp_result == "timeout" or tcp_result == "unreachable":
        hints.append(
            f"Nothing answered at {ip}:{port}. Double-check the IP in your router's "
            "connected-devices list or the camera app's Device Info page, and make sure "
            "this computer is on the same network as the camera (not a guest Wi-Fi)."
        )
        hints.append(f"Quick check from a terminal: ping {ip}")
    elif tcp_result == "refused":
        hints.append(
            f"The device at {ip} is reachable, but nothing is listening on port {port}. "
            "Most likely the camera's RTSP/camera-account feature is turned off, or it "
            "uses a different port. On Tapo cameras: app > camera > Advanced Settings > "
            "Camera Account - creating the account is what enables RTSP."
        )
    elif tcp_result == "open":
        if "401" in err or "unauthorized" in err:
            hints.append(
                "The camera answered but rejected the username/password. Use the "
                "camera-account (local RTSP) credentials, NOT your cloud-app login. On "
                "Tapo: Advanced Settings > Camera Account."
            )
        elif "404" in err or "not found" in err:
            hints.append(
                "The camera answered but that stream path doesn't exist. Most cameras "
                "use stream1 (HD) or stream2 (SD) - try the other one."
            )
        else:
            hints.append(
                f"Port {port} is open, so the camera is reachable - the RTSP handshake "
                "itself failed. Check the stream path (stream1 vs stream2) and "
                "credentials, and make sure nothing else is holding the camera's "
                "connection limit open."
            )
    return hints

PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>NomWatch Setup</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
    <style>
        :root {{
            --nom-bg-1: #fff8ec;
            --nom-bg-2: #ffe9d6;
            --nom-green: #1a8f5e;
            --nom-green-dark: #12704a;
            --nom-orange: #ff8a3d;
            --nom-orange-dark: #e96f22;
            --nom-ink: #2b2420;
            --nom-muted: #7a6f66;
            --nom-card: #ffffff;
            --nom-border: #f0e2d2;
            --nom-shadow: 0 12px 32px rgba(120, 72, 20, 0.10), 0 2px 8px rgba(120, 72, 20, 0.06);
        }}

        * {{ box-sizing: border-box; }}

        body {{
            font-family: "Nunito", -apple-system, system-ui, sans-serif;
            margin: 0; padding: 40px 20px 80px;
            color: var(--nom-ink);
            background:
                radial-gradient(circle at 10% -10%, #fff2df 0%, transparent 45%),
                radial-gradient(circle at 100% 0%, #e8f6ee 0%, transparent 40%),
                linear-gradient(180deg, var(--nom-bg-1) 0%, var(--nom-bg-2) 100%);
            min-height: 100vh;
        }}

        .app-shell {{ max-width: 720px; margin: 0 auto; }}

        .brand-row {{ text-align: center; margin-bottom: 22px; }}
        .brand-row h1 {{
            font-size: 2em; margin: 6px 0 4px; letter-spacing: -0.02em;
            color: var(--nom-green-dark);
        }}
        .brand-tag {{ color: var(--nom-muted); font-size: 0.95em; margin: 0; }}
        .brand-tag a {{ color: var(--nom-green-dark); font-weight: 700; text-decoration: none; }}
        .brand-tag a:hover {{ text-decoration: underline; }}

        .progress-track {{
            display: flex; align-items: center; justify-content: center;
            gap: 6px; margin: 18px auto 26px; max-width: 460px;
        }}
        .progress-dot {{
            width: 30px; height: 6px; border-radius: 999px; background: #f0dcc4;
            transition: background 0.25s ease;
        }}
        .progress-dot.done {{ background: var(--nom-green); }}
        .progress-dot.current {{ background: var(--nom-orange); }}

        .step {{
            color: var(--nom-orange-dark); font-size: 0.85em; font-weight: 800;
            text-align: center; text-transform: uppercase; letter-spacing: 0.06em;
            margin-bottom: 2px;
        }}

        .card {{
            background: var(--nom-card); border-radius: 20px; padding: 32px 34px 30px;
            box-shadow: var(--nom-shadow); border: 1px solid var(--nom-border);
        }}

        h2 {{ font-size: 1.35em; margin-top: 0; color: var(--nom-ink); }}
        h2::before {{ content: "🐾  "; }}
        h3 {{ color: var(--nom-ink); }}

        label {{ display: flex; align-items: center; gap: 6px; margin-top: 18px; font-weight: 700; font-size: 0.92em; color: #4a4038; }}

        input[type=text], input[type=number], input[type=password] {{
            width: 100%; padding: 11px 12px; margin-top: 6px; font-size: 1em; box-sizing: border-box;
            border: 2px solid #ecdfcd; border-radius: 10px; background: #fffdfa;
            font-family: inherit; transition: border-color 0.15s ease, box-shadow 0.15s ease;
        }}
        input[type=text]:focus, input[type=number]:focus, input[type=password]:focus {{
            outline: none; border-color: var(--nom-orange); box-shadow: 0 0 0 3px rgba(255, 138, 61, 0.18);
        }}

        button {{
            margin-top: 22px; padding: 12px 24px; font-size: 0.98em; font-weight: 800;
            cursor: pointer; border: none; border-radius: 999px; color: white;
            background: linear-gradient(135deg, var(--nom-green) 0%, var(--nom-green-dark) 100%);
            box-shadow: 0 6px 16px rgba(26, 143, 94, 0.28);
            transition: transform 0.12s ease, box-shadow 0.12s ease;
            font-family: inherit;
        }}
        button:hover {{ transform: translateY(-1px); box-shadow: 0 8px 20px rgba(26, 143, 94, 0.35); }}
        button:active {{ transform: translateY(0); }}
        button.secondary {{
            background: #fff; color: var(--nom-ink); border: 2px solid #ecdfcd;
            box-shadow: none; margin-right: 8px;
        }}
        button.secondary:hover {{ box-shadow: none; border-color: var(--nom-orange); }}

        .status-box {{
            margin-top: 20px; padding: 14px 16px; border-radius: 12px;
            background: #fbf3e6; border: 1px solid #f0e2c9; font-size: 0.92em;
        }}
        .ok {{ background: #e9f8ef; border-color: #cdeedb; color: #14603f; }}
        .warn {{ background: #fff2e2; border-color: #f6ddb4; color: #8a5a12; }}
        pre {{ background: #26211c; color: #f2ead9; padding: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; border-radius: 10px; }}

        .info-icon {{
            display: inline-flex; align-items: center; justify-content: center;
            width: 16px; height: 16px; border-radius: 50%;
            background: var(--nom-orange); color: white; font-size: 11px; font-weight: bold;
            cursor: help; position: relative; flex-shrink: 0;
        }}
        .info-icon:hover .tooltip {{ display: block; }}
        .tooltip {{
            display: none; position: absolute; left: 22px; top: -4px; z-index: 10;
            background: #2b2420; color: #fff; padding: 8px 10px; border-radius: 8px;
            font-weight: normal; font-size: 0.8em; width: 240px; line-height: 1.4;
            box-shadow: 0 4px 14px rgba(0,0,0,0.25);
        }}
        .password-row {{ position: relative; }}
        .password-row input {{ padding-right: 64px; }}
        .toggle-password {{
            position: absolute; right: 6px; top: 50%; transform: translateY(calc(-50% + 3px));
            margin-top: 0; padding: 6px 10px; font-size: 0.75em; cursor: pointer;
            background: #f3e9d8; color: var(--nom-ink); border: none; border-radius: 999px;
            box-shadow: none;
        }}
        .screen {{ display: none; }}
        .screen.active {{ display: block; animation: nom-fade-in 0.25s ease; }}
        @keyframes nom-fade-in {{
            from {{ opacity: 0; transform: translateY(6px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .placeholder {{
            margin-top: 14px; padding: 16px; border: 2px dashed #f3c98f; border-radius: 14px;
            background: repeating-linear-gradient(45deg, #fffaf1, #fffaf1 10px, #fff3de 10px, #fff3de 20px);
            color: #8a6d3f;
        }}
        .placeholder-badge {{
            display: inline-block; background: var(--nom-orange); color: white; font-size: 0.7em;
            padding: 3px 10px; border-radius: 999px; font-weight: 800; margin-bottom: 8px;
            letter-spacing: 0.03em;
        }}
        .camera-preview {{ max-width: 100%; border-radius: 14px; margin-top: 14px; border: 3px solid #fff; box-shadow: var(--nom-shadow); }}
        .radio-group label {{ font-weight: normal; margin-top: 10px; }}
        .helper-text {{ color: var(--nom-muted); font-size: 0.88em; margin-top: 6px; }}
        .copy-row {{ display: flex; gap: 8px; align-items: center; margin-top: 8px; }}
        .copy-row input {{ flex: 1; }}
        .toggle-row {{ display: flex; align-items: center; gap: 10px; margin-top: 16px; }}
        .toggle-row label {{ margin-top: 0; }}
        code {{ background: #f3e9d8; padding: 2px 6px; border-radius: 6px; font-size: 0.9em; }}
        .hint-list {{ margin: 8px 0 0; padding-left: 18px; }}
        .hint-list li {{ margin-top: 6px; line-height: 1.45; }}
        @media (max-width: 640px) {{
            body {{ padding: 18px 10px 60px; }}
            .card {{ padding: 20px 16px; border-radius: 16px; }}
            .brand-row h1 {{ font-size: 1.6em; }}
            button {{ width: 100%; margin-right: 0; }}
            .toggle-password {{ width: auto; }}
            .copy-row {{ flex-wrap: wrap; }}
            .copy-row input {{ flex-basis: 100%; }}
            .copy-row button {{ flex: 1; width: auto; }}
            .tooltip {{ left: auto; right: 0; top: 20px; }}
        }}
    </style>
</head>
<body>
  <div class="app-shell">
    <div class="brand-row">
        <h1>🐾 NomWatch Setup</h1>
        <p class="brand-tag">Let's get your camera watching the food bowl &mdash; the fun way.</p>
        <p class="brand-tag"><a href="/">&larr; Back to dashboard</a></p>
    </div>
    <div class="progress-track" id="progress-track">
        <div class="progress-dot current" data-dot="1"></div>
        <div class="progress-dot" data-dot="2"></div>
        <div class="progress-dot" data-dot="3"></div>
        <div class="progress-dot" data-dot="4"></div>
        <div class="progress-dot" data-dot="5"></div>
        <div class="progress-dot" data-dot="6"></div>
    </div>
    <div class="step" id="step-label">Step 1 of 6 &mdash; Camera</div>
    <div class="card">

    <!-- SCREEN 1: Camera -->
    <div class="screen active" id="screen-1">
        <h2>Camera</h2>
        <div id="system-check-status" class="status-box">Checking required tools (ffmpeg)...</div>
        <form id="camera-form">
            <label>Camera LAN IP address
                <span class="info-icon">i<span class="tooltip">
                    The local network address of your camera, e.g. 192.168.1.250. Find it in your
                    router's connected-devices list, or in the camera's own app under Device Info.
                </span></span>
            </label>
            <input type="text" name="ip" placeholder="192.168.1.250" value="{ip}" required>

            <label>RTSP port
                <span class="info-icon">i<span class="tooltip">
                    Almost always 554 &mdash; this is the standard RTSP port, and most consumer cameras
                    (including Tapo) don't let you change it or even show it as a setting. Just leave
                    the default unless you know your camera uses something else.
                </span></span>
            </label>
            <input type="number" name="rtsp_port" value="{rtsp_port}" required>

            <label>Camera-account username
                <span class="info-icon">i<span class="tooltip">
                    A separate local login you create specifically for RTSP/third-party access &mdash;
                    NOT your camera app's cloud login. On Tapo, find this under Advanced Settings &rarr;
                    Camera Account.
                </span></span>
            </label>
            <input type="text" name="username" value="{username}" required>

            <label>Camera-account password
                <span class="info-icon">i<span class="tooltip">
                    The password for the camera account above (not your cloud account password).
                    Stored locally on this device only, never sent anywhere outside your own network.
                </span></span>
            </label>
            <div class="password-row">
                <input type="password" name="password" value="{password}" id="password-field" required>
                <button type="button" class="toggle-password" onclick="togglePassword()">Show</button>
            </div>

            <label>Stream path
                <span class="info-icon">i<span class="tooltip">
                    Which stream quality to pull: "stream1" is usually the main/HD feed, "stream2" a
                    lower-resolution one. Stick with stream1 unless you have a reason to use the other.
                </span></span>
            </label>
            <input type="text" name="stream_path" value="{stream_path}" required>

            <button type="button" class="secondary" onclick="testConnection()">Test connection</button>
        </form>
        <div id="camera-test-status"></div>
        <div id="camera-preview-container"></div>
        <div>
            <button onclick="saveCameraAndAdvance()">Save camera settings &amp; continue</button>
        </div>
    </div>

    <!-- SCREEN 2: Detection -->
    <div class="screen" id="screen-2">
        <h2>Detection settings</h2>
        <p class="helper-text">Now let's configure how NomWatch decides a feeding event is happening.</p>

        <div id="model-status" class="status-box">Checking for a local model server...</div>

        <label>Detection engine
            <span class="info-icon">i<span class="tooltip">How NomWatch decides feeding is happening. Motion-gated vision is recommended; hybrid is strictest; motion-only needs no AI model.</span></span>
        </label>
        <div class="radio-group" id="engine-group">
            <label><input type="radio" name="detection-engine" value="ollama"> Vision model (AI) &mdash; recommended</label>
            <label><input type="radio" name="detection-engine" value="hybrid"> Hybrid: motion AND the AI must agree (strictest, fewest false alarms)</label>
            <label><input type="radio" name="detection-engine" value="motion"> Motion only (no AI model at all)</label>
        </div>

        <label id="motion-gating-row" style="display:flex; align-items:center; gap:8px;">
            <input type="checkbox" id="motion-gating"> Only run the AI when the scene changes (ignore a static empty bowl)
            <span class="info-icon">i<span class="tooltip">Skips the vision model on frames with no motion since the last check, so a still empty scene never reaches the AI. Recommended - this is the main defense against empty-scene false alarms.</span></span>
        </label>

        <div id="motion-threshold-row">
            <label>Motion sensitivity threshold
                <span class="info-icon">i<span class="tooltip">Lower = more sensitive. Measured noise floor on a still scene is ~0.3, a moving cat ~20-48; 2.0 is a safe default.</span></span>
            </label>
            <input type="number" step="0.5" min="0" id="motion-threshold" value="{motion_threshold}">
        </div>

        <label>Minimum AI confidence to count (0.0&ndash;1.0)
            <span class="info-icon">i<span class="tooltip">The vision model must be at least this sure before a frame counts as feeding. Higher = fewer false alarms, but may miss borderline real ones. Run `nomwatch calibrate` to get a suggestion for your camera.</span></span>
        </label>
        <input type="number" step="0.05" min="0" max="1" id="min-confidence" value="{min_confidence}">

        <div style="border:1px solid var(--border, #d0d0d0); border-radius:8px; padding:10px; margin:12px 0;">
            <label style="display:flex; align-items:center; gap:8px; margin:0;">
                <input type="checkbox" id="zone-enabled" onchange="onZoneToggle()"> Restrict detection to a feeding zone (optional)
                <span class="info-icon">i<span class="tooltip">Crop every frame to just the bowl area before analysis, so motion and objects elsewhere in the room are ignored.</span></span>
            </label>
            <div id="zone-picker" style="display:none; margin-top:10px;">
                <button type="button" class="secondary" onclick="loadZoneSnapshot()">Load camera snapshot</button>
                <p class="helper-text">Then click and drag on the image to draw a box around the feeder bowl.</p>
                <div id="zone-canvas-wrap" style="position:relative; display:inline-block; max-width:100%; touch-action:none; user-select:none; cursor:crosshair;">
                    <img id="zone-img" alt="camera snapshot" style="max-width:100%; display:block; border-radius:6px;">
                    <div id="zone-rect" style="position:absolute; border:2px solid #16a34a; background:rgba(22,163,74,0.20); display:none; pointer-events:none;"></div>
                </div>
                <p class="helper-text" id="zone-coords"></p>
                <button type="button" class="secondary" onclick="clearZone()">Clear box</button>
            </div>
        </div>

        <label>Poll interval (seconds)
            <span class="info-icon">i<span class="tooltip">How often to check the camera for a feeding event.</span></span>
        </label>
        <input type="number" id="poll-interval" value="{poll_interval_seconds}">

        <label>Seconds of continuous eating required before notifying
            <span class="info-icon">i<span class="tooltip">Higher = fewer false alerts, more delay before you're notified.</span></span>
        </label>
        <input type="number" id="required-eating" value="{required_eating_seconds}">

        <label>Pre-roll seconds (video BEFORE the pet arrives)
            <span class="info-icon">i<span class="tooltip">Requires continuous local segment recording via MediaMTX. 0 disables pre-roll.</span></span>
        </label>
        <input type="number" id="pre-roll" value="{pre_roll_seconds}">

        <label>Post-confirm seconds (clip length after confirmation)</label>
        <input type="number" id="post-confirm" value="{clip_post_confirm_seconds}">

        <p class="helper-text" id="debounce-math"></p>

        <button onclick="saveDetectionAndAdvance()">Save detection settings &amp; continue</button>
        <button class="secondary" onclick="goToScreen(1)">Back</button>
    </div>

    <!-- SCREEN 3: Storage -->
    <div class="screen" id="screen-3">
        <h2>Storage</h2>
        <p class="helper-text">Pre-roll cache is always local. Choose where the FINAL event clip goes:</p>

        <div class="radio-group">
            <label><input type="radio" name="storage-provider" value="local" checked> Local folder only (no cloud, zero setup)</label>
            <label><input type="radio" name="storage-provider" value="google_drive_sync"> Google Drive, via Drive for Desktop's sync folder (zero OAuth)</label>
            <label><input type="radio" name="storage-provider" value="google_drive_api"> Google Drive, via direct API (advanced &mdash; requires your own OAuth client)</label>
            <label><input type="radio" name="storage-provider" value="none"> None</label>
        </div>

        <div id="storage-detail"></div>

        <button onclick="saveStorageAndAdvance()">Save storage settings &amp; continue</button>
        <button class="secondary" onclick="goToScreen(2)">Back</button>
    </div>

    <!-- SCREEN 4: Notifications -->
    <div class="screen" id="screen-4">
        <h2>Notifications</h2>
        <p class="helper-text">
            NomWatch uses <a href="https://ntfy.sh" target="_blank">ntfy</a>, a free push notification
            service with no account required. Install the app, subscribe to your topic below, and
            you'll get a push the moment a feeding is confirmed.
        </p>
        <p>
            <a href="https://apps.apple.com/us/app/ntfy/id1625396347" target="_blank">iOS App Store</a> &middot;
            <a href="https://play.google.com/store/apps/details?id=io.heckel.ntfy" target="_blank">Google Play</a> &middot;
            <a href="https://ntfy.sh/app" target="_blank">Web app</a>
        </p>

        <label>Your ntfy topic (hard to guess is safer &mdash; anyone with this name can read your notifications)</label>
        <div class="copy-row">
            <input type="text" id="ntfy-topic" value="{ntfy_topic}">
            <button type="button" class="secondary" onclick="copyTopicLink()">Copy link</button>
            <button type="button" class="secondary" onclick="copyTopicCode()">Copy code</button>
            <button type="button" class="secondary" onclick="regenerateTopic()">Generate new</button>
        </div>
        <div id="qr-container" style="margin-top:12px;"></div>
        <p class="helper-text">
            Scan the QR code above with your phone to subscribe instantly, or paste "Copy link" into a
            browser, or paste "Copy code" directly into the ntfy app's "Subscribe to topic" field. (QR
            image is generated by the free api.qrserver.com service - your topic name is sent to them
            to render the image.)
        </p>

        <button type="button" class="secondary" onclick="testNotification()">Test now</button>
        <div id="test-notify-status" class="helper-text"></div>

        <button onclick="saveNotifyAndAdvance()">Save notification settings &amp; continue</button>
        <button class="secondary" onclick="goToScreen(3)">Back</button>
    </div>

    <!-- SCREEN 5: Appearance ID -->
    <div class="screen" id="screen-5">
        <h2>Tell NomWatch about your pet(s)</h2>
        <p class="helper-text">
            Describe your pet's species/color/breed (e.g. "black cat", "golden retriever named Max").
            This is fed to the vision model as a hint so it looks for your specific animal and can rule
            out other animals or people near the bowl. Optional - leave blank to detect any animal.
        </p>
        <label style="margin-top:8px;">Pet description</label>
        <input type="text" id="pet-description" placeholder="e.g. black cat, golden retriever named Max" value="{pet_description}">

        <button onclick="saveAppearanceAndAdvance()">Continue</button>
        <button class="secondary" onclick="goToScreen(4)">Back</button>
    </div>

    <!-- SCREEN 6: Live stream -->
    <div class="screen" id="screen-6">
        <h2>Live stream</h2>
        <p class="helper-text">
            This shows the actual live video feed from MediaMTX (the local bridge that pulls RTSP from
            your camera).
        </p>
        <div id="mediamtx-status" class="status-box">Checking if MediaMTX is running...</div>
        <div id="mediamtx-actions"></div>
        <video id="live-video" controls autoplay muted style="width:100%; max-width:640px; border-radius:8px; margin-top:12px; display:none; background:#000;"></video>

        <button onclick="finishSetup()">Finish setup &amp; go to dashboard</button>
        <button class="secondary" onclick="goToScreen(5)">Back</button>
    </div>

    <div id="final-message" class="status-box ok" style="display:none;">
        ✅ Setup complete! Taking you to your dashboard...
    </div>
    </div>
  </div>

    <script>
        const stepLabels = {{
            1: "Step 1 of 6 &mdash; Camera",
            2: "Step 2 of 6 &mdash; Detection settings",
            3: "Step 3 of 6 &mdash; Storage",
            4: "Step 4 of 6 &mdash; Notifications",
            5: "Step 5 of 6 &mdash; Tell NomWatch about your pet",
            6: "Step 6 of 6 &mdash; Live stream",
        }};

        let hlsPlayer = null;

        function goToScreen(n) {{
            document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
            document.getElementById('screen-' + n).classList.add('active');
            document.getElementById('step-label').innerHTML = stepLabels[n];
            document.querySelectorAll('.progress-dot').forEach(dot => {{
                const dotStep = parseInt(dot.getAttribute('data-dot'));
                dot.classList.remove('done', 'current');
                if (dotStep < n) dot.classList.add('done');
                else if (dotStep === n) dot.classList.add('current');
            }});
            if (n === 2) checkModel();
            if (n === 2) updateDebounceMath();
            if (n === 3) updateStorageDetail();
            if (n === 4) renderQr();
            if (n === 6) checkMediaMtxAndPlay();
        }}

        function togglePassword() {{
            const field = document.getElementById('password-field');
            const btn = document.querySelector('.toggle-password');
            if (field.type === 'password') {{ field.type = 'text'; btn.textContent = 'Hide'; }}
            else {{ field.type = 'password'; btn.textContent = 'Show'; }}
        }}

        function cameraFormData() {{
            const form = document.getElementById('camera-form');
            return Object.fromEntries(new FormData(form));
        }}

        function esc(s) {{
            return String(s ?? '').replace(/[&<>"']/g, c => ({{
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }})[c]);
        }}

        async function testConnection() {{
            const statusEl = document.getElementById('camera-test-status');
            const previewEl = document.getElementById('camera-preview-container');
            statusEl.innerHTML = '<div class="status-box">Testing connection... (up to ~10s)</div>';
            previewEl.innerHTML = '';
            const res = await fetch('/api/test-camera', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(cameraFormData()),
            }});
            const data = await res.json();
            if (data.ok) {{
                statusEl.innerHTML = '<div class="status-box ok">Connected! Here is your camera feed right now:</div>';
                previewEl.innerHTML = `<img class="camera-preview" src="data:image/jpeg;base64,${{data.image}}">
                    <p class="helper-text">This is a single still-frame snapshot, not a live stream.</p>`;
            }} else {{
                const hints = (data.hints || []).map(h => `<li>${{esc(h)}}</li>`).join('');
                statusEl.innerHTML = `<div class="status-box warn">
                    <strong>${{esc(data.error)}}</strong>
                    ${{hints ? `<ul class="hint-list">${{hints}}</ul>` : ''}}
                    ${{data.detail ? `<details style="margin-top:8px;"><summary>Technical details</summary><pre>${{esc(data.detail)}}</pre></details>` : ''}}
                </div>`;
            }}
        }}

        async function checkSystem() {{
            const res = await fetch('/api/check-system');
            const data = await res.json();
            const el = document.getElementById('system-check-status');
            if (data.ffmpeg) {{
                el.className = 'status-box ok';
                el.textContent = 'ffmpeg found - camera testing/recording is ready.';
            }} else {{
                el.className = 'status-box warn';
                el.innerHTML = `ffmpeg not found - required to capture frames/clips from your camera.<br>
                    <button onclick="installFfmpeg()">Install ffmpeg now</button>
                    (or run <code>brew install ffmpeg</code> yourself in a terminal)
                    <pre id="ffmpeg-log" style="display:none"></pre>`;
            }}
        }}

        async function installFfmpeg() {{
            const log = document.getElementById('ffmpeg-log');
            log.style.display = 'block';
            log.textContent = 'Installing... this can take a few minutes.';
            const res = await fetch('/api/install-ffmpeg', {{ method: 'POST' }});
            const data = await res.json();
            log.textContent = data.output || (data.success ? 'Done.' : 'Install failed - see terminal output above, or install manually.');
            checkSystem();
        }}

        async function saveCameraAndAdvance() {{
            const res = await fetch('/api/save-camera', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(cameraFormData()),
            }});
            const data = await res.json();
            if (!data.ok) {{
                document.getElementById('camera-test-status').innerHTML =
                    `<div class="status-box warn">Couldn't save: ${{esc(data.error)}}</div>`;
                return;
            }}
            goToScreen(2);
        }}

        async function checkModel() {{
            const res = await fetch('/api/check-model');
            const data = await res.json();
            const el = document.getElementById('model-status');
            if (data.vision_model) {{
                el.className = 'status-box ok';
                el.innerHTML = `Found local Ollama server with vision-capable model: <b>${{data.vision_model}}</b>`;
            }} else if (data.server_running) {{
                el.className = 'status-box warn';
                el.innerHTML = `Found local Ollama server, but no vision-capable model installed.<br>
                    <button onclick="pullModel()">Install ${{data.default_model}} now</button>
                    <pre id="pull-log" style="display:none"></pre>`;
            }} else {{
                el.className = 'status-box warn';
                el.innerHTML = `No local Ollama server detected. Install it from
                    <a href="https://ollama.com/download" target="_blank">ollama.com/download</a>, then
                    <button onclick="checkModel()">recheck</button>`;
            }}
        }}

        async function pullModel() {{
            const log = document.getElementById('pull-log');
            log.style.display = 'block';
            log.textContent = 'Installing... this can take a few minutes.';
            const res = await fetch('/api/pull-model', {{ method: 'POST' }});
            const data = await res.json();
            log.textContent = data.output || '';
            checkModel();
        }}

        function updateDebounceMath() {{
            const poll = parseInt(document.getElementById('poll-interval').value) || 10;
            const eating = parseInt(document.getElementById('required-eating').value) || 20;
            const required = Math.max(1, Math.round(eating / poll));
            document.getElementById('debounce-math').textContent =
                `-> Will require ${{required}} consecutive positive checks in a row (~${{required * poll}}s) before notifying.`;
        }}
        document.addEventListener('input', (e) => {{
            if (['poll-interval', 'required-eating'].includes(e.target.id)) updateDebounceMath();
        }});

        // --- detection engine + zone picker ---
        const DETECTION_INIT = {{
            engine: "{detection_engine}",
            motion_gating: {motion_gating_js},
            zone_enabled: {zone_enabled_js},
            zone: {zone_js}
        }};
        let zoneNorm = DETECTION_INIT.zone;  // normalized {{x,y,w,h}} or null

        function updateEngineUI() {{
            const sel = document.querySelector('input[name="detection-engine"]:checked');
            const engine = sel ? sel.value : 'ollama';
            document.getElementById('motion-gating-row').style.display = (engine === 'ollama') ? 'flex' : 'none';
            const gating = document.getElementById('motion-gating').checked;
            const motionUsed = (engine === 'motion' || engine === 'hybrid' || (engine === 'ollama' && gating));
            document.getElementById('motion-threshold-row').style.display = motionUsed ? 'block' : 'none';
        }}
        function initDetectionForm() {{
            const radio = document.querySelector(`input[name="detection-engine"][value="${{DETECTION_INIT.engine}}"]`);
            if (radio) radio.checked = true;
            document.getElementById('motion-gating').checked = DETECTION_INIT.motion_gating;
            if (DETECTION_INIT.zone_enabled) {{
                document.getElementById('zone-enabled').checked = true;
                onZoneToggle();
            }}
            updateEngineUI();
        }}
        document.addEventListener('change', (e) => {{
            if (e.target.name === 'detection-engine' || e.target.id === 'motion-gating') updateEngineUI();
        }});
        if (document.readyState !== 'loading') initDetectionForm();
        else window.addEventListener('DOMContentLoaded', initDetectionForm);

        function onZoneToggle() {{
            const on = document.getElementById('zone-enabled').checked;
            document.getElementById('zone-picker').style.display = on ? 'block' : 'none';
        }}
        function zoneText(n) {{
            return `Zone: ${{Math.round(n.w*100)}}% x ${{Math.round(n.h*100)}}% of frame, ` +
                   `top-left at (${{Math.round(n.x*100)}}%, ${{Math.round(n.y*100)}}%).`;
        }}
        function drawZoneRect(n) {{
            const img = document.getElementById('zone-img');
            const rect = document.getElementById('zone-rect');
            rect.style.display = 'block';
            rect.style.left = (n.x * img.clientWidth) + 'px';
            rect.style.top = (n.y * img.clientHeight) + 'px';
            rect.style.width = (n.w * img.clientWidth) + 'px';
            rect.style.height = (n.h * img.clientHeight) + 'px';
        }}
        function clearZone() {{
            zoneNorm = null;
            document.getElementById('zone-rect').style.display = 'none';
            document.getElementById('zone-coords').textContent = 'Zone cleared - the full frame will be analysed.';
        }}
        async function loadZoneSnapshot() {{
            const coords = document.getElementById('zone-coords');
            coords.textContent = 'Loading snapshot...';
            let data;
            try {{
                const res = await fetch('/api/snapshot');
                data = await res.json();
            }} catch (err) {{ coords.textContent = 'Could not load snapshot.'; return; }}
            if (!data.ok) {{ coords.textContent = 'Could not load snapshot: ' + (data.error || ''); return; }}
            const img = document.getElementById('zone-img');
            img.onload = () => {{ if (zoneNorm) drawZoneRect(zoneNorm); }};
            img.src = 'data:image/jpeg;base64,' + data.image;
            coords.textContent = zoneNorm ? zoneText(zoneNorm) : 'Click and drag on the image to draw the feeding zone.';
        }}
        (function setupZoneDrag() {{
            const wrap = document.getElementById('zone-canvas-wrap');
            if (!wrap) return;
            let dragging = false, sx = 0, sy = 0;
            const relative = (e) => {{
                const img = document.getElementById('zone-img');
                const r = img.getBoundingClientRect();
                const cx = e.clientX, cy = e.clientY;
                return {{ x: Math.min(Math.max(cx - r.left, 0), r.width), y: Math.min(Math.max(cy - r.top, 0), r.height) }};
            }};
            wrap.addEventListener('pointerdown', (e) => {{
                const img = document.getElementById('zone-img');
                if (!img.src) return;
                dragging = true;
                const p = relative(e);
                sx = p.x; sy = p.y;
                const rect = document.getElementById('zone-rect');
                rect.style.display = 'block';
                rect.style.left = sx + 'px'; rect.style.top = sy + 'px';
                rect.style.width = '0px'; rect.style.height = '0px';
                e.preventDefault();
            }});
            wrap.addEventListener('pointermove', (e) => {{
                if (!dragging) return;
                const p = relative(e);
                const rect = document.getElementById('zone-rect');
                rect.style.left = Math.min(sx, p.x) + 'px';
                rect.style.top = Math.min(sy, p.y) + 'px';
                rect.style.width = Math.abs(p.x - sx) + 'px';
                rect.style.height = Math.abs(p.y - sy) + 'px';
            }});
            const finish = (e) => {{
                if (!dragging) return;
                dragging = false;
                const p = relative(e);
                const img = document.getElementById('zone-img');
                const x = Math.min(sx, p.x) / img.clientWidth;
                const y = Math.min(sy, p.y) / img.clientHeight;
                const w = Math.abs(p.x - sx) / img.clientWidth;
                const h = Math.abs(p.y - sy) / img.clientHeight;
                if (w < 0.02 || h < 0.02) {{ document.getElementById('zone-coords').textContent = 'Box too small - drag a larger area.'; return; }}
                zoneNorm = {{ x: +x.toFixed(4), y: +y.toFixed(4), w: +w.toFixed(4), h: +h.toFixed(4) }};
                document.getElementById('zone-coords').textContent = zoneText(zoneNorm);
            }};
            wrap.addEventListener('pointerup', finish);
            wrap.addEventListener('pointerleave', finish);
        }})();

        async function saveDetectionAndAdvance() {{
            const poll = parseInt(document.getElementById('poll-interval').value) || 10;
            const eating = parseInt(document.getElementById('required-eating').value) || 20;
            const preRoll = parseInt(document.getElementById('pre-roll').value) || 0;
            const postConfirm = parseInt(document.getElementById('post-confirm').value) || 0;
            const engine = document.querySelector('input[name="detection-engine"]:checked').value;
            const motionGating = document.getElementById('motion-gating').checked;
            const motionThreshold = parseFloat(document.getElementById('motion-threshold').value) || 2.0;
            const minConfidence = parseFloat(document.getElementById('min-confidence').value);
            const zoneEnabled = document.getElementById('zone-enabled').checked && !!zoneNorm;
            const body = {{
                poll_interval_seconds: poll,
                required_eating_seconds: eating,
                pre_roll_seconds: preRoll,
                clip_post_confirm_seconds: postConfirm,
                engine: engine,
                motion_gating: motionGating,
                motion_threshold: motionThreshold,
                min_confidence: minConfidence,
                zone_detection_enabled: zoneEnabled,
            }};
            if (zoneEnabled) {{
                body.zone_x = zoneNorm.x; body.zone_y = zoneNorm.y;
                body.zone_w = zoneNorm.w; body.zone_h = zoneNorm.h;
            }}
            const res = await fetch('/api/save-detection', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(body),
            }});
            const data = await res.json();
            const statusEl = document.getElementById('model-status');
            if (!data.ok) {{
                statusEl.className = 'status-box warn';
                statusEl.textContent = "Couldn't save: " + data.error;
                return;
            }}
            if (data.warning) {{
                // Saved, but the user should know detection isn't fully set
                // up - show it, still advance.
                alert('Saved, but heads up: ' + data.warning);
            }}
            goToScreen(3);
        }}

        function updateStorageDetail() {{
            const choice = document.querySelector('input[name="storage-provider"]:checked').value;
            const el = document.getElementById('storage-detail');
            if (choice === 'local') {{
                el.innerHTML = `<label>Local folder path</label>
                    <input type="text" id="local-save-dir" placeholder="~/.config/nomwatch/clips" value="{local_save_dir}">
                    <p class="helper-text">Leave blank to use the default (~/.config/nomwatch/clips).</p>`;
            }} else if (choice === 'google_drive_sync') {{
                el.innerHTML = `<button type="button" class="secondary" onclick="detectDriveSync()">Auto-detect Drive folder</button>
                    <div id="drive-sync-result" class="helper-text"></div>`;
            }} else if (choice === 'google_drive_api') {{
                el.innerHTML = `<p class="helper-text">Requires a one-time OAuth client setup - see docs/GOOGLE_DRIVE_SETUP.md.</p>`;
            }} else {{
                el.innerHTML = '';
            }}
        }}
        document.addEventListener('change', (e) => {{
            if (e.target.name === 'storage-provider') updateStorageDetail();
        }});

        async function detectDriveSync() {{
            const res = await fetch('/api/detect-drive-sync');
            const data = await res.json();
            const el = document.getElementById('drive-sync-result');
            el.textContent = data.folder ? `Found: ${{data.folder}}` : 'Not found - install Google Drive for Desktop, or skip for now.';
        }}

        async function saveStorageAndAdvance() {{
            const choice = document.querySelector('input[name="storage-provider"]:checked').value;
            const body = {{ provider: choice }};
            if (choice === 'local') {{
                const dirField = document.getElementById('local-save-dir');
                if (dirField && dirField.value) body.local_save_dir = dirField.value;
            }}
            const res = await fetch('/api/save-storage', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(body),
            }});
            const data = await res.json();
            const detailEl = document.getElementById('storage-detail');
            if (!data.ok) {{
                detailEl.insertAdjacentHTML('beforeend',
                    `<div class="status-box warn">Couldn't save: ${{esc(data.error)}}</div>`);
                return;
            }}
            if (data.warning) alert('Saved, but heads up: ' + data.warning);
            goToScreen(4);
        }}

        function renderQr() {{
            const topic = document.getElementById('ntfy-topic').value;
            const url = `https://ntfy.sh/${{topic}}`;
            document.getElementById('qr-container').innerHTML =
                `<img src="https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=${{encodeURIComponent(url)}}" alt="QR code for ${{url}}">`;
        }}

        function copyTopicLink() {{
            const topic = document.getElementById('ntfy-topic').value;
            navigator.clipboard.writeText(`https://ntfy.sh/${{topic}}`);
            alert('Copied link: https://ntfy.sh/' + topic);
        }}

        function copyTopicCode() {{
            const topic = document.getElementById('ntfy-topic').value;
            navigator.clipboard.writeText(topic);
            alert('Copied topic code: ' + topic);
        }}

        async function regenerateTopic() {{
            const res = await fetch('/api/generate-topic');
            const data = await res.json();
            document.getElementById('ntfy-topic').value = data.topic;
            renderQr();
        }}

        async function testNotification() {{
            const topic = document.getElementById('ntfy-topic').value;
            const statusEl = document.getElementById('test-notify-status');
            statusEl.textContent = 'Sending test notification...';
            const res = await fetch('/api/test-notify', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ ntfy_topic: topic }}),
            }});
            const data = await res.json();
            statusEl.textContent = data.ok
                ? 'Sent! Check your phone/ntfy app for a push notification.'
                : `Failed to send: ${{data.error}}`;
        }}

        async function saveNotifyAndAdvance() {{
            const topic = document.getElementById('ntfy-topic').value;
            const res = await fetch('/api/save-notify', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ provider: 'ntfy', ntfy_topic: topic }}),
            }});
            const data = await res.json();
            if (!data.ok) {{
                document.getElementById('test-notify-status').textContent = "Couldn't save: " + data.error;
                return;
            }}
            goToScreen(5);
        }}

        async function saveAppearanceAndAdvance() {{
            const description = document.getElementById('pet-description').value;
            await fetch('/api/save-appearance', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ pet_description: description }}),
            }});
            goToScreen(6);
        }}

        async function checkMediaMtxAndPlay() {{
            const statusEl = document.getElementById('mediamtx-status');
            const actionsEl = document.getElementById('mediamtx-actions');
            const videoEl = document.getElementById('live-video');
            actionsEl.innerHTML = '';

            const res = await fetch('/api/check-mediamtx');
            const data = await res.json();

            if (data.reachable) {{
                statusEl.className = 'status-box ok';
                statusEl.textContent = 'MediaMTX is running - loading live video...';
                actionsEl.innerHTML = `<button class="secondary" onclick="startMediaMtx()">Restart MediaMTX (apply the settings you just saved)</button>
                    <pre id="mediamtx-log" style="display:none"></pre>`;
                videoEl.style.display = 'block';

                if (hlsPlayer) {{ hlsPlayer.destroy(); }}
                if (Hls.isSupported()) {{
                    hlsPlayer = new Hls();
                    hlsPlayer.loadSource(data.url);
                    hlsPlayer.attachMedia(videoEl);
                }} else if (videoEl.canPlayType('application/vnd.apple.mpegurl')) {{
                    videoEl.src = data.url;
                }} else {{
                    statusEl.textContent = 'This browser cannot play HLS video.';
                }}
                return;
            }}

            videoEl.style.display = 'none';
            if (!data.mediamtx_installed) {{
                statusEl.className = 'status-box warn';
                statusEl.textContent = "MediaMTX isn't installed.";
                actionsEl.innerHTML = `<button onclick="installMediaMtx()">Install MediaMTX now</button>
                    <pre id="mediamtx-log" style="display:none"></pre>`;
            }} else {{
                statusEl.className = 'status-box warn';
                statusEl.textContent = "MediaMTX is installed but not running.";
                actionsEl.innerHTML = `<button onclick="startMediaMtx()">Start MediaMTX</button>
                    <button class="secondary" onclick="checkMediaMtxAndPlay()">Recheck</button>
                    <pre id="mediamtx-log" style="display:none"></pre>`;
            }}
        }}

        async function installMediaMtx() {{
            const log = document.getElementById('mediamtx-log');
            log.style.display = 'block';
            log.textContent = 'Installing... this can take a minute.';
            const res = await fetch('/api/install-mediamtx', {{ method: 'POST' }});
            const data = await res.json();
            log.textContent = data.output || (data.success ? 'Done.' : 'Install failed - see output above.');
            checkMediaMtxAndPlay();
        }}

        async function startMediaMtx() {{
            const log = document.getElementById('mediamtx-log');
            log.style.display = 'block';
            log.textContent = 'Starting MediaMTX...';
            const res = await fetch('/api/start-mediamtx', {{ method: 'POST' }});
            const data = await res.json();
            log.textContent = data.ok ? `Started (pid ${{data.pid}}). Checking stream...` : `Failed to start: ${{data.error}}`;
            setTimeout(checkMediaMtxAndPlay, 1500);  // give it a moment to bind/connect
        }}

        function finishSetup() {{
            document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
            document.getElementById('step-label').textContent = 'All done!';
            document.getElementById('final-message').style.display = 'block';
            setTimeout(() => {{ window.location.href = '/'; }}, 900);
        }}

        checkSystem();
    </script>
</body>
</html>
"""


DASHBOARD_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>NomWatch</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
    <style>
        :root {{
            --nom-bg-1: #fff8ec; --nom-bg-2: #ffe9d6;
            --nom-green: #1a8f5e; --nom-green-dark: #12704a;
            --nom-orange: #ff8a3d; --nom-orange-dark: #e96f22;
            --nom-ink: #2b2420; --nom-muted: #7a6f66;
            --nom-card: #ffffff; --nom-border: #f0e2d2;
            --nom-shadow: 0 12px 32px rgba(120, 72, 20, 0.10), 0 2px 8px rgba(120, 72, 20, 0.06);
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: "Nunito", -apple-system, system-ui, sans-serif;
            margin: 0; padding: 40px 20px 80px; color: var(--nom-ink);
            background:
                radial-gradient(circle at 10% -10%, #fff2df 0%, transparent 45%),
                radial-gradient(circle at 100% 0%, #e8f6ee 0%, transparent 40%),
                linear-gradient(180deg, var(--nom-bg-1) 0%, var(--nom-bg-2) 100%);
            min-height: 100vh;
        }}
        .app-shell {{ max-width: 760px; margin: 0 auto; }}
        .brand-row {{ text-align: center; margin-bottom: 28px; }}
        .brand-row h1 {{ font-size: 2em; margin: 6px 0 4px; letter-spacing: -0.02em; color: var(--nom-green-dark); }}
        .brand-tag {{ color: var(--nom-muted); font-size: 0.95em; margin: 0; }}
        .brand-tag a {{ color: var(--nom-green-dark); font-weight: 700; text-decoration: none; }}
        .brand-tag a:hover {{ text-decoration: underline; }}
        .card {{
            background: var(--nom-card); border-radius: 20px; padding: 26px 30px;
            box-shadow: var(--nom-shadow); border: 1px solid var(--nom-border);
            margin-bottom: 22px;
        }}
        .card h2 {{ font-size: 1.15em; margin: 0 0 4px; }}
        .card h2::before {{ content: "🐾  "; }}
        .helper-text {{ color: var(--nom-muted); font-size: 0.88em; margin-top: 4px; }}
        .status-box {{
            margin-top: 14px; padding: 12px 14px; border-radius: 12px;
            background: #fbf3e6; border: 1px solid #f0e2c9; font-size: 0.92em;
        }}
        .ok {{ background: #e9f8ef; border-color: #cdeedb; color: #14603f; }}
        .warn {{ background: #fff2e2; border-color: #f6ddb4; color: #8a5a12; }}
        .bad {{ background: #fdeaea; border-color: #f3c6c6; color: #8a1f1f; }}
        button {{
            margin-top: 14px; padding: 11px 22px; font-size: 0.95em; font-weight: 800;
            cursor: pointer; border: none; border-radius: 999px; color: white;
            background: linear-gradient(135deg, var(--nom-green) 0%, var(--nom-green-dark) 100%);
            box-shadow: 0 6px 16px rgba(26, 143, 94, 0.28);
            transition: transform 0.12s ease, box-shadow 0.12s ease;
            font-family: inherit;
        }}
        button:hover {{ transform: translateY(-1px); }}
        button.secondary {{ background: #fff; color: var(--nom-ink); border: 2px solid #ecdfcd; box-shadow: none; margin-right: 8px; }}
        button.stop {{ background: linear-gradient(135deg, #e05656 0%, #b23a3a 100%); box-shadow: 0 6px 16px rgba(224, 86, 86, 0.28); }}
        video {{ width: 100%; max-width: 100%; border-radius: 14px; margin-top: 14px; background: #000; }}
        pre {{ background: #26211c; color: #f2ead9; padding: 12px; max-height: 160px; overflow-y: auto; font-size: 0.8em; border-radius: 10px; }}
        .setup-banner {{
            text-align: center; padding: 26px; border-radius: 20px;
            background: linear-gradient(135deg, #fff1de, #ffe4cc);
            border: 1px dashed var(--nom-orange); margin-bottom: 22px;
        }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.88em; }}
        th, td {{ text-align: left; padding: 8px 6px; border-bottom: 1px solid #f0e2d2; }}
        th {{ color: var(--nom-muted); font-weight: 700; }}
        .empty-note {{ color: var(--nom-muted); font-size: 0.9em; margin-top: 10px; }}
        code {{ background: #f3e9d8; padding: 2px 6px; border-radius: 6px; font-size: 0.9em; }}
        .heartbeat-line {{ margin-top: 10px; font-size: 0.88em; color: var(--nom-muted); }}
        .stale-note {{
            margin-top: 10px; padding: 10px 12px; border-radius: 10px; font-size: 0.88em;
            background: #fff2e2; border: 1px dashed var(--nom-orange); color: #8a5a12;
        }}
        .gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; margin-top: 14px; }}
        .event-card {{
            border: 1px solid var(--nom-border); border-radius: 14px; overflow: hidden;
            background: #fffdf9; display: flex; flex-direction: column;
        }}
        .event-thumb {{
            width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block;
            background: #26211c; cursor: pointer; border: none; padding: 0; margin: 0; border-radius: 0;
        }}
        .event-thumb-none {{
            width: 100%; aspect-ratio: 16/9; display: flex; align-items: center; justify-content: center;
            background: #f3e9d8; color: var(--nom-muted); font-size: 0.85em;
        }}
        .event-body {{ padding: 10px 12px 12px; font-size: 0.85em; }}
        .event-when {{ font-weight: 800; }}
        .event-conf {{ color: var(--nom-muted); }}
        .event-reason {{ margin: 6px 0 0; color: #4a4038; }}
        .event-error {{ margin: 6px 0 0; color: #a33; }}
        .event-actions {{ display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }}
        .event-actions button, .event-actions a {{
            margin: 0; padding: 6px 12px; font-size: 0.82em; font-weight: 700;
        }}
        .event-actions a {{
            display: inline-block; border-radius: 999px; text-decoration: none;
            background: #fff; color: var(--nom-ink); border: 2px solid #ecdfcd; cursor: pointer;
        }}
        .player-overlay {{
            display: none; position: fixed; inset: 0; z-index: 50;
            background: rgba(30, 22, 14, 0.75); align-items: center; justify-content: center; padding: 20px;
        }}
        .player-overlay.open {{ display: flex; }}
        .player-box {{ max-width: 820px; width: 100%; }}
        .player-box video {{ width: 100%; border-radius: 14px; background: #000; margin: 0; }}
        .player-close {{ margin: 0 0 10px; float: right; }}
        @media (max-width: 640px) {{
            body {{ padding: 18px 10px 60px; }}
            .card {{ padding: 18px 16px; border-radius: 16px; }}
            .brand-row h1 {{ font-size: 1.6em; }}
            button {{ width: 100%; margin-right: 0; }}
            button.secondary {{ margin-right: 0; }}
            .event-actions button, .event-actions a {{ width: auto; flex: 1; text-align: center; }}
            .gallery {{ grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }}
        }}
    </style>
</head>
<body>
  <div class="app-shell">
    <div class="brand-row">
        <h1>🐾 NomWatch</h1>
        <p class="brand-tag">Your feeder camera, at a glance.</p>
    </div>

    <div id="setup-banner" class="setup-banner" style="display:none;">
        <p style="margin:0 0 12px; font-weight:700;">You haven't finished setting up a camera yet.</p>
        <button onclick="window.location.href='/setup'">Run setup</button>
    </div>

    <div class="card">
        <h2>Live view</h2>
        <p class="helper-text">Camera: <strong>{camera_ip}</strong></p>
        <div id="mediamtx-status" class="status-box">Checking if MediaMTX is running...</div>
        <div id="mediamtx-stale"></div>
        <div id="mediamtx-actions"></div>
        <video id="live-video" controls autoplay muted style="display:none;"></video>
    </div>

    <div class="card">
        <h2>Monitoring</h2>
        <p class="helper-text">
            This is the actual detection loop (<code>nomwatch run</code>) that watches for feeding
            events, sends notifications, and uploads clips. It must be running for any of that to
            happen &mdash; configuring settings alone doesn't start it.
        </p>
        <div id="monitor-status" class="status-box">Checking...</div>
        <div id="monitor-heartbeat" class="heartbeat-line"></div>
        <div id="monitor-stale"></div>
        <div id="monitor-actions"></div>
        <p class="helper-text" id="monitor-detail">
            Model: <strong>{detection_model}</strong> &middot;
            Requires {required_eating_seconds}s of continuous eating (polling every {poll_interval_seconds}s)
        </p>
    </div>

    <div class="card">
        <h2>Automatic start-up</h2>
        <p class="helper-text">
            Without this, monitoring stops when you reboot or log out and you'd have to come back
            and press Start. Installing the auto-start service (macOS launchd) makes
            <code>nomwatch run</code> launch at login and restart itself if it ever crashes.
        </p>
        <div id="service-status" class="status-box">Checking...</div>
        <div id="service-actions"></div>
    </div>

    <div class="card">
        <h2>Notifications</h2>
        <p class="helper-text">
            Provider: <strong>{notify_provider}</strong>
            {ntfy_topic_line}
        </p>
        <button class="secondary" onclick="testNotify()">Send test notification</button>
        <div id="notify-test-status"></div>
    </div>

    <div class="card">
        <h2>Feeding events</h2>
        <p class="helper-text">
            Every confirmed feeding, newest first. Click a thumbnail to watch the clip right here.
            Deleting a clip frees disk space but keeps the event in the history (and doesn't touch
            copies already synced to cloud storage).
        </p>
        <div id="events-container"><p class="empty-note">Loading...</p></div>
        <div style="margin-top:12px; border-top:1px solid var(--border, #e0e0e0); padding-top:10px;">
            <button class="secondary" type="button" onclick="clearHistory()">Clear history &amp; clips</button>
            <span class="helper-text" id="clear-history-status"></span>
            <p class="helper-text">
                Moves all events, clips, thumbnails and diagnostic logs into a recoverable archive
                folder (great after a batch of false alarms). To permanently free the disk space,
                run <code>nomwatch prune --delete</code> in a terminal.
            </p>
        </div>
    </div>

    <div class="card">
        <h2>Backup &amp; restore</h2>
        <p class="helper-text">
            Your whole configuration lives in one local file (<code>config.yml</code>, includes the
            camera password &mdash; treat the backup like a password). Losing it means redoing the
            wizard, so keep a copy somewhere safe.
        </p>
        <a href="/api/export-config" download><button class="secondary" type="button">Download config backup</button></a>
        <input type="file" id="restore-file" accept=".yml,.yaml" style="display:none" onchange="restoreConfig(this.files[0])">
        <button class="secondary" type="button" onclick="document.getElementById('restore-file').click()">Restore from backup...</button>
        <div id="restore-status"></div>
    </div>

    <div class="card" style="text-align:center;">
        <button class="secondary" onclick="window.location.href='/setup'">Reconfigure / rerun setup</button>
    </div>
  </div>

  <div class="player-overlay" id="player-overlay" onclick="if (event.target === this) closePlayer()">
    <div class="player-box">
        <button class="player-close" onclick="closePlayer()">Close</button>
        <video id="player-video" controls autoplay playsinline></video>
    </div>
  </div>

    <script>
        let hlsPlayer = null;
        const ntfyTopic = {ntfy_topic_json};

        // Escape BEFORE inserting anything data-derived into innerHTML.
        // Event "reasoning" text comes from a vision model looking at a
        // camera - treat it as untrusted, not as HTML.
        function esc(s) {{
            return String(s ?? '').replace(/[&<>"']/g, c => ({{
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }})[c]);
        }}

        function describeHeartbeat(hb) {{
            if (!hb) return '';
            const age = Math.round(hb.age_seconds);
            const staleAfter = Math.max(30, (hb.poll_interval_seconds || 10) * 3);
            let what;
            if (hb.phase === 'recording clip') what = 'recording an event clip 🎥';
            else if (hb.error) what = '⚠️ ' + esc(hb.error);
            else if (hb.is_feeding) what = `saw feeding (confidence ${{(hb.confidence ?? 0).toFixed(2)}}, streak ${{hb.streak}})`;
            else what = `no feeding (confidence ${{(hb.confidence ?? 0).toFixed(2)}})`;
            let line = `Last check: ${{age}}s ago &mdash; ${{what}}`;
            if (hb.storage_error) line += `<br>⚠️ ${{esc(hb.storage_error)}}`;
            if (age > staleAfter) {{
                line = `⚠️ No activity for ${{age}}s (expected a check every ${{hb.poll_interval_seconds}}s) - ` +
                       `the loop may be hung or mid-clip. If this persists, restart monitoring. ` + line;
            }}
            return line;
        }}

        async function refreshDashboard() {{
            const res = await fetch('/api/dashboard-status');
            const data = await res.json();

            document.getElementById('setup-banner').style.display = data.configured ? 'none' : 'block';

            // --- Live view ---
            const mtxStatus = document.getElementById('mediamtx-status');
            const mtxActions = document.getElementById('mediamtx-actions');
            const videoEl = document.getElementById('live-video');
            mtxActions.innerHTML = '';

            if (data.mediamtx_reachable) {{
                mtxStatus.className = 'status-box ok';
                mtxStatus.textContent = 'MediaMTX is running - loading live video...';
                mtxActions.innerHTML = '<button class="secondary" onclick="startMediaMtx()">Restart (apply latest settings)</button><pre id="mediamtx-log" style="display:none"></pre>';
                videoEl.style.display = 'block';
                if (hlsPlayer) {{ hlsPlayer.destroy(); }}
                if (Hls.isSupported()) {{
                    hlsPlayer = new Hls();
                    hlsPlayer.loadSource(data.mediamtx_url);
                    hlsPlayer.attachMedia(videoEl);
                }} else if (videoEl.canPlayType('application/vnd.apple.mpegurl')) {{
                    videoEl.src = data.mediamtx_url;
                }}
            }} else {{
                videoEl.style.display = 'none';
                mtxStatus.className = 'status-box warn';
                if (!data.mediamtx_installed) {{
                    mtxStatus.textContent = "MediaMTX isn't installed.";
                    mtxActions.innerHTML = '<button onclick="installMediaMtx()">Install MediaMTX now</button><pre id="mediamtx-log" style="display:none"></pre>';
                }} else {{
                    mtxStatus.textContent = "MediaMTX is installed but not running.";
                    mtxActions.innerHTML = '<button onclick="startMediaMtx()">Start MediaMTX</button><button class="secondary" onclick="refreshDashboard()">Recheck</button><pre id="mediamtx-log" style="display:none"></pre>';
                }}
            }}

            // --- MediaMTX staleness ---
            document.getElementById('mediamtx-stale').innerHTML = data.mediamtx_stale_config
                ? '<div class="stale-note">Settings were saved after MediaMTX started - it is still using the OLD settings. Click "Restart (apply latest settings)" above.</div>'
                : '';

            // --- Monitoring ---
            const monStatus = document.getElementById('monitor-status');
            const monActions = document.getElementById('monitor-actions');
            const external = data.monitoring_external_pids || [];
            if (data.monitoring_running) {{
                monStatus.className = 'status-box ok';
                if (data.monitoring_pid) {{
                    monStatus.textContent = '✅ Monitoring is running - feeding events will notify and upload as configured.';
                    monActions.innerHTML = '<button class="secondary" onclick="startMonitoring()">Restart (apply latest settings)</button><button class="stop" onclick="stopMonitoring()">Stop monitoring</button><pre id="monitor-log" style="display:none"></pre>';
                }} else {{
                    monStatus.textContent = '✅ Monitoring is running (started outside this UI - e.g. the auto-start service or a terminal).';
                    monActions.innerHTML = '<pre id="monitor-log" style="display:none"></pre>';
                }}
                if (data.monitoring_pid && external.length) {{
                    monStatus.className = 'status-box warn';
                    monStatus.textContent = '⚠️ TWO monitoring processes are running (this UI\\'s, plus pid ' + external[0] +
                        ' started elsewhere - probably the auto-start service). Every event will notify twice. Stop one: ' +
                        'either click Stop below, or run `nomwatch service-uninstall` in a terminal.';
                }}
            }} else if (!data.detection_model) {{
                monStatus.className = 'status-box warn';
                monStatus.textContent = 'No vision model picked yet - finish detection setup first.';
                monActions.innerHTML = '<button class="secondary" onclick="window.location.href=\\'/setup\\'">Go to setup</button>';
            }} else {{
                monStatus.className = 'status-box warn';
                monStatus.textContent = 'Monitoring is NOT running - notifications and uploads will not happen until you start it.';
                monActions.innerHTML = '<button onclick="startMonitoring()">Start monitoring</button><pre id="monitor-log" style="display:none"></pre>';
            }}

            document.getElementById('monitor-heartbeat').innerHTML = describeHeartbeat(data.heartbeat);
            document.getElementById('monitor-stale').innerHTML = data.monitoring_stale_config
                ? '<div class="stale-note">Settings were saved after monitoring started - it is still using the OLD settings. Click "Restart (apply latest settings)" below.</div>'
                : '';

            // --- Automatic start-up (launchd) ---
            const svcStatus = document.getElementById('service-status');
            const svcActions = document.getElementById('service-actions');
            if (svcStatus && svcActions) {{
                if (data.service_installed) {{
                    svcStatus.className = 'status-box ok';
                    svcStatus.textContent = '✅ Auto-start is installed - monitoring will come back on its own after a reboot.';
                    svcActions.innerHTML = '<button class="secondary" onclick="uninstallService()">Turn off auto-start</button>';
                }} else {{
                    svcStatus.className = 'status-box';
                    svcStatus.textContent = 'Auto-start is off - monitoring will NOT restart by itself after a reboot.';
                    svcActions.innerHTML = '<button onclick="installService()">Turn on auto-start</button>';
                }}
            }}
        }}

        async function installService() {{
            const el = document.getElementById('service-status');
            el.textContent = 'Installing auto-start service...';
            const res = await fetch('/api/install-service', {{ method: 'POST' }});
            const data = await res.json();
            if (!data.ok) {{ el.className = 'status-box warn'; el.textContent = 'Failed: ' + data.error; return; }}
            setTimeout(refreshDashboard, 800);
        }}

        async function uninstallService() {{
            const el = document.getElementById('service-status');
            el.textContent = 'Removing auto-start service...';
            const res = await fetch('/api/uninstall-service', {{ method: 'POST' }});
            const data = await res.json();
            if (!data.ok) {{ el.className = 'status-box warn'; el.textContent = 'Failed: ' + data.error; return; }}
            setTimeout(refreshDashboard, 800);
        }}

        async function clearHistory() {{
            if (!confirm('Move all events, clips, thumbnails and diagnostic logs into a recoverable archive folder?')) return;
            const el = document.getElementById('clear-history-status');
            el.textContent = ' archiving...';
            const res = await fetch('/api/prune', {{ method: 'POST' }});
            const data = await res.json();
            if (!data.ok) {{ el.textContent = ' failed.'; return; }}
            const mb = (data.bytes / (1024*1024)).toFixed(1);
            el.textContent = data.archived ? ` archived ${{data.archived}} file(s), ${{mb}}MB.` : ' nothing to clear.';
            loadEvents();
        }}

        async function installMediaMtx() {{
            const log = document.getElementById('mediamtx-log');
            log.style.display = 'block';
            log.textContent = 'Installing... this can take a minute.';
            const res = await fetch('/api/install-mediamtx', {{ method: 'POST' }});
            const data = await res.json();
            log.textContent = data.output || (data.success ? 'Done.' : 'Install failed.');
            refreshDashboard();
        }}

        async function startMediaMtx() {{
            const log = document.getElementById('mediamtx-log');
            log.style.display = 'block';
            log.textContent = 'Starting MediaMTX...';
            const res = await fetch('/api/start-mediamtx', {{ method: 'POST' }});
            const data = await res.json();
            log.textContent = data.ok ? `Started (pid ${{data.pid}}).` : `Failed: ${{data.error}}`;
            setTimeout(refreshDashboard, 1500);
        }}

        async function startMonitoring() {{
            const log = document.getElementById('monitor-log');
            if (log) {{ log.style.display = 'block'; log.textContent = 'Starting...'; }}
            const res = await fetch('/api/start-monitoring', {{ method: 'POST' }});
            const data = await res.json();
            if (log) {{ log.textContent = data.ok ? `Started (pid ${{data.pid}}).` : `Failed: ${{data.error}}`; }}
            setTimeout(refreshDashboard, 1000);
        }}

        async function stopMonitoring() {{
            await fetch('/api/stop-monitoring', {{ method: 'POST' }});
            setTimeout(refreshDashboard, 500);
        }}

        async function testNotify() {{
            const statusEl = document.getElementById('notify-test-status');
            if (!ntfyTopic) {{
                statusEl.innerHTML = '<div class="status-box warn">No ntfy topic configured - set one up in Notifications.</div>';
                return;
            }}
            statusEl.innerHTML = '<div class="status-box">Sending...</div>';
            const res = await fetch('/api/test-notify', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ ntfy_topic: ntfyTopic }}),
            }});
            const data = await res.json();
            statusEl.innerHTML = data.ok
                ? '<div class="status-box ok">Sent! Check your phone/ntfy app.</div>'
                : `<div class="status-box bad">Failed: ${{data.error || 'unknown error'}}</div>`;
        }}

        async function loadEvents() {{
            const container = document.getElementById('events-container');
            const res = await fetch('/api/recent-events?limit=50');
            const data = await res.json();
            if (!data.events || data.events.length === 0) {{
                container.innerHTML = '<p class="empty-note">No feeding events logged yet. Once monitoring confirms a feeding, it shows up here with its clip.</p>';
                return;
            }}
            const cards = data.events.map(e => {{
                const when = new Date(e.timestamp * 1000).toLocaleString();
                const conf = (e.confidence || 0).toFixed(2);
                const clipFile = e.clip_file ? encodeURIComponent(e.clip_file) : null;
                const thumb = clipFile
                    ? `<img class="event-thumb" loading="lazy" src="/api/clip-thumbnail/${{clipFile}}"
                           alt="clip thumbnail" onclick="playClip('${{clipFile}}')"
                           onerror="this.outerHTML='<div class=\\'event-thumb-none\\'>clip unplayable</div>'">`
                    : `<div class="event-thumb-none">${{e.clip_path ? 'clip deleted' : 'no clip'}}</div>`;
                const notified = e.notified === false ? '<p class="event-error">⚠️ push notification failed</p>' : '';
                const error = e.error ? `<p class="event-error">⚠️ ${{esc(e.error)}}</p>` : '';
                const actions = clipFile ? `
                    <div class="event-actions">
                        <button type="button" onclick="playClip('${{clipFile}}')">Play</button>
                        <a href="/clips/${{clipFile}}" download>Download</a>
                        <a onclick="deleteClip('${{clipFile}}')">Delete</a>
                    </div>` : '';
                return `<div class="event-card">
                    ${{thumb}}
                    <div class="event-body">
                        <span class="event-when">${{esc(when)}}</span>
                        <span class="event-conf">&middot; conf ${{conf}}</span>
                        <p class="event-reason">${{esc(e.reasoning || '')}}</p>
                        ${{error}}${{notified}}
                    </div>
                    ${{actions}}
                </div>`;
            }}).join('');
            container.innerHTML = `<div class="gallery">${{cards}}</div>`;
        }}

        function playClip(clipFile) {{
            const overlay = document.getElementById('player-overlay');
            const video = document.getElementById('player-video');
            video.src = '/clips/' + clipFile;
            overlay.classList.add('open');
            video.play().catch(() => {{}});
        }}

        function closePlayer() {{
            const overlay = document.getElementById('player-overlay');
            const video = document.getElementById('player-video');
            video.pause();
            video.removeAttribute('src');
            video.load();
            overlay.classList.remove('open');
        }}
        document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closePlayer(); }});

        async function deleteClip(clipFile) {{
            if (!confirm('Delete this clip file from this computer? The event stays in the history; copies already synced to cloud storage are not touched.')) return;
            const res = await fetch('/api/delete-clip', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ clip_file: decodeURIComponent(clipFile) }}),
            }});
            const data = await res.json();
            if (!data.ok) alert('Could not delete: ' + (data.error || 'unknown error'));
            loadEvents();
        }}

        async function restoreConfig(file) {{
            if (!file) return;
            const statusEl = document.getElementById('restore-status');
            if (!confirm('Restore configuration from "' + file.name + '"? This REPLACES all current NomWatch settings.')) {{
                document.getElementById('restore-file').value = '';
                return;
            }}
            statusEl.innerHTML = '<div class="status-box">Restoring...</div>';
            const form = new FormData();
            form.append('config', file);
            const res = await fetch('/api/import-config', {{ method: 'POST', body: form }});
            const data = await res.json();
            statusEl.innerHTML = data.ok
                ? `<div class="status-box ok">✅ ${{esc(data.message)}}</div>`
                : `<div class="status-box bad">Restore failed: ${{esc(data.error || 'unknown error')}}</div>`;
            document.getElementById('restore-file').value = '';
            if (data.ok) setTimeout(() => window.location.reload(), 2500);
        }}

        refreshDashboard();
        loadEvents();
        setInterval(refreshDashboard, 5000);
        setInterval(loadEvents, 30000);
    </script>
</body>
</html>
"""


def create_app():
    # Imported lazily so `pip install nomwatch` (no [ui] extra) doesn't
    # require Flask at all.
    from flask import Flask, jsonify, request, send_file

    app = Flask(__name__)

    @app.errorhandler(Exception)
    def _api_errors_as_json(exc):
        """
        API routes must answer JSON even when they blow up. Before this,
        malformed input (e.g. a poll interval of 0) returned Flask's HTML
        500 page, which the frontend's res.json() choked on - the user just
        saw a hung button. Non-API paths keep Flask's default behavior.
        """
        from werkzeug.exceptions import HTTPException

        if not request.path.startswith("/api/"):
            if isinstance(exc, HTTPException):
                return exc  # normal 404/405/... pages for non-API paths
            raise exc
        if isinstance(exc, HTTPException):
            return jsonify({"ok": False, "error": f"{exc.code} {exc.name}: {exc.description}"}), exc.code
        app.logger.exception("Unhandled error on %s", request.path)
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    def _json_body() -> dict:
        """Tolerant JSON body reader - missing/invalid bodies become {}."""
        return request.get_json(silent=True) or {}

    def _positive_int(data: dict, key: str, default: int, minimum: int = 1) -> int:
        try:
            value = int(data.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number")
        if value < minimum:
            raise ValueError(f"{key} must be at least {minimum}")
        return value

    # --- Home: dashboard (viewer + status) ----------------------------------

    @app.route("/")
    def dashboard():
        cfg = _get_or_init_config()
        ntfy_topic = cfg.notify.ntfy_topic or ""
        ntfy_topic_line = (
            f'&middot; Topic: <code>{html.escape(ntfy_topic)}</code>' if ntfy_topic else "(no topic configured yet)"
        )
        return DASHBOARD_TEMPLATE.format(
            configured="true" if cfg.camera.ip else "false",
            camera_ip=html.escape(cfg.camera.ip or "(not set up yet)"),
            poll_interval_seconds=cfg.detection.poll_interval_seconds,
            required_eating_seconds=cfg.detection.required_eating_seconds,
            detection_model=html.escape(cfg.detection.ollama_model or "(none picked yet)"),
            notify_provider=html.escape(cfg.notify.provider),
            ntfy_topic_line=ntfy_topic_line,
            ntfy_topic_json=json.dumps(ntfy_topic),
            storage_provider=html.escape(cfg.storage.provider),
        )

    @app.route("/api/dashboard-status")
    def api_dashboard_status():
        import requests

        cfg = _get_or_init_config()
        url = f"http://127.0.0.1:{cfg.bridge.mediamtx_hls_port}/cam/index.m3u8"
        try:
            resp = requests.get(url, timeout=2)
            mediamtx_reachable = resp.status_code == 200
        except requests.RequestException:
            mediamtx_reachable = False

        run_pid, run_started = read_pid_info(RUN_PID_PATH)
        mtx_pid, mtx_started = read_pid_info(MEDIAMTX_PID_PATH)
        external_pids = _external_run_pids()
        service_status = launchd_service_status()
        service_installed = service_status.startswith("Installed")
        heartbeat = _read_heartbeat()
        # A heartbeat is only meaningful while some monitoring process is
        # alive - otherwise it's the corpse of the last run.
        monitoring_alive = run_pid is not None or bool(external_pids) or monitorlock.run_loop_locked()
        if heartbeat is not None and not monitoring_alive:
            heartbeat = None

        return jsonify({
            "configured": bool(cfg.camera.ip),
            "camera_ip": cfg.camera.ip,
            "mediamtx_reachable": mediamtx_reachable,
            "mediamtx_installed": binary_available("mediamtx"),
            "mediamtx_url": url,
            "mediamtx_stale_config": mediamtx_reachable and _config_changed_since(mtx_started),
            "monitoring_running": monitoring_alive,
            "monitoring_pid": run_pid,
            "monitoring_external_pids": external_pids,
            "monitoring_stale_config": run_pid is not None and _config_changed_since(run_started),
            "service_installed": service_installed,
            "service_status_text": service_status,
            "heartbeat": heartbeat,
            "detection_engine": cfg.detection.engine,
            "detection_model": cfg.detection.ollama_model,
            "poll_interval_seconds": cfg.detection.poll_interval_seconds,
            "required_eating_seconds": cfg.detection.required_eating_seconds,
            "notify_provider": cfg.notify.provider,
            "ntfy_topic": cfg.notify.ntfy_topic,
            "storage_provider": cfg.storage.provider,
        })

    @app.route("/api/start-monitoring", methods=["POST"])
    def api_start_monitoring():
        cfg = _get_or_init_config()
        engine = cfg.detection.engine
        if engine not in ("ollama", "motion", "hybrid"):
            return jsonify({
                "ok": False,
                "pid": None,
                "error": f"Detection engine '{engine}' can't be started from here - finish setup first.",
            })
        if engine in ("ollama", "hybrid") and not cfg.detection.ollama_model:
            return jsonify({
                "ok": False,
                "pid": None,
                "error": "No vision model is picked yet - finish detection setup, or choose the motion-only engine.",
            })
        external = _external_run_pids()
        untracked_monitor = _run_loop_pid_running() is None and monitorlock.run_loop_locked()
        if external or untracked_monitor:
            return jsonify({
                "ok": False,
                "pid": None,
                "error": (
                    f"A monitoring process this UI doesn't manage is already running "
                    f"({f'pid {external[0]}' if external else 'detected by its monitor lock'}) - probably the auto-start service installed by "
                    "`nomwatch setup`, or a `nomwatch run` left in a terminal. Starting a "
                    "second one would double every notification and clip. Stop that one "
                    "first (`nomwatch service-uninstall`, or Ctrl+C in its terminal)."
                ),
            })
        pid = _start_run_loop()
        if pid is None:
            return jsonify({"ok": False, "pid": None, "error": "Could not start `nomwatch run` - check it's installed and on PATH."})
        return jsonify({"ok": True, "pid": pid, "error": None})

    @app.route("/api/stop-monitoring", methods=["POST"])
    def api_stop_monitoring():
        stopped = _stop_run_loop()
        return jsonify({"ok": stopped})

    @app.route("/api/install-service", methods=["POST"])
    def api_install_service():
        # A launchd agent and a UI-started loop would both run `nomwatch run`
        # and double every notification/clip. Stop the manual loop first so the
        # service becomes the single owner.
        _stop_run_loop()
        error = install_launchd_service(CONFIG_DIR / "logs")
        if error:
            return jsonify({"ok": False, "error": error})
        return jsonify({"ok": True, "error": None})

    @app.route("/api/uninstall-service", methods=["POST"])
    def api_uninstall_service():
        error = uninstall_launchd_service()
        # "No service found" is a success from the user's point of view.
        if error and "No NomWatch launchd service" not in error:
            return jsonify({"ok": False, "error": error})
        return jsonify({"ok": True, "error": None})

    @app.route("/api/prune", methods=["POST"])
    def api_prune():
        """Clear event history/clips/logs. Always ARCHIVES (moves to a
        recoverable folder) from the UI - the irreversible hard-delete is
        intentionally CLI-only (`nomwatch prune --delete`)."""
        from .cli import prune_targets, _path_stats

        targets = prune_targets(include_saved=False)
        if not targets:
            return jsonify({"ok": True, "archived": 0, "bytes": 0, "archive_dir": None})
        total_files = total_bytes = 0
        for _label, p in targets:
            f, b = _path_stats(p)
            total_files += f
            total_bytes += b
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_dir = CONFIG_DIR / "archive" / f"prune-{stamp}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for _label, p in targets:
            shutil.move(str(p), str(archive_dir / p.name))
        return jsonify({
            "ok": True,
            "archived": total_files,
            "bytes": total_bytes,
            "archive_dir": str(archive_dir),
        })

    @app.route("/api/recent-events")
    def api_recent_events():
        try:
            limit = min(200, max(1, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        return jsonify({"events": _read_recent_events(limit)})

    # --- Clip gallery ---------------------------------------------------------

    @app.route("/clips/<name>")
    def serve_clip(name: str):
        path = _safe_clip_path(name)
        if path is None:
            return jsonify({"ok": False, "error": "No such clip."}), 404
        # conditional=True enables HTTP range requests, which <video> needs
        # for seeking.
        return send_file(path, mimetype="video/mp4", conditional=True)

    @app.route("/api/clip-thumbnail/<name>")
    def clip_thumbnail(name: str):
        path = _safe_clip_path(name)
        if path is None:
            return jsonify({"ok": False, "error": "No such clip."}), 404
        thumb = _thumbnail_for(path)
        if thumb is None:
            return jsonify({"ok": False, "error": "Could not generate thumbnail."}), 500
        return send_file(thumb, mimetype="image/jpeg")

    @app.route("/api/delete-clip", methods=["POST"])
    def api_delete_clip():
        data = _json_body()
        path = _safe_clip_path(str(data.get("clip_file", "")))
        if path is None:
            return jsonify({"ok": False, "error": "No such clip."}), 404
        path.unlink()
        thumb = THUMBNAILS_DIR / (path.stem + ".jpg")
        thumb.unlink(missing_ok=True)
        # The event log entry stays (history is history) - the gallery shows
        # it without a clip from now on. Copies already uploaded/synced to
        # cloud storage are deliberately untouched.
        return jsonify({"ok": True})

    # --- Config backup / restore ----------------------------------------------

    @app.route("/api/export-config")
    def api_export_config():
        if not CONFIG_PATH.exists():
            return jsonify({"ok": False, "error": "No config to export yet."}), 404
        # Contains the camera password - fine to hand to the local user (it
        # is their own config), and this UI is loopback/tailnet-only.
        return send_file(
            CONFIG_PATH,
            mimetype="application/x-yaml",
            as_attachment=True,
            download_name="nomwatch-config-backup.yml",
        )

    @app.route("/api/import-config", methods=["POST"])
    def api_import_config():
        uploaded = request.files.get("config")
        raw_text = uploaded.read().decode("utf-8", errors="replace") if uploaded else request.get_data(as_text=True)
        if not raw_text.strip():
            return jsonify({"ok": False, "error": "No file received."})
        try:
            raw = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            return jsonify({"ok": False, "error": f"Not valid YAML: {exc}"})
        if not isinstance(raw, dict) or "camera" not in raw:
            return jsonify({"ok": False, "error": "That file doesn't look like a NomWatch config (no camera section)."})
        try:
            cfg = config_from_dict(raw)
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"Config didn't validate: {exc}"})
        save_config(cfg)
        write_mediamtx_config(cfg, CONFIG_DIR / "mediamtx.yml")
        return jsonify({
            "ok": True,
            "message": "Config restored. Restart MediaMTX and monitoring below so the restored settings actually take effect.",
        })

    # --- Setup wizard --------------------------------------------------------

    @app.route("/setup")
    def setup_wizard():
        cfg = _get_or_init_config()
        ntfy_topic = cfg.notify.ntfy_topic or f"nomwatch-{secrets.token_hex(4)}"
        det = cfg.detection
        # A JS object literal (or "null") describing the saved zone, passed as a
        # ready-made string so str.format doesn't try to re-parse its braces.
        if None not in (det.zone_x, det.zone_y, det.zone_w, det.zone_h):
            zone_js = "{" + f"x:{det.zone_x},y:{det.zone_y},w:{det.zone_w},h:{det.zone_h}" + "}"
        else:
            zone_js = "null"
        # html.escape everything user-controlled: a camera password (or any
        # field) containing a double-quote used to break out of its value=""
        # attribute and mangle the whole form.
        return PAGE_TEMPLATE.format(
            ip=html.escape(cfg.camera.ip, quote=True),
            rtsp_port=cfg.camera.rtsp_port,
            username=html.escape(cfg.camera.username, quote=True),
            password=html.escape(cfg.camera.password, quote=True),
            stream_path=html.escape(cfg.camera.stream_path, quote=True),
            poll_interval_seconds=cfg.detection.poll_interval_seconds,
            required_eating_seconds=cfg.detection.required_eating_seconds,
            pre_roll_seconds=cfg.detection.pre_roll_seconds,
            clip_post_confirm_seconds=cfg.detection.clip_post_confirm_seconds,
            ntfy_topic=html.escape(ntfy_topic, quote=True),
            local_save_dir=html.escape(cfg.storage.local_save_dir or "", quote=True),
            pet_description=html.escape(cfg.detection.pet_description or "", quote=True),
            detection_engine=det.engine,
            motion_gating_js=("true" if det.motion_gating else "false"),
            motion_threshold=det.motion_threshold,
            min_confidence=det.min_confidence,
            zone_enabled_js=("true" if det.zone_detection_enabled else "false"),
            zone_js=zone_js,
        )

    # --- Screen 1: camera ---------------------------------------------------

    @app.route("/api/check-system")
    def api_check_system():
        return jsonify({"ffmpeg": binary_available("ffmpeg")})

    @app.route("/api/install-ffmpeg", methods=["POST"])
    def api_install_ffmpeg():
        lines = []
        ok = install_ffmpeg(on_output=lines.append)
        return jsonify({"success": ok, "output": "\n".join(lines[-30:])})

    @app.route("/api/test-camera", methods=["POST"])
    def api_test_camera():
        data = _json_body()
        missing = [k for k in ("ip", "username", "password", "stream_path") if not str(data.get(k, "")).strip()]
        if missing:
            return jsonify({"ok": False, "error": f"Missing field(s): {', '.join(missing)}", "hints": []})
        try:
            port = _positive_int(data, "rtsp_port", 554)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc), "hints": []})

        probe_cfg = NomWatchConfig(camera=CameraConfig(
            ip=str(data["ip"]).strip(),
            rtsp_port=port,
            username=str(data["username"]),
            password=str(data["password"]),
            stream_path=str(data["stream_path"]).strip().strip("/"),
        ))
        frame, ffmpeg_error = capture_frame_with_error(rtsp_url(probe_cfg))
        if frame is None:
            hints = _camera_failure_hints(probe_cfg.camera.ip, port, ffmpeg_error or "")
            return jsonify({
                "ok": False,
                "error": "Could not capture a frame from the camera.",
                "detail": ffmpeg_error,
                "hints": hints,
            })
        return jsonify({"ok": True, "image": base64.b64encode(frame).decode("ascii")})

    @app.route("/api/snapshot")
    def api_snapshot():
        """Single still frame from the ALREADY-SAVED camera config (for the
        zone picker background). Unlike /api/test-camera it takes no body -
        the camera is configured on screen 1 before the zone picker is used."""
        cfg = load_config()
        if cfg is None or not cfg.camera.ip:
            return jsonify({"ok": False, "error": "No camera configured yet - complete step 1 first."})
        frame, ffmpeg_error = capture_frame_with_error(rtsp_url(cfg))
        if frame is None:
            return jsonify({"ok": False, "error": ffmpeg_error or "Could not capture a frame from the camera."})
        return jsonify({"ok": True, "image": base64.b64encode(frame).decode("ascii")})

    # --- Screen 6: live stream ------------------------------------------------

    @app.route("/api/check-mediamtx")
    def api_check_mediamtx():
        import requests

        cfg = _get_or_init_config()
        url = f"http://127.0.0.1:{cfg.bridge.mediamtx_hls_port}/cam/index.m3u8"
        try:
            resp = requests.get(url, timeout=2)
            reachable = resp.status_code == 200
        except requests.RequestException:
            reachable = False
        return jsonify({
            "reachable": reachable,
            "url": url,
            "mediamtx_installed": binary_available("mediamtx"),
        })

    @app.route("/api/install-mediamtx", methods=["POST"])
    def api_install_mediamtx():
        lines = []
        ok = install_mediamtx(on_output=lines.append)
        return jsonify({"success": ok, "output": "\n".join(lines[-30:])})

    @app.route("/api/start-mediamtx", methods=["POST"])
    def api_start_mediamtx():
        cfg = _get_or_init_config()
        config_path = CONFIG_DIR / "mediamtx.yml"
        try:
            write_mediamtx_config(cfg, config_path)
        except Exception as exc:  # noqa: BLE001 - surface any templating error to the UI
            return jsonify({"ok": False, "pid": None, "error": str(exc)})

        pid = start_mediamtx(config_path)
        if pid is None:
            return jsonify({
                "ok": False,
                "pid": None,
                "error": "MediaMTX didn't start. Make sure it's installed and check for a port conflict.",
            })
        return jsonify({"ok": True, "pid": pid, "error": None})

    @app.route("/api/save-camera", methods=["POST"])
    def api_save_camera():
        data = _json_body()
        missing = [k for k in ("ip", "username", "password") if not str(data.get(k, "")).strip()]
        if missing:
            return jsonify({"ok": False, "error": f"Missing field(s): {', '.join(missing)}"})
        try:
            port = _positive_int(data, "rtsp_port", 554)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)})
        cfg = _get_or_init_config()
        cfg.camera = CameraConfig(
            ip=str(data["ip"]).strip(),
            rtsp_port=port,
            username=str(data["username"]),
            password=str(data["password"]),
            stream_path=str(data.get("stream_path") or "stream1").strip().strip("/"),
        )
        save_config(cfg)
        return jsonify({"ok": True})

    # --- Screen 2: detection -------------------------------------------------

    @app.route("/api/check-model")
    def check_model():
        running = probe_local_model_server()
        vision_model = None
        if running:
            vision_model = pick_vision_model(list_local_models())
        return jsonify({
            "server_running": running,
            "vision_model": vision_model,
            "default_model": DEFAULT_VISION_MODEL,
        })

    @app.route("/api/pull-model", methods=["POST"])
    def api_pull_model():
        lines = []
        ok = pull_model(DEFAULT_VISION_MODEL, on_output=lines.append)
        verified = ok and model_installed(list_local_models(), DEFAULT_VISION_MODEL)
        return jsonify({"success": verified, "output": "\n".join(lines[-20:])})

    @app.route("/api/save-detection", methods=["POST"])
    def api_save_detection():
        data = _json_body()
        cfg = _get_or_init_config()

        try:
            poll = _positive_int(data, "poll_interval_seconds", 10)
            eating = _positive_int(data, "required_eating_seconds", 20)
            pre_roll = _positive_int(data, "pre_roll_seconds", 5, minimum=0)
            post_confirm = _positive_int(data, "clip_post_confirm_seconds", 20, minimum=0)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)})

        # Mutate the existing detection config instead of rebuilding it from
        # scratch - rebuilding silently wiped every field this screen
        # doesn't own (pet_description from screen 5, min_confidence,
        # ollama_host) each time someone re-ran the wizard.
        requested_engine = str(data.get("engine", cfg.detection.engine or "ollama")).strip().lower()
        if requested_engine not in ("ollama", "motion", "hybrid"):
            requested_engine = "ollama"

        # Discover the local vision model so ollama/hybrid can be validated.
        available_model = None
        if probe_local_model_server(cfg.detection.ollama_host):
            available_model = pick_vision_model(list_local_models(cfg.detection.ollama_host))

        warning = None
        if requested_engine in ("ollama", "hybrid"):
            if available_model:
                cfg.detection.engine = requested_engine
                cfg.detection.ollama_model = available_model
            elif cfg.detection.ollama_model:
                # Ollama unreachable right now but a model was picked before -
                # keep the choice, just warn.
                cfg.detection.engine = requested_engine
                warning = (
                    "Couldn't reach Ollama just now - keeping the previously picked model "
                    f"({cfg.detection.ollama_model}). Make sure Ollama is running before starting monitoring."
                )
            else:
                # No model at all - a vision engine can't run. Fall back to motion.
                cfg.detection.engine = "motion"
                cfg.detection.ollama_model = None
                warning = (
                    f"No vision-capable Ollama model is installed, so '{requested_engine}' "
                    "can't run yet - saved as motion-only for now. Install a model "
                    "(e.g. gemma3:4b) and re-run detection setup to enable the AI."
                )
        else:  # motion-only, no model needed
            cfg.detection.engine = "motion"

        # Motion tuning.
        cfg.detection.motion_gating = bool(data.get("motion_gating", cfg.detection.motion_gating))
        try:
            mt = float(data.get("motion_threshold", cfg.detection.motion_threshold))
            if mt > 0:
                cfg.detection.motion_threshold = mt
        except (TypeError, ValueError):
            pass
        try:
            mc = float(data.get("min_confidence", cfg.detection.min_confidence))
            if 0.0 <= mc <= 1.0:
                cfg.detection.min_confidence = mc
        except (TypeError, ValueError):
            pass

        # Zone crop (normalized coords). Any missing/invalid coordinate disables it.
        cfg.detection.zone_detection_enabled = bool(data.get("zone_detection_enabled", False))
        if cfg.detection.zone_detection_enabled:
            try:
                cfg.detection.zone_x = float(data["zone_x"])
                cfg.detection.zone_y = float(data["zone_y"])
                cfg.detection.zone_w = float(data["zone_w"])
                cfg.detection.zone_h = float(data["zone_h"])
            except (KeyError, TypeError, ValueError):
                cfg.detection.zone_detection_enabled = False
                cfg.detection.zone_x = cfg.detection.zone_y = None
                cfg.detection.zone_w = cfg.detection.zone_h = None
        else:
            cfg.detection.zone_x = cfg.detection.zone_y = None
            cfg.detection.zone_w = cfg.detection.zone_h = None

        cfg.detection.poll_interval_seconds = poll
        cfg.detection.required_eating_seconds = eating
        cfg.detection.consecutive_required = max(1, round(eating / poll))
        cfg.detection.pre_roll_seconds = pre_roll
        cfg.detection.clip_post_confirm_seconds = post_confirm
        save_config(cfg)
        return jsonify({
            "ok": True,
            "engine": cfg.detection.engine,
            "model": cfg.detection.ollama_model,
            "warning": warning,
        })

    # --- Screen 3: storage ---------------------------------------------------

    @app.route("/api/detect-drive-sync")
    def api_detect_drive_sync():
        folder = find_google_drive_sync_folder()
        return jsonify({"folder": str(folder) if folder else None})

    @app.route("/api/save-storage", methods=["POST"])
    def api_save_storage():
        data = _json_body()
        cfg = _get_or_init_config()
        provider = data.get("provider", "local")
        if provider not in ("local", "google_drive_sync", "google_drive_api", "none"):
            return jsonify({"ok": False, "error": f"Unknown storage provider: {provider!r}"})

        warning = None
        storage_cfg = StorageConfig(provider=provider)
        if provider == "local":
            # Strip quotes and expand ~ BEFORE saving. A real user pasted a
            # quoted path here and NomWatch dutifully created a literal
            # `'` directory inside its own working directory and saved
            # clips there while the UI claimed everything was fine.
            local_dir = clean_user_path(data.get("local_save_dir"))
            if local_dir and not Path(local_dir).is_absolute():
                return jsonify({
                    "ok": False,
                    "error": f"The clips folder must be a full path starting with / (got: {local_dir}). "
                             "Example: /Users/you/Desktop/PetClips",
                })
            storage_cfg.local_save_dir = local_dir
        if provider == "google_drive_sync":
            folder = find_google_drive_sync_folder()
            if folder:
                storage_cfg.drive_sync_folder = str(folder)
            else:
                warning = (
                    "Couldn't find a Google Drive for Desktop sync folder on this machine. "
                    "Clips will fail to sync until Drive for Desktop is installed and signed "
                    "in - monitoring will fall back to saving clips locally in the meantime."
                )
        cfg.storage = storage_cfg
        save_config(cfg)
        resolved = storage_cfg.local_save_dir or (str(CLIPS_DIR) if provider == "local" else None)
        return jsonify({"ok": True, "warning": warning, "resolved_local_dir": resolved})

    # --- Screen 4: notifications ---------------------------------------------

    @app.route("/api/generate-topic")
    def api_generate_topic():
        return jsonify({"topic": f"nomwatch-{secrets.token_hex(4)}"})

    @app.route("/api/save-notify", methods=["POST"])
    def api_save_notify():
        data = _json_body()
        provider = data.get("provider", "ntfy")
        topic = (data.get("ntfy_topic") or "").strip()
        if provider == "ntfy" and not topic:
            return jsonify({"ok": False, "error": "Enter (or generate) an ntfy topic first."})
        cfg = _get_or_init_config()
        cfg.notify = NotifyConfig(provider=provider, ntfy_topic=topic or None)
        save_config(cfg)
        return jsonify({"ok": True})

    @app.route("/api/test-notify", methods=["POST"])
    def api_test_notify():
        data = _json_body()
        topic = data.get("ntfy_topic")
        if not topic:
            return jsonify({"ok": False, "error": "No topic given."})
        try:
            ok = NtfyNotifier(topic).send(
                "NomWatch test notification",
                "If you see this, your ntfy topic is set up correctly!",
            )
        except Exception as exc:  # noqa: BLE001 - report to the UI, don't crash the server
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": ok, "error": None if ok else "ntfy.sh did not accept the request."})

    # --- Screen 5: appearance ID ----------------------------------------------

    @app.route("/api/save-appearance", methods=["POST"])
    def api_save_appearance():
        data = _json_body()
        cfg = _get_or_init_config()
        cfg.detection.pet_description = data.get("pet_description") or None
        save_config(cfg)
        return jsonify({"ok": True})

    return app


def run_ui(host: str = "127.0.0.1", port: int = 5151):
    app = create_app()
    app.run(host=host, port=port)
