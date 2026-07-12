"""
Local web UI - first slice (Screen 1 only: camera config + local model
detection/install). See docs/UI_SPEC.md for the full planned wizard.

This is NOT a replacement for `nomwatch setup` yet - it's the first real
screen of what will eventually be the primary setup path, per the product
decision to make a local web app the main UI with the CLI wizard kept
underneath for power users/automation/headless devices.

Run via `nomwatch ui` (requires the `ui` extra: `pip install nomwatch[ui]`).

Known limitation, noted rather than hidden: model pulls currently block the
request until `ollama pull` finishes rather than streaming real progress -
true live progress (matching the CLI's line-by-line streaming) is planned
but not built in this first slice.
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
        label {{ display: block; margin-top: 16px; font-weight: 600; font-size: 0.9em; }}
        input {{ width: 100%; padding: 8px; margin-top: 4px; font-size: 1em; box-sizing: border-box; }}
        button {{ margin-top: 20px; padding: 10px 20px; font-size: 1em; cursor: pointer; }}
        #model-status {{ margin-top: 20px; padding: 12px; border-radius: 6px; background: #f0f0f0; }}
        .ok {{ background: #e6f7e6; }}
        .warn {{ background: #fff3e0; }}
        pre {{ background: #111; color: #eee; padding: 10px; max-height: 200px; overflow-y: auto; font-size: 0.8em; }}
    </style>
</head>
<body>
    <div class="step">Step 1 of 5 &mdash; Camera + Detection Model</div>
    <h1>🐾 NomWatch Setup</h1>

    <form id="camera-form">
        <label>Camera LAN IP address</label>
        <input name="ip" placeholder="192.168.1.250" value="{ip}" required>
        <label>RTSP port</label>
        <input name="rtsp_port" type="number" value="{rtsp_port}" required>
        <label>Camera-account username</label>
        <input name="username" value="{username}" required>
        <label>Camera-account password</label>
        <input name="password" type="password" value="{password}" required>
        <label>Stream path</label>
        <input name="stream_path" value="{stream_path}" required>
        <button type="submit">Save camera settings</button>
    </form>

    <div id="model-status">Checking for a local model server...</div>

    <script>
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
