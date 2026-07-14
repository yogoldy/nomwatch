from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nomwatch.auth import AuthService
from nomwatch.paths import NomWatchPaths
from nomwatch.state import LocalState
from nomwatch.webui import create_app


def cookie_value(response, name):
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith(name + "="):
            return header.split(";", 1)[0].split("=", 1)[1]
    return None


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = LocalState(NomWatchPaths(Path(self.temp.name) / "home"))
        self.auth = AuthService(self.state)
        self.code = self.auth.ensure_bootstrap()
        self.app = create_app(state=self.state, auth=self.auth)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def claim(self, client=None):
        client = client or self.client
        response = client.post("/api/v1/auth/claim", data={
            "code": self.code, "username": "owner", "display_name": "Owner",
            "password": "correct horse battery",
        })
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return cookie_value(response, "nomwatch_csrf")

    def test_loopback_host_auth_csrf_origin_and_local_only_controls(self):
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/bootstrap", headers={"Host": "evil.example"}).status_code, 400)
        csrf = self.claim()
        self.assertEqual(self.client.get("/api/v1/me").get_json()["role"], "owner")
        self.assertEqual(self.client.post("/api/stop-monitoring").status_code, 403)
        self.assertEqual(self.client.post("/api/stop-monitoring", headers={
            "X-CSRF-Token": csrf, "Origin": "http://evil.example"
        }).status_code, 403)
        response = self.client.post("/api/install-service", headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 403)
        self.assertIn("local OS", response.get_json()["error"])

    def test_loopback_alias_origin_is_accepted_but_non_loopback_is_rejected(self):
        response = self.client.post("/api/v1/auth/claim", data={
            "code": self.code, "username": "owner", "display_name": "Owner",
            "password": "correct horse battery",
        }, base_url="http://127.0.0.1:5151", headers={"Origin": "http://localhost:5151"})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

        other = self.app.test_client()
        self.assertEqual(other.post("/api/v1/auth/login", data={"username": "owner", "password": "correct horse battery"},
                                    base_url="http://127.0.0.1:5151", headers={"Origin": "http://evil.example:5151"}).status_code, 403)
        self.assertEqual(other.post("/api/v1/auth/login", data={"username": "owner", "password": "correct horse battery"},
                                    base_url="http://127.0.0.1:5151", headers={"Origin": "http://127.0.0.1:not-a-port"}).status_code, 403)

    def test_first_owner_page_is_a_host_setup_flow(self):
        page = self.client.get("/claim").get_data(as_text=True)
        self.assertIn("This computer is the host", page)
        self.assertIn("Create owner account", page)

    def test_role_matrix_and_invitation(self):
        csrf = self.claim()
        invitation = self.client.post("/api/v1/invitations", json={"role": "viewer"},
                                      headers={"X-CSRF-Token": csrf}).get_json()["activation_code"]
        viewer = self.app.test_client()
        response = viewer.post("/api/v1/auth/accept-invitation", json={
            "code": invitation, "username": "viewer", "display_name": "Viewer",
            "password": "another correct horse",
        })
        viewer_csrf = cookie_value(response, "nomwatch_csrf")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(viewer.get("/api/recent-events").status_code, 200)
        self.assertEqual(viewer.post("/api/start-monitoring", headers={"X-CSRF-Token": viewer_csrf}).status_code, 403)
        self.assertEqual(viewer.get("/setup").status_code, 403)

    def test_security_headers_and_loopback_contract(self):
        self.claim()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("nomwatch_csrf", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/", base_url="http://192.168.1.2").status_code, 400)


if __name__ == "__main__":
    unittest.main()
