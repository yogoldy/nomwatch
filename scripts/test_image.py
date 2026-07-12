#!/usr/bin/env python3
"""
Standalone test: feed any local image file straight to the configured Ollama
vision model and print its feeding-event judgment - no camera, no ffmpeg,
no NomWatch config required. Useful for sanity-checking the model's
detection quality in isolation, e.g. against a stock photo of a pet eating,
before worrying about camera framing/positioning.

Usage:
    python3 scripts/test_image.py path/to/photo.jpg
    python3 scripts/test_image.py path/to/photo.jpg --model gemma3:4b
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly from a repo checkout without installing
# the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nomwatch.detection import DEFAULT_VISION_MODEL, OllamaVisionDetector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Path to a local image file (jpg/png)")
    parser.add_argument("--model", default=DEFAULT_VISION_MODEL, help="Ollama model tag to use")
    parser.add_argument("--host", default="http://localhost:11434", help="Ollama server URL")
    parser.add_argument("--min-confidence", type=float, default=0.6)
    args = parser.parse_args()

    if not args.image.exists():
        print(f"File not found: {args.image}")
        sys.exit(1)

    frame_bytes = args.image.read_bytes()
    print(f"Loaded {len(frame_bytes)} bytes from {args.image}")
    print(f"Asking {args.model} ...\n")

    detector = OllamaVisionDetector(
        model=args.model,
        host=args.host,
        min_confidence=args.min_confidence,
    )
    event = detector.check_frame(frame_bytes)

    if event:
        print(f"✅ FEEDING EVENT detected - confidence {event.confidence:.2f}")
        print(f"   Reason: {event.reasoning}")
    else:
        print("❌ No feeding event detected (or confidence below threshold).")
        print("   Tip: rerun with a lower --min-confidence to see the raw judgment if it's borderline.")


if __name__ == "__main__":
    main()
