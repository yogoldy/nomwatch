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
    "/api/install-mediamtx", "/api/pull-model",
}
OPERATOR_MUTATIONS = {
    "/api/start-monitoring", "/api/stop-monitoring", "/api/test-notify",
}
OWNER_GET_PREFIXES = ("/setup", "/api/v1/users", "/api/v1/invitations", "/api/v1/sessions")

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>NomWatch sign in</title></head>
<body><main><h1>NomWatch</h1><form method="post" action="/api/v1/auth/login">
<label>Username <input name="username" autocomplete="username" required></label>
<label>Password <input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button></form></main></body></html>"""

CLAIM_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Claim NomWatch</title></head>
<body><main><h1>Create the first owner</h1><p>This page is available on loopback only. Enter the code printed by <code>nomwatch host</code>.</p>
<form method="post" action="/api/v1/auth/claim"><label>Claim code <input name="code" required></label>
<label>Username <input name="username" required></label><label>Display name <input name="display_name"></label>
<label>Password <input name="password" type="password" minlength="12" required></label><button>Create owner</button></form></main></body></html>"""


def _payload(request) -> dict:
    return request.get_json(silent=True) or request.form.to_dict()


def init_security(app, auth: AuthService) -> None:
    from flask import g, jsonify, make_response, redirect, request

    app.extensions["nomwatch_auth"] = auth

    def error(message: str, status: int):
        return jsonify({"ok": False, "error": message}), status

    def is_loopback() -> bool:
        return request.remote_addr in ("127.0.0.1", "::1", None)

    def expected_origin() -> str:
        return f"{request.scheme}://{request.host}"

    @app.before_request
    def enforce_security():
        g.nomwatch_session = None
        raw_host = request.host.lower()
        host = raw_host[1:raw_host.index("]")] if raw_host.startswith("[") and "]" in raw_host else raw_host.split(":", 1)[0]
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return error("Host is not allowed while NomWatch is loopback-only", 400)

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != expected_origin().rstrip("/"):
                return error("Origin is not allowed", 403)
            fetch_site = request.headers.get("Sec-Fetch-Site")
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                return error("Cross-site request rejected", 403)

        if request.path in ANONYMOUS_PATHS:
            if request.path in {"/claim", "/api/v1/auth/claim"} and not is_loopback():
                return error("Owner claim is available only through loopback", 403)
            return None

        token = request.cookies.get(SESSION_COOKIE, "")
        session = auth.authenticate(token, origin_class="loopback") if token else None
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
        response.set_cookie(SESSION_COOKIE, issued.token, httponly=True, samesite="Lax", secure=False, max_age=7 * 86400)
        response.set_cookie(CSRF_COOKIE, issued.csrf, httponly=False, samesite="Lax", secure=False, max_age=7 * 86400)
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
                                request.remote_addr or "local", "loopback")
        except StateError as exc:
            return error(str(exc), 429 if "too many" in str(exc) else 401)
        return issue_response(issued)

    @app.post("/api/v1/auth/accept-invitation")
    def accept_invitation():
        data = _payload(request)
        try:
            issued = auth.accept_invitation(str(data.get("code", "")), str(data.get("username", "")),
                                            str(data.get("display_name", "")), str(data.get("password", "")), "loopback")
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
