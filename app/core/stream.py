"""RTSP stream worker. One QThread per camera tile.

Decodes frames with OpenCV (FFmpeg backend, TCP transport via the env flags
set in app/__init__.py), emits each decoded frame as a QImage to the UI,
and reconnects with exponential backoff on failure.
"""

from __future__ import annotations

import time

import cv2
from PySide6.QtCore import QMutex, QMutexLocker, QThread, Signal
from PySide6.QtGui import QImage

from app.core.log import bus, mask_url


class StreamWorker(QThread):
    frame_ready = Signal(QImage)
    status_changed = Signal(str)  # "connecting" | "online" | "reconnecting" | "stopped"

    INITIAL_BACKOFF_S = 1.0
    MAX_BACKOFF_S = 30.0
    READ_TIMEOUT_S = 5.0
    TARGET_INTERVAL_MS = 33  # ~30 FPS upper bound; the source rate caps below this

    def __init__(self, url: str, label: str = "STRM", parent=None) -> None:
        super().__init__(parent)
        self._mutex = QMutex()
        self._url = url
        self._stop = False
        self._reopen = False
        self._label = label

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

    def run(self) -> None:
        bus.info(self._label, f"worker started for {mask_url(self._url)}")
        backoff = self.INITIAL_BACKOFF_S
        first_frame_seen = False
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
                if not first_frame_seen:
                    first_frame_seen = True
                    h0, w0 = frame.shape[:2]
                    bus.info(self._label, f"first frame {w0}x{h0}")
                h, w = frame.shape[:2]
                stride = frame.strides[0]
                qimg = QImage(frame.data, w, h, stride, QImage.Format.Format_BGR888).copy()
                self.frame_ready.emit(qimg)
                self.msleep(self.TARGET_INTERVAL_MS)

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
