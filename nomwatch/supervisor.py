"""Bounded foreground supervision for NomWatch-owned child processes."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .bridge import write_mediamtx_config
from .config import load_config
from .paths import NomWatchPaths
from .state import LocalState


def _rotate(path: Path, limit: int = 2_000_000, backups: int = 3) -> None:
    if not path.exists() or path.stat().st_size < limit:
        return
    for number in range(backups, 0, -1):
        source = path.with_suffix(path.suffix + f".{number}") if number > 1 else path
        target = path.with_suffix(path.suffix + f".{number + 1}")
        if source.exists():
            if number == backups:
                source.unlink(missing_ok=True)
            else:
                source.replace(target)


@dataclass
class ChildSpec:
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    desired: bool = True
    readiness: Optional[Callable[[], bool]] = None


@dataclass
class ManagedChild:
    spec: ChildSpec
    process: Optional[subprocess.Popen] = None
    status: str = "stopped"
    restarts: list[float] = field(default_factory=list)
    next_start: float = 0.0
    log_handle: object = None


class HostSupervisor:
    def __init__(self, state: LocalState, paths: NomWatchPaths, *, launcher=subprocess.Popen,
                 clock=time.monotonic, sleeper=time.sleep):
        self.state = state
        self.paths = paths
        self.launcher = launcher
        self.clock = clock
        self.sleep = sleeper
        self.children: dict[str, ManagedChild] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._build_specs()

    def _build_specs(self) -> None:
        cfg = load_config()
        common_env = {
            "NOMWATCH_HOME": str(self.paths.home),
            "NOMWATCH_SUPERVISED": "1",
            "NOMWATCH_MEDIAMTX_READ_USER": "nomwatch",
            "NOMWATCH_MEDIAMTX_READ_PASSWORD": self.state.secret_get_or_create("mediamtx_read_password"),
        }
        python = sys.executable
        self.children["jobs"] = ManagedChild(ChildSpec(
            "jobs", [python, "-m", "nomwatch.cli", "jobs"], dict(common_env), True,
        ))
        if cfg is not None and cfg.camera.ip:
            mediamtx = shutil.which("mediamtx")
            if mediamtx:
                config_path = self.paths.home / "mediamtx.yml"
                write_mediamtx_config(
                    cfg, config_path, read_user=common_env["NOMWATCH_MEDIAMTX_READ_USER"],
                    read_password=common_env["NOMWATCH_MEDIAMTX_READ_PASSWORD"],
                )
                self.children["mediamtx"] = ManagedChild(ChildSpec(
                    "mediamtx", [mediamtx, str(config_path)], dict(common_env), True,
                    readiness=lambda: self._port_ready(cfg.bridge.mediamtx_rtsp_port),
                ))
                self.children["monitor"] = ManagedChild(ChildSpec(
                    "monitor", [python, "-m", "nomwatch.cli", "worker"], dict(common_env), False,
                ))

    @staticmethod
    def _port_ready(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            return False

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="nomwatch-supervisor", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                for child in self.children.values():
                    self._reconcile(child)
            self._stop.wait(0.25)

    def _reconcile(self, child: ManagedChild) -> None:
        now = self.clock()
        process = child.process
        if process is not None and process.poll() is not None:
            child.process = None
            if child.log_handle:
                child.log_handle.close()
                child.log_handle = None
            child.restarts = [stamp for stamp in child.restarts if now - stamp < 60]
            child.restarts.append(now)
            if len(child.restarts) >= 5:
                child.status = "degraded"
                child.next_start = now + 30
            else:
                child.status = "backoff"
                child.next_start = now + min(30, 2 ** (len(child.restarts) - 1))
        if not child.spec.desired:
            if child.process is not None:
                self._terminate(child)
            child.status = "stopped"
            return
        if child.process is None and now >= child.next_start:
            self._launch(child)
        if child.process is not None:
            child.status = "ready" if child.spec.readiness is None or child.spec.readiness() else "starting"

    def _launch(self, child: ManagedChild) -> None:
        logs = self.paths.home / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = logs / f"{child.spec.name}.log"
        _rotate(log_path)
        child.log_handle = open(log_path, "a", buffering=1)
        env = os.environ.copy()
        env.update(child.spec.env)
        try:
            child.process = self.launcher(
                child.spec.argv, stdout=child.log_handle, stderr=subprocess.STDOUT,
                cwd=str(self.paths.home), env=env, start_new_session=False,
            )
            child.status = "starting"
        except (OSError, ValueError) as exc:
            child.log_handle.write(f"launch failed: {exc}\n")
            child.log_handle.close()
            child.log_handle = None
            child.process = None
            child.status = "degraded"
            child.next_start = self.clock() + 30

    def _terminate(self, child: ManagedChild) -> None:
        process = child.process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        child.process = None
        if child.log_handle:
            child.log_handle.close()
            child.log_handle = None

    def set_monitoring(self, desired: bool) -> dict:
        with self._lock:
            child = self.children.get("monitor")
            if child is None:
                return {"ok": False, "error": "MediaMTX is unavailable; monitoring cannot start"}
            child.spec.desired = desired
            self._reconcile(child)
            return {"ok": True, "desired": desired, "status": child.status}

    def health(self) -> dict:
        with self._lock:
            children = {
                name: {"desired": child.spec.desired, "status": child.status,
                       "pid": child.process.pid if child.process else None,
                       "restarts_last_minute": len(child.restarts)}
                for name, child in self.children.items()
            }
        degraded = any(item["desired"] and item["status"] == "degraded" for item in children.values())
        return {"status": "degraded" if degraded else "ready", "children": children}

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            for child in reversed(list(self.children.values())):
                self._terminate(child)

