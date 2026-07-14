from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nomwatch.auth import AuthService
from nomwatch.paths import NomWatchPaths
from nomwatch.state import LocalState
from nomwatch.tailscale import (
    DISABLE_OPERATION,
    ENABLE_OPERATION,
    Observation,
    ServeMapping,
    TailscaleAdapter,
    TailscaleError,
    command_for,
    parse_serve_config,
    validate_action_url,
)
from nomwatch.webui import create_app


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeBackend:
    def __init__(self):
        self.closed = False

    def run(self):
        return None

    def close(self):
        self.closed = True


class FakeTailscale:
    def __init__(self):
        self.version = "1.96.2"
        self.backend = "Running"
        self.serve = {}
        self.funnel = {}
        self.services = {}
        self.enable_result = None
        self.commands = []

    @property
    def dns(self):
        return "nomwatch.tail123.ts.net"

    def running_status(self):
        return {
            "BackendState": self.backend,
            "Self": {"StableID": "node-stable-1", "DNSName": self.dns + "."},
            "CurrentTailnet": {"MagicDNSSuffix": "tail123.ts.net"},
        }

    def exact_mapping(self):
        return {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {f"{self.dns}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5152"}}}},
            "AllowFunnel": {},
        }

    def __call__(self, argv, **kwargs):
        self.commands.append(list(argv))
        tail = argv[1:]
        if tail == ["version", "--json"]:
            return Result(stdout=json.dumps({"Version": self.version}))
        if tail == ["status", "--json"]:
            if self.backend == "daemon-error":
                return Result(1, stderr="daemon unavailable")
            return Result(stdout=json.dumps(self.running_status()))
        if tail == ["serve", "status", "--json"]:
            return Result(stdout=json.dumps(self.serve))
        if tail == ["funnel", "status", "--json"]:
            return Result(stdout=json.dumps(self.funnel))
        if tail == ["serve", "get-config", "--all"]:
            return Result(stdout=json.dumps({"Version": "alpha0", "Services": self.services}))
        if tail and tail[0] == "login":
            return Result(stdout="To authenticate, visit: https://login.tailscale.com/a/abc123")
        if tail[:2] == ["serve", "--bg"]:
            if self.enable_result:
                return self.enable_result
            self.serve = self.exact_mapping()
            return Result()
        if tail == ["serve", "--yes", "--https=443", "--set-path=/", "off"]:
            self.serve = {}
            return Result()
        raise AssertionError(f"unexpected argv: {argv}")


class TailscaleAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = LocalState(NomWatchPaths(Path(self.temp.name) / "home"))
        self.auth = AuthService(self.state)
        code = self.auth.ensure_bootstrap()
        self.owner_session = self.auth.claim_owner(code, "owner", "Owner", "correct horse battery")
        self.fake = FakeTailscale()
        self.adapter = TailscaleAdapter(
            self.state, self.auth, runner=self.fake, which=lambda name: "/fake/tailscale", system="Linux",
        )
        self.backend_patch = patch("nomwatch.tailscale.create_server", return_value=FakeBackend())
        self.backend_patch.start()
        self.adapter.attach_app(lambda environ, start_response: [])

    def tearDown(self):
        self.adapter.shutdown()
        self.backend_patch.stop()
        self.temp.cleanup()

    def test_detects_all_prerequisite_and_conflict_states(self):
        missing = TailscaleAdapter(self.state, self.auth, which=lambda name: None, system="Linux")
        self.assertEqual(missing.observe().state, "not_installed")
        self.fake.backend = "daemon-error"
        self.assertEqual(self.adapter.observe().state, "daemon_unavailable")
        self.fake.backend = "NeedsLogin"
        self.assertEqual(self.adapter.observe().state, "needs_login")
        self.fake.backend = "Running"
        self.assertEqual(self.adapter.observe().state, "connected")

        self.fake.serve = self.fake.exact_mapping()
        self.assertEqual(self.adapter.observe().state, "conflict")
        self.fake.serve = {}
        self.fake.services = {"svc:other": {}}
        self.assertEqual(self.adapter.observe().state, "conflict")
        self.fake.services = {}
        self.fake.funnel = self.fake.exact_mapping()
        self.assertEqual(self.adapter.observe().state, "conflict")

    def test_unknown_version_and_malformed_inventory_fail_closed(self):
        self.fake.version = "2.0.0"
        self.assertEqual(self.adapter.observe().state, "unsupported_version")
        self.fake.version = "1.96.2"
        self.fake.serve = {"Unexpected": {}}
        observation = self.adapter.observe()
        self.assertEqual(observation.state, "unavailable")
        self.assertIn("unrecognized", observation.error)

    def test_enable_verify_disable_uses_only_exact_private_path_commands(self):
        enabled = self.adapter.enable()
        self.assertEqual(enabled["state"], "enabled")
        enable_argv = next(argv for argv in self.fake.commands if "--bg" in argv)
        self.assertEqual(enable_argv, [
            "/fake/tailscale", "serve", "--bg", "--yes", "--https=443", "--set-path=/",
            "http://127.0.0.1:5152",
        ])
        self.assertNotIn("funnel", " ".join(enable_argv).lower())
        self.assertNotIn("reset", " ".join(enable_argv).lower())
        self.assertEqual(self.adapter.observe().state, "enabled")
        disabled = self.adapter.disable()
        self.assertEqual(disabled["state"], "disabled")
        self.assertIn([
            "/fake/tailscale", "serve", "--yes", "--https=443", "--set-path=/", "off",
        ], self.fake.commands)

    def test_consent_url_is_validated_and_ct_disclosed(self):
        self.fake.enable_result = Result(1, stderr="Approve: https://login.tailscale.com/admin/serve/abc")
        result = self.adapter.enable()
        self.assertEqual(result["state"], "needs_serve_consent")
        self.assertEqual(result["action_url"], "https://login.tailscale.com/admin/serve/abc")
        self.assertIn("Certificate Transparency", result["certificate_transparency_disclosure"])
        self.assertIsNone(validate_action_url("https://evil.example/a/token"))

    def test_drift_never_mutates_and_revokes_remote_sessions(self):
        self.adapter.enable()
        remote = self.auth.create_session(self.owner_session.user_id, "tailscale")
        self.fake.serve["Web"]["other.tail123.ts.net:443"] = {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
        }
        mutations_before = len([argv for argv in self.fake.commands if "--bg" in argv or "off" in argv])
        observed = self.adapter.reconcile()
        mutations_after = len([argv for argv in self.fake.commands if "--bg" in argv or "off" in argv])
        self.assertEqual(observed.state, "degraded")
        self.assertEqual(mutations_before, mutations_after)
        self.assertIsNone(self.auth.authenticate(remote.token))

    def test_login_uses_bounded_allowlisted_command(self):
        self.fake.backend = "NeedsLogin"
        result = self.adapter.login()
        self.assertEqual(result["action_url"], "https://login.tailscale.com/a/abc123")
        self.assertIn(["/fake/tailscale", "login", "--timeout=120s"], self.fake.commands)

    def test_command_policy_has_no_public_or_global_mutation(self):
        for operation in (ENABLE_OPERATION, DISABLE_OPERATION):
            argv = command_for(operation, "/usr/bin/tailscale", 5152)
            joined = " ".join(argv).lower()
            self.assertNotIn("funnel", joined)
            self.assertNotIn("reset", joined)
            self.assertNotIn("0.0.0.0", joined)
            self.assertNotIn("authkey", joined)
        with self.assertRaises(TailscaleError):
            command_for("arbitrary.shell", "/usr/bin/tailscale", 5152)


class TailscaleParsingTests(unittest.TestCase):
    def test_structured_mapping_and_public_guard(self):
        raw = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {"host.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5152"}}}},
            "AllowFunnel": {"host.tail.ts.net:443": True},
        }
        mappings, public = parse_serve_config(raw)
        self.assertEqual(mappings, (ServeMapping("host.tail.ts.net:443", "/", "http://127.0.0.1:5152"),))
        self.assertTrue(public)


class TailscaleWebBoundaryTests(unittest.TestCase):
    def test_ts_sessions_are_secure_and_listener_separated(self):
        with tempfile.TemporaryDirectory() as temp:
            state = LocalState(NomWatchPaths(Path(temp) / "home"))
            auth = AuthService(state)
            code = auth.ensure_bootstrap()
            auth.claim_owner(code, "owner", "Owner", "correct horse battery")
            fake = FakeTailscale()
            adapter = TailscaleAdapter(state, auth, runner=fake, which=lambda name: "/fake/tailscale", system="Linux")
            adapter.last_observation = Observation("connected", dns_name=fake.dns)
            hosts = lambda: {"localhost", "127.0.0.1", "::1", fake.dns}
            app = create_app(state=state, auth=auth, tailscale_adapter=adapter,
                             allowed_hosts_provider=hosts, listener_policy=adapter.listener_allows)
            app.testing = True
            client = app.test_client()
            rejected = client.get("/login", headers={"Host": fake.dns}, environ_overrides={"SERVER_PORT": "5151"})
            self.assertEqual(rejected.status_code, 400)
            login = client.post(
                "/api/v1/auth/login", json={"username": "owner", "password": "correct horse battery"},
                headers={"Host": fake.dns}, environ_overrides={"SERVER_PORT": "5152", "wsgi.url_scheme": "https"},
            )
            self.assertEqual(login.status_code, 200, login.get_data(as_text=True))
            cookies = login.headers.getlist("Set-Cookie")
            self.assertTrue(all("Secure" in value for value in cookies))


if __name__ == "__main__":
    unittest.main()
