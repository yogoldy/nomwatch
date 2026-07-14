"""Narrow, fail-closed Tailscale Serve integration for NomWatch only."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from waitress import create_server

from .state import LocalState, StateError

MIN_VERSION = (1, 90, 0)
MAX_VERSION_EXCLUSIVE = (1, 99, 0)
SERVE_PORT = 443
SERVE_PATH = "/"
LOGIN_URL_RE = re.compile(r"https://[^\s<>\"']+")
SAFE_LOGIN_HOSTS = {"login.tailscale.com"}
MAC_CLI_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/tailscale",
)

ENABLE_OPERATION = "serve.enable"
DISABLE_OPERATION = "serve.disable"
LOGIN_OPERATION = "login"
READ_OPERATIONS = {"version", "status", "serve.status", "funnel.status", "services.inventory"}
HELPER_OPERATIONS = READ_OPERATIONS | {ENABLE_OPERATION, DISABLE_OPERATION, LOGIN_OPERATION}


class TailscaleError(StateError):
    pass


@dataclass(frozen=True)
class ServeMapping:
    authority: str
    path: str
    proxy: str


@dataclass
class Observation:
    state: str
    cli_path: Optional[str] = None
    version: Optional[str] = None
    dns_name: Optional[str] = None
    node_id: Optional[str] = None
    tailnet_id: Optional[str] = None
    backend_state: Optional[str] = None
    mappings: tuple[ServeMapping, ...] = ()
    public_present: bool = False
    services_present: bool = False
    error: Optional[str] = None
    url: Optional[str] = None
    last_verified_at: Optional[float] = None

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state, "version": self.version, "dns_name": self.dns_name,
            "url": self.url, "error": self.error, "last_verified_at": self.last_verified_at,
            "private_only": self.state == "enabled", "certificate_transparency_disclosure":
            "Enabling Tailscale HTTPS publishes this device's full .ts.net name in public Certificate Transparency logs.",
            "install_url": "https://tailscale.com/download", "tailnet_policy_note":
            "Tailscale access controls still apply, and NomWatch login is always required.",
        }


def _version_tuple(raw: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if not match:
        raise TailscaleError("unrecognized Tailscale version")
    return tuple(int(value or 0) for value in match.groups())


def validate_action_url(text: str) -> Optional[str]:
    for candidate in LOGIN_URL_RE.findall(text or ""):
        parsed = urllib.parse.urlparse(candidate.rstrip(".,;)"))
        if parsed.scheme == "https" and parsed.hostname in SAFE_LOGIN_HOSTS and not parsed.username and not parsed.password:
            return parsed.geturl()
    return None


def parse_serve_config(raw: Any) -> tuple[tuple[ServeMapping, ...], bool]:
    if raw in (None, {}, []):
        return (), False
    if not isinstance(raw, dict):
        raise TailscaleError("Serve status JSON is not an object")
    allowed = {"TCP", "Web", "AllowFunnel", "Foreground", "Services"}
    unknown = set(raw) - allowed
    if unknown:
        raise TailscaleError(f"unrecognized Serve status fields: {', '.join(sorted(unknown))}")
    mappings = []
    web = raw.get("Web") or {}
    if not isinstance(web, dict):
        raise TailscaleError("Serve Web inventory is malformed")
    for authority, website in web.items():
        if not isinstance(website, dict) or set(website) - {"Handlers"}:
            raise TailscaleError("Serve website entry is malformed")
        handlers = website.get("Handlers") or {}
        if not isinstance(handlers, dict):
            raise TailscaleError("Serve handlers are malformed")
        for path, handler in handlers.items():
            if not isinstance(handler, dict) or set(handler) != {"Proxy"} or not isinstance(handler["Proxy"], str):
                raise TailscaleError("Serve handler is not an understood HTTP proxy")
            mappings.append(ServeMapping(str(authority).rstrip("."), str(path), handler["Proxy"]))
    tcp = raw.get("TCP") or {}
    if not isinstance(tcp, dict):
        raise TailscaleError("Serve TCP inventory is malformed")
    if tcp and not mappings:
        raise TailscaleError("Serve has a non-web listener NomWatch cannot own")
    funnel = raw.get("AllowFunnel") or {}
    if not isinstance(funnel, dict):
        raise TailscaleError("Serve public-state inventory is malformed")
    public = any(bool(value) for value in funnel.values())
    return tuple(sorted(mappings, key=lambda item: (item.authority, item.path, item.proxy))), public


def parse_services_inventory(text: str) -> bool:
    stripped = (text or "").strip()
    if stripped in {"", "{}", "[]", "null"}:
        return False
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise TailscaleError("Tailscale Services inventory is not recognized JSON") from exc
    if isinstance(data, dict) and set(data) <= {"Version", "Services"}:
        services = data.get("Services") or {}
        if not isinstance(services, (dict, list)):
            raise TailscaleError("Tailscale Services inventory is malformed")
        return bool(services)
    if isinstance(data, (dict, list)):
        return bool(data)
    raise TailscaleError("Tailscale Services inventory is malformed")


def command_for(operation: str, cli: str, ingress_port: int) -> list[str]:
    fixed = {
        "version": [cli, "version", "--json"],
        "status": [cli, "status", "--json"],
        "serve.status": [cli, "serve", "status", "--json"],
        "funnel.status": [cli, "funnel", "status", "--json"],
        "services.inventory": [cli, "serve", "get-config", "--all"],
        LOGIN_OPERATION: [cli, "login", "--timeout=120s"],
        ENABLE_OPERATION: [cli, "serve", "--bg", "--yes", "--https=443", "--set-path=/",
                           f"http://127.0.0.1:{ingress_port}"],
        DISABLE_OPERATION: [cli, "serve", "--yes", "--https=443", "--set-path=/", "off"],
    }
    if operation not in fixed:
        raise TailscaleError("helper operation is not allowed")
    argv = fixed[operation]
    lowered = " ".join(argv).lower()
    if operation in {ENABLE_OPERATION, DISABLE_OPERATION} and ("funnel" in lowered or "reset" in lowered):
        raise AssertionError("public or global Serve mutation is prohibited")
    return argv


class TailscaleAdapter:
    def __init__(self, state: LocalState, auth, ingress_port: int = 5152, *, runner=subprocess.run,
                 which=shutil.which, system: Optional[str] = None, clock=time.time,
                 helper_client=None):
        self.state = state
        self.auth = auth
        self.ingress_port = ingress_port
        self.runner = runner
        self.which = which
        self.system = system or platform.system()
        self.clock = clock
        self.helper_client = helper_client
        self.app = None
        self.backend = None
        self.backend_thread = None
        self.last_observation = Observation("not_installed")
        self._reconcile_stop = threading.Event()
        self._reconcile_thread = None

    def discover_cli(self) -> Optional[str]:
        found = self.which("tailscale")
        candidates = [found] if found else []
        if self.system == "Darwin":
            candidates.extend(MAC_CLI_PATHS)
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return found

    def _run(self, operation: str, cli: str, timeout: int = 10):
        if self.helper_client is not None and operation in {ENABLE_OPERATION, DISABLE_OPERATION, LOGIN_OPERATION}:
            return self.helper_client(operation)
        return self.runner(command_for(operation, cli, self.ingress_port), capture_output=True,
                           text=True, timeout=timeout)

    @staticmethod
    def _json_result(result, label: str):
        if result.returncode != 0:
            raise TailscaleError(f"{label} failed")
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise TailscaleError(f"{label} returned malformed JSON") from exc

    def _ownership(self):
        with self.state.connect() as conn:
            row = conn.execute("SELECT * FROM remote_access WHERE id=1").fetchone()
        return dict(row)

    def observe(self) -> Observation:
        cli = self.discover_cli()
        if not cli:
            return self._remember(Observation("not_installed"))
        try:
            version_json = self._json_result(self._run("version", cli), "Tailscale version")
            version = str(version_json.get("Version") or version_json.get("version") or version_json.get("ClientVersion") or version_json.get("MajorMinorPatch") or "")
            parsed_version = _version_tuple(version)
            if parsed_version < MIN_VERSION or parsed_version >= MAX_VERSION_EXCLUSIVE:
                return self._remember(Observation("unsupported_version", cli, version=version,
                                                  error=f"supported range is >=1.90 and <1.99"))
            status_result = self._run("status", cli)
            if status_result.returncode != 0:
                return self._remember(Observation("daemon_unavailable", cli, version=version,
                                                  error="Tailscale daemon or app is unavailable"))
            status = self._json_result(status_result, "Tailscale status")
            backend = str(status.get("BackendState", ""))
            if backend != "Running":
                return self._remember(Observation("needs_login", cli, version=version, backend_state=backend,
                                                  error="Sign in to Tailscale to continue"))
            self_info = status.get("Self") or {}
            tailnet = status.get("CurrentTailnet") or {}
            dns_name = str(self_info.get("DNSName") or "").rstrip(".")
            node_id = str(self_info.get("StableID") or self_info.get("ID") or "")
            tailnet_id = str(tailnet.get("MagicDNSSuffix") or tailnet.get("Name") or "")
            if not dns_name.endswith(".ts.net") or not node_id or not tailnet_id:
                raise TailscaleError("connected status lacks a stable .ts.net node/tailnet identity")
            serve = self._json_result(self._run("serve.status", cli), "Serve status")
            mappings, serve_public = parse_serve_config(serve)
            funnel_result = self._run("funnel.status", cli)
            funnel_public = False
            if funnel_result.returncode == 0:
                _funnel_mappings, funnel_public = parse_serve_config(self._json_result(funnel_result, "Funnel status"))
                funnel_public = funnel_public or bool(_funnel_mappings)
            elif not (self.system == "Darwin" and "not supported" in (funnel_result.stderr or "").lower()):
                raise TailscaleError("Funnel public-state guard could not be enumerated")
            services_result = self._run("services.inventory", cli)
            if services_result.returncode != 0:
                raise TailscaleError("Tailscale Services inventory could not be enumerated")
            services = parse_services_inventory(services_result.stdout)
            ownership = self._ownership()
            target = f"http://127.0.0.1:{self.ingress_port}"
            exact = (len(mappings) == 1 and mappings[0] == ServeMapping(f"{dns_name}:443", "/", target))
            identity_match = ownership.get("node_identity") == node_id and ownership.get("tailnet_identity") == tailnet_id
            public = serve_public or funnel_public
            if ownership.get("desired_enabled") and exact and identity_match and not public and not services:
                observed = Observation("enabled", cli, version, dns_name, node_id, tailnet_id, backend,
                                       mappings, public, services, url=f"https://{dns_name}/", last_verified_at=self.clock())
            elif ownership.get("desired_enabled"):
                observed = Observation("degraded", cli, version, dns_name, node_id, tailnet_id, backend,
                                       mappings, public, services, error="owned Serve mapping or Tailscale identity drifted")
            elif mappings or public or services:
                observed = Observation("conflict", cli, version, dns_name, node_id, tailnet_id, backend,
                                       mappings, public, services,
                                       error="existing Serve, Funnel, or Tailscale Service configuration must remain user-owned")
            else:
                observed = Observation("connected", cli, version, dns_name, node_id, tailnet_id, backend)
            return self._remember(observed)
        except (TailscaleError, subprocess.TimeoutExpired, OSError) as exc:
            return self._remember(Observation("unavailable", cli, error=str(exc)))

    def _remember(self, observation: Observation) -> Observation:
        self.last_observation = observation
        return observation

    def attach_app(self, app) -> None:
        self.app = app

    def allowed_hosts(self) -> set[str]:
        return {self.last_observation.dns_name} if self.last_observation.dns_name else set()

    def listener_allows(self, hostname: str, server_port: str) -> bool:
        is_backend = str(server_port) == str(self.ingress_port)
        expected = self.last_observation.dns_name
        return hostname == expected if is_backend else hostname != expected

    def _start_backend(self) -> None:
        if self.backend is not None:
            return
        if self.app is None:
            raise TailscaleError("Serve-only gateway is not ready")
        self.backend = create_server(self.app, host="127.0.0.1", port=self.ingress_port, threads=4)
        self.backend_thread = threading.Thread(target=self.backend.run, name="nomwatch-tailscale", daemon=True)
        self.backend_thread.start()

    def _stop_backend(self) -> None:
        if self.backend:
            self.backend.close()
        self.backend = self.backend_thread = None

    def _persist(self, observation: Observation, desired: bool, target: Optional[str], status: str,
                 error: Optional[str] = None) -> None:
        fingerprint = hashlib.sha256(json.dumps([
            (item.authority, item.path, item.proxy) for item in observation.mappings
        ], sort_keys=True).encode()).hexdigest()
        with self.state.connect() as conn:
            conn.execute(
                "UPDATE remote_access SET desired_enabled=?,owned_https_port=?,owned_target=?,node_identity=?,tailnet_identity=?,status_fingerprint=?,observed_url=?,status=?,error=?,updated_at=? WHERE id=1",
                (int(desired), SERVE_PORT if desired else None, target, observation.node_id,
                 observation.tailnet_id, fingerprint if desired else None, observation.url,
                 status, error, self.clock()),
            )

    def login(self) -> dict:
        observation = self.observe()
        if observation.state not in {"needs_login", "connected"} or not observation.cli_path:
            raise TailscaleError("Tailscale login is not currently available")
        result = self._run(LOGIN_OPERATION, observation.cli_path, timeout=130)
        url = validate_action_url((result.stdout or "") + "\n" + (result.stderr or ""))
        if result.returncode != 0 and not url:
            raise TailscaleError("Tailscale login did not provide a valid official action URL")
        return {"state": "login_started", "action_url": url}

    def enable(self) -> dict:
        before = self.observe()
        if before.state != "connected" or not before.cli_path:
            raise TailscaleError(before.error or f"cannot enable from state {before.state}")
        self._start_backend()
        result = self._run(ENABLE_OPERATION, before.cli_path, timeout=30)
        if result.returncode != 0:
            self._stop_backend()
            action_url = validate_action_url((result.stdout or "") + "\n" + (result.stderr or ""))
            if action_url:
                self._persist(before, False, None, "needs_serve_consent", "HTTPS/Serve consent is required")
                return {"state": "needs_serve_consent", "action_url": action_url,
                        "certificate_transparency_disclosure": before.public()["certificate_transparency_disclosure"]}
            raise TailscaleError("Tailscale Serve enable failed")
        after = self.observe()
        target = f"http://127.0.0.1:{self.ingress_port}"
        expected = ServeMapping(f"{before.dns_name}:443", "/", target)
        if len(after.mappings) != 1 or after.mappings[0] != expected or after.public_present or after.services_present:
            self._stop_backend()
            self._persist(after, False, None, "cleanup_required", "post-enable verification did not match exact private ownership")
            raise TailscaleError("Serve changed concurrently; cleanup requires local diagnosis")
        after.state = "enabled"
        after.url = f"https://{before.dns_name}/"
        after.last_verified_at = self.clock()
        self._persist(after, True, target, "enabled")
        self.last_observation = after
        return after.public()

    def disable(self) -> dict:
        current = self.observe()
        owned = self._ownership()
        target = f"http://127.0.0.1:{self.ingress_port}"
        expected = ServeMapping(f"{owned.get('node_identity') and current.dns_name}:443", "/", target)
        identity_match = owned.get("node_identity") == current.node_id and owned.get("tailnet_identity") == current.tailnet_id
        if not owned.get("desired_enabled"):
            self._stop_backend()
            return {"state": "disabled"}
        if len(current.mappings) != 1 or current.mappings[0] != expected or not identity_match or current.public_present:
            self._stop_backend()
            self.auth.revoke_origin("tailscale")
            self._persist(current, True, target, "cleanup_required", "live state no longer exactly matches owned mapping")
            raise TailscaleError("refusing to disable changed Tailscale configuration; cleanup is required")
        result = self._run(DISABLE_OPERATION, current.cli_path, timeout=30)
        if result.returncode != 0:
            self._persist(current, True, target, "cleanup_required", "path-scoped disable failed")
            raise TailscaleError("path-scoped Serve disable failed")
        after = self.observe()
        if after.mappings or after.public_present:
            self._persist(after, True, target, "cleanup_required", "Serve mapping remained after disable")
            raise TailscaleError("Serve mapping remained after disable")
        self._stop_backend()
        self.auth.revoke_origin("tailscale")
        self._persist(after, False, None, "connected")
        return {"state": "disabled"}

    def reconcile(self) -> Observation:
        observation = self.observe()
        owned = self._ownership()
        if owned.get("desired_enabled") and observation.state != "enabled":
            self._stop_backend()
            self.auth.revoke_origin("tailscale")
            self._persist(observation, True, owned.get("owned_target"), "degraded", observation.error)
        elif observation.state == "enabled":
            self._start_backend()
        return observation

    def start_reconciler(self, interval: float = 30.0) -> None:
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            return
        self._reconcile_stop.clear()
        def loop():
            while not self._reconcile_stop.wait(interval):
                self.reconcile()
        self._reconcile_thread = threading.Thread(target=loop, name="nomwatch-tailscale-reconcile", daemon=True)
        self._reconcile_thread.start()

    def shutdown(self) -> None:
        self._reconcile_stop.set()
        if self._reconcile_thread:
            self._reconcile_thread.join(timeout=2)
        self._stop_backend()


def init_tailscale_routes(app, adapter: TailscaleAdapter) -> None:
    from flask import jsonify, request

    @app.get("/api/v1/access/tailscale")
    def tailscale_access_status():
        return jsonify(adapter.observe().public())

    @app.post("/api/v1/access/tailscale/login")
    def tailscale_login():
        try:
            return jsonify({"ok": True, **adapter.login()}), 202
        except TailscaleError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.post("/api/v1/access/tailscale/enable")
    def tailscale_enable():
        data = request.get_json(silent=True) or {}
        if not data.get("certificate_transparency_acknowledged"):
            return jsonify({"ok": False, "error": "Certificate Transparency disclosure must be acknowledged"}), 422
        try:
            return jsonify({"ok": True, **adapter.enable()})
        except TailscaleError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.post("/api/v1/access/tailscale/disable")
    def tailscale_disable():
        try:
            return jsonify({"ok": True, **adapter.disable()})
        except TailscaleError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
