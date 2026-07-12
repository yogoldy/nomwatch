"""
Storage backends for uploading event clips.

v1: Google Drive only, via OAuth the user grants directly to their own
Google account. NomWatch has no backend server of its own to hold tokens -
credentials/tokens are stored locally, permissioned 600, and never leave
this machine.

Why this needs code, not an AI agent doing the upload: NomWatch has to run
completely unattended (e.g. it detects a feeding event at 2am while nobody's
watching a chat window), so the upload has to be a deterministic function
call NomWatch itself makes - not something routed through an AI assistant
session that isn't guaranteed to be running.

One-time setup required (Google doesn't allow a shared/public OAuth client
for something like this without an app review process, so each user needs
their own free Google Cloud project):
  1. Go to https://console.cloud.google.com/apis/credentials
  2. Create a project (or reuse one), enable the "Google Drive API"
  3. Create an OAuth client ID, type "Desktop app"
  4. Download the JSON, save it as ~/.config/nomwatch/drive_credentials.json
     (or wherever storage.drive_credentials_path points)
  5. First time NomWatch tries to upload, it opens a browser for you to
     grant access once - after that, a cached token handles future uploads
     silently.
See docs/GOOGLE_DRIVE_SETUP.md for the full walkthrough.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from .config import CONFIG_DIR, StorageConfig

SCOPES = ["https://www.googleapis.com/auth/drive.file"]  # least-privilege: only files this app creates


class StorageBackend(ABC):
    @abstractmethod
    def upload_clip(self, clip_path: Path) -> str:
        """Uploads a clip and returns a reference (e.g. a Drive file id or URL)."""
        raise NotImplementedError


class GoogleDriveBackend(StorageBackend):
    def __init__(self, cfg: StorageConfig):
        self.credentials_path = Path(
            cfg.drive_credentials_path or (CONFIG_DIR / "drive_credentials.json")
        )
        self.token_path = Path(cfg.drive_token_path or (CONFIG_DIR / "drive_token.json"))
        self.folder_id = cfg.drive_folder_id
        self._service = None

    def _get_credentials(self):
        # Imported lazily so the base install doesn't require these
        # dependencies unless Drive upload is actually used (`pip install
        # nomwatch[drive]`).
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
                        "See docs/GOOGLE_DRIVE_SETUP.md to create one (free, one-time, ~5 minutes)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)  # opens a browser for one-time consent

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
    if cfg.provider == "google_drive":
        return GoogleDriveBackend(cfg)
    return None
