"""Foreground NomWatch host: authenticated gateway plus owned children."""
from __future__ import annotations

import signal
import errno
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
from .tailscale import TailscaleAdapter
from .tailscale_helper import helper_request
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
    legacy_plist = Path.home() / "Library" / "LaunchAgents" / "com.nomwatch.run.plist"
    cutover = coordinator.atomic_cutover(CONFIG_PATH, paths.home / "events.jsonl", legacy_plist)

    supervisor = HostSupervisor(state, paths)
    control = None
    network = None
    tailscale = None
    server = None
    startup_verified = False
    try:
        supervisor.start()
        control = ControlServer(paths.runtime / "control.sock", supervisor)
        control.start()
        network = LanAccessManager(state, port, advertiser=MdnsAdvertiser())
        helper_socket = Path("/run/nomwatch/tailscale-helper.sock")
        helper_client = (lambda operation: helper_request(helper_socket, operation)) if helper_socket.exists() else None
        tailscale = TailscaleAdapter(state, auth, helper_client=helper_client)
        allowed_hosts = lambda: network.allowed_hosts() | tailscale.allowed_hosts()
        app = create_app(
            state=state, auth=auth, supervisor=supervisor, network_manager=network,
            tailscale_adapter=tailscale, allowed_hosts_provider=allowed_hosts,
            listener_policy=tailscale.listener_allows,
        )
        network.attach_app(app)
        tailscale.attach_app(app)
        network.restore()
        tailscale.reconcile()
        tailscale.start_reconciler()
        server = create_server(app, host="127.0.0.1", port=port, threads=8)
        if cutover:
            coordinator.finalize_cutover(cutover)
        startup_verified = True

        def stop(_signum=None, _frame=None):
            server.close()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.run()
        except OSError as exc:
            # Waitress wakes its selector by closing the server socket.  On
            # macOS that can surface as EBADF after Ctrl-C/SIGTERM even though
            # shutdown succeeded.
            if exc.errno != errno.EBADF:
                raise
    except BaseException:
        if cutover and not startup_verified:
            coordinator.rollback_failed_cutover(cutover)
        raise
    finally:
        if tailscale:
            tailscale.shutdown()
        if network:
            network.shutdown()
        if control:
            control.close()
        supervisor.stop()
