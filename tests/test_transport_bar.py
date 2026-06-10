"""TransportBar behavioral checks: scrub math, speed presets, step signal —
the contract PlaybackView relies on after the R3 extraction. Offscreen,
real widgets. Stdlib unittest only; run with:
    python -m unittest discover -s tests
"""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.transport_bar import TransportBar


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class TransportBarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bar = TransportBar(boxes_on=True)

    def test_scrub_math_round_trip(self) -> None:
        seeks: list[float] = []
        self.bar.seek_requested.connect(seeks.append)
        self.bar.set_duration(20.0)
        self.bar.sync_scrub(10.0)
        self.assertEqual(self.bar._scrub.value(), 500)   # 10s of 20s = 500‰
        self.assertEqual(self.bar._scrub_cur.text(), "00:10")
        # User drags to 75% -> a 15.0s seek intent.
        self.bar._on_scrub_moved(750)
        self.assertEqual(seeks, [15.0])
        # Mid-drag, position syncs must not fight the user's hand.
        self.bar._on_scrub_pressed()
        self.bar.sync_scrub(3.0)
        self.assertNotEqual(self.bar._scrub_cur.text(), "00:03")
        self.bar._on_scrub_released()
        self.assertFalse(self.bar.is_scrubbing())
        self.bar.pin_scrub_to_end()
        self.assertEqual(self.bar._scrub.value(), 1000)

    def test_speed_presets_and_clamping(self) -> None:
        speeds: list[float] = []
        self.bar.speed_changed.connect(speeds.append)
        self.bar._step_speed(1)
        self.assertEqual(speeds[-1], 2.0)
        self.bar.set_speed(3.0)              # snaps to nearest preset
        self.assertEqual(speeds[-1], 2.0)
        for _ in range(10):                  # never beyond the last preset
            self.bar._step_speed(1)
        self.assertEqual(self.bar.speed(), 16.0)
        self.assertFalse(self.bar._speed_up.isEnabled())

    def test_step_signal_uses_active_step_size(self) -> None:
        steps: list[int] = []
        self.bar.step_requested.connect(steps.append)
        self.bar.set_step(15)
        self.bar.step_requested.emit(-self.bar._step_seconds)  # back button path
        self.assertEqual(steps, [-15])
        self.assertTrue(self.bar._step_buttons[15].isChecked())
        self.assertFalse(self.bar._step_buttons[5].isChecked())

    def test_reset_and_duration_gate(self) -> None:
        self.bar.set_duration(8.0)
        self.assertTrue(self.bar._scrub.isEnabled())
        self.bar.reset_scrub()
        self.assertFalse(self.bar._scrub.isEnabled())
        seeks: list[float] = []
        self.bar.seek_requested.connect(seeks.append)
        self.bar._on_scrub_moved(500)  # no duration -> no seek emitted
        self.assertEqual(seeks, [])


if __name__ == "__main__":
    unittest.main()
