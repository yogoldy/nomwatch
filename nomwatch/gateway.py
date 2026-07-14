"""Authenticated same-origin media and access APIs."""
from __future__ import annotations

import io
import re
import threading
import urllib.parse

import requests

from .network import NetworkPolicyError

_HLS_URI = re.compile(r'URI="([^"]+)"')


def rewrite_hls_manifest(text: str, gateway_prefix: str) -> str:
    def rewrite(value: str) -> str:
        parsed = urllib.parse.urlparse(value)
        path = parsed.path.rsplit("/cam/", 1)[-1].lstrip("/")
        if ".." in path.split("/"):
            raise ValueError("unsafe HLS path")
        return gateway_prefix.rstrip("/") + "/" + path

    lines = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            line = rewrite(line)
        else:
            line = _HLS_URI.sub(lambda match: f'URI="{rewrite(match.group(1))}"', line)
        lines.append(line)
    return "\n".join(lines) + "\n"


def init_gateway_routes(app, state, auth, lan_manager=None, tailscale_adapter=None, *,
                        allowed_hosts=None, http_get=requests.get, hls_limit: int = 4) -> None:
    from flask import Response, g, jsonify, request, send_file

    hls_slots = threading.BoundedSemaphore(hls_limit)

    @app.get("/access")
    def access_page():
        return """<!doctype html><html><head><meta charset='utf-8'><title>NomWatch Access</title></head><body>
<main><h1>Access NomWatch</h1><section><h2>Private LAN</h2><p><strong>Trusted-network HTTP:</strong> traffic is not encrypted. Prefer Tailscale HTTPS for sensitive use, even at home.</p><p>LAN access is disabled until an owner selects and confirms a private interface.</p></section>
<section><h2>Private Tailscale HTTPS</h2><p>Tailscale access remains tailnet-only and still requires a NomWatch account. NomWatch never enables Funnel or a public route.</p><p>Before enabling HTTPS, note that the full device <code>.ts.net</code> name will appear in public Certificate Transparency logs.</p>
<div id='ts-status'>Checking Tailscale…</div><label><input id='ct-ack' type='checkbox'> I understand the public Certificate Transparency hostname disclosure.</label><p><button onclick='tsAction("login")'>Sign in to Tailscale</button> <button onclick='tsAction("enable")'>Enable private access</button> <button onclick='tsAction("disable")'>Disable</button></p><div id='ts-url'></div></section>
<script>
async function tsRefresh(){const r=await fetch('/api/v1/access/tailscale');const d=await r.json();document.getElementById('ts-status').textContent=(d.state||'unknown')+(d.error?': '+d.error:'');const box=document.getElementById('ts-url');box.innerHTML='';if(d.url){const a=document.createElement('a');a.href=d.url;a.textContent=d.url;box.appendChild(a);const img=document.createElement('img');img.src='/api/v1/qr?url='+encodeURIComponent(d.url);img.alt='Private NomWatch URL QR';img.width=160;box.appendChild(img);const b=document.createElement('button');b.textContent='Copy URL';b.onclick=()=>navigator.clipboard.writeText(d.url);box.appendChild(b);}}
async function tsAction(action){const body=action==='enable'?{certificate_transparency_acknowledged:document.getElementById('ct-ack').checked}:{};const r=await fetch('/api/v1/access/tailscale/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(d.action_url)window.open(d.action_url,'_blank','noopener');if(!r.ok)alert(d.error||'Tailscale action failed');await tsRefresh();}
tsRefresh();
</script></main></body></html>"""

    @app.get("/api/v1/access/lan")
    def lan_status():
        return jsonify(lan_manager.status() if lan_manager else {"enabled": False, "available": False})

    @app.get("/api/v1/access/interfaces")
    def lan_interfaces():
        if not lan_manager:
            return jsonify({"interfaces": []})
        return jsonify({"interfaces": lan_manager.interfaces()})

    @app.post("/api/v1/access/lan/stage")
    def lan_stage():
        if not lan_manager:
            return jsonify({"ok": False, "error": "LAN adapter unavailable"}), 503
        data = request.get_json(silent=True) or {}
        try:
            return jsonify({"ok": True, **lan_manager.stage(str(data.get("interface", "")), str(data.get("address", "")))})
        except NetworkPolicyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422

    @app.post("/api/v1/access/lan/confirm")
    def lan_confirm():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify({"ok": True, **lan_manager.confirm(str(data.get("confirmation_token", "")), request.host)})
        except NetworkPolicyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422

    @app.post("/api/v1/access/lan/disable")
    def lan_disable():
        lan_manager.disable()
        return jsonify({"ok": True})

    @app.get("/api/v1/qr")
    def local_qr():
        import qrcode
        value = request.args.get("url", "")
        parsed = urllib.parse.urlparse(value)
        current_hosts = allowed_hosts() if allowed_hosts else (lan_manager.allowed_hosts() if lan_manager else {"localhost", "127.0.0.1"})
        is_ntfy = parsed.scheme == "https" and parsed.hostname == "ntfy.sh" and bool(parsed.path.strip("/"))
        if parsed.scheme not in {"http", "https"} or (parsed.hostname not in current_hosts and not is_ntfy):
            return jsonify({"ok": False, "error": "URL is not a current NomWatch access URL"}), 422
        image = qrcode.make(value)
        output = io.BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        return send_file(output, mimetype="image/png", max_age=0)

    @app.get("/api/v1/live/<camera_id>/<path:asset>")
    def live_media(camera_id: str, asset: str):
        if camera_id not in {"camera", "cam"} or not asset or ".." in asset.split("/"):
            return jsonify({"ok": False, "error": "invalid media path"}), 404
        if not hls_slots.acquire(blocking=False):
            return jsonify({"ok": False, "error": "live-view concurrency limit reached"}), 429
        cfg = __import__("nomwatch.config", fromlist=["load_config"]).load_config()
        if cfg is None:
            hls_slots.release()
            return jsonify({"ok": False, "error": "camera is not configured"}), 404
        from .bridge import _internal_media_credentials
        user, password = _internal_media_credentials()
        upstream = f"http://127.0.0.1:{cfg.bridge.mediamtx_hls_port}/cam/{asset}"
        headers = {"Range": request.headers["Range"]} if "Range" in request.headers else {}
        try:
            response = http_get(upstream, headers=headers, auth=(user, password), stream=True, timeout=10)
        except requests.RequestException as exc:
            hls_slots.release()
            return jsonify({"ok": False, "error": "local media service unavailable"}), 503
        if response.status_code >= 400:
            hls_slots.release()
            return jsonify({"ok": False, "error": "media unavailable"}), response.status_code
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        if asset.endswith(".m3u8") or "mpegurl" in content_type:
            try:
                body = rewrite_hls_manifest(response.text, f"/api/v1/live/{camera_id}")
            finally:
                response.close()
                hls_slots.release()
            result = Response(body, status=response.status_code, mimetype="application/vnd.apple.mpegurl")
        else:
            def generate():
                try:
                    yield from response.iter_content(64 * 1024)
                finally:
                    response.close()
                    hls_slots.release()
            result = Response(generate(), status=response.status_code, content_type=content_type)
            for name in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if name in response.headers:
                    result.headers[name] = response.headers[name]
        result.headers["Cache-Control"] = "no-store"
        result.headers["Referrer-Policy"] = "no-referrer"
        return result

    if tailscale_adapter is not None:
        from .tailscale import init_tailscale_routes
        init_tailscale_routes(app, tailscale_adapter)
