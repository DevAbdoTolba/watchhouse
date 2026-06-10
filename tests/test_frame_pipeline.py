"""Behavioral checks for the frame-pipeline lag fixes, run through the REAL
signal chain (real QThreads, real cv2 decoders) on the offscreen Qt platform:

- StreamWorker drops frames instead of queuing them (no ack -> exactly one
  emit), scales display frames to the tile size on its own thread, and taps
  full-res samples for the live-alert tier.
- PlaybackPlayer's deadline pacing holds real-time speed (a 2 s clip plays
  in ~2 s) and honours the same drop-don't-queue mailbox.
- LiveDetector.submit (frame convert + pre-roll JPEG buffering) executes on
  the detector's own thread, never the GUI thread.
- frames.fit_to aspect-fit math.

Stdlib unittest only (no pytest dependency); run with:
    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from app.core.frames import fit_to
from app.core.live_detector import LiveDetector
from app.core.playback_player import PlaybackPlayer
from app.core.stream import StreamWorker
from app.ui.camera_tile import VideoPanel

_MODEL = Path(__file__).resolve().parents[1] / "app" / "resources" / "models" / "yolov8n.onnx"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _spin(ms: int) -> None:
    """Run the (real) event loop for `ms` so queued signals are delivered."""
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


def _make_clip(path: Path, frames: int = 30, fps: float = 15.0,
               size: tuple = (640, 360)) -> Path:
    w, h = size
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert vw.isOpened(), "test clip writer failed to open"
    for i in range(frames):
        img = np.full((h, w, 3), 32, np.uint8)
        cv2.putText(img, str(i), (40, 200), cv2.FONT_HERSHEY_SIMPLEX, 4,
                    (0, 220, 0), 6, cv2.LINE_AA)
        vw.write(img)
    vw.release()
    assert path.is_file() and path.stat().st_size > 0
    return path


class FitToTests(unittest.TestCase):
    def test_shrinks_keeping_aspect(self) -> None:
        frame = np.zeros((360, 640, 3), np.uint8)
        out = fit_to(frame, 320, 320)
        self.assertEqual(out.shape[1], 320)
        self.assertEqual(out.shape[0], 180)

    def test_grows_keeping_aspect(self) -> None:
        frame = np.zeros((360, 640, 3), np.uint8)
        out = fit_to(frame, 1280, 9999)
        self.assertEqual((out.shape[1], out.shape[0]), (1280, 720))

    def test_unknown_target_is_passthrough(self) -> None:
        frame = np.zeros((360, 640, 3), np.uint8)
        self.assertIs(fit_to(frame, 0, 0), frame)


class StreamWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="wh_pipe_")
        cls.clip = _make_clip(Path(cls._tmp.name) / "clip.mp4", frames=300)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _run_worker(self, auto_ack: bool):
        worker = StreamWorker(str(self.clip), label="TEST")
        worker.set_display_size(320, 180)
        frames: list[QImage] = []
        taps: list[QImage] = []

        def on_frame(img: QImage) -> None:
            frames.append(img)
            if auto_ack:
                worker.ack_frame()

        worker.frame_ready.connect(on_frame)
        worker.tap_ready.connect(taps.append)
        worker.start()
        _spin_until(lambda: len(taps) >= 1, timeout_s=5.0)
        _spin(400)  # let the playthrough finish either way
        worker.request_stop()
        self.assertTrue(worker.wait(5000), "worker did not stop")
        return frames, taps

    def test_mailbox_holds_one_frame_without_ack(self) -> None:
        frames, _ = self._run_worker(auto_ack=False)
        # Drop-don't-queue: the UI never acked, so exactly one frame may
        # ever be in flight regardless of how fast the source decodes.
        self.assertEqual(len(frames), 1)

    def test_ack_resumes_flow_and_frames_are_prescaled(self) -> None:
        frames, taps = self._run_worker(auto_ack=True)
        self.assertGreater(len(frames), 2, "acked stream should keep flowing")
        for img in frames:
            self.assertLessEqual(img.width(), 320)
            self.assertLessEqual(img.height(), 180)
        # 640x360 fit into 320x180 is exactly 320x180.
        self.assertEqual((frames[0].width(), frames[0].height()), (320, 180))
        # The detector tap stays full-resolution.
        self.assertEqual((taps[0].width(), taps[0].height()), (640, 360))


class PlaybackPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="wh_pipe_")
        # 30 frames @ 15 fps = 2.0 s of video.
        cls.clip = _make_clip(Path(cls._tmp.name) / "clip.mp4",
                              frames=30, fps=15.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_realtime_pacing_and_scaling(self) -> None:
        player = PlaybackPlayer(label="PBT")
        player.set_display_size(320, 180)
        frames: list[QImage] = []
        states: list[str] = []

        def on_frame(img: QImage) -> None:
            frames.append(img)
            player.ack_frame()

        player.frame_ready.connect(on_frame)
        player.state_changed.connect(states.append)
        player.start()
        player.load(self.clip)
        t0 = time.monotonic()
        self.assertTrue(_spin_until(lambda: "eof" in states, timeout_s=10.0),
                        f"no eof; states={states}")
        elapsed = time.monotonic() - t0
        player.request_stop()
        self.assertTrue(player.wait(5000), "player did not stop")
        # Deadline pacing: a 2.0 s clip must take ~2 s — not 2 s plus a per-
        # frame overhead tax (slow-motion drift), not a no-sleep burst.
        self.assertGreater(elapsed, 1.5, f"played too fast: {elapsed:.2f}s")
        self.assertLess(elapsed, 3.5, f"played too slow: {elapsed:.2f}s")
        self.assertTrue(frames)
        self.assertEqual((frames[0].width(), frames[0].height()), (320, 180))


class _Tap(QObject):
    sig = Signal(int, QImage)


class LiveDetectorThreadingTests(unittest.TestCase):
    @unittest.skipUnless(_MODEL.is_file(), "yolov8n.onnx not present")
    def test_submit_runs_off_the_gui_thread(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="wh_live_")
        self.addCleanup(tmp.cleanup)
        det = LiveDetector(_MODEL, armed={1}, clips_dir=Path(tmp.name),
                           quick_clip_enabled=True, pre_roll_s=2.0)
        self.assertTrue(det.enabled)
        thread = QThread()
        det.moveToThread(thread)
        thread.start()
        try:
            buffer_threads: list[int] = []
            orig = det._buffer_frame

            def spying_buffer(cam_id, frame):
                buffer_threads.append(threading.get_ident())
                return orig(cam_id, frame)

            det._buffer_frame = spying_buffer

            tap = _Tap()
            tap.sig.connect(det.submit)  # queued: det lives on `thread`
            img = QImage(640, 360, QImage.Format.Format_BGR888)
            img.fill(0xFF202020)
            tap.sig.emit(1, img)

            self.assertTrue(_spin_until(lambda: buffer_threads, timeout_s=5.0),
                            "submit never ran on the detector thread")
            self.assertNotEqual(buffer_threads[0], threading.get_ident(),
                                "frame buffering ran on the GUI thread")
            self.assertTrue(det._buffers.get(1), "pre-roll buffer is empty")
            # Let the pooled inference task drain before tearing down.
            self.assertTrue(_spin_until(lambda: not det._busy, timeout_s=20.0),
                            "inference never completed")
            # PERF surface: the latency window recorded the run.
            self.assertTrue(_spin_until(lambda: det.perf_snapshot()[0] >= 1,
                                        timeout_s=5.0),
                            "inference latency not recorded")
        finally:
            thread.quit()
            self.assertTrue(thread.wait(5000))


class _FileCamera:
    """Camera stand-in whose RTSP URL is a local file, so the real tile +
    worker chain runs without a DVR."""

    def __init__(self, path: Path) -> None:
        self.index = 1
        self.label = "CAM 01"
        self.location = "Test bench"
        self._path = str(path)

    def url(self, _stream: str, _settings) -> str:
        return self._path


class _FakeSettings:
    detection_cameras = (1,)


class CameraTileChainTests(unittest.TestCase):
    """The full live wiring: StreamWorker -> CameraTile -> VideoPanel paint
    + frame_tapped (the LiveDetector feed MainWindow subscribes to)."""

    def test_tile_renders_and_taps(self) -> None:
        from app.ui.camera_tile import CameraTile

        tmp = tempfile.TemporaryDirectory(prefix="wh_tile_")
        self.addCleanup(tmp.cleanup)
        clip = _make_clip(Path(tmp.name) / "clip.mp4", frames=300)

        tile = CameraTile(_FileCamera(clip), _FakeSettings(), "sub")
        taps: list[tuple] = []
        tile.frame_tapped.connect(lambda cam, img: taps.append((cam, img)))
        tile.show()
        tile.resize(400, 300)
        _spin(50)
        tile.start()
        self.addCleanup(lambda: tile.shutdown(wait_ms=5000))

        self.assertTrue(_spin_until(lambda: taps, timeout_s=5.0),
                        "frame_tapped never fired through the tile")
        cam, img = taps[0]
        self.assertEqual(cam, 1)
        self.assertEqual((img.width(), img.height()), (640, 360))  # full-res
        # The panel received a frame and painted it (no message overlay).
        self.assertIsNotNone(tile._video._image)
        self.assertLessEqual(tile._video._image.width(),
                             tile._video.width())  # pre-scaled in the worker
        # PERF surface: the (!) tooltip reports shown fps next to source fps.
        self.assertIn("shown", tile._info_text())
        self.assertGreater(tile._shown_fps(), 0.0)


class VideoPanelTests(unittest.TestCase):
    def test_blits_prescaled_and_survives_mismatch(self) -> None:
        panel = VideoPanel()
        sizes: list[tuple] = []
        panel.resized.connect(lambda w, h: sizes.append((w, h)))
        panel.show()  # hidden widgets don't receive resize events
        panel.resize(320, 180)
        _spin(50)
        self.assertIn((320, 180), sizes)

        prescaled = QImage(320, 180, QImage.Format.Format_BGR888)
        prescaled.fill(0xFF112233)
        panel.set_frame(prescaled)
        self.assertEqual(panel.grab().width(), 320)  # exercises paintEvent

        # A full-res frame right after a resize (worker not caught up yet)
        # must still paint via the transient fallback, not crash or distort.
        full = QImage(640, 360, QImage.Format.Format_BGR888)
        full.fill(0xFF332211)
        panel.set_frame(full)
        self.assertEqual(panel.grab().height(), 180)


if __name__ == "__main__":
    unittest.main()
