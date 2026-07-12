"""
Pluggable detection engine.

Priority at startup, per the project's product decision:
1. An already-running local model server (e.g. Ollama) - checked via a
   lightweight list/status request. If present, prefer it.
2. An existing open-source video-analysis tool capable of consuming the
   same stream this project already produces (e.g. Frigate-style detectors) -
   left as an integration point (see FrigateDetector stub) rather than
   reinventing video object detection.
3. A bundled lightweight local model (YOLOv8n-class) as the zero-config
   default.
4. Plain motion detection as the absolute floor.

This module only defines the interface + the startup probe; concrete
detectors are implemented incrementally (see docs/ROADMAP.md v0.3).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class FeedingEvent:
    timestamp: float
    confidence: float
    clip_path: Optional[str] = None
    label: str = "feeding_event"


class DetectionEngine(ABC):
    @abstractmethod
    def check_frame(self, frame) -> Optional[FeedingEvent]:
        """Return a FeedingEvent if this frame indicates a feeding event, else None."""
        raise NotImplementedError


def probe_local_model_server(host: str = "http://localhost:11434") -> bool:
    """
    Checks whether a local model server (e.g. Ollama) is already running,
    by hitting its list endpoint. Used at startup to decide whether NomWatch
    should defer to a model the user already has, instead of bundling its own.
    """
    try:
        resp = requests.get(f"{host}/api/tags", timeout=1.5)
        return resp.status_code == 200
    except requests.RequestException:
        return False


class FrigateDetector(DetectionEngine):
    """
    Stub integration point for consuming Frigate (or similar) as the
    detection backend, pointed at the same MediaMTX HLS/RTSP output this
    project already exposes, rather than re-implementing object detection.
    """

    def __init__(self, frigate_api_url: str):
        self.frigate_api_url = frigate_api_url

    def check_frame(self, frame):
        raise NotImplementedError("Frigate integration - see docs/ROADMAP.md v0.3")


class MotionOnlyDetector(DetectionEngine):
    """Minimum viable fallback: simple frame-diff motion detection."""

    def __init__(self, threshold: float = 25.0):
        self.threshold = threshold
        self._prev_frame = None

    def check_frame(self, frame) -> Optional[FeedingEvent]:
        raise NotImplementedError("Frame-diff motion detection - see docs/ROADMAP.md v0.3")
