#!/usr/bin/env python3
"""
Simulates poll_stream()'s debounce logic against a scripted sequence of
fake classification results - no camera, no Ollama, no real time delay.
Useful for verifying "requires N consecutive positives, fires once per
visit, and only re-arms after several negatives" without staging a real
feeding event.

Usage:
    python3 scripts/simulate_poll.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nomwatch.detection import ClassificationResult, OllamaVisionDetector, poll_stream

# Scripted sequence of poll results: (is_feeding, confidence, reason)
# Mirrors a realistic session: idle -> cat approaches (ambiguous) -> real
# 20s+ eating streak -> cat leaves -> idle -> a SECOND separate feeding later.
SCRIPT = [
    (False, 0.1, "empty bowl"),
    (False, 0.2, "no pet visible"),
    (True, 0.6, "cat is nearby, ambiguous"),   # single ambiguous positive - should NOT fire alone
    (False, 0.3, "cat walked away"),
    (True, 0.85, "cat is eating"),             # streak starts
    (True, 0.9, "cat is eating"),              # streak = 2 -> should fire here (consecutive_required=2)
    (True, 0.88, "cat is still eating"),       # streak continues, must NOT fire again
    (True, 0.82, "cat is still eating"),
    (False, 0.2, "cat finished, walked off"),
    (False, 0.15, "empty bowl"),
    (False, 0.15, "empty bowl"),              # 3 negatives -> re-arm
    (True, 0.7, "cat is back eating again"),   # new streak starts
    (True, 0.75, "cat is back eating again"),  # streak = 2 -> should fire a SECOND event
]


def main():
    detector = OllamaVisionDetector(model="fake-model", min_confidence=0.6)
    call_count = {"n": 0}

    def fake_classify(self, frame_bytes):
        i = call_count["n"]
        call_count["n"] += 1
        if i >= len(SCRIPT):
            return ClassificationResult(is_feeding=False, confidence=0.0, reason="script exhausted", raw_text="")
        is_feeding, confidence, reason = SCRIPT[i]
        return ClassificationResult(is_feeding=is_feeding, confidence=confidence, reason=reason, raw_text="")

    with patch.object(OllamaVisionDetector, "classify", fake_classify), \
         patch("nomwatch.detection.capture_frame", return_value=b"fake-frame-bytes"), \
         patch("time.sleep", return_value=None):

        events = []
        gen = poll_stream(
            "rtsp://fake",
            detector=detector,
            motion_gating=False,
            interval_seconds=10,
            consecutive_required=2,
            rearm_after_negative_polls=3,
        )
        for _ in range(len(SCRIPT) + 2):  # a couple extra ticks past the script, harmless
            event = next(gen)
            events.append(event)
            print(f"Event fired: confidence={event.confidence:.2f} reason='{event.reasoning}'")
            if len(events) >= 2:  # we expect exactly 2 events from this script
                break

    print(f"\nTotal events fired: {len(events)} (expected: 2)")
    if len(events) == 2:
        print("PASS - debounce logic fired once per continuous feeding streak, as expected.")
    else:
        print("FAIL - unexpected number of events. Check poll_stream() debounce logic.")


if __name__ == "__main__":
    main()
