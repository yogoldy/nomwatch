"""
Storage backends for uploading event clips.

v1: Google Drive only, via OAuth the user grants directly to their own
Google account. NomWatch has no backend server of its own to hold tokens -
credentials/tokens are stored locally (see config.py), permissioned 600.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    @abstractmethod
    def upload_clip(self, clip_path: Path) -> str:
        """Uploads a clip and returns a reference (e.g. a Drive file id or URL)."""
        raise NotImplementedError


class GoogleDriveBackend(StorageBackend):
    """
    Stub for Google Drive upload via the user's own OAuth grant.
    Full implementation planned for v0.4 - see docs/ROADMAP.md.
    Requires the `nomwatch[drive]` extra (google-api-python-client, google-auth-oauthlib).
    """

    def __init__(self, folder_id: str | None = None):
        self.folder_id = folder_id

    def upload_clip(self, clip_path: Path) -> str:
        raise NotImplementedError("Google Drive upload - see docs/ROADMAP.md v0.4")
