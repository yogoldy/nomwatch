"""Flask security boundary for local accounts and the legacy-compatible UI."""
from __future__ import annotations

from .auth import AuthService
from .state import StateError

SESSION_COOKIE = "nomwatch_session"
CSRF_COOKIE = "nomwatch_csrf"

ANONYMOUS_PATHS = {
    "/login", "/claim", "/api/v1/bootstrap", "/api/v1/auth/login",
    "/api/v1/auth/claim", "/api/v1/auth/accept-invitation",
}
LOCAL_ONLY_PATHS = {
    "/api/install-service", "/api/uninstall-service", "/api/install-ffmpeg",
    "/api/install-mediamtx", "/api/start-mediamtx", "/api/pull-model",
}
OPERATOR_MUTATIONS = {
    "/api/start-monitoring", "/api/stop-monitoring", "/api/test-notify",
}
OWNER_GET_PREFIXES = ("/setup", "/api/v1/users", "/api/v1/invitations", "/api/v1/sessions", "/api/v1/access/")

AUTH_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
:root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
* {{ box-sizing: border-box; }} body {{ margin: 0; min-height: 100vh; color: #edf4f1; background: radial-gradient(circle at 15% 15%, #1a5d50 0, transparent 31rem), radial-gradient(circle at 92% 88%, #183f63 0, transparent 28rem), #0d1718; display: grid; place-items: center; padding: 24px; }}
main {{ width: min(100%, 510px); border: 1px solid #315453; border-radius: 22px; padding: 38px; background: rgba(16, 31, 32, .94); box-shadow: 0 24px 80px rgba(0,0,0,.35); }}
.mark {{ width: 42px; height: 42px; border-radius: 13px; display: grid; place-items: center; color: #09201b; background: #7de5be; font-size: 25px; font-weight: 800; margin-bottom: 22px; }} h1 {{ margin: 0; font-size: 29px; letter-spacing: -.04em; }}
.eyebrow {{ color: #7de5be; font-size: 12px; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; margin: 0 0 10px; }} p {{ color: #b5c8c4; line-height: 1.55; }} form {{ display: grid; gap: 16px; margin-top: 27px; }} label {{ display: grid; gap: 7px; color: #d5e2df; font-size: 14px; font-weight: 650; }}
input {{ width: 100%; border: 1px solid #45615d; background: #0b1415; color: #f4faf8; border-radius: 10px; padding: 12px 13px; font: inherit; }} input:focus {{ outline: 3px solid rgba(125,229,190,.22); border-color: #7de5be; }}
button {{ border: 0; border-radius: 10px; padding: 13px 16px; cursor: pointer; font: inherit; font-weight: 800; color: #082019; background: #7de5be; }} button:disabled {{ opacity: .55; cursor: wait; }} .notice {{ min-height: 22px; margin: 3px 0 0; color: #ffb9a9; font-size: 14px; }}
.quiet {{ margin-top: 24px; font-size: 13px; }} code {{ color: #d7f8e9; }} @media (max-width: 520px) {{ main {{ padding: 29px 23px; }} }}
</style></head><body><main><div class="mark">N</div>{body}</main>
<script>(function() {{ const form=document.querySelector('form[data-auth]'); if (!form) return; const notice=document.querySelector('.notice'); form.addEventListener('submit', async function(event) {{ event.preventDefault(); notice.textContent=''; const button=form.querySelector('button'); button.disabled=true; const original=button.textContent; button.textContent='Working…'; try {{ const response=await fetch(form.action, {{method:'POST', headers:{{Accept:'application/json'}}, body:new FormData(form)}}); const data=await response.json(); if (!response.ok) throw new Error(data.error || 'That did not work. Please try again.'); window.location.assign(form.dataset.next); }} catch (error) {{ notice.textContent=error.message; button.disabled=false; button.textContent=original; }} }}); }}());</script></body></html>"""

LOGIN_PAGE = AUTH_SHELL.format(title="Sign in to NomWatch", body="""
<p class="eyebrow">Local host</p><h1>Welcome back</h1><p>Sign in to manage this NomWatch host.</p>
<form data-auth data-next="/" method="post" action="/api/v1/auth/login">
<label>Username <input name="username" autocomplete="username" required autofocus></label>
<label>Password <input name="password" type="password" autocomplete="current-password" required></label>
<p class="notice" role="alert"></p><button type="submit">Sign in</button></form>
<p class="quiet">This is a local NomWatch account, separate from your camera-app login.</p>""")

CLAIM_PAGE = AUTH_SHELL.format(title="Set up your NomWatch host", body="""
<p class="eyebrow">This computer is the host</p><h1>Create the owner account</h1><p>You are setting up the account that controls this NomWatch host. The one-time setup code was printed when the host started.</p>
<form data-auth data-next="/setup" method="post" action="/api/v1/auth/claim">
<label>Host setup code <input name="code" autocomplete="one-time-code" required autofocus></label>
<label>Username <input name="username" autocomplete="username" required></label>
<label>Display name <input name="display_name" autocomplete="name" placeholder="Optional"></label>
<label>Password <input name="password" type="password" autocomplete="new-password" minlength="12" required></label>
<p class="notice" role="alert"></p><button type="submit">Create owner account</button></form>
<p class="quiet">The setup code works only on this computer and expires after 15 minutes. It is not a camera password.</p>""")


def _payload(request) -> dict:
    return request.get_json(silent=True) or request.form.to_dict()


def init_security(app, auth: AuthService, *, allowed_hosts=None, listener_policy=None) -> None:
    from flask import g, jsonify, make_response, redirect, request

    app.extensions["nomwatch_auth"] = auth

    def error(message: str, status: int):
        return jsonify({"ok": False, "error": message}), status

    def is_loopback() -> bool:
        return request.remote_addr in ("127.0.0.1", "::1", None)

    def expected_origin() -> str:
        return f"{request.scheme}://{request.host}"

    def origin_is_allowed(origin: str) -> bool:
        """Accept the exact origin, plus equivalent loopback hostnames.

        Browsers and local launchers sometimes switch between localhost and
        127.0.0.1.  They are the same local security boundary, but the old
        literal string comparison turned that harmless switch into a failed
        first-owner claim.
        """
        from urllib.parse import urlsplit

        try:
            expected = urlsplit(expected_origin())
            supplied = urlsplit(origin)
            expected_port = expected.port or (443 if expected.scheme == "https" else 80)
            supplied_port = supplied.port or (443 if supplied.scheme == "https" else 80)
        except ValueError:
            return False
        if supplied.scheme != expected.scheme or supplied.username or supplied.password:
            return False
        loopback = {"localhost", "127.0.0.1", "::1"}
        expected_host = (expected.hostname or "").lower()
        supplied_host = (supplied.hostname or "").lower()
        return (supplied_host == expected_host or (supplied_host in loopback and expected_host in loopback)) and supplied_port == expected_port

    def request_origin_class() -> str:
        host = request.host.lower()
        hostname = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.split(":", 1)[0]
        if hostname.endswith(".ts.net"):
            return "tailscale"
        return "loopback" if hostname in {"localhost", "127.0.0.1", "::1"} else "lan"

    @app.before_request
    def enforce_security():
        g.nomwatch_session = None
        raw_host = request.host.lower()
        host = raw_host[1:raw_host.index("]")] if raw_host.startswith("[") and "]" in raw_host else raw_host.split(":", 1)[0]
        permitted = allowed_hosts() if allowed_hosts else {"localhost", "127.0.0.1", "::1"}
        if host not in permitted:
            return error("Host is not an active NomWatch listener", 400)
        if listener_policy and not listener_policy(host, request.environ.get("SERVER_PORT", "")):
            return error("Host is not valid for this listener", 400)

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin and not origin_is_allowed(origin):
                return error("Origin is not allowed", 403)
            fetch_site = request.headers.get("Sec-Fetch-Site")
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                return error("Cross-site request rejected", 403)

        if request.path in ANONYMOUS_PATHS:
            if request.path in {"/claim", "/api/v1/auth/claim"} and not is_loopback():
                return error("Owner claim is available only through loopback", 403)
            return None

        token = request.cookies.get(SESSION_COOKIE, "")
        session = auth.authenticate(token, origin_class=request_origin_class()) if token else None
        if not session:
            if request.path.startswith("/api/"):
                return error("authentication required", 401)
            return redirect("/claim" if not auth.has_users() else "/login")
        g.nomwatch_session = session

        if request.path in LOCAL_ONLY_PATHS:
            return error("This operation is available only from the local OS installer/CLI", 403)

        required = "viewer"
        if request.path.startswith(OWNER_GET_PREFIXES):
            required = "owner"
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            required = "operator" if request.path in OPERATOR_MUTATIONS else "owner"
            if request.path in {"/api/v1/auth/logout", "/api/v1/auth/reauth"}:
                required = "viewer"
            csrf = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not auth.verify_csrf(session, csrf or ""):
                return error("valid CSRF token required", 403)
            if request.path.startswith("/api/v1/access/") and not auth.recent_reauth(session):
                return error("recent password reauthentication required", 403)
        if not auth.require_role(session, required):
            return error(f"{required} role required", 403)
        return None

    @app.after_request
    def secure_response(response):
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.path.startswith(("/api/", "/clips/")):
            response.headers.setdefault("Cache-Control", "no-store")
        if g.get("nomwatch_session") and response.mimetype == "text/html":
            body = response.get_data(as_text=True)
            wrapper = """<script>(function(){const base=window.fetch;window.fetch=function(u,o){o=o||{};const m=(o.method||'GET').toUpperCase();if(!['GET','HEAD','OPTIONS'].includes(m)){const c=document.cookie.split('; ').find(v=>v.startsWith('nomwatch_csrf='));o.headers=new Headers(o.headers||{});if(c)o.headers.set('X-CSRF-Token',decodeURIComponent(c.split('=').slice(1).join('=')));}return base(u,o);};})();</script>"""
            response.set_data(body.replace("</head>", wrapper + "</head>", 1))
            response.headers["Content-Length"] = str(len(response.get_data()))
        return response

    @app.get("/login")
    def login_page():
        return LOGIN_PAGE

    @app.get("/claim")
    def claim_page():
        if auth.has_users():
            return redirect("/login")
        return CLAIM_PAGE

    @app.get("/api/v1/bootstrap")
    def bootstrap_status():
        return jsonify({"claim_required": not auth.has_users(), "claim_loopback_only": True})

    def issue_response(issued, status=200):
        response = make_response(jsonify({"ok": True}), status)
        secure = request_origin_class() == "tailscale"
        response.set_cookie(SESSION_COOKIE, issued.token, httponly=True, samesite="Lax", secure=secure, max_age=7 * 86400)
        response.set_cookie(CSRF_COOKIE, issued.csrf, httponly=False, samesite="Lax", secure=secure, max_age=7 * 86400)
        return response

    @app.post("/api/v1/auth/claim")
    def claim_owner():
        data = _payload(request)
        try:
            issued = auth.claim_owner(str(data.get("code", "")), str(data.get("username", "")),
                                      str(data.get("display_name", "")), str(data.get("password", "")))
        except StateError as exc:
            return error(str(exc), 422)
        return issue_response(issued, 201)

    @app.post("/api/v1/auth/login")
    def login():
        data = _payload(request)
        try:
            issued = auth.login(str(data.get("username", "")), str(data.get("password", "")),
                                request.remote_addr or "local", request_origin_class())
        except StateError as exc:
            return error(str(exc), 429 if "too many" in str(exc) else 401)
        return issue_response(issued)

    @app.post("/api/v1/auth/accept-invitation")
    def accept_invitation():
        data = _payload(request)
        try:
            issued = auth.accept_invitation(str(data.get("code", "")), str(data.get("username", "")),
                                            str(data.get("display_name", "")), str(data.get("password", "")), request_origin_class())
        except (StateError, Exception) as exc:
            return error(str(exc), 422)
        return issue_response(issued, 201)

    @app.get("/api/v1/me")
    def me():
        session = g.nomwatch_session
        return jsonify({"id": session["user_id"], "username": session["username"],
                        "display_name": session["display_name"], "role": session["role"]})

    @app.post("/api/v1/auth/logout")
    def logout():
        auth.revoke(g.nomwatch_session["id"], g.nomwatch_session["user_id"])
        response = make_response(jsonify({"ok": True}))
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return response

    @app.post("/api/v1/auth/reauth")
    def reauthenticate():
        try:
            auth.reauthenticate(g.nomwatch_session["id"], str(_payload(request).get("password", "")))
        except StateError as exc:
            return error(str(exc), 403)
        return jsonify({"ok": True, "valid_for_seconds": 600})

    @app.get("/api/v1/users")
    def users():
        return jsonify({"users": auth.list_users()})

    @app.post("/api/v1/invitations")
    def invitation():
        data = _payload(request)
        code = auth.create_invitation(str(data.get("role", "viewer")), g.nomwatch_session["user_id"])
        return jsonify({"ok": True, "activation_code": code, "expires_in_seconds": 1800}), 201

    @app.get("/api/v1/sessions")
    def sessions():
        user_id = request.args.get("user_id")
        return jsonify({"sessions": auth.list_sessions(user_id)})

    @app.post("/api/v1/sessions/<session_id>/revoke")
    def revoke_session(session_id):
        return jsonify({"ok": auth.revoke(session_id, g.nomwatch_session["user_id"])})


def redacted_config_payload(cfg) -> dict:
    from dataclasses import asdict
    raw = asdict(cfg)
    raw["camera"].pop("password", None)
    raw["camera"]["password_set"] = bool(cfg.camera.password)
    for namespace in ("notify", "storage"):
        for key in list(raw[namespace]):
            if any(fragment in key.lower() for fragment in ("token", "credential")):
                raw[namespace][key + "_set"] = bool(raw[namespace].pop(key))
            elif any(fragment in key.lower() for fragment in ("path", "dir", "folder")):
                raw[namespace].pop(key)
    return raw
