"""Durable outbox worker; slow event finalization never blocks detection."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .bridge import local_mediamtx_rtsp_url
from .clip import build_clip_with_preroll, record_clip
from .config import CONFIG_DIR, StorageConfig, clean_user_path, load_config
from .notify import build_notifier
from .paths import NomWatchPaths
from .state import LocalState
from .storage import build_storage_backend


class JobWorker:
    def __init__(self, state: LocalState, *, clock=time.time, sleeper=time.sleep):
        self.state = state
        self.clock = clock
        self.sleep = sleeper

    def run_once(self) -> bool:
        job = self.state.claim_job()
        if not job:
            return False
        payload = json.loads(job["payload_json"])
        event_id = payload.get("event_id")
        try:
            if job["kind"] != "finalize_event":
                raise RuntimeError(f"unsupported job kind: {job['kind']}")
            self._finalize_event(event_id, payload)
            self.state.finish_job(job["id"], event_id=event_id)
        except Exception as exc:  # durable retry boundary
            self.state.retry_job(job["id"], str(exc), job["attempts"], event_id=event_id)
        return True

    def run_forever(self, stop=None) -> None:
        while stop is None or not stop():
            if not self.run_once():
                self.sleep(1.0)

    def _finalize_event(self, event_id: str, payload: dict) -> None:
        cfg = load_config()
        if cfg is None:
            raise RuntimeError("configuration is unavailable")
        stream_url = local_mediamtx_rtsp_url(cfg)
        clips_dir = self.state.paths.home / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        clip_path = None
        if cfg.detection.pre_roll_seconds > 0:
            recordings = Path(clean_user_path(cfg.bridge.recordings_dir) or (self.state.paths.home / "recordings"))
            clip_path = build_clip_with_preroll(
                recordings, "cam", float(payload["timestamp"]), cfg.detection.pre_roll_seconds,
                cfg.detection.clip_post_confirm_seconds, out_dir=clips_dir,
            )
        if clip_path is None and cfg.detection.clip_post_confirm_seconds > 0:
            clip_path = record_clip(stream_url, cfg.detection.clip_post_confirm_seconds, out_dir=clips_dir)
        if clip_path:
            self.state.add_media(event_id, clip_path)

        notifier = build_notifier(cfg.notify)
        if notifier and not notifier.send(
            "NomWatch: feeding detected",
            f"Confidence {float(payload['confidence']):.2f}. {payload['reason']}",
        ):
            raise RuntimeError("notification provider rejected the event")
        if clip_path:
            backend = build_storage_backend(cfg.storage)
            if backend:
                backend.upload_clip(clip_path)


def run_job_worker() -> None:
    paths = NomWatchPaths.from_environment()
    JobWorker(LocalState(paths)).run_forever()
