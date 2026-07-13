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

DEFAULT_VISION_MODEL = "gemma3:4b"

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
class ClassificationResult:
    is_feeding: bool
    confidence: float
    reason: str
    raw_text: str


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


def model_installed(models: List[str], model_name: str) -> bool:
    """Exact-match check (case-insensitive) that a given model tag is in the installed list."""
    lowered = {m.lower() for m in models if m}
    return model_name.lower() in lowered


def pull_model(model_name: str = DEFAULT_VISION_MODEL, on_output=None) -> bool:
    """
    Runs `ollama pull <model_name>`, streaming output line-by-line to `on_output`
    (if given, e.g. click.echo) so the user sees real download progress.
    Returns True if the pull succeeded (exit code 0), False otherwise
    (e.g. `ollama` binary not installed/running, network failure, bad tag).
    """
    try:
        process = subprocess.Popen(
            ["ollama", "pull", model_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        if on_output:
            on_output("The `ollama` command wasn't found - install Ollama first: https://ollama.com/download")
        return False

    for line in process.stdout:
        if on_output:
            on_output(line.rstrip())
    process.wait()
    return process.returncode == 0


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

def capture_frame_with_error(stream_url: str, timeout: int = 10) -> tuple[Optional[bytes], Optional[str]]:
    """
    Grabs a single JPEG frame from an RTSP/HLS stream using ffmpeg.
    Returns (jpeg_bytes, None) on success, or (None, error_text) on failure -
    error_text is ffmpeg's own stderr tail (with any credential-bearing URL
    redacted) so callers can tell apart "wrong password" from "host
    unreachable" instead of showing one generic failure message.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "frame.jpg"
        try:
            proc = subprocess.run(
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
            )
        except subprocess.TimeoutExpired:
            return None, "timeout: no response from the camera within {}s".format(timeout)
        except FileNotFoundError:
            return None, "ffmpeg is not installed"

        if proc.returncode != 0 or not out_path.exists():
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            # Redact the rtsp://user:pass@ URL ffmpeg echoes back in errors.
            redacted = "\n".join(
                line for line in stderr.splitlines()[-6:]
            ).replace(stream_url, "rtsp://<redacted>")
            return None, redacted or f"ffmpeg exited with code {proc.returncode}"
        return out_path.read_bytes(), None


def capture_frame(stream_url: str, timeout: int = 10) -> Optional[bytes]:
    """
    Grabs a single JPEG frame from an RTSP/HLS stream using ffmpeg.
    Returns raw JPEG bytes, or None if capture failed (e.g. ffmpeg not
    installed, or the stream is unreachable).
    """
    frame, _ = capture_frame_with_error(stream_url, timeout=timeout)
    return frame


# --- Ollama vision detector --------------------------------------------------

def build_prompt(pet_description: Optional[str] = None) -> str:
    """
    Builds the vision-model prompt. The single most important thing this
    prompt does is force the model to judge whether an ANIMAL IS PRESENT AND
    EATING - not whether the bowl merely contains food.

    Confirmed live on 2026-07-12: the previous prompt ("is a pet actively
    eating") was silently answered by gemma3:4b as "is there food in the
    bowl", firing FEEDING: yes 0.8 on an empty room with a full bowl on
    EVERY poll (18/18 empty-scene frames from the real camera were false
    positives). The fix is to make "no visible animal" an explicit, hard NO
    regardless of how much food is in the bowl - which took that same
    empty-scene set to 0/18 false positives while still detecting 6/6 real
    cat-eating frames.

    If `pet_description` is set (e.g. "black cat", from config screen 5, and
    previously never used) it's passed as a concrete visual anchor so the
    model can also rule out other animals/people. Measured neutral on the
    empty-scene case here, but it's cheap disambiguation and it's what the
    user actually configured.
    """
    pet_line = (
        f"- The animal you are watching for is a {pet_description}.\n"
        if pet_description else ""
    )
    return (
        "You are looking at a single still frame from a camera aimed at a pet's food bowl.\n"
        "Your ONLY task: decide whether a live animal is physically present in this frame "
        "AND has its head or mouth down at the bowl, actively eating or drinking RIGHT NOW.\n\n"
        "Rules you must follow:\n"
        "- Food, kibble, pellets, or water sitting in the bowl is NOT feeding by itself. "
        "A full or partly-full bowl with NO animal visible is 'no'.\n"
        "- You must actually SEE the animal's body: head, face, muzzle, paws, or fur at the bowl. "
        "If you cannot clearly see an animal, answer 'no' - even if the bowl obviously contains food.\n"
        "- An empty scene, an empty room, or just the bowl/feeder sitting there is 'no'.\n"
        "- A person, or an animal merely walking past or near the bowl without eating, is 'no'.\n"
        f"{pet_line}"
        "\nAnswer with EXACTLY one line, nothing else:\n"
        "FEEDING: yes|no CONFIDENCE: 0.0-1.0 REASON: <one short sentence: what animal you see and "
        "what it is doing, or why there is no animal eating>"
    )


# Backwards-compatible default (no pet hint) for any caller importing PROMPT.
PROMPT = build_prompt()


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
        pet_description: Optional[str] = None,
    ):
        self.model = model
        self.host = host
        self.min_confidence = min_confidence
        # Build the prompt once at construction, folding in the user's pet
        # description (if any). See build_prompt() for why the wording matters.
        self.pet_description = pet_description
        self.prompt = build_prompt(pet_description)

    def classify(self, frame_bytes: bytes) -> Optional["ClassificationResult"]:
        """
        Runs the model on a frame and returns the full raw judgment
        (is_feeding, confidence, reason, raw response text) regardless of
        whether it clears min_confidence. Use this when you want to inspect
        or log borderline/negative calls, not just act on positives.
        Returns None only on a request failure (model unreachable, etc.).
        """
        b64 = base64.b64encode(frame_bytes).decode("ascii")
        try:
            resp = requests.post(
                f"{self.host}/api/generate",
                json={
                    "model": self.model,
                    "prompt": self.prompt,
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
        return ClassificationResult(
            is_feeding=is_feeding, confidence=confidence, reason=reason, raw_text=text
        )

    def check_frame(self, frame_bytes: bytes) -> Optional[FeedingEvent]:
        result = self.classify(frame_bytes)
        if result and result.is_feeding and result.confidence >= self.min_confidence:
            return FeedingEvent(
                timestamp=time.time(),
                confidence=result.confidence,
                reasoning=result.reason,
            )
        return None

    @staticmethod
    def _parse(text: str) -> tuple[bool, float, str]:
        """
        Parses the model's "FEEDING: yes|no CONFIDENCE: 0.0-1.0 REASON: ..."
        response. Tolerant of the model dropping the REASON: label entirely
        (observed in practice) and of extra colons inside the reason text
        itself (fixed: previously used a colon-count split that mangled
        reasoning on the standard 3-colon response format).
        """
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

            reason_idx = lower.find("reason:")
            if reason_idx != -1:
                # Slice the ORIGINAL text (preserves casing) right after the
                # "reason:" label, regardless of how many colons came before it.
                reason = text[reason_idx + len("reason:"):].strip()
            elif "confidence:" in lower:
                # Model dropped the REASON: label but still gave one after
                # the confidence number - grab whatever trails the number.
                after_conf = text[lower.find("confidence:") + len("confidence:"):].strip()
                parts = after_conf.split(None, 1)
                if len(parts) == 2:
                    reason = parts[1].strip()
        except (ValueError, IndexError):
            pass
        return is_feeding, confidence, reason

    def poll_stream(
        self,
        stream_url: str,
        interval_seconds: int = 10,
        consecutive_required: int = 2,
        on_poll=None,
    ):
        """
        Generator: captures a frame every `interval_seconds` and yields a
        FeedingEvent only once `consecutive_required` polls IN A ROW have all
        independently judged "feeding" above min_confidence. Runs indefinitely -
        caller decides how to consume it (e.g. wire to notify.py / storage.py).

        This debounce exists because a single frame can be wrongly classified
        with real confidence (observed in practice: the model confidently
        misreading an atypical camera angle) - requiring several agreeing
        polls in a row, spanning real wall-clock seconds of actual behavior,
        makes a single bad frame far less likely to trigger a false alert.
        Only one event is yielded per continuous feeding streak (it won't
        re-fire every poll while the pet keeps eating) - the streak resets
        once a poll comes back negative.

        `on_poll`, if given, is called after EVERY cycle with a status dict
        (ok / is_feeding / confidence / reason / streak / error) - this is
        the loop's heartbeat, letting a dashboard answer "is this thing
        actually alive and what did it last see" without tailing a log file.
        """
        streak = 0
        already_fired = False
        best_confidence = 0.0
        best_reason = ""

        while True:
            status = {"ok": False, "is_feeding": False, "confidence": None, "reason": None, "error": None}
            frame = capture_frame(stream_url)
            if frame is None:
                status["error"] = "frame capture failed (camera unreachable, or ffmpeg problem)"
            else:
                result = self.classify(frame)
                if result is None:
                    status["error"] = "vision model unreachable (is Ollama still running?)"
                else:
                    status.update(
                        ok=True,
                        is_feeding=result.is_feeding,
                        confidence=result.confidence,
                        reason=result.reason,
                        raw_text=result.raw_text,
                    )
                    # Only attach the actual frame bytes when the model said
                    # "yes" - this is the diagnostic case that matters (why
                    # did it think this was feeding), and doing it for every
                    # single poll would be a lot of needless disk churn on a
                    # camera polled every few seconds. The caller (cli.py) is
                    # responsible for saving this to disk and NOT persisting
                    # it into the heartbeat file itself.
                    if result.is_feeding:
                        status["frame_bytes"] = frame
                    if result.is_feeding and result.confidence >= self.min_confidence:
                        streak += 1
                        best_confidence = max(best_confidence, result.confidence)
                        best_reason = result.reason
                        if streak >= consecutive_required and not already_fired:
                            already_fired = True
                            if on_poll:
                                on_poll({**status, "streak": streak})
                            yield FeedingEvent(
                                timestamp=time.time(),
                                confidence=best_confidence,
                                reasoning=best_reason,
                            )
                            # skip the duplicate on_poll below for this cycle
                            time.sleep(interval_seconds)
                            continue
                    else:
                        streak = 0
                        already_fired = False
                        best_confidence = 0.0
                        best_reason = ""
            if on_poll:
                on_poll({**status, "streak": streak})
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
