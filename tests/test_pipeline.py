"""Headless Pipeline checks: the backend seam must construct, route events,
and shut down cleanly with no MainWindow at all — that is the point of the
extraction. Telegram is disabled (empty creds), recording and the watchdog
are disabled via env, so nothing touches the network or spawns processes.

Stdlib unittest only; run with:  python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Must be set before Settings.load() (the repo .env loads with override=False,
# so pre-set env vars win). Applied per-setUp, not at import: unittest imports
# every test module before running any test, so module-level assignments from
# sibling modules would leak into ours.
_ENV = {
    "RECORDING_ENABLED": "0",
    "WATCHDOG_ENABLED": "0",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "TELEGRAM_COMMANDS": "0",
}

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.core.cameras import default_cameras
from app.core.config import Settings
from app.core.pipeline import Pipeline


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _fake_clip(tmp: str, cam_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        cam_id=cam_id, cams_captured=[cam_id], start_at=datetime.now(),
        label="person", folder=Path(tmp), in_region=True,
        presence_state="single", presence_seconds=3.0,
        peak_person=1, peak_vehicle=0, thumb_path=None,
    )


class MainWindowWiringTests(unittest.TestCase):
    """Construct the REAL MainWindow against the Pipeline seam and tear it
    down through the real closeEvent. The window is never shown, so no
    stream workers start and nothing touches the DVR or the network."""

    def test_constructs_and_closes_cleanly(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="wh_mw_")
        self.addCleanup(tmp.cleanup)
        os.environ.update(_ENV)
        os.environ["RECORDING_DIR"] = tmp.name
        settings = Settings.load()

        from app.ui.main_window import MainWindow
        win = MainWindow(settings)
        self.assertTrue(win._pipeline._live_thread.isRunning())
        self.assertEqual(len(win._tiles), 4)
        # Exercise the real playback mode switch (transport bar wiring).
        win._playback_view._set_mode("events")
        win._playback_view._set_mode("recordings")
        win.close()  # real closeEvent: tiles -> pipeline -> playback
        self.assertFalse(win._pipeline._live_thread.isRunning())


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wh_pl_")
        self.addCleanup(self._tmp.cleanup)
        os.environ.update(_ENV)
        os.environ["RECORDING_DIR"] = self._tmp.name
        settings = Settings.load()
        self.pipe = Pipeline(settings, default_cameras(), {1, 2, 3, 4},
                             {}, {}, [], {1: "Front"})
        self.addCleanup(self.pipe.shutdown)  # idempotent; safety net

    def test_constructs_headless_with_telegram_off(self) -> None:
        self.assertFalse(self.pipe._notifier.enabled)
        self.assertTrue(self.pipe._live_thread.isRunning())

    def test_event_routes_to_ui_signal_and_single_notify(self) -> None:
        seen: list = []
        self.pipe.event_extracted.connect(seen.append)
        clip = _fake_clip(self._tmp.name)
        self.pipe._on_event_extracted(clip)
        # UI signal fired (gallery refresh path)…
        self.assertEqual(seen, [clip])
        # …and with no camera links the matcher released it immediately
        # (notifier disabled -> no network; nothing pending in the matcher).
        self.assertFalse(self.pipe._collision._pending)

    def test_setters_fan_out_to_live_detector(self) -> None:
        self.pipe.set_armed({2})
        self.assertEqual(self.pipe.live_detector._armed, {2})
        self.pipe.set_regions({3: (0.1, 0.1, 0.9, 0.9)})
        self.assertIn(3, self.pipe.live_detector._regions)
        self.pipe.set_person_floors({4: 0.78})
        self.assertEqual(self.pipe.live_detector._person_floor_by_cam, {4: 0.78})

    def test_start_and_shutdown_clean(self) -> None:
        self.pipe.start()
        _spin(100)
        # Heartbeat seeded; recording disabled -> no recorder scheduled.
        self.assertTrue((Path(self._tmp.name) / ".watchhouse.heartbeat").is_file())
        self.assertIsNone(self.pipe._recorder)
        self.pipe.shutdown()
        self.assertFalse(self.pipe._live_thread.isRunning())


if __name__ == "__main__":
    unittest.main()
