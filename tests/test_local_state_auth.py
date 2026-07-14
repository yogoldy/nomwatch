from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from nomwatch.auth import AuthService
from nomwatch.config import CameraConfig, NomWatchConfig, NotifyConfig, StorageConfig
from nomwatch.paths import NomWatchPaths
from nomwatch.state import LocalState, StateError
from nomwatch.websecurity import redacted_config_payload


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = NomWatchPaths(Path(self.temp.name) / "home")
        self.clock = Clock()
        self.state = LocalState(self.paths, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_paths_and_complete_schema(self):
        self.assertEqual(stat.S_IMODE(self.paths.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.paths.database.stat().st_mode), 0o600)
        with self.state.connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"users", "sessions", "settings", "events", "media_files", "jobs", "audit_log", "remote_access"} <= tables)

    def test_legacy_shadow_import_is_idempotent_and_redacts_secrets(self):
        config = Path(self.temp.name) / "config.yml"
        config.write_text("""camera:\n  ip: 192.168.1.20\n  username: cam\n  password: swordfish\nbridge:\n  mediamtx_hls_port: 8888\nnotify:\n  provider: ntfy\n  pushover_app_token: hidden\nunknown: keep-tolerant\n""")
        events = Path(self.temp.name) / "events.jsonl"
        events.write_text(json.dumps({"ts": 1234, "confidence": 0.9, "reason": "cat"}) + "\nnot-json\n")
        first = self.state.import_legacy_shadow(config, events)
        second = self.state.import_legacy_shadow(config, events)
        self.assertEqual(first["events"], 1)
        self.assertEqual(second, {"settings": 0, "events": 0})
        with self.state.connect() as conn:
            notify = conn.execute("SELECT value_json FROM settings WHERE namespace='notify'").fetchone()[0]
            camera = conn.execute("SELECT password_secret_ref FROM cameras").fetchone()[0]
        self.assertNotIn("hidden", notify)
        self.assertEqual(self.state.get_secret(camera), "swordfish")

    def test_redacted_export_has_no_secrets_or_local_paths(self):
        cfg = NomWatchConfig(
            camera=CameraConfig(ip="192.168.1.20", username="cam", password="swordfish"),
            notify=NotifyConfig(pushover_app_token="topsecret"),
            storage=StorageConfig(local_save_dir="/Users/private/clips", drive_token_path="/tmp/token"),
        )
        payload = redacted_config_payload(cfg)
        encoded = json.dumps(payload)
        self.assertNotIn("swordfish", encoded)
        self.assertNotIn("topsecret", encoded)
        self.assertNotIn("/Users/private", encoded)
        self.assertTrue(payload["camera"]["password_set"])


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.state = LocalState(NomWatchPaths(Path(self.temp.name) / "home"), clock=self.clock)
        self.auth = AuthService(self.state, clock=self.clock)
        self.code = self.auth.ensure_bootstrap()

    def tearDown(self):
        self.temp.cleanup()

    def test_bootstrap_is_single_use_and_session_is_opaque(self):
        issued = self.auth.claim_owner(self.code, "Owner", "Owner", "correct horse battery")
        self.assertIsNone(self.auth.ensure_bootstrap())
        session = self.auth.authenticate(issued.token, origin_class="loopback")
        self.assertEqual(session["role"], "owner")
        with self.state.connect() as conn:
            stored = conn.execute("SELECT token_digest,csrf_digest FROM sessions").fetchone()
        self.assertNotEqual(stored[0], issued.token)
        self.assertNotEqual(stored[1], issued.csrf)
        with self.assertRaises(StateError):
            self.auth.claim_owner(self.code, "another", "Another", "correct horse battery")

    def test_idle_expiry_reauth_and_session_version_revocation(self):
        issued = self.auth.claim_owner(self.code, "owner", "Owner", "correct horse battery")
        session = self.auth.authenticate(issued.token)
        self.auth.reauthenticate(session["id"], "correct horse battery")
        refreshed = self.auth.authenticate(issued.token)
        self.assertTrue(self.auth.recent_reauth(refreshed))
        with self.state.connect() as conn:
            conn.execute("UPDATE users SET session_version=session_version+1 WHERE id=?", (issued.user_id,))
        self.assertIsNone(self.auth.authenticate(issued.token))

    def test_last_enabled_owner_cannot_be_disabled_or_demoted(self):
        issued = self.auth.claim_owner(self.code, "owner", "Owner", "correct horse battery")
        with self.assertRaises(StateError):
            self.auth.update_user(issued.user_id, role="viewer")
        with self.assertRaises(StateError):
            self.auth.update_user(issued.user_id, disabled=True)


if __name__ == "__main__":
    unittest.main()
