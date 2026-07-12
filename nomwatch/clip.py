"""
Post-confirmation clip recording.

v1 scope: starts recording from the moment feeding is CONFIRMED (i.e. once
poll_stream's debounce has already fired), not before. True pre-roll (a clip
that reaches back a few seconds before the first positive poll) would require
MediaMTX to be continuously recording rolling segments to disk so a clip can
be stitched from (confirm_time - buffer) forward - that's a real, doable
feature, just more plumbing than a live RTSP pull allows on its own. Tracked
as a fast-follow, not built here.

Caveat: this opens a second RTSP connection to the camera, separate from the
one poll_stream/capture_frame uses for detection. Most consumer cameras
(including the Tapo C120) support at least 2 concurrent RTSP clients, but if
recording ever interferes with detection polling, that's the first place to
look.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def record_clip(stream_url: str, duration_seconds: int, out_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Records `duration_seconds` of the live RTSP stream to an mp4 file,
    starting immediately (blocking call - runs for the full duration).
    Returns the path to the recorded file, or None if recording failed.
    """
    out_dir = out_dir or Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"nomwatch_clip_{int(__import__('time').time())}.mp4"

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", stream_url,
                "-t", str(duration_seconds),
                "-c", "copy",  # no re-encode - fast, low CPU, matches source codec
                str(out_path),
            ],
            capture_output=True,
            timeout=duration_seconds + 15,  # grace period beyond the recording itself
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if not out_path.exists() or out_path.stat().st_size == 0:
        return None
    return out_path
