"""EventsSidebar behavioral checks through the real widget: scan lifecycle,
day/confidence/camera filtering, and the follow-latest-day jump policy —
the contract PlaybackView relies on after the R3 extraction.

Stdlib unittest only; run with:  python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.core.cameras import default_cameras
from app.core.config import Settings
from app.core.event_library import EventRecord
from app.ui.events_sidebar import EventsSidebar


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _spin_until(predicate, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        _spin(50)
    return predicate()


def _event(day: date, cam: int = 1, conf: float = 0.9,
           sid: str = "") -> EventRecord:
    when = datetime.combine(day, datetime.min.time()).replace(hour=12)
    return EventRecord(
        start_at=when, trigger_cam=cam, label="person_x1",
        peak_person=1, peak_vehicle=0, person_conf=conf, vehicle_conf=0.0,
        folder=Path(f"D:/x/{day}/cam{cam}/{sid or conf}"), thumb=None,
        duration_s=10.0, session_id=sid or f"s-{day}-{cam}-{conf}",
    )


class EventsSidebarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wh_es_")
        self.addCleanup(self._tmp.cleanup)
        os.environ["RECORDING_DIR"] = self._tmp.name
        self.sidebar = EventsSidebar(default_cameras(), Settings.load())
        self.addCleanup(self.sidebar.shutdown)
        self.changed: list = []
        self.jumps: list = []
        self.sidebar.sessions_changed.connect(self.changed.append)
        self.sidebar.day_jump_requested.connect(self.jumps.append)

    def test_real_scan_lifecycle_on_empty_tree(self) -> None:
        self.sidebar.refresh()  # real EventsScanWorker on an empty events dir
        self.assertTrue(_spin_until(lambda: self.changed, timeout_s=8.0),
                        "scan never completed / sessions_changed never fired")
        self.assertEqual(self.changed[-1], [])
        self.assertIn("(0)", self.sidebar._heading.text())

    def test_follow_latest_jumps_to_newest_day(self) -> None:
        d_old, d_new = date(2026, 6, 9), date(2026, 6, 10)
        self.sidebar.set_day(d_old)
        self.changed.clear()
        # Following (the default at startup): a scan revealing a newer day
        # must request the jump instead of silently filtering the old day.
        self.sidebar._on_scanned([_event(d_old), _event(d_new)], [])
        self.assertEqual(self.jumps, [d_new])
        # The view answers by moving the calendar and calling set_day.
        self.sidebar.set_day(d_new)
        self.assertEqual(len(self.changed[-1]), 1)
        self.assertEqual(self.changed[-1][0].start_at.date(), d_new)

    def test_browsing_history_disables_the_jump(self) -> None:
        d_old, d_new = date(2026, 6, 9), date(2026, 6, 10)
        self.sidebar._on_scanned([_event(d_old), _event(d_new)], [])
        self.jumps.clear()
        self.sidebar.user_selected_day(d_old)  # deliberate history browse
        self.sidebar._on_scanned([_event(d_old), _event(d_new)], [])
        self.assertEqual(self.jumps, [], "history browsing must not be yanked")
        # Returning to the newest day resumes following.
        self.sidebar.user_selected_day(d_new)
        self.sidebar._on_scanned([_event(d_new), _event(date(2026, 6, 11))], [])
        self.assertEqual(self.jumps, [date(2026, 6, 11)])

    def test_confidence_and_camera_filters(self) -> None:
        d = date(2026, 6, 10)
        events = [_event(d, cam=1, conf=0.9, sid="a"),
                  _event(d, cam=2, conf=0.3, sid="b")]
        self.sidebar._follow_latest_day = False
        self.sidebar.set_day(d)
        self.sidebar._on_scanned(events, [])
        self.assertEqual(len(self.changed[-1]), 2)
        # Min-human 60% drops the 0.3-confidence session.
        idx = self.sidebar._conf_combo.findData(0.60)
        self.sidebar._conf_combo.setCurrentIndex(idx)
        self.assertEqual(len(self.changed[-1]), 1)
        self.assertEqual(self.changed[-1][0].trigger_cam, 1)
        # Camera filter: drop cam 1 too -> nothing left.
        self.sidebar._on_cam_filter_toggled(1, False)
        self.assertEqual(self.changed[-1], [])


if __name__ == "__main__":
    unittest.main()
