"""
Bridge setup: MediaMTX config generation + Tailscale serve wiring.

This module generates config and shells out to `mediamtx`/`tailscale` binaries
that must already be installed (via brew, apt, or the official installers).
It deliberately does not manage installation of those binaries itself in v1 -
see cli.py's `doctor` command for install-detection and guidance.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import NomWatchConfig

MEDIAMTX_CONFIG_TEMPLATE = """\
rtspAddress: 127.0.0.1:{rtsp_port}
hlsAddress: 127.0.0.1:{hls_port}
webrtc: false
rtmp: false
srt: false
api: false
metrics: false
playback: false

paths:
  cam:
    source: rtsp://{username}:{password}@{ip}:{camera_rtsp_port}/{stream_path}
    rtspTransport: tcp
"""


def render_mediamtx_config(cfg: NomWatchConfig) -> str:
    return MEDIAMTX_CONFIG_TEMPLATE.format(
        rtsp_port=cfg.bridge.mediamtx_rtsp_port,
        hls_port=cfg.bridge.mediamtx_hls_port,
        username=cfg.camera.username,
        password=cfg.camera.password,
        ip=cfg.camera.ip,
        camera_rtsp_port=cfg.camera.rtsp_port,
        stream_path=cfg.camera.stream_path,
    )


def binary_available(name: str) -> bool:
    return shutil.which(name) is not None


def write_mediamtx_config(cfg: NomWatchConfig, path: Path) -> Path:
    path.write_text(render_mediamtx_config(cfg))
    return path


def enable_tailscale_serve(hls_port: int) -> subprocess.CompletedProcess:
    """
    Publishes the local-only HLS endpoint over the tailnet.
    Deliberately uses `serve`, never `funnel` - funnel makes it public,
    which defeats the entire point of this project.
    """
    return subprocess.run(
        ["tailscale", "serve", "--bg", f"http://127.0.0.1:{hls_port}"],
        capture_output=True,
        text=True,
    )


def tailscale_status() -> Optional[str]:
    if not binary_available("tailscale"):
        return None
    result = subprocess.run(["tailscale", "status"], capture_output=True, text=True)
    return result.stdout
