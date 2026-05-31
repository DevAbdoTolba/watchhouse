"""Unit tests for the derived timeout helpers in app.core.config.

The held-event hold-timeout is the one place that *derives* behaviour from
RECORDING_SEGMENT_MINUTES, so it gets a focused test. Stdlib unittest only
(no pytest dependency); run with:  python -m unittest discover -s tests
"""

import os
import unittest

from app.core.config import _event_hold_timeout_s


# Env keys this module reads; saved/restored around every test so cases don't
# leak into each other (or into a real .env-loaded process).
_KEYS = (
    "EVENT_HOLD_TIMEOUT_SECONDS",
    "RECORDING_SEGMENT_MINUTES",
    "RECORDING_RETENTION_MINUTES",
)


class HoldTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _KEYS}
        for k in _KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_tracks_3min_segment(self) -> None:
        # seg=3 -> hold = max(5, 2*3)*60 = 360s; retention 90min leaves room.
        self.assertEqual(_event_hold_timeout_s(), 360.0)

    def test_follows_a_larger_segment(self) -> None:
        os.environ["RECORDING_SEGMENT_MINUTES"] = "15"
        # hold = max(5, 30)*60 = 1800s; retention 90min (5400s) doesn't clamp.
        self.assertEqual(_event_hold_timeout_s(), 1800.0)

    def test_five_minute_floor_for_tiny_segments(self) -> None:
        os.environ["RECORDING_SEGMENT_MINUTES"] = "1"
        # 2*1 = 2 < 5, so the 5-minute floor wins: 300s.
        self.assertEqual(_event_hold_timeout_s(), 300.0)

    def test_explicit_override_wins(self) -> None:
        os.environ["EVENT_HOLD_TIMEOUT_SECONDS"] = "600"
        os.environ["RECORDING_SEGMENT_MINUTES"] = "15"  # ignored when overridden
        self.assertEqual(_event_hold_timeout_s(), 600.0)

    def test_override_floored_at_60s(self) -> None:
        os.environ["EVENT_HOLD_TIMEOUT_SECONDS"] = "10"
        self.assertEqual(_event_hold_timeout_s(), 60.0)

    def test_never_outlives_the_rolling_window(self) -> None:
        # Tiny retention + big segment: hold must be clamped, never exceeding
        # the window, and never below the 60s safety floor.
        os.environ["RECORDING_RETENTION_MINUTES"] = "10"
        os.environ["RECORDING_SEGMENT_MINUTES"] = "15"
        hold = _event_hold_timeout_s()
        self.assertGreaterEqual(hold, 60.0)
        self.assertLessEqual(hold, 10 * 60.0)


if __name__ == "__main__":
    unittest.main()
