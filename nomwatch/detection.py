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


# --- Motion detection + zone cropping (ffmpeg-only, no image library) -------
# Deliberately implemented with ffmpeg alone rather than pulling in Pillow /
# numpy / OpenCV. See docs/ARCHITECTURE.md ("Why no image library") for the
# tradeoff: this keeps NomWatch's dependency surface tiny for a privacy-first
# local tool, at the cost of shelling out to ffmpeg (already a hard dependency
# for frame capture and clips) per frame instead of doing array math in-process.

MOTION_THUMB_SIZE = 48  # downscale grid for the motion diff


def _gray_thumbnail(jpeg_bytes: bytes, size: int = MOTION_THUMB_SIZE) -> Optional[bytes]:
    """
    Decode a JPEG to a `size`x`size` single-channel grayscale raw buffer using
    ffmpeg. Returns size*size bytes (one 0-255 value per pixel), or None on
    failure. Downscaling to a tiny grid makes the frame-to-frame diff cheap AND
    smooths out per-pixel IR sensor noise, so a genuinely static scene reads as
    near-zero motion (measured ~0.3 MAD on the real camera).
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0",
             "-vf", f"scale={size}:{size},format=gray",
             "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
            input=jpeg_bytes, capture_output=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    data = proc.stdout
    return data if data and len(data) >= size * size else None


@dataclass
class MotionResult:
    score: float             # mean absolute difference between frames, 0-255
    changed_fraction: float  # fraction of pixels changed beyond per_pixel_delta
    moved: bool              # score >= threshold


class FrameDiffMotion:
    """
    Cheap frame-to-frame motion detector built entirely on ffmpeg (no Python
    image library). Compares the current frame against the previous one on a
    downscaled grayscale grid and reports a motion score.

    Measured on the real camera (2026-07-12): a static empty scene sits at
    MAD ~0.3 (pure IR sensor noise), while a cat moving at the bowl is MAD
    ~20-48 - a ~50x gap - so the default threshold (2.0) has a very wide
    margin. The threshold is deliberately LOW: a false "motion" only means the
    vision model gets consulted (and the fixed prompt correctly says "no cat"),
    which is harmless; a MISSED motion could drop a real feeding, which is not.
    """

    def __init__(self, threshold: float = 2.0, size: int = MOTION_THUMB_SIZE,
                 per_pixel_delta: int = 20):
        self.threshold = threshold
        self.size = size
        self.per_pixel_delta = per_pixel_delta

    def thumbnail(self, jpeg_bytes: bytes) -> Optional[bytes]:
        return _gray_thumbnail(jpeg_bytes, self.size)

    def compare(self, prev_thumb: bytes, cur_thumb: bytes) -> MotionResult:
        n = min(len(prev_thumb), len(cur_thumb))
        if n == 0:
            return MotionResult(0.0, 0.0, False)
        total = 0
        changed = 0
        for i in range(n):
            d = prev_thumb[i] - cur_thumb[i]
            if d < 0:
                d = -d
            total += d
            if d >= self.per_pixel_delta:
                changed += 1
        score = total / n
        return MotionResult(
            score=score,
            changed_fraction=changed / n,
            moved=score >= self.threshold,
        )


@dataclass
class Zone:
    """Normalized bounding box (all in 0.0-1.0, fractions of frame w/h)."""
    x: float
    y: float
    w: float
    h: float

    def valid(self) -> bool:
        return (
            0.0 <= self.x < 1.0 and 0.0 <= self.y < 1.0
            and 0.0 < self.w <= 1.0 and 0.0 < self.h <= 1.0
            and self.x + self.w <= 1.0001 and self.y + self.h <= 1.0001
            # A near-full-frame "zone" is just no zone; ignore trivial ones.
            and not (self.w > 0.99 and self.h > 0.99)
        )

    @classmethod
    def from_config(cls, det) -> Optional["Zone"]:
        """Build a Zone from a DetectionConfig, or None if not enabled/complete."""
        if not getattr(det, "zone_detection_enabled", False):
            return None
        coords = (det.zone_x, det.zone_y, det.zone_w, det.zone_h)
        if any(c is None for c in coords):
            return None
        zone = cls(*[float(c) for c in coords])
        return zone if zone.valid() else None


def crop_to_zone(jpeg_bytes: bytes, zone: Optional[Zone]) -> bytes:
    """
    Crop a JPEG to a normalized bounding box using ffmpeg's crop filter with
    in_w/in_h expressions - so we never need the pixel dimensions or an image
    library. Returns the cropped JPEG, or the ORIGINAL bytes unchanged if the
    zone is absent/invalid or the crop fails (a crop hiccup must never break
    the detection pipeline).
    """
    if zone is None or not zone.valid():
        return jpeg_bytes
    vf = (
        f"crop=in_w*{zone.w:.4f}:in_h*{zone.h:.4f}:"
        f"in_w*{zone.x:.4f}:in_h*{zone.y:.4f}"
    )
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-vf", vf,
             "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"],
            input=jpeg_bytes, capture_output=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return jpeg_bytes
    return proc.stdout if proc.returncode == 0 and proc.stdout else jpeg_bytes


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
    """
    Fully non-AI fallback (`detection.engine = "motion"`): flags "activity"
    purely from frame-to-frame motion, no vision model required at all. Far
    cruder than the vision model - it cannot tell a cat eating from a cat
    walking past or a person reaching in - but it needs no model server and
    never produces the empty-scene "there's food in the bowl" false positive,
    because a static scene simply has no motion. Keeps the previous frame's
    thumbnail as state across calls, so the first call always returns None
    (no baseline yet).
    """

    def __init__(self, threshold: float = 2.0):
        self.motion = FrameDiffMotion(threshold=threshold)
        self._prev_thumb: Optional[bytes] = None

    def check_frame(self, frame_bytes: bytes) -> Optional[FeedingEvent]:
        thumb = self.motion.thumbnail(frame_bytes)
        prev = self._prev_thumb
        if thumb is not None:
            self._prev_thumb = thumb
        if prev is None or thumb is None:
            return None
        res = self.motion.compare(prev, thumb)
        if not res.moved:
            return None
        # Crude confidence: how far above threshold the motion sits, capped.
        confidence = min(1.0, res.score / (self.motion.threshold * 10 or 1.0))
        return FeedingEvent(
            timestamp=time.time(),
            confidence=confidence,
            reasoning=f"motion near the bowl (score {res.score:.1f})",
        )


# --- Engine-aware polling driver --------------------------------------------

def poll_stream(
    stream_url: str,
    *,
    engine: str = "ollama",
    detector: Optional[OllamaVisionDetector] = None,
    motion: Optional[FrameDiffMotion] = None,
    motion_gating: bool = True,
    zone: Optional[Zone] = None,
    interval_seconds: int = 10,
    consecutive_required: int = 2,
    rearm_after_negative_polls: int = 3,
    on_poll=None,
):
    """
    Generator that drives the whole detection pipeline and yields a
    FeedingEvent once `consecutive_required` polls IN A ROW have all come back
    positive. Runs indefinitely; the caller decides how to consume events
    (notify / record clip / log). Supports three engines:

    - "motion": non-AI. A poll is positive iff frame-to-frame motion exceeds
      the threshold. `detector` is unused.
    - "ollama": the vision model decides. If `motion` is provided and
      `motion_gating` is on, the model is NOT even called on a static frame
      (no motion since the last frame) - that static frame is simply negative.
      This is the single most direct fix for the empty-scene false positive:
      an unchanging bowl-with-food never reaches the LLM.
    - "hybrid": motion AND the vision model must BOTH agree for a poll to
      count. Motion alone (a cat walking past) is not enough; a model "yes" on
      a static frame (the residual hallucination the fixed prompt can still
      occasionally produce) is suppressed because nothing moved.

    After an event fires, it remains part of the same visit until
    `rearm_after_negative_polls` negative polls occur in a row. This prevents
    one model false negative from splitting an ongoing meal into multiple
    notifications and clips.

    Every frame is optionally cropped to `zone` FIRST, so both the motion diff
    and the image sent to the model see only the bowl area.

    `on_poll`, if given, is called after EVERY cycle with a status dict
    (ok / is_feeding / confidence / reason / moved / motion_score / gated /
    streak / error, plus frame_bytes on a positive) - the loop's heartbeat and
    the source of the per-poll diagnostic log.
    """
    streak = 0
    already_fired = False
    negative_polls_since_event = 0
    best_confidence = 0.0
    best_reason = ""
    prev_thumb: Optional[bytes] = None

    def emit(status, streak_val):
        if on_poll:
            on_poll({**status, "streak": streak_val})

    while True:
        status = {
            "ok": False, "is_feeding": False, "confidence": None,
            "reason": None, "error": None, "engine": engine,
            "moved": None, "motion_score": None, "gated": False,
        }
        frame = capture_frame(stream_url)
        if frame is None:
            status["error"] = "frame capture failed (camera unreachable, or ffmpeg problem)"
            emit(status, streak)
            time.sleep(interval_seconds)
            continue

        # Crop to the configured zone (if any) before ANY analysis, so motion
        # and the vision model both reason only about the bowl area.
        analysed = crop_to_zone(frame, zone) if zone is not None else frame
        status["zone_cropped"] = zone is not None

        # --- motion (computed whenever a motion detector is present) ---
        moved: Optional[bool] = None
        if motion is not None:
            thumb = motion.thumbnail(analysed)
            if prev_thumb is not None and thumb is not None:
                mres = motion.compare(prev_thumb, thumb)
                moved = mres.moved
                status["motion_score"] = round(mres.score, 2)
            else:
                # First frame (or a decode failure): no baseline yet. Treat as
                # "no motion" so we don't fire on startup, but never gate out
                # the very first model call in plain ollama mode below.
                moved = False
            if thumb is not None:
                prev_thumb = thumb
        status["moved"] = moved

        # --- engine decision -> `positive` (does this poll count?) ---
        positive = False
        if engine == "motion":
            status["ok"] = True
            positive = bool(moved)
            status["is_feeding"] = positive
            status["confidence"] = 1.0 if positive else 0.0
            status["reason"] = (
                f"motion detected near the bowl (score {status['motion_score']})"
                if positive else "no motion since last frame"
            )
        else:
            # "ollama" or "hybrid" - both use the vision model.
            gated_out = motion is not None and motion_gating and moved is False
            if gated_out:
                status["ok"] = True
                status["is_feeding"] = False
                status["confidence"] = 0.0
                status["reason"] = "no motion since last frame; vision model skipped"
                status["gated"] = True
            else:
                result = detector.classify(analysed) if detector else None
                if result is None:
                    status["error"] = "vision model unreachable (is Ollama still running?)"
                    emit(status, streak)
                    time.sleep(interval_seconds)
                    continue
                status["ok"] = True
                status["confidence"] = result.confidence
                status["reason"] = result.reason
                status["raw_text"] = result.raw_text
                llm_positive = result.is_feeding and result.confidence >= detector.min_confidence
                if engine == "hybrid":
                    positive = bool(llm_positive and moved)
                    status["is_feeding"] = positive
                    if llm_positive and moved is False:
                        status["reason"] = (
                            "vision model said feeding but nothing moved - suppressed "
                            f"(hybrid). Model said: {result.reason}"
                        )
                else:  # plain ollama
                    positive = llm_positive
                    status["is_feeding"] = positive

        # Attach the analysed frame only when this poll is positive - that's
        # the diagnostic case worth saving to disk (why did it think feeding?).
        if positive:
            status["frame_bytes"] = analysed

        # --- debounce / streak ---
        if positive:
            negative_polls_since_event = 0
            if already_fired:
                # This is still the same visit. Do not let a prior isolated
                # negative re-arm the detector and create a second alert.
                emit(status, streak)
                time.sleep(interval_seconds)
                continue
            streak += 1
            best_confidence = max(best_confidence, status.get("confidence") or 0.0)
            best_reason = status.get("reason") or best_reason
            if streak >= consecutive_required and not already_fired:
                already_fired = True
                emit(status, streak)
                yield FeedingEvent(
                    timestamp=time.time(),
                    confidence=best_confidence,
                    reasoning=best_reason,
                )
                time.sleep(interval_seconds)
                continue
        else:
            if already_fired:
                negative_polls_since_event += 1
                if negative_polls_since_event < max(1, rearm_after_negative_polls):
                    emit(status, streak)
                    time.sleep(interval_seconds)
                    continue
            streak = 0
            already_fired = False
            best_confidence = 0.0
            best_reason = ""

        emit(status, streak)
        time.sleep(interval_seconds)
