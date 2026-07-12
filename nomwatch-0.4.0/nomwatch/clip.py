"""
Clip building, with real pre-roll support.

v1 approach (`record_clip`): open a second RTSP connection the moment
feeding is CONFIRMED and record forward only. Simple, but can't include
anything from before confirmation - live RTSP has no way to reach into the
past on its own.

v2 approach (`build_clip_with_preroll`), used when BridgeConfig.recordings_dir
is enabled: MediaMTX continuously records small rolling segments to local
disk the whole time it's pulling the camera stream (see bridge.py's
recording block), auto-deleting old ones after a retention window. When an
event confirms, we stitch together whichever segments cover
[confirm_time - pre_roll_seconds, confirm_time + clip_post_confirm_seconds]
into one final clip - giving a real pre-roll buffer, not a simulated one.
This also means no second RTSP connection is needed at all for clip
purposes, since MediaMTX is already recording continuously in the
background.
"""
from __future__ import annotations

import datetime
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional

SEGMENT_FILENAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-\d+\.mp4$")


def record_clip(stream_url: str, duration_seconds: int, out_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Fallback/simple mode: records `duration_seconds` of the live RTSP stream
    starting immediately (blocking call). No pre-roll - use
    build_clip_with_preroll when continuous recording is enabled instead.
    """
    out_dir = out_dir or Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"nomwatch_clip_{int(time.time())}.mp4"

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", stream_url,
                "-t", str(duration_seconds),
                # Video copied as-is; audio transcoded to AAC because many
                # RTSP cameras (e.g. Tapo) send PCM A-law/mu-law audio, which
                # MP4 can't hold directly - a blanket "-c copy" makes ffmpeg
                # refuse to write the file header at all on those cameras
                # (found via a real live test).
                "-c:v", "copy",
                "-c:a", "aac",
                str(out_path),
            ],
            capture_output=True,
            timeout=duration_seconds + 15,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if not out_path.exists() or out_path.stat().st_size == 0:
        return None
    return out_path


def _segment_start_time(path: Path) -> Optional[float]:
    """Parses the %Y-%m-%d_%H-%M-%S timestamp MediaMTX puts in each segment's filename."""
    match = SEGMENT_FILENAME_RE.search(path.name)
    if not match:
        return None
    try:
        dt = datetime.datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S")
        return dt.timestamp()
    except ValueError:
        return None


def find_segments_covering(recordings_dir: Path, camera_path: str, start_time: float, end_time: float) -> List[Path]:
    """
    Returns the MediaMTX-recorded segment files (sorted chronologically)
    whose time range overlaps [start_time, end_time].
    """
    path_dir = recordings_dir / camera_path
    if not path_dir.exists():
        return []

    candidates = []
    for f in path_dir.glob("*.mp4"):
        seg_start = _segment_start_time(f)
        if seg_start is None:
            continue
        # A segment covers roughly [seg_start, seg_start + segment_duration];
        # since we don't know the exact duration here, over-include slightly
        # by treating anything within a generous window as relevant - a few
        # extra seconds of untrimmed segment is harmless since we trim below.
        if seg_start <= end_time and seg_start >= start_time - 30:
            candidates.append((seg_start, f))

    candidates.sort(key=lambda pair: pair[0])
    return [f for _, f in candidates]


def build_clip_with_preroll(
    recordings_dir: Path,
    camera_path: str,
    confirm_time: float,
    pre_roll_seconds: int,
    post_confirm_seconds: int,
    out_dir: Optional[Path] = None,
) -> Optional[Path]:
    """
    Stitches continuously-recorded segments into one clip spanning
    [confirm_time - pre_roll_seconds, confirm_time + post_confirm_seconds].
    Must be called AFTER post_confirm_seconds has actually elapsed, so the
    trailing segments exist on disk yet.
    """
    window_start = confirm_time - pre_roll_seconds
    window_end = confirm_time + post_confirm_seconds

    segments = find_segments_covering(recordings_dir, camera_path, window_start, window_end)
    if not segments:
        return None

    out_dir = out_dir or Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"nomwatch_clip_{int(confirm_time)}.mp4"

    first_segment_start = _segment_start_time(segments[0]) or window_start
    trim_offset = max(0.0, window_start - first_segment_start)
    total_duration = window_end - window_start

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as list_file:
        for seg in segments:
            # ffmpeg concat demuxer requires escaped single quotes around paths
            list_file.write(f"file '{seg.as_posix()}'\n")
        list_path = Path(list_file.name)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-ss", str(trim_offset),
                "-t", str(total_duration),
                "-c:v", "copy",
                "-c:a", "aac",
                str(out_path),
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        list_path.unlink(missing_ok=True)

    if not out_path.exists() or out_path.stat().st_size == 0:
        return None
    return out_path
