"""Foreground NomWatch host: authenticated gateway plus owned children."""
from __future__ import annotations

import signal
from pathlib import Path

from waitress import create_server

from .auth import AuthService
from .config import CONFIG_PATH
from .control import ControlServer
from .migration import MigrationCoordinator
from .network import LanAccessManager, MdnsAdvertiser
from .paths import NomWatchPaths
from .state import LocalState
from .supervisor import HostSupervisor
from .webui import create_app


def run_host(port: int = 5151) -> None:
    paths = NomWatchPaths.from_environment()
    state = LocalState(paths)
    auth = AuthService(state)
    bootstrap = auth.ensure_bootstrap()
    if bootstrap:
        print(f"NomWatch first-owner code (expires in 15 minutes): {bootstrap}", flush=True)
        print(f"Open http://127.0.0.1:{port}/claim", flush=True)

    coordinator = MigrationCoordinator(state)
    coordinator.snapshot_and_import(CONFIG_PATH, paths.home / "events.jsonl")

    supervisor = HostSupervisor(state, paths)
    supervisor.start()
    control = ControlServer(paths.runtime / "control.sock", supervisor)
    control.start()
    network = LanAccessManager(state, port, advertiser=MdnsAdvertiser())
    app = create_app(state=state, auth=auth, supervisor=supervisor, network_manager=network)
    network.attach_app(app)
    network.restore()
    server = create_server(app, host="127.0.0.1", port=port, threads=8)

    def stop(_signum=None, _frame=None):
        server.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.run()
    finally:
        network.shutdown()
        control.close()
        supervisor.stop()
