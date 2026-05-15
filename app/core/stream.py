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


class StreamWorker(QThread):
    frame_ready = Signal(QImage)
    status_changed = Signal(str)  # "connecting" | "online" | "reconnecting" | "stopped"

    INITIAL_BACKOFF_S = 1.0
    MAX_BACKOFF_S = 30.0
    READ_TIMEOUT_S = 5.0
    TARGET_INTERVAL_MS = 33  # ~30 FPS upper bound; the source rate caps below this

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self._mutex = QMutex()
        self._url = url
        self._stop = False
        self._reopen = False

    def set_url(self, url: str) -> None:
        with QMutexLocker(self._mutex):
            if url == self._url:
                return
            self._url = url
            self._reopen = True

    def request_stop(self) -> None:
        with QMutexLocker(self._mutex):
            self._stop = True
            self._reopen = True  # break inner read loop too

    def run(self) -> None:
        backoff = self.INITIAL_BACKOFF_S
        while True:
            with QMutexLocker(self._mutex):
                if self._stop:
                    break
                url = self._url
                self._reopen = False

            self.status_changed.emit("connecting")
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            if not cap.isOpened():
                cap.release()
                self.status_changed.emit("reconnecting")
                self._sleep_backoff(backoff)
                backoff = min(backoff * 2.0, self.MAX_BACKOFF_S)
                continue

            backoff = self.INITIAL_BACKOFF_S
            self.status_changed.emit("online")

            last_frame_at = time.monotonic()
            while True:
                with QMutexLocker(self._mutex):
                    if self._stop or self._reopen:
                        break

                ok, frame = cap.read()
                now = time.monotonic()

                if not ok or frame is None:
                    if now - last_frame_at > self.READ_TIMEOUT_S:
                        break
                    self.msleep(20)
                    continue

                last_frame_at = now
                h, w = frame.shape[:2]
                stride = frame.strides[0]
                # Copy detaches the QImage from the numpy buffer, so the
                # array can be safely overwritten on the next read.
                qimg = QImage(frame.data, w, h, stride, QImage.Format.Format_BGR888).copy()
                self.frame_ready.emit(qimg)
                self.msleep(self.TARGET_INTERVAL_MS)

            cap.release()

            with QMutexLocker(self._mutex):
                if self._stop:
                    break
                if self._reopen:
                    continue

            self.status_changed.emit("reconnecting")
            self._sleep_backoff(backoff)
            backoff = min(backoff * 2.0, self.MAX_BACKOFF_S)

        self.status_changed.emit("stopped")

    def _sleep_backoff(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with QMutexLocker(self._mutex):
                if self._stop or self._reopen:
                    return
            self.msleep(100)
