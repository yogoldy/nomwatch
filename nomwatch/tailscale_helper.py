"""Linux root helper with a fixed typed Tailscale operation protocol."""
from __future__ import annotations

import json
import os
import pwd
import grp
import socket
import socketserver
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .tailscale import HELPER_OPERATIONS, command_for

PROTOCOL_VERSION = 1
FIXED_CLI = "/usr/bin/tailscale"
FIXED_INGRESS_PORT = 5152


@dataclass
class HelperResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        if not self.server.peer_allowed(self.request):
            self.wfile.write(b'{"returncode":126,"stdout":"","stderr":"peer not allowed"}\n')
            return
        try:
            payload = json.loads(self.rfile.readline(4097))
            if payload.get("version") != PROTOCOL_VERSION or payload.get("operation") not in HELPER_OPERATIONS:
                raise ValueError("operation is not allowed")
            operation = payload["operation"]
            argv = command_for(operation, FIXED_CLI, FIXED_INGRESS_PORT)
            timeout = 130 if operation == "login" else 30
            result = self.server.runner(argv, capture_output=True, text=True, timeout=timeout)
            response = {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        except (ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            response = {"returncode": 124, "stdout": "", "stderr": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode())


class TailscaleHelperServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, *, allowed_user: str = "nomwatch", runner=subprocess.run):
        self.path = path
        self.allowed_uid = pwd.getpwnam(allowed_user).pw_uid
        self.runner = runner
        path.unlink(missing_ok=True)
        super().__init__(str(path), _Handler)

    def peer_allowed(self, sock) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return False
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid == self.allowed_uid


def helper_request(path: Path, operation: str, timeout: float = 135.0) -> HelperResult:
    if operation not in HELPER_OPERATIONS:
        raise ValueError("operation is not allowed")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall((json.dumps({"version": PROTOCOL_VERSION, "operation": operation}) + "\n").encode())
        response = json.loads(client.makefile("rb").readline(65537))
    return HelperResult(int(response["returncode"]), str(response.get("stdout", "")), str(response.get("stderr", "")))


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("nomwatch-tailscale-helper must run as root under its systemd unit")
    path = Path("/run/nomwatch/tailscale-helper.sock")
    server = TailscaleHelperServer(path)
    group = grp.getgrnam("nomwatch").gr_gid
    os.chown(path, 0, group)
    os.chmod(path, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        path.unlink(missing_ok=True)
