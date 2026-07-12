"""
Local configuration handling for NomWatch.

Config lives at ~/.config/nomwatch/config.yml, permissioned 600.
Never committed to git, never logged, never sent anywhere.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG_DIR = Path(os.path.expanduser("~/.config/nomwatch"))
CONFIG_PATH = CONFIG_DIR / "config.yml"


@dataclass
class CameraConfig:
    ip: str
    rtsp_port: int = 554
    username: str = ""
    password: str = ""
    stream_path: str = "stream1"


@dataclass
class BridgeConfig:
    mediamtx_hls_port: int = 8888
    mediamtx_rtsp_port: int = 8554
    tailscale_hostname: Optional[str] = None  # auto-detected if None


@dataclass
class NotifyConfig:
    provider: str = "ntfy"  # "ntfy" | "pushover" | "none"
    ntfy_topic: Optional[str] = None
    pushover_user_key: Optional[str] = None
    pushover_app_token: Optional[str] = None


@dataclass
class StorageConfig:
    provider: str = "google_drive"  # "google_drive" | "none"
    drive_folder_id: Optional[str] = None


@dataclass
class NomWatchConfig:
    camera: CameraConfig
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)


def save_config(cfg: NomWatchConfig) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(asdict(cfg), f, default_flow_style=False)
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 600, owner-only
    return CONFIG_PATH


def load_config() -> Optional[NomWatchConfig]:
    if not CONFIG_PATH.exists():
        return None
    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    return NomWatchConfig(
        camera=CameraConfig(**raw["camera"]),
        bridge=BridgeConfig(**raw.get("bridge", {})),
        notify=NotifyConfig(**raw.get("notify", {})),
        storage=StorageConfig(**raw.get("storage", {})),
    )
