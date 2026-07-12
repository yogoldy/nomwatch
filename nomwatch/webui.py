"""
Local web UI - first slice (Screen 1 only: camera config + local model
detection/install). See docs/UI_SPEC.md for the full planned wizard.

This is NOT a replacement for `nomwatch setup` yet - it's the first real
screen of what will eventually be the primary setup path, per the product
decision to make a local web app the main UI with the CLI wizard kept
underneath for power users/automation/headless devices.

Run via `nomwatch ui` (requires the `ui` extra: `pip install nomwatch[ui]`).
Binds to 127.0.0.1 only by default, plain HTTP, no encryption of its own -
same "loopback-only" philosophy as MediaMTX. If you want it reachable from
another device, expose it manually via `tailscale serve` (same pattern used
for the HLS stream) - there's no automatic Tailscale wiring for the UI yet.

Known limitations, noted rather than hidden:
- Model pulls currently block the request until `ollama pull` finishes
  rather than streaming real progress.
- No host-vs-client/multi-device delegation concept exists anywhere in
  NomWatch yet - whichever machine runs `nomwatch run`/`nomwatch ui` is
  simply "the bridge" by virtue of running it there. Nothing coordinates
  multiple devices or tracks a designated bridge role.
"""
from __future__ import annotations

from .config import (
    CameraConfig,
    NomWatchConfig,
    load_config,
    save_config,
)
from .detection import (
    DEFAULT_VISION_MODEL,
    list_local_models,
    model_installed,
    pick_vision_model,
    probe_local_model_server,
    pull_model,
)

PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>NomWatch Setup</title>
    <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }}
        h1 {{ font-size: 1.5em; }}
        .step {{ color: #888; font-size: 0.9em; margin-bottom: 4px; }}
        label {{ display: flex; align-items: center; gap: 6px; margin-top: 16px; font-weight: 600; font-size: 0.9em; }}
        input {{ width: 100%; padding: 8px; margin-top: 4px; font-size: 1em; box-sizing: border-box; }}
        button {{ margin-top: 20px; padding: 10px 20px; font-size: 1em; cursor: pointer; }}
        #model-status {{ margin-top: 20px; padding: 12px; border-radius: 6px; background: #f0f0f0; }}
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
    </style>
</head>
<body>
    <div class="step">Step 1 of 5 &mdash; Camera + Detection Model</div>
    <h1>🐾 NomWatch Setup</h1>

    <form id="camera-form">
        <label>Camera LAN IP address
            <span class="info-icon">i<span class="tooltip">
                The local network address of your camera, e.g. 192.168.1.250. Find it in your
                router's connected-devices list, or in the camera's own app under Device Info.
            </span></span>
        </label>
        <input name="ip" placeholder="192.168.1.250" value="{ip}" required>

        <label>RTSP port
            <span class="info-icon">i<span class="tooltip">
                Almost always 554 &mdash; this is the standard RTSP port, and most consumer cameras
                (including Tapo) don't let you change it or even show it as a setting. Just leave
                the default unless you know your camera uses something else.
            </span></span>
        </label>
        <input name="rtsp_port" type="number" value="{rtsp_port}" required>

        <label>Camera-account username
            <span class="info-icon">i<span class="tooltip">
                A separate local login you create specifically for RTSP/third-party access &mdash;
                NOT your camera app's cloud login. On Tapo, find this under Advanced Settings &rarr;
                Camera Account.
            </span></span>
        </label>
        <input name="username" value="{username}" required>

        <label>Camera-account password
            <span class="info-icon">i<span class="tooltip">
                The password for the camera account above (not your cloud account password).
                Stored locally on this device only, never sent anywhere outside your own network.
            </span></span>
        </label>
        <div class="password-row">
            <input name="password" type="password" value="{password}" id="password-field" required>
            <button type="button" class="toggle-password" onclick="togglePassword()">Show</button>
        </div>

        <label>Stream path
            <span class="info-icon">i<span class="tooltip">
                Which stream quality to pull: "stream1" is usually the main/HD feed, "stream2" a
                lower-resolution one. Stick with stream1 unless you have a reason to use the other.
            </span></span>
        </label>
        <input name="stream_path" value="{stream_path}" required>

        <button type="submit">Save camera settings</button>
    </form>

    <div id="model-status">Checking for a local model server...</div>

    <script>
        function togglePassword() {{
            const field = document.getElementById('password-field');
            const btn = document.querySelector('.toggle-password');
            if (field.type === 'password') {{
                field.type = 'text';
                btn.textContent = 'Hide';
            }} else {{
                field.type = 'password';
                btn.textContent = 'Show';
            }}
        }}

        async function checkModel() {{
            const res = await fetch('/api/check-model');
            const data = await res.json();
            const el = document.getElementById('model-status');
            if (data.vision_model) {{
                el.className = 'ok';
                el.innerHTML = `Found local Ollama server with vision-capable model: <b>${{data.vision_model}}</b>`;
            }} else if (data.server_running) {{
                el.className = 'warn';
                el.innerHTML = `Found local Ollama server, but no vision-capable model installed.<br>
                    <button onclick="pullModel()">Install ${{data.default_model}} now</button>
                    <pre id="pull-log" style="display:none"></pre>`;
            }} else {{
                el.className = 'warn';
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

        document.getElementById('camera-form').addEventListener('submit', async (e) => {{
            e.preventDefault();
            const form = new FormData(e.target);
            await fetch('/api/save-camera', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(Object.fromEntries(form)),
            }});
            alert('Saved. (Screens 2-5 - timing, storage, notifications, appearance ID - are next up, not built in this first slice.)');
        }});

        checkModel();
    </script>
</body>
</html>
"""


def create_app():
    # Imported lazily so `pip install nomwatch` (no [ui] extra) doesn't
    # require Flask at all.
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.route("/")
    def index():
        cfg = load_config()
        camera = cfg.camera if cfg else CameraConfig(ip="")
        return PAGE_TEMPLATE.format(
            ip=camera.ip,
            rtsp_port=camera.rtsp_port,
            username=camera.username,
            password=camera.password,
            stream_path=camera.stream_path,
        )

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

    @app.route("/api/save-camera", methods=["POST"])
    def api_save_camera():
        data = request.get_json()
        cfg = load_config()
        camera = CameraConfig(
            ip=data["ip"],
            rtsp_port=int(data["rtsp_port"]),
            username=data["username"],
            password=data["password"],
            stream_path=data["stream_path"],
        )
        if cfg:
            cfg.camera = camera
        else:
            cfg = NomWatchConfig(camera=camera)
        save_config(cfg)
        return jsonify({"ok": True})

    return app


def run_ui(host: str = "127.0.0.1", port: int = 5151):
    app = create_app()
    app.run(host=host, port=port)
