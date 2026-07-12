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
class DetectionConfig:
    engine: str = "ollama"  # "ollama" | "frigate" | "yolo" | "motion"
    ollama_host: str = "http://localhost:11434"
    ollama_model: Optional[str] = None  # auto-picked at setup time if None
    poll_interval_seconds: int = 10
    min_confidence: float = 0.6
    # How many consecutive "yes" polls in a row are required before a
    # notification actually fires - derived from required_eating_seconds
    # at setup time (e.g. 20s of continuous eating / 10s poll = 2 in a row).
    # Guards against a single hallucinated/ambiguous frame firing a false alert.
    consecutive_required: int = 2
    required_eating_seconds: int = 20
    # How long (seconds) to record a clip starting the moment feeding is
    # CONFIRMED (post-confirmation only - see clip.py for why there's no
    # pre-roll buffer yet).
    clip_post_confirm_seconds: int = 20


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
    # Path to the OAuth client secret JSON downloaded from Google Cloud
    # Console (one-time user setup, see docs/GOOGLE_DRIVE_SETUP.md).
    # Defaults to ~/.config/nomwatch/drive_credentials.json if not set.
    drive_credentials_path: Optional[str] = None
    # Path where the OAuth token (obtained after first-run browser login)
    # is cached so future uploads don't need re-auth. Defaults to
    # ~/.config/nomwatch/drive_token.json.
    drive_token_path: Optional[str] = None


@dataclass
class NomWatchConfig:
    camera: CameraConfig
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
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
        detection=DetectionConfig(**raw.get("detection", {})),
        notify=NotifyConfig(**raw.get("notify", {})),
        storage=StorageConfig(**raw.get("storage", {})),
    )
