"""UiLoopProbe behavioral checks: it must measure event-loop stalls and
report them on the PERF channel (WARN when a tick crossed the stall
threshold). The stall path is driven deterministically (synthetic late
ticks) so the test can't flake on machine load; a live spin covers the
real timer wiring. Stdlib unittest only; run with:
    python -m unittest discover -s tests
"""

import os
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.core.log import bus
from app.core.perf import UiLoopProbe


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class UiLoopProbeTests(unittest.TestCase):
    def _capture(self) -> list:
        entries: list = []
        bus.entry.connect(entries.append)
        self.addCleanup(lambda: bus.entry.disconnect(entries.append))
        return entries

    def test_stalled_tick_reports_warn(self) -> None:
        entries = self._capture()
        probe = UiLoopProbe(report_ticks=2)
        # Synthetic late tick: pretend the previous tick was 950 ms ago, so
        # this one is ~450 ms past its 500 ms schedule — a real stall.
        probe._last = time.monotonic() - 0.95
        probe._tick()
        self.assertGreater(probe.worst_lag_ms, 200.0)
        probe._tick()  # second tick completes the window -> report

        perf = [e for e in entries if e.source == "PERF"]
        self.assertTrue(perf, "no PERF report emitted")
        self.assertTrue(any(e.level == "WARN" and "ui-loop lag" in e.message
                            for e in perf),
                        f"stall not flagged WARN: {[e.message for e in perf]}")
        self.assertEqual(probe.worst_lag_ms, 0.0)  # window reset after report

    def test_live_timer_drives_a_report(self) -> None:
        entries = self._capture()
        probe = UiLoopProbe(report_ticks=3)
        probe.start()
        self.assertTrue(probe._timer.isActive())
        probe.start()  # idempotent (showEvent fires repeatedly)
        _spin(2300)    # > 3 ticks at 500 ms
        probe.stop()
        self.assertFalse(probe._timer.isActive())

        perf = [e for e in entries if e.source == "PERF"]
        self.assertTrue(perf, "live timer never produced a report")
        self.assertIn("ui-loop lag", perf[0].message)


if __name__ == "__main__":
    unittest.main()
