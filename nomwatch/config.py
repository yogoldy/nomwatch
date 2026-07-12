"""
Local configuration handling for NomWatch.

Config lives at ~/.config/nomwatch/config.yml, permissioned 600.
Never committed to git, never logged, never sent anywhere.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import Optional

import yaml

CONFIG_DIR = Path(os.path.expanduser("~/.config/nomwatch"))
CONFIG_PATH = CONFIG_DIR / "config.yml"


def clean_user_path(raw: Optional[str]) -> Optional[str]:
    """
    Normalizes a user-typed filesystem path: strips whitespace and any
    wrapping/stray quotes (a real user pasted '/Users/... Events' WITH the
    quotes, and NomWatch created a literal `'` directory tree relative to
    its own CWD and silently saved clips there), expands ~ and $VARS.
    Returns None for empty input. Callers that need an absolute path should
    check .startswith("/") (or Path.is_absolute) and reject otherwise -
    a relative path would resolve against whatever CWD the daemon happens
    to have, which is never what the user meant.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip().strip("'\"").strip()
    if not cleaned:
        return None
    return os.path.expandvars(os.path.expanduser(cleaned))


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
    # Continuous local segment recording (enables real pre-roll clips - see
    # clip.py). Segments are small rolling mp4 files, auto-deleted after
    # record_retention_seconds so this doesn't grow unbounded.
    recordings_dir: Optional[str] = None  # defaults to ~/.config/nomwatch/recordings
    record_segment_seconds: int = 5
    record_retention_seconds: int = 120


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
    # Clip window around a confirmed event: `pre_roll_seconds` before the
    # first positive poll in the streak, `clip_post_confirm_seconds` after
    # confirmation. Pre-roll requires continuous local recording (see
    # BridgeConfig.recordings_dir) - if that's off, pre-roll is simply 0.
    pre_roll_seconds: int = 5
    clip_post_confirm_seconds: int = 20
    # Optional enrichment: ask the vision model to also describe what it
    # sees (species/color/etc.) alongside the feeding judgment. Off by
    # default. This is NOT a new event category - see docs/ROADMAP.md's
    # "explicitly not planned" note on generalizing beyond feeding events.
    describe_appearance: bool = False
    # User-entered description of their pet(s) (e.g. "black cat", "golden
    # retriever named Max") intended to help the vision model recognize the
    # specific animal. PLACEHOLDER ONLY - not yet wired into the detection
    # prompt in detection.py. Stored so the UI has somewhere real to save
    # it once that wiring is built.
    pet_description: Optional[str] = None
    # Placeholder only - no architecture exists for this yet. Recorded here
    # so the config shape is ready whenever it's actually built.
    zone_detection_enabled: bool = False


@dataclass
class NotifyConfig:
    provider: str = "ntfy"  # "ntfy" | "pushover" | "none"
    ntfy_topic: Optional[str] = None
    pushover_user_key: Optional[str] = None
    pushover_app_token: Optional[str] = None


@dataclass
class StorageConfig:
    # "local"            - just save clips to a local folder, no cloud at all
    # "google_drive_sync" - copy into the Google Drive for Desktop sync folder
    #                       (zero setup: reuses whatever Google account the
    #                       user already signed into that app with)
    # "google_drive_api"  - full OAuth/API upload (advanced; requires the
    #                       user to create their own Google Cloud OAuth
    #                       client, see docs/GOOGLE_DRIVE_SETUP.md)
    # "none"
    provider: str = "local"

    local_save_dir: Optional[str] = None  # defaults to ~/.config/nomwatch/clips

    drive_sync_folder: Optional[str] = None  # auto-detected if None

    drive_folder_id: Optional[str] = None
    drive_credentials_path: Optional[str] = None
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


def _known_fields(cls, raw: dict) -> dict:
    """
    Drops keys a dataclass doesn't know about instead of crashing with a
    TypeError - keeps configs written by a newer/older NomWatch loadable
    (extra keys are simply ignored; missing ones get defaults).
    """
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (raw or {}).items() if k in names}


def config_from_dict(raw: dict) -> NomWatchConfig:
    """Builds a NomWatchConfig from a parsed YAML dict, tolerating unknown keys."""
    return NomWatchConfig(
        camera=CameraConfig(**_known_fields(CameraConfig, raw.get("camera", {}))),
        bridge=BridgeConfig(**_known_fields(BridgeConfig, raw.get("bridge", {}))),
        detection=DetectionConfig(**_known_fields(DetectionConfig, raw.get("detection", {}))),
        notify=NotifyConfig(**_known_fields(NotifyConfig, raw.get("notify", {}))),
        storage=StorageConfig(**_known_fields(StorageConfig, raw.get("storage", {}))),
    )


def load_config() -> Optional[NomWatchConfig]:
    if not CONFIG_PATH.exists():
        return None
    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict) or "camera" not in raw:
        return None
    return config_from_dict(raw)
