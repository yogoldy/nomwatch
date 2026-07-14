"""Versioned same-user Unix control protocol for the foreground host."""
from __future__ import annotations

import json
import os
import socket
import socketserver
import stat
import struct
import threading
from pathlib import Path

PROTOCOL_VERSION = 1
ALLOWED_OPERATIONS = {"status", "monitoring.start", "monitoring.stop"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        if not self.server.peer_allowed(self.request):
            self.wfile.write(b'{"ok":false,"error":"peer not allowed"}\n')
            return
        try:
            request = json.loads(self.rfile.readline(65537))
            if request.get("version") != PROTOCOL_VERSION:
                raise ValueError("unsupported protocol version")
            operation = request.get("operation")
            if operation not in ALLOWED_OPERATIONS:
                raise ValueError("operation is not allowed")
            response = self.server.dispatch(operation)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response, sort_keys=True) + "\n").encode())


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, supervisor):
        self.path = path
        self.supervisor = supervisor
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.unlink(missing_ok=True)
        super().__init__(str(path), _Handler)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        self._thread = None

    @staticmethod
    def peer_allowed(sock) -> bool:
        if hasattr(socket, "SO_PEERCRED"):
            _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return uid == os.getuid()
        return True  # macOS relies on the containing 0700 dir and 0600 socket

    def dispatch(self, operation: str) -> dict:
        if operation == "status":
            return {"ok": True, **self.supervisor.health()}
        return self.supervisor.set_monitoring(operation == "monitoring.start")

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="nomwatch-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self.path.unlink(missing_ok=True)


def request(path: Path, operation: str, timeout: float = 3.0) -> dict:
    if operation not in ALLOWED_OPERATIONS:
        raise ValueError("operation is not allowed")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall((json.dumps({"version": PROTOCOL_VERSION, "operation": operation}) + "\n").encode())
        response = client.makefile("rb").readline(65537)
    return json.loads(response)
