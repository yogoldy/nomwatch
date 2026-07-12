"""
Local web UI - full 5-screen setup wizard. See docs/UI_SPEC.md for the spec.

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
- Zone/bounding-box detection (screen 2) is a PLACEHOLDER ONLY. No
  architecture exists for it yet - see docs/ROADMAP.md.
- No host-vs-client/multi-device delegation concept exists anywhere in
  NomWatch yet - whichever machine runs `nomwatch run`/`nomwatch ui` is
  simply "the bridge" by virtue of running it there.
"""
from __future__ import annotations

import base64
import secrets

from .bridge import binary_available, install_ffmpeg
from .clip import record_clip  # noqa: F401 - kept available for future screens
from .config import (
    BridgeConfig,
    CameraConfig,
    DetectionConfig,
    NomWatchConfig,
    NotifyConfig,
    StorageConfig,
    load_config,
    save_config,
)
from .detection import (
    DEFAULT_VISION_MODEL,
    capture_frame,
    list_local_models,
    model_installed,
    pick_vision_model,
    probe_local_model_server,
    pull_model,
)
from .notify import NtfyNotifier
from .storage import find_google_drive_sync_folder

PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>NomWatch Setup</title>
    <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 680px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }}
        h1 {{ font-size: 1.5em; }}
        h2 {{ font-size: 1.2em; margin-top: 0; }}
        .step {{ color: #888; font-size: 0.9em; margin-bottom: 4px; }}
        label {{ display: flex; align-items: center; gap: 6px; margin-top: 16px; font-weight: 600; font-size: 0.9em; }}
        input[type=text], input[type=number], input[type=password] {{ width: 100%; padding: 8px; margin-top: 4px; font-size: 1em; box-sizing: border-box; }}
        button {{ margin-top: 20px; padding: 10px 20px; font-size: 1em; cursor: pointer; }}
        button.secondary {{ background: #eee; border: 1px solid #ccc; }}
        .status-box {{ margin-top: 20px; padding: 12px; border-radius: 6px; background: #f0f0f0; }}
        .ok {{ background: #e6f7e6; }}
        .warn {{ background: #fff3e0; }}
        pre {{ background: #111; color: #eee; padding: 10px; max-height: 200px; overflow-y: auto; font-size: 0.8em; }}

        .info-icon {{
            display: inline-flex; align-items: center; justify-content: center;
            width: 16px; height: 16px; border-radius: 50%;
            background: #ccc; color: white; font-size: 11px; font-weight: bold;
            cursor: help; position: relative; flex-shrink: 0;
        }}
        .info-icon:hover .tooltip {{ display: block; }}
        .tooltip {{
            display: none; position: absolute; left: 22px; top: -4px; z-index: 10;
            background: #222; color: #fff; padding: 8px 10px; border-radius: 6px;
            font-weight: normal; font-size: 0.8em; width: 240px; line-height: 1.4;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }}
        .password-row {{ position: relative; }}
        .password-row input {{ padding-right: 60px; }}
        .toggle-password {{
            position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
            margin-top: 0; padding: 4px 8px; font-size: 0.8em; cursor: pointer;
            background: #eee; border: 1px solid #ccc; border-radius: 4px;
        }}
        .screen {{ display: none; }}
        .screen.active {{ display: block; }}
        .placeholder {{
            margin-top: 12px; padding: 14px; border: 2px dashed #ccc; border-radius: 8px;
            background: repeating-linear-gradient(45deg, #fafafa, #fafafa 10px, #f0f0f0 10px, #f0f0f0 20px);
            color: #888;
        }}
        .placeholder-badge {{
            display: inline-block; background: #999; color: white; font-size: 0.7em;
            padding: 2px 8px; border-radius: 10px; font-weight: bold; margin-bottom: 8px;
        }}
        .camera-preview {{ max-width: 100%; border-radius: 8px; margin-top: 12px; border: 1px solid #ccc; }}
        .radio-group label {{ font-weight: normal; margin-top: 8px; }}
        .helper-text {{ color: #666; font-size: 0.85em; margin-top: 4px; }}
        .copy-row {{ display: flex; gap: 8px; align-items: center; margin-top: 8px; }}
        .copy-row input {{ flex: 1; }}
        .toggle-row {{ display: flex; align-items: center; gap: 10px; margin-top: 16px; }}
        .toggle-row label {{ margin-top: 0; }}
    </style>
</head>
<body>
    <div class="step" id="step-label">Step 1 of 5 &mdash; Camera</div>
    <h1>🐾 NomWatch Setup</h1>

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

        <div class="placeholder">
            <span class="placeholder-badge">PLACEHOLDER &mdash; NOT IMPLEMENTED</span>
            <h3 style="margin:6px 0;">Zone-based detection (bounding boxes)</h3>
            <p style="margin:4px 0;">
                Draw a box around just the feeder area so detection ignores everything else in frame.
                No architecture exists for this yet &mdash; this is a placeholder to show where it will
                eventually live. Currently NomWatch always evaluates the full frame.
            </p>
            <button class="secondary" disabled>Draw zone (coming later)</button>
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
        <div class="placeholder">
            <span class="placeholder-badge">PLACEHOLDER &mdash; NOT WIRED UP YET</span>
            <p style="margin:4px 0;">
                Describe your pet's species/color/breed (e.g. "black cat", "golden retriever named Max")
                so detection could eventually be harnessed to specifically recognize your animal, rather
                than any animal in frame. This is saved to your config, but detection.py does not use it
                yet &mdash; it's a placeholder for a future improvement, not a working feature.
            </p>
            <label style="margin-top:8px;">Pet description</label>
            <input type="text" id="pet-description" placeholder="e.g. black cat, golden retriever named Max" value="{pet_description}">
        </div>

        <button onclick="saveAppearanceAndFinish()">Finish setup</button>
        <button class="secondary" onclick="goToScreen(4)">Back</button>
    </div>

    <div id="final-message" class="status-box ok" style="display:none;">
        ✅ Setup complete! Run <code>nomwatch run</code> to start watching, or <code>nomwatch status</code>
        to check on things anytime.
    </div>

    <script>
        const stepLabels = {{
            1: "Step 1 of 5 &mdash; Camera",
            2: "Step 2 of 5 &mdash; Detection settings",
            3: "Step 3 of 5 &mdash; Storage",
            4: "Step 4 of 5 &mdash; Notifications",
            5: "Step 5 of 5 &mdash; Appearance identification",
        }};

        function goToScreen(n) {{
            document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
            document.getElementById('screen-' + n).classList.add('active');
            document.getElementById('step-label').innerHTML = stepLabels[n];
            if (n === 2) checkModel();
            if (n === 2) updateDebounceMath();
            if (n === 3) updateStorageDetail();
            if (n === 4) renderQr();
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

        async function testConnection() {{
            const statusEl = document.getElementById('camera-test-status');
            const previewEl = document.getElementById('camera-preview-container');
            statusEl.textContent = 'Testing connection...';
            previewEl.innerHTML = '';
            const res = await fetch('/api/test-camera', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(cameraFormData()),
            }});
            const data = await res.json();
            if (data.ok) {{
                statusEl.textContent = 'Connected! Here is your camera feed right now:';
                previewEl.innerHTML = `<img class="camera-preview" src="data:image/jpeg;base64,${{data.image}}">
                    <p class="helper-text">This is a single still-frame snapshot, not a live stream.</p>`;
            }} else {{
                statusEl.textContent = 'Could not connect: ' + data.error;
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
            await fetch('/api/save-camera', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(cameraFormData()),
            }});
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

        async function saveDetectionAndAdvance() {{
            const poll = parseInt(document.getElementById('poll-interval').value) || 10;
            const eating = parseInt(document.getElementById('required-eating').value) || 20;
            const preRoll = parseInt(document.getElementById('pre-roll').value) || 0;
            const postConfirm = parseInt(document.getElementById('post-confirm').value) || 0;
            await fetch('/api/save-detection', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    poll_interval_seconds: poll,
                    required_eating_seconds: eating,
                    pre_roll_seconds: preRoll,
                    clip_post_confirm_seconds: postConfirm,
                }}),
            }});
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
            await fetch('/api/save-storage', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(body),
            }});
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
            await fetch('/api/save-notify', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ provider: 'ntfy', ntfy_topic: topic }}),
            }});
            goToScreen(5);
        }}

        async function saveAppearanceAndFinish() {{
            const description = document.getElementById('pet-description').value;
            await fetch('/api/save-appearance', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ pet_description: description }}),
            }});
            document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
            document.getElementById('step-label').textContent = 'All done!';
            document.getElementById('final-message').style.display = 'block';
        }}

        checkSystem();
    </script>
</body>
</html>
"""


def create_app():
    # Imported lazily so `pip install nomwatch` (no [ui] extra) doesn't
    # require Flask at all.
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    def _get_or_init_config() -> NomWatchConfig:
        cfg = load_config()
        if cfg is None:
            cfg = NomWatchConfig(camera=CameraConfig(ip=""))
        return cfg

    @app.route("/")
    def index():
        cfg = _get_or_init_config()
        ntfy_topic = cfg.notify.ntfy_topic or f"nomwatch-{secrets.token_hex(4)}"
        return PAGE_TEMPLATE.format(
            ip=cfg.camera.ip,
            rtsp_port=cfg.camera.rtsp_port,
            username=cfg.camera.username,
            password=cfg.camera.password,
            stream_path=cfg.camera.stream_path,
            poll_interval_seconds=cfg.detection.poll_interval_seconds,
            required_eating_seconds=cfg.detection.required_eating_seconds,
            pre_roll_seconds=cfg.detection.pre_roll_seconds,
            clip_post_confirm_seconds=cfg.detection.clip_post_confirm_seconds,
            ntfy_topic=ntfy_topic,
            local_save_dir=cfg.storage.local_save_dir or "",
            pet_description=cfg.detection.pet_description or "",
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
        data = request.get_json()
        try:
            stream_url = (
                f"rtsp://{data['username']}:{data['password']}@"
                f"{data['ip']}:{int(data['rtsp_port'])}/{data['stream_path']}"
            )
        except (KeyError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"Missing/invalid field: {exc}"})

        frame = capture_frame(stream_url)
        if frame is None:
            return jsonify({
                "ok": False,
                "error": "Could not capture a frame. Check ffmpeg is installed, the IP/credentials "
                         "are correct, and this device can reach the camera on the LAN.",
            })
        return jsonify({"ok": True, "image": base64.b64encode(frame).decode("ascii")})

    @app.route("/api/save-camera", methods=["POST"])
    def api_save_camera():
        data = request.get_json()
        cfg = _get_or_init_config()
        cfg.camera = CameraConfig(
            ip=data["ip"],
            rtsp_port=int(data["rtsp_port"]),
            username=data["username"],
            password=data["password"],
            stream_path=data["stream_path"],
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
        data = request.get_json()
        cfg = _get_or_init_config()

        vision_model = None
        if probe_local_model_server(cfg.detection.ollama_host):
            vision_model = pick_vision_model(list_local_models())

        poll = int(data.get("poll_interval_seconds", 10))
        eating = int(data.get("required_eating_seconds", 20))
        cfg.detection = DetectionConfig(
            engine="ollama" if vision_model else "motion",
            ollama_model=vision_model,
            poll_interval_seconds=poll,
            required_eating_seconds=eating,
            consecutive_required=max(1, round(eating / poll)),
            pre_roll_seconds=int(data.get("pre_roll_seconds", 5)),
            clip_post_confirm_seconds=int(data.get("clip_post_confirm_seconds", 20)),
            describe_appearance=cfg.detection.describe_appearance,
        )
        save_config(cfg)
        return jsonify({"ok": True})

    # --- Screen 3: storage ---------------------------------------------------

    @app.route("/api/detect-drive-sync")
    def api_detect_drive_sync():
        folder = find_google_drive_sync_folder()
        return jsonify({"folder": str(folder) if folder else None})

    @app.route("/api/save-storage", methods=["POST"])
    def api_save_storage():
        data = request.get_json()
        cfg = _get_or_init_config()
        provider = data.get("provider", "local")
        storage_cfg = StorageConfig(provider=provider)
        if provider == "local":
            local_dir = data.get("local_save_dir")
            if local_dir:
                storage_cfg.local_save_dir = local_dir
        if provider == "google_drive_sync":
            folder = find_google_drive_sync_folder()
            if folder:
                storage_cfg.drive_sync_folder = str(folder)
        cfg.storage = storage_cfg
        save_config(cfg)
        return jsonify({"ok": True})

    # --- Screen 4: notifications ---------------------------------------------

    @app.route("/api/generate-topic")
    def api_generate_topic():
        return jsonify({"topic": f"nomwatch-{secrets.token_hex(4)}"})

    @app.route("/api/save-notify", methods=["POST"])
    def api_save_notify():
        data = request.get_json()
        cfg = _get_or_init_config()
        cfg.notify = NotifyConfig(provider=data.get("provider", "ntfy"), ntfy_topic=data.get("ntfy_topic"))
        save_config(cfg)
        return jsonify({"ok": True})

    @app.route("/api/test-notify", methods=["POST"])
    def api_test_notify():
        data = request.get_json()
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
        data = request.get_json()
        cfg = _get_or_init_config()
        cfg.detection.pet_description = data.get("pet_description") or None
        save_config(cfg)
        return jsonify({"ok": True})

    return app


def run_ui(host: str = "127.0.0.1", port: int = 5151):
    app = create_app()
    app.run(host=host, port=port)
