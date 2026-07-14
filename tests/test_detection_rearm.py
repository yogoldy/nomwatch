"""Regression tests for one-alert-per-feeding-visit behavior."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nomwatch import monitorlock
from nomwatch.detection import ClassificationResult, poll_stream


class ScriptedDetector:
    def __init__(self, results):
        self.results = iter(results)
        self.min_confidence = 0.6

    def classify(self, frame_bytes):
        is_feeding, confidence, reason = next(self.results)
        return ClassificationResult(is_feeding, confidence, reason, "")


class DetectionRearmTests(unittest.TestCase):
    def test_single_negative_does_not_split_one_feeding_visit(self):
        # First yes fires. The following no is a bad frame, so the next yes
        # must remain part of that same visit. Three real negatives re-arm
        # the detector, allowing the final yes to create the next event.
        detector = ScriptedDetector([
            (True, 0.8, "cat at bowl"),
            (False, 0.9, "blurred frame"),
            (True, 0.8, "cat at bowl"),
            (False, 0.9, "cat gone"),
            (False, 0.9, "cat gone"),
            (False, 0.9, "cat gone"),
            (True, 0.8, "cat returned"),
        ])
        statuses = []

        with patch("nomwatch.detection.capture_frame", return_value=b"frame"), \
             patch("nomwatch.detection.time.sleep", return_value=None):
            events = poll_stream(
                "rtsp://fake",
                detector=detector,
                motion_gating=False,
                interval_seconds=1,
                consecutive_required=1,
                rearm_after_negative_polls=3,
                on_poll=statuses.append,
            )
            first = next(events)
            second = next(events)

        self.assertEqual(first.reasoning, "cat at bowl")
        self.assertEqual(second.reasoning, "cat returned")
        self.assertEqual(
            [status["is_feeding"] for status in statuses],
            [True, False, True, False, False, False, True],
        )


class RunLockTests(unittest.TestCase):
    def test_monitor_lock_excludes_a_second_loop(self):
        with tempfile.TemporaryDirectory() as tempdir:
            lock_path = Path(tempdir) / "run.lock"
            with patch.object(monitorlock, "RUN_LOCK_PATH", lock_path):
                with monitorlock.run_loop_lock() as first_acquired:
                    self.assertTrue(first_acquired)
                    self.assertTrue(monitorlock.run_loop_locked())
                    with monitorlock.run_loop_lock() as second_acquired:
                        self.assertFalse(second_acquired)
                self.assertFalse(monitorlock.run_loop_locked())


if __name__ == "__main__":
    unittest.main()
