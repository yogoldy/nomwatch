from __future__ import annotations

import json
import os
import plistlib
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from nomwatch import config, monitorlock
from nomwatch.cli import main
from nomwatch.config import (
    CameraConfig,
    DetectionConfig,
    NomWatchConfig,
    NotifyConfig,
    StorageConfig,
)
from nomwatch.migration import MigrationCoordinator
from nomwatch.paths import NomWatchPaths
from nomwatch.recovery import (
    create_operational_backup,
    diagnostics,
    prune_migration_backups,
    restore_operational_backup,
)
from nomwatch.state import LocalState, StateError


class Result:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class RecoveryPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = NomWatchPaths(Path(self.temp.name) / "home")
        self.state = LocalState(self.paths)

    def tearDown(self):
        self.temp.cleanup()

    def test_sqlite_becomes_post_cutover_config_source_without_yaml_dual_write(self):
        cfg = NomWatchConfig(
            camera=CameraConfig(ip="192.168.1.20", username="cam", password="swordfish"),
            detection=DetectionConfig(engine="motion", poll_interval_seconds=17),
            notify=NotifyConfig(provider="pushover", pushover_app_token="push-secret"),
            storage=StorageConfig(
                provider="google_drive_api",
                drive_credentials_path="/private/oauth-client.json",
                drive_token_path="/private/oauth-token.json",
            ),
        )
        with patch.object(config, "CONFIG_DIR", self.paths.home), \
             patch.object(config, "CONFIG_PATH", self.paths.home / "config.yml"):
            saved = config.save_config(cfg)
            self.assertEqual(saved, self.paths.database)
            self.assertFalse((self.paths.home / "config.yml").exists())
            loaded = config.load_config()
        self.assertEqual(loaded.camera.password, "swordfish")
        self.assertEqual(loaded.detection.poll_interval_seconds, 17)
        self.assertEqual(loaded.notify.pushover_app_token, "push-secret")
        self.assertEqual(loaded.storage.drive_credentials_path, "/private/oauth-client.json")
        self.assertEqual(loaded.storage.drive_token_path, "/private/oauth-token.json")
        with self.state.connect() as conn:
            settings_json = " ".join(row[0] for row in conn.execute("SELECT value_json FROM settings"))
        self.assertNotIn("push-secret", settings_json)
        self.assertNotIn("/private/oauth-client.json", settings_json)
        self.assertNotIn("/private/oauth-token.json", settings_json)

    def test_verified_backup_restore_preserves_rollback_and_rejects_tamper(self):
        self.state.put_setting("test.before", {"value": 1})
        backup = create_operational_backup(self.state)
        self.state.put_setting("test.after", {"value": 2})
        rollback = restore_operational_backup(self.paths, backup)
        self.assertTrue((rollback / "nomwatch.sqlite3").exists())
        with self.state.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM settings WHERE namespace='test.after'").fetchone())
        manifest = json.loads((backup / "manifest.json").read_text())
        manifest["sha256"] = "0" * 64
        (backup / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(StateError):
            restore_operational_backup(self.paths, backup)

    def test_atomic_cutover_targets_only_exact_legacy_service(self):
        cfg = Path(self.temp.name) / "config.yml"
        cfg.write_text("camera:\n  ip: 192.168.1.20\n  username: cam\n  password: pw\n")
        events = Path(self.temp.name) / "events.jsonl"
        events.write_text('{"ts":1,"reason":"cat"}\n')
        plist = Path(self.temp.name) / "com.nomwatch.run.plist"
        plist.write_bytes(plistlib.dumps({"Label": "com.nomwatch.run", "ProgramArguments": ["nomwatch", "run"]}))
        commands = []
        def runner(argv, **kwargs):
            commands.append(argv)
            return Result()
        lock_path = Path(self.temp.name) / "run.lock"
        with patch.object(monitorlock, "RUN_LOCK_PATH", lock_path):
            coordinator = MigrationCoordinator(self.state)
            cutover = coordinator.atomic_cutover(
                cfg, events, plist, runner=runner, system="Darwin",
            )
        self.assertTrue(cutover.snapshot.exists())
        self.assertEqual(commands[0][:3], ["launchctl", "bootout", f"gui/{os.getuid()}"])
        self.assertEqual(commands[0][-1], str(plist))
        coordinator.rollback_failed_cutover(cutover, runner=runner, system="Darwin")
        self.assertFalse(coordinator.already_complete())
        self.assertEqual(commands[-1][:2], ["launchctl", "bootstrap"])

        cutover = coordinator.atomic_cutover(cfg, events, plist, runner=runner, system="Darwin")
        coordinator.finalize_cutover(cutover, runner=runner, system="Darwin")
        self.assertFalse(plist.exists())
        command_count = len(commands)

        other_state = LocalState(NomWatchPaths(Path(self.temp.name) / "other"))
        wrong = Path(self.temp.name) / "wrong.plist"
        wrong.write_bytes(plistlib.dumps({"Label": "com.someone.else"}))
        with self.assertRaises(RuntimeError):
            MigrationCoordinator(other_state).atomic_cutover(cfg, events, wrong, runner=runner, system="Darwin")
        self.assertEqual(len(commands), command_count)

    def test_diagnostics_and_snapshot_retention_are_bounded_and_redacted(self):
        (self.paths.home / "run.pid").write_text("123")
        old = self.paths.migrations / "old"
        old.mkdir(parents=True)
        os.utime(old, (1, 1))
        recent = self.paths.migrations / "recent"
        recent.mkdir()
        self.assertEqual(prune_migration_backups(self.paths, clock=lambda: 40 * 86400), 1)
        self.assertTrue(recent.exists())
        report = diagnostics(self.paths)
        encoded = json.dumps(report)
        self.assertIn("run.pid", report["legacy_artifacts"])
        self.assertNotIn("password", encoded.lower())
        self.assertNotIn("token", encoded.lower())

    def test_packaging_artifacts_are_exact_and_hardened(self):
        root = Path(__file__).parents[1]
        unit = (root / "packaging/systemd/nomwatch.service").read_text()
        helper = (root / "packaging/systemd/nomwatch-tailscale-helper.service").read_text()
        self.assertIn("ExecStart=/usr/bin/nomwatch host", unit)
        self.assertIn("User=nomwatch", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertNotIn("funnel", (unit + helper).lower())
        self.assertIn("CapabilityBoundingSet=", helper)
        template = (root / "packaging/macos/com.nomwatch.host.plist.in").read_bytes()
        parsed = plistlib.loads(template)
        self.assertEqual(parsed["Label"], "com.nomwatch.host")
        self.assertEqual(parsed["ProgramArguments"][-1], "host")

    def test_uninstall_is_blocked_by_remote_cleanup_state(self):
        with self.state.connect() as conn:
            conn.execute("UPDATE remote_access SET desired_enabled=1,status='cleanup_required' WHERE id=1")
        runner = CliRunner()
        result = runner.invoke(main, ["service-uninstall"], env={"NOMWATCH_HOME": str(self.paths.home)})
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cleanup", result.output.lower())


if __name__ == "__main__":
    unittest.main()
