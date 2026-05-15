"""Single-clip playback worker.

One QThread per playback tile. Decodes an MP4 file with cv2 and emits
each frame as a QImage. Supports pause, play, seek (millisecond precision
via CAP_PROP_POS_MSEC), and speed multiplier. The thread loops forever
once started; the supervisor swaps clips by calling `load(...)`.
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
from PySide6.QtCore import QMutex, QMutexLocker, QThread, Signal
from PySide6.QtGui import QImage

from app.core.log import bus


class PlaybackPlayer(QThread):
    frame_ready = Signal(QImage)
    position_changed = Signal(float)  # seconds into current clip
    duration_known = Signal(float)    # seconds; emitted once per clip
    state_changed = Signal(str)       # "loading" | "playing" | "paused" | "eof" | "error" | "empty"

    DEFAULT_FPS = 15.0

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self._mutex = QMutex()
        self._stop = False
        self._paused = True
        self._speed = 1.0
        self._clip_path: Path | None = None
        self._pending_seek_ms: float | None = None
        self._load_request = False
        self._duration_ms = 0.0
        self._label = label

    # Public API (call from GUI thread)

    def load(self, clip_path: Path | None, start_offset_s: float = 0.0) -> None:
        with QMutexLocker(self._mutex):
            self._clip_path = clip_path
            self._pending_seek_ms = max(0.0, start_offset_s * 1000.0)
            self._load_request = True
            self._paused = False  # auto-play on load

    def play(self) -> None:
        with QMutexLocker(self._mutex):
            self._paused = False

    def pause(self) -> None:
        with QMutexLocker(self._mutex):
            self._paused = True

    def toggle(self) -> None:
        with QMutexLocker(self._mutex):
            self._paused = not self._paused

    def set_speed(self, s: float) -> None:
        with QMutexLocker(self._mutex):
            self._speed = max(0.25, min(8.0, s))

    def seek_seconds(self, offset_s: float) -> None:
        with QMutexLocker(self._mutex):
            self._pending_seek_ms = max(0.0, offset_s * 1000.0)

    def request_stop(self) -> None:
        with QMutexLocker(self._mutex):
            self._stop = True

    # Worker loop

    def run(self) -> None:
        bus.info(self._label, "playback player started")
        cap: cv2.VideoCapture | None = None
        frame_interval_ms = 1000.0 / self.DEFAULT_FPS
        last_emit_t = time.monotonic()

        while True:
            with QMutexLocker(self._mutex):
                if self._stop:
                    break
                load_req = self._load_request
                self._load_request = False
                clip = self._clip_path
                seek_ms = self._pending_seek_ms
                self._pending_seek_ms = None
                paused = self._paused
                speed = self._speed

            if load_req:
                if cap is not None:
                    cap.release()
                    cap = None
                if clip is None:
                    self.state_changed.emit("empty")
                    self.msleep(50)
                    continue
                bus.info(self._label, f"loading clip {clip.name}")
                self.state_changed.emit("loading")
                cap = cv2.VideoCapture(str(clip), cv2.CAP_FFMPEG)
                if not cap.isOpened():
                    bus.warn(self._label, f"could not open {clip.name}")
                    self.state_changed.emit("error")
                    cap.release()
                    cap = None
                    self.msleep(100)
                    continue
                fps = cap.get(cv2.CAP_PROP_FPS) or self.DEFAULT_FPS
                if fps <= 1.0 or fps > 240:
                    fps = self.DEFAULT_FPS
                frame_interval_ms = 1000.0 / fps
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                self._duration_ms = (frame_count / fps) * 1000.0 if frame_count else 0.0
                self.duration_known.emit(self._duration_ms / 1000.0)
                if seek_ms and seek_ms > 0:
                    cap.set(cv2.CAP_PROP_POS_MSEC, seek_ms)
                self.state_changed.emit("playing" if not paused else "paused")
                last_emit_t = time.monotonic()
                continue

            if cap is None:
                self.msleep(50)
                continue

            if seek_ms is not None:
                cap.set(cv2.CAP_PROP_POS_MSEC, seek_ms)

            if paused:
                self.msleep(40)
                continue

            ok, frame = cap.read()
            now = time.monotonic()
            if not ok or frame is None:
                self.state_changed.emit("eof")
                with QMutexLocker(self._mutex):
                    self._paused = True
                continue

            h, w = frame.shape[:2]
            stride = frame.strides[0]
            qimg = QImage(frame.data, w, h, stride, QImage.Format.Format_BGR888).copy()
            self.frame_ready.emit(qimg)
            pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            if now - last_emit_t >= 0.25:
                self.position_changed.emit(pos_ms / 1000.0)
                last_emit_t = now

            target_interval_ms = max(5.0, frame_interval_ms / max(0.1, speed))
            self.msleep(int(target_interval_ms))

        if cap is not None:
            cap.release()
        bus.info(self._label, "playback player stopped")
        self.state_changed.emit("stopped")
