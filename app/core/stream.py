"""RTSP stream worker. One QThread per camera tile.

Decodes frames with OpenCV (FFmpeg backend, TCP transport via the env flags
set in app/__init__.py), scales each frame to the tile's size on this thread,
emits it as a QImage to the UI, and reconnects with exponential backoff on
failure.

Two rules keep the live view honest:

- The read loop never sleeps: `cap.read()` blocks until the camera delivers
  the next frame, so the source paces us. Any extra sleep makes us consume
  slower than the camera produces, FFmpeg's buffer backs up, and the picture
  drifts seconds behind live.
- Frames are dropped, never queued: a frame is only emitted after the UI
  acknowledged the previous one (`ack_frame`). A busy UI gets fewer frames,
  not a growing backlog of stale ones.
"""

from __future__ import annotations

import time

import cv2
from PySide6.QtCore import QMutex, QMutexLocker, QThread, Signal
from PySide6.QtGui import QImage

from app.core.frames import fit_to, to_qimage
from app.core.log import bus, mask_url


class StreamWorker(QThread):
    frame_ready = Signal(QImage)  # scaled to the display size on this thread
    tap_ready = Signal(QImage)    # full-res sample for the live-alert tier
    status_changed = Signal(str)  # "connecting" | "online" | "reconnecting" | "stopped"
    stats_changed = Signal(float, int, int)  # measured fps, width, height

    INITIAL_BACKOFF_S = 1.0
    MAX_BACKOFF_S = 30.0
    READ_TIMEOUT_S = 5.0
    TAP_INTERVAL_S = 0.5  # full-res sample rate feeding the live detector

    def __init__(self, url: str, label: str = "STRM", parent=None) -> None:
        super().__init__(parent)
        self._mutex = QMutex()
        self._url = url
        self._stop = False
        self._reopen = False
        self._label = label
        # Plain ints/bools written from the UI thread, read here; assignment
        # is atomic under the GIL so no mutex is needed.
        self._display_w = 0
        self._display_h = 0
        self._frame_in_flight = False

    def set_display_size(self, w: int, h: int) -> None:
        """Tell the worker the tile's current size so frames are scaled here,
        off the UI thread. Called from the UI on resize."""
        self._display_w = max(0, int(w))
        self._display_h = max(0, int(h))

    def ack_frame(self) -> None:
        """The UI consumed the last emitted frame; allow the next one."""
        self._frame_in_flight = False

    def set_url(self, url: str) -> None:
        with QMutexLocker(self._mutex):
            if url == self._url:
                return
            self._url = url
            self._reopen = True
        bus.info(self._label, f"stream switched to {mask_url(url)}")

    def request_stop(self) -> None:
        with QMutexLocker(self._mutex):
            self._stop = True
            self._reopen = True  # break inner read loop too

    def request_reopen(self) -> None:
        """Soft reconnect: drop the current cap and reopen the same URL.

        Cheaper than stopping the thread and creating a new worker, and the
        natural fit for the RECONNECT ALL button on a healthy stream that's
        merely stalled.
        """
        with QMutexLocker(self._mutex):
            if self._stop:
                return
            self._reopen = True
        bus.info(self._label, "reopen requested")

    def run(self) -> None:
        bus.info(self._label, f"worker started for {mask_url(self._url)}")
        backoff = self.INITIAL_BACKOFF_S
        while True:
            with QMutexLocker(self._mutex):
                if self._stop:
                    break
                url = self._url
                self._reopen = False

            self.status_changed.emit("connecting")
            t_open = time.monotonic()
            bus.info(self._label, f"opening {mask_url(url)}")
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            elapsed = (time.monotonic() - t_open) * 1000
            if not cap.isOpened():
                bus.error(
                    self._label,
                    f"VideoCapture.isOpened()=False after {elapsed:.0f} ms. "
                    f"Backing off {backoff:.1f}s.",
                )
                cap.release()
                self.status_changed.emit("reconnecting")
                self._sleep_backoff(backoff)
                backoff = min(backoff * 2.0, self.MAX_BACKOFF_S)
                continue

            bus.info(self._label, f"VideoCapture opened in {elapsed:.0f} ms")
            backoff = self.INITIAL_BACKOFF_S
            self.status_changed.emit("online")

            last_frame_at = time.monotonic()
            first_frame_seen = False
            last_tap = 0.0
            stat_count = 0
            stat_t0 = time.monotonic()
            while True:
                with QMutexLocker(self._mutex):
                    if self._stop or self._reopen:
                        break

                ok, frame = cap.read()
                now = time.monotonic()

                if not ok or frame is None:
                    if now - last_frame_at > self.READ_TIMEOUT_S:
                        bus.warn(
                            self._label,
                            f"no frame received for {now - last_frame_at:.1f}s, dropping connection",
                        )
                        break
                    self.msleep(20)
                    continue

                last_frame_at = now
                h, w = frame.shape[:2]
                if not first_frame_seen:
                    first_frame_seen = True
                    bus.info(self._label, f"first frame {w}x{h}")

                if not self._frame_in_flight:
                    self._frame_in_flight = True
                    self.frame_ready.emit(
                        to_qimage(fit_to(frame, self._display_w, self._display_h)))

                # Full-res sample for the live-alert tier, ~2/s. Display frames
                # are tile-sized, so the detector gets its own undegraded tap.
                if now - last_tap >= self.TAP_INTERVAL_S:
                    last_tap = now
                    self.tap_ready.emit(to_qimage(frame))

                # Measured source FPS over ~1s windows - the live "is HQ
                # keeping up?" signal surfaced in the tile's (!) badge.
                stat_count += 1
                dt = now - stat_t0
                if dt >= 1.0:
                    self.stats_changed.emit(stat_count / dt, w, h)
                    stat_count = 0
                    stat_t0 = now

            cap.release()

            with QMutexLocker(self._mutex):
                if self._stop:
                    break
                if self._reopen:
                    bus.info(self._label, "reopening with new URL")
                    continue

            self.status_changed.emit("reconnecting")
            bus.info(self._label, f"reconnect in {backoff:.1f}s")
            self._sleep_backoff(backoff)
            backoff = min(backoff * 2.0, self.MAX_BACKOFF_S)

        bus.info(self._label, "worker stopped")
        self.status_changed.emit("stopped")

    def _sleep_backoff(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with QMutexLocker(self._mutex):
                if self._stop or self._reopen:
                    return
            self.msleep(100)
