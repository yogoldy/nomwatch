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

from nomwatch.bridge import render_mediamtx_config
from nomwatch.cli import main
from nomwatch.config import CameraConfig, NomWatchConfig
from nomwatch.control import ControlServer, request
from nomwatch.jobs import JobWorker
from nomwatch.migration import MigrationCoordinator
from nomwatch.paths import NomWatchPaths
from nomwatch.service import render_launchd_plist, render_systemd_unit
from nomwatch.state import LocalState
from nomwatch.supervisor import HostSupervisor


class FakeProcess:
    next_pid = 9000

    def __init__(self):
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeSupervisor:
    def __init__(self):
        self.desired = False

    def health(self):
        return {"status": "ready", "children": {}}

    def set_monitoring(self, desired):
        self.desired = desired
        return {"ok": True, "desired": desired}


class HostRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = NomWatchPaths(Path(self.temp.name) / "home")
        self.state = LocalState(self.paths)

    def tearDown(self):
        self.temp.cleanup()

    def test_mediamtx_is_loopback_authenticated_and_not_public(self):
        cfg = NomWatchConfig(camera=CameraConfig(ip="192.168.1.2", username="cam", password="pw"))
        rendered = render_mediamtx_config(cfg, read_user="reader", read_password="random-secret")
        self.assertIn("rtspAddress: 127.0.0.1:", rendered)
        self.assertIn("hlsAddress: 127.0.0.1:", rendered)
        self.assertIn("pass: random-secret", rendered)
        self.assertIn("action: read", rendered)
        self.assertNotIn("0.0.0.0", rendered)
        self.assertNotIn("funnel", rendered.lower())

    def test_supervisor_retains_handles_and_stops_children(self):
        cfg = NomWatchConfig(camera=CameraConfig(ip="192.168.1.2", username="cam", password="pw"))
        launched = []

        def launcher(*args, **kwargs):
            process = FakeProcess()
            launched.append((args[0], process, kwargs))
            return process

        with patch("nomwatch.supervisor.load_config", return_value=cfg), \
             patch("nomwatch.supervisor.shutil.which", return_value="/usr/local/bin/mediamtx"), \
             patch.object(HostSupervisor, "_port_ready", return_value=True):
            supervisor = HostSupervisor(self.state, self.paths, launcher=launcher)
            supervisor.start()
            deadline = time.time() + 2
            while len(launched) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual({"mediamtx", "jobs"}, {Path(argv[0]).name if "mediamtx" in argv[0] else argv[-1] for argv, _, _ in launched})
            result = supervisor.set_monitoring(True)
            self.assertTrue(result["ok"])
            deadline = time.time() + 2
            while len(launched) < 3 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(supervisor.health()["children"]["monitor"]["status"], "ready")
            processes = [item[1] for item in launched]
            supervisor.stop()
            self.assertTrue(all(process.terminated for process in processes))

    def test_control_socket_accepts_only_fixed_operations(self):
        supervisor = FakeSupervisor()
        server = ControlServer(self.paths.runtime / "control.sock", supervisor)
        server.start()
        try:
            self.assertTrue(request(server.path, "monitoring.start")["ok"])
            self.assertTrue(supervisor.desired)
            with self.assertRaises(ValueError):
                request(server.path, "shell.exec")
            mode = stat.S_IMODE(server.path.stat().st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            server.close()

    def test_durable_outbox_completes_and_is_idempotent(self):
        event_id = self.state.persist_event_with_job(
            timestamp=1234, confidence=0.9, reason="cat",
            payload={"timestamp": 1234, "confidence": 0.9, "reason": "cat"}, run_at=0,
        )
        worker = JobWorker(self.state)
        with patch.object(worker, "_finalize_event") as finalize:
            self.assertTrue(worker.run_once())
            self.assertFalse(worker.run_once())
        finalize.assert_called_once()
        with self.state.connect() as conn:
            event = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()[0]
            job = conn.execute("SELECT status,attempts FROM jobs").fetchone()
        self.assertEqual(event, "complete")
        self.assertEqual(tuple(job), ("complete", 1))

    def test_migration_snapshot_is_private_checksummed_and_idempotent(self):
        config = Path(self.temp.name) / "config.yml"
        config.write_text("camera:\n  ip: 192.168.1.2\n  username: cam\n  password: pw\n")
        events = Path(self.temp.name) / "events.jsonl"
        events.write_text('{"ts":1234,"reason":"cat"}\n')
        coordinator = MigrationCoordinator(self.state)
        snapshot = coordinator.snapshot_and_import(config, events)
        self.assertIsNotNone(snapshot)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((snapshot / "config.yml").stat().st_mode), 0o600)
        manifest = json.loads((snapshot / "manifest.json").read_text())
        self.assertIn("config.yml", manifest["files"])
        self.assertIsNone(coordinator.snapshot_and_import(config, events))

    def test_platform_services_launch_only_host(self):
        with patch("nomwatch.service._nomwatch_command", return_value=["/opt/nomwatch", "host"]):
            plist = render_launchd_plist(self.paths.home / "logs")
        parsed = plistlib.loads(plist.encode())
        self.assertEqual(parsed["Label"], "com.nomwatch.host")
        self.assertEqual(parsed["ProgramArguments"], ["/opt/nomwatch", "host"])
        unit = render_systemd_unit()
        self.assertIn("ExecStart=/usr/bin/nomwatch host", unit)
        self.assertIn("User=nomwatch", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertNotIn("tailscale", unit.lower())

    def test_cli_exposes_host_and_run_is_control_shim(self):
        runner = CliRunner()
        help_result = runner.invoke(main, ["--help"])
        self.assertEqual(help_result.exit_code, 0)
        self.assertIn("host", help_result.output)
        result = runner.invoke(main, ["run"], env={"NOMWATCH_HOME": str(self.paths.home)})
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("host is not running", result.output)


if __name__ == "__main__":
    unittest.main()
