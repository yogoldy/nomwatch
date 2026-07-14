from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nomwatch.auth import AuthService
from nomwatch.config import CameraConfig, NomWatchConfig
from nomwatch.gateway import rewrite_hls_manifest
from nomwatch.network import (
    InterfaceAddress,
    LanAccessManager,
    NetworkPolicyError,
    create_interface_bound_socket,
    enumerate_interfaces,
    is_allowed_private_address,
    validate_camera_target,
)
from nomwatch.paths import NomWatchPaths
from nomwatch.state import LocalState
from nomwatch.webui import create_app


class FakeCommandResult:
    returncode = 0

    def __init__(self, stdout):
        self.stdout = stdout


class FakeSocket:
    def __init__(self, *args):
        self.options = []
        self.bound = None
        self.closed = False

    def setsockopt(self, *args):
        self.options.append(args)

    def bind(self, address):
        self.bound = address

    def listen(self, backlog):
        self.backlog = backlog

    def close(self):
        self.closed = True


class FakeServer:
    def __init__(self):
        self.closed = False

    def run(self):
        return None

    def close(self):
        self.closed = True


class FakeUpstream:
    def __init__(self, text, content_type="application/vnd.apple.mpegurl", status=200):
        self.text = text
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, size):
        yield self.text.encode()


class LanPolicyTests(unittest.TestCase):
    def test_private_address_policy_rejects_public_loopback_linklocal_and_vpn(self):
        self.assertTrue(is_allowed_private_address("192.168.1.20"))
        self.assertTrue(is_allowed_private_address("fd00::20"))
        for value in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "fe80::1", "::"):
            self.assertFalse(is_allowed_private_address(value), value)

        payload = json.dumps([
            {"ifname": "eth0", "ifindex": 2, "addr_info": [{"local": "192.168.1.10", "prefixlen": 24}]},
            {"ifname": "tailscale0", "ifindex": 3, "addr_info": [{"local": "100.64.0.1", "prefixlen": 32}]},
        ])
        found = enumerate_interfaces(lambda *a, **k: FakeCommandResult(payload), system="Linux")
        self.assertEqual(found, [InterfaceAddress("eth0", found[0].index, "192.168.1.10", 24)])

    def test_interface_bound_socket_sets_platform_isolation_before_bind(self):
        candidate = InterfaceAddress("en0", 7, "192.168.1.10", 24)
        fake = FakeSocket()
        result = create_interface_bound_socket(candidate, 5151, system="Darwin", socket_factory=lambda *a: fake)
        self.assertIs(result, fake)
        self.assertIn((socket.IPPROTO_IP, 25, 7), fake.options)
        self.assertEqual(fake.bound, ("192.168.1.10", 5151))

    def test_camera_ssrf_policy_resolves_and_pins_one_private_address(self):
        resolver = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.25", 554))]
        pinned = validate_camera_target("camera.home", 554, [InterfaceAddress("en0", 1, "192.168.1.10", 24).network], resolver)
        self.assertEqual(pinned, "192.168.1.25")
        with self.assertRaises(NetworkPolicyError):
            validate_camera_target("metadata", 80, resolver=resolver)
        public = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 554))]
        with self.assertRaises(NetworkPolicyError):
            validate_camera_target("metadata", 554, resolver=public)


class LanGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = NomWatchPaths(Path(self.temp.name) / "home")
        self.state = LocalState(self.paths)
        self.auth = AuthService(self.state)
        self.code = self.auth.ensure_bootstrap()
        self.candidate = InterfaceAddress("en0", 4, "192.168.1.10", 24)
        self.manager = LanAccessManager(
            self.state, 5151, interface_provider=lambda: [self.candidate],
            socket_binder=lambda candidate, port: FakeSocket(),
        )
        self.server_patch = patch("nomwatch.network.create_server", return_value=FakeServer())
        self.server_patch.start()
        self.upstream_calls = []
        self.upstream = FakeUpstream("#EXTM3U\nsegment.mp4\n")
        def fake_get(*args, **kwargs):
            self.upstream_calls.append((args, kwargs))
            return self.upstream
        self.app = create_app(state=self.state, auth=self.auth, network_manager=self.manager,
                              gateway_http_get=fake_get)
        self.manager.attach_app(self.app)
        self.app.testing = True
        self.client = self.app.test_client()
        response = self.client.post("/api/v1/auth/claim", data={
            "code": self.code, "username": "owner", "display_name": "Owner",
            "password": "correct horse battery",
        })
        self.csrf = next(h.split(";", 1)[0].split("=", 1)[1] for h in response.headers.getlist("Set-Cookie") if h.startswith("nomwatch_csrf="))

    def tearDown(self):
        self.server_patch.stop()
        self.temp.cleanup()

    def reauth(self, client, csrf, base_url="http://localhost"):
        response = client.post("/api/v1/auth/reauth", json={"password": "correct horse battery"},
                               headers={"X-CSRF-Token": csrf}, base_url=base_url)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_lan_enable_is_explicit_staged_reauthenticated_and_confirmed_on_new_host(self):
        with self.state.connect() as conn:
            conn.execute("UPDATE sessions SET reauthenticated_at=0")
        denied = self.client.post("/api/v1/access/lan/stage", json={"interface": "en0", "address": "192.168.1.10"},
                                  headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(denied.status_code, 403)
        self.reauth(self.client, self.csrf)
        staged = self.client.post("/api/v1/access/lan/stage", json={"interface": "en0", "address": "192.168.1.10"},
                                  headers={"X-CSRF-Token": self.csrf}).get_json()
        self.assertFalse(self.manager.status()["enabled"])

        lan = self.app.test_client()
        login = lan.post("/api/v1/auth/login", json={"username": "owner", "password": "correct horse battery"},
                         base_url="http://192.168.1.10:5151")
        lan_csrf = next(h.split(";", 1)[0].split("=", 1)[1] for h in login.headers.getlist("Set-Cookie") if h.startswith("nomwatch_csrf="))
        self.reauth(lan, lan_csrf, "http://192.168.1.10:5151")
        confirmed = lan.post("/api/v1/access/lan/confirm", json={"confirmation_token": staged["confirmation_token"]},
                             headers={"X-CSRF-Token": lan_csrf}, base_url="http://192.168.1.10:5151")
        self.assertEqual(confirmed.status_code, 200, confirmed.get_data(as_text=True))
        self.assertTrue(self.manager.status()["enabled"])
        self.assertIn("not encrypted", self.manager.status()["trusted_lan_warning"])

        restored = LanAccessManager(
            self.state, 5151, interface_provider=lambda: [self.candidate],
            socket_binder=lambda candidate, port: FakeSocket(),
        )
        restored.attach_app(self.app)
        self.assertTrue(restored.restore())
        self.assertTrue(restored.reconcile())

    def test_hls_manifest_rewriting_has_no_loopback_or_credentials(self):
        manifest = "#EXTM3U\n#EXT-X-MAP:URI=\"http://127.0.0.1:8888/cam/init.mp4\"\nsegment0001.mp4\n"
        rewritten = rewrite_hls_manifest(manifest, "/api/v1/live/camera")
        self.assertIn('/api/v1/live/camera/init.mp4', rewritten)
        self.assertIn('/api/v1/live/camera/segment0001.mp4', rewritten)
        self.assertNotIn("127.0.0.1", rewritten)

        cfg = NomWatchConfig(camera=CameraConfig(ip="192.168.1.2", username="cam", password="pw"))
        with patch("nomwatch.config.load_config", return_value=cfg), \
             patch("nomwatch.bridge._internal_media_credentials", return_value=("reader", "secret")):
            response = self.client.get("/api/v1/live/camera/index.m3u8", headers={"Range": "bytes=0-99"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"127.0.0.1", response.data)
        self.assertEqual(self.upstream_calls[0][1]["auth"], ("reader", "secret"))
        self.assertEqual(self.upstream_calls[0][1]["headers"]["Range"], "bytes=0-99")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_offline_assets_have_no_cdn_or_external_qr_service(self):
        source = (Path(__file__).parents[1] / "nomwatch" / "webui.py").read_text()
        self.assertNotIn("cdn.jsdelivr.net", source)
        self.assertNotIn("api.qrserver.com", source)
        asset = Path(__file__).parents[1] / "nomwatch" / "static" / "hls.min.js"
        self.assertGreater(asset.stat().st_size, 500_000)


if __name__ == "__main__":
    unittest.main()
