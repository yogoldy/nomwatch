"""
Storage backends for event clips.

Three options, in order of setup friction:

1. "local" (default) - just save clips to a local folder. Zero setup,
   zero cloud, zero accounts. Good default for anyone who doesn't want any
   external dependency at all.

2. "google_drive_sync" (recommended cloud option) - copy the clip into the
   local sync folder used by the official Google Drive for Desktop app.
   Zero OAuth, zero Google Cloud project - it reuses whatever Google account
   the user already signed into that app with, and Drive's own already-
   authenticated sync client handles the actual cloud upload. This is the
   right default for "I just want it in my existing consumer Google Drive."

3. "google_drive_api" (advanced) - direct OAuth + Drive API upload. Requires
   the user to create their own free Google Cloud OAuth client (Google
   doesn't allow a shared client for arbitrary third-party desktop apps
   without an app-review process) - see docs/GOOGLE_DRIVE_SETUP.md. Useful
   if someone doesn't want to install the Drive desktop app, or wants
   uploads to work on a headless bridge device with no GUI.

Why any of this needs code, not an AI agent doing the copy/upload: NomWatch
has to run completely unattended (e.g. it detects a feeding event at 2am
while nobody's watching a chat window), so this has to be a deterministic
function call NomWatch itself makes.
"""
from __future__ import annotations

import os
import platform
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from .config import CONFIG_DIR, StorageConfig

SCOPES = ["https://www.googleapis.com/auth/drive.file"]  # least-privilege: only files this app creates


class StorageBackend(ABC):
    @abstractmethod
    def upload_clip(self, clip_path: Path) -> str:
        """Stores/uploads a clip and returns a reference (a path, link, or file id)."""
        raise NotImplementedError


class LocalBackend(StorageBackend):
    """Just copies the clip to a local folder. No cloud, no accounts, no setup."""

    def __init__(self, save_dir: Optional[str] = None):
        self.save_dir = Path(save_dir or (CONFIG_DIR / "clips"))
        self.save_dir.mkdir(parents=True, exist_ok=True)

    def upload_clip(self, clip_path: Path) -> str:
        dest = self.save_dir / clip_path.name
        if clip_path.resolve() != dest.resolve():
            shutil.copy2(clip_path, dest)
        return str(dest)


# Common locations where Google Drive for Desktop mounts/syncs a local
# folder, by platform. Checked in order; first existing one wins.
_DRIVE_SYNC_CANDIDATES = {
    "Darwin": [
        # Newer "Drive for desktop" (2022+) mounts each account under
        # ~/Library/CloudStorage/GoogleDrive-<email>/My Drive
        Path.home() / "Library" / "CloudStorage",
        Path.home() / "Google Drive" / "My Drive",  # legacy Backup and Sync
    ],
    "Windows": [
        Path.home() / "Google Drive",
        Path("G:/My Drive"),
    ],
    "Linux": [
        Path.home() / "Google Drive",
    ],
}


def find_google_drive_sync_folder() -> Optional[Path]:
    """
    Auto-detects the Google Drive for Desktop local sync folder, if the app
    is installed and signed in. Returns None if not found - caller should
    fall back to asking the user for a path, or a different storage option.
    """
    system = platform.system()
    for candidate in _DRIVE_SYNC_CANDIDATES.get(system, []):
        if not candidate.exists():
            continue
        if candidate.name == "CloudStorage":
            # Look inside for a GoogleDrive-* folder, then its "My Drive" subfolder.
            for sub in candidate.glob("GoogleDrive-*"):
                my_drive = sub / "My Drive"
                if my_drive.exists():
                    return my_drive
            continue
        return candidate
    return None


class GoogleDriveDesktopSyncBackend(StorageBackend):
    """
    Copies clips into the Google Drive for Desktop sync folder. The already-
    authenticated Drive app picks up the new file and uploads it on its own -
    no OAuth code, no Google Cloud project, no API calls from NomWatch at all.
    """

    def __init__(self, sync_folder: Optional[str] = None, subfolder: str = "NomWatch"):
        base = Path(sync_folder) if sync_folder else find_google_drive_sync_folder()
        if base is None:
            raise FileNotFoundError(
                "Could not find a Google Drive for Desktop sync folder. Install/sign into "
                "Google Drive for Desktop (https://www.google.com/drive/download/), or set "
                "storage.drive_sync_folder manually to its 'My Drive' path, or use "
                "storage.provider: local / google_drive_api instead."
            )
        self.target_dir = base / subfolder
        self.target_dir.mkdir(parents=True, exist_ok=True)

    def upload_clip(self, clip_path: Path) -> str:
        dest = self.target_dir / clip_path.name
        shutil.copy2(clip_path, dest)
        return f"(syncing via Google Drive for Desktop) {dest}"


class GoogleDriveAPIBackend(StorageBackend):
    """Advanced option: direct OAuth + Drive API upload. See module docstring."""

    def __init__(self, cfg: StorageConfig):
        self.credentials_path = Path(
            cfg.drive_credentials_path or (CONFIG_DIR / "drive_credentials.json")
        )
        self.token_path = Path(cfg.drive_token_path or (CONFIG_DIR / "drive_token.json"))
        self.folder_id = cfg.drive_folder_id
        self._service = None

    def _get_credentials(self):
        # Imported lazily so the base install doesn't require these
        # dependencies unless this advanced option is actually used
        # (`pip install nomwatch[drive]`).
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"No Google OAuth client secret found at {self.credentials_path}. "
                        "See docs/GOOGLE_DRIVE_SETUP.md to create one (free, one-time, ~5 minutes) - "
                        "or switch storage.provider to 'google_drive_sync' for a zero-setup option."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)

            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(creds.to_json())
            os.chmod(self.token_path, 0o600)

        return creds

    def _get_service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            creds = self._get_credentials()
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    def upload_clip(self, clip_path: Path) -> str:
        from googleapiclient.http import MediaFileUpload

        service = self._get_service()
        metadata = {"name": clip_path.name}
        if self.folder_id:
            metadata["parents"] = [self.folder_id]

        media = MediaFileUpload(str(clip_path), mimetype="video/mp4", resumable=True)
        uploaded = service.files().create(
            body=metadata, media_body=media, fields="id, webViewLink"
        ).execute()

        return uploaded.get("webViewLink") or uploaded.get("id")


def build_storage_backend(cfg: StorageConfig) -> Optional[StorageBackend]:
    if cfg.provider == "local":
        return LocalBackend(cfg.local_save_dir)
    if cfg.provider == "google_drive_sync":
        return GoogleDriveDesktopSyncBackend(cfg.drive_sync_folder)
    if cfg.provider == "google_drive_api":
        return GoogleDriveAPIBackend(cfg)
    return None
