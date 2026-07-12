"""
Pluggable detection engine.

Priority at startup, per the project's product decision:
1. An already-running local model server (e.g. Ollama) - checked via a
   lightweight list request. If present and it has a vision-capable model,
   prefer it - avoids bundling/managing a separate inference stack when the
   user already has one running.
2. An existing open-source video-analysis tool capable of consuming the
   same stream this project already produces (e.g. Frigate-style detectors) -
   left as an integration point (see FrigateDetector stub) rather than
   reinventing video object detection.
3. A bundled lightweight local model (YOLOv8n-class) as the zero-config
   default (planned, not yet implemented).
4. Plain motion detection as the absolute floor (planned, not yet implemented).
"""
from __future__ import annotations

import base64
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests

# Name substrings of Ollama models known to support image input.
# Matched case-insensitively against whatever's in `ollama list`.
VISION_MODEL_HINTS = [
    "gemma3",       # Gemma 3 (4b+) is multimodal
    "llava",
    "bakllava",
    "moondream",
    "qwen2-vl",
    "qwen2.5vl",
    "minicpm-v",
    "llama3.2-vision",
    "pixtral",
]


@dataclass
class FeedingEvent:
    timestamp: float
    confidence: float
    clip_path: Optional[str] = None
    label: str = "feeding_event"
    reasoning: str = ""


class DetectionEngine(ABC):
    @abstractmethod
    def check_frame(self, frame_bytes: bytes) -> Optional[FeedingEvent]:
        """Return a FeedingEvent if this frame indicates a feeding event, else None."""
        raise NotImplementedError


# --- Local model server discovery (Ollama) ---------------------------------

def list_local_models(host: str = "http://localhost:11434") -> List[str]:
    """Returns the names of models available on a local Ollama server, or [] if unreachable."""
    try:
        resp = requests.get(f"{host}/api/tags", timeout=2)
        resp.raise_for_status()
        data = resp.json()
        return [m.get("name") or m.get("model") for m in data.get("models", [])]
    except (requests.RequestException, ValueError):
        return []


def probe_local_model_server(host: str = "http://localhost:11434") -> bool:
    """Checks whether a local model server (e.g. Ollama) is reachable at all."""
    try:
        resp = requests.get(f"{host}/api/tags", timeout=1.5)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def pick_vision_model(models: List[str]) -> Optional[str]:
    """
    Picks the best available vision-capable model from a list of installed
    Ollama models, preferring the smallest/fastest match first since this is
    meant to run continuously on modest bridge hardware.
    """
    lowered = {m: m.lower() for m in models if m}
    for hint in VISION_MODEL_HINTS:
        for original, low in lowered.items():
            if hint in low:
                return original
    return None


# --- Frame capture via ffmpeg -----------------------------------------------

def capture_frame(stream_url: str, timeout: int = 10) -> Optional[bytes]:
    """
    Grabs a single JPEG frame from an RTSP/HLS stream using ffmpeg.
    Returns raw JPEG bytes, or None if capture failed (e.g. ffmpeg not
    installed, or the stream is unreachable).
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "frame.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-rtsp_transport", "tcp",
                    "-i", stream_url,
                    "-frames:v", "1",
                    "-q:v", "3",
                    str(out_path),
                ],
                capture_output=True,
                timeout=timeout,
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return None

        if not out_path.exists():
            return None
        return out_path.read_bytes()


# --- Ollama vision detector --------------------------------------------------

PROMPT = (
    "You are watching a single still frame from a camera pointed at a pet feeder. "
    "Decide if this frame shows a pet actively eating or drinking at the feeder "
    "(as opposed to an empty scene, a pet just walking by, or a person). "
    "Respond with exactly one line in this format, nothing else:\n"
    "FEEDING: yes|no CONFIDENCE: 0.0-1.0 REASON: <one short sentence>"
)


class OllamaVisionDetector(DetectionEngine):
    """
    Uses a locally running Ollama vision model (e.g. gemma3:4b) to classify
    single frames as feeding events or not. Preferred over a bundled model
    per the project's "use what the user already has" design.
    """

    def __init__(
        self,
        model: str,
        host: str = "http://localhost:11434",
        min_confidence: float = 0.6,
    ):
        self.model = model
        self.host = host
        self.min_confidence = min_confidence

    def check_frame(self, frame_bytes: bytes) -> Optional[FeedingEvent]:
        b64 = base64.b64encode(frame_bytes).decode("ascii")
        try:
            resp = requests.post(
                f"{self.host}/api/generate",
                json={
                    "model": self.model,
                    "prompt": PROMPT,
                    "images": [b64],
                    "stream": False,
                },
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException:
            return None

        text = resp.json().get("response", "")
        is_feeding, confidence, reason = self._parse(text)
        if is_feeding and confidence >= self.min_confidence:
            return FeedingEvent(
                timestamp=time.time(),
                confidence=confidence,
                reasoning=reason,
            )
        return None

    @staticmethod
    def _parse(text: str) -> tuple[bool, float, str]:
        is_feeding = False
        confidence = 0.0
        reason = text.strip()
        try:
            lower = text.lower()
            is_feeding = "feeding: yes" in lower
            if "confidence:" in lower:
                after = lower.split("confidence:", 1)[1].strip()
                num = after.split()[0].rstrip(".,")
                confidence = float(num)
            if "reason:" in lower:
                reason = text.split(":", 2)[-1].strip() if "reason:" in lower else reason
        except (ValueError, IndexError):
            pass
        return is_feeding, confidence, reason

    def poll_stream(self, stream_url: str, interval_seconds: int = 15):
        """
        Generator: captures a frame every `interval_seconds` and yields
        FeedingEvents as they're detected. Runs indefinitely - caller decides
        how to consume it (e.g. wire to notify.py / storage.py).
        """
        while True:
            frame = capture_frame(stream_url)
            if frame is not None:
                event = self.check_frame(frame)
                if event is not None:
                    yield event
            time.sleep(interval_seconds)


class FrigateDetector(DetectionEngine):
    """
    Stub integration point for consuming Frigate (or similar) as the
    detection backend, pointed at the same MediaMTX HLS/RTSP output this
    project already exposes, rather than re-implementing object detection.
    """

    def __init__(self, frigate_api_url: str):
        self.frigate_api_url = frigate_api_url

    def check_frame(self, frame_bytes: bytes):
        raise NotImplementedError("Frigate integration - see docs/ROADMAP.md v0.3")


class MotionOnlyDetector(DetectionEngine):
    """Minimum viable fallback: simple frame-diff motion detection. Not yet implemented."""

    def __init__(self, threshold: float = 25.0):
        self.threshold = threshold

    def check_frame(self, frame_bytes: bytes) -> Optional[FeedingEvent]:
        raise NotImplementedError("Frame-diff motion detection - see docs/ROADMAP.md v0.3")
