"""Single-clip playback worker.

One QThread per playback tile. Decodes an MP4 file with cv2, scales each
frame to the tile's size on this thread, and emits it as a QImage. Supports
pause, play, seek (millisecond precision via CAP_PROP_POS_MSEC), and speed
multiplier. The thread loops forever once started; the supervisor swaps
clips by calling `load(...)`.

Pacing is deadline-based (decode time is subtracted from the sleep) so
playback holds real-time speed, and frames are dropped — never queued —
when the UI hasn't consumed the previous one (see `ack_frame`).
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
from PySide6.QtCore import QMutex, QMutexLocker, QThread, Signal
from PySide6.QtGui import QImage

from app.core.frames import fit_to, to_qimage
from app.core.log import bus

# How long (s) a sampled box set stays drawn until the next sample. Detection
# runs ~1 fps, so we hold each box briefly to cover the frames in between.
_BOX_HOLD_S = 1.2


def _boxes_at(track: list, pos_s: float):
    """Nearest box set at or just before pos_s, within the hold window."""
    best = None
    best_t = -1.0
    for t, boxes in track:
        if t <= pos_s + 0.05 and t > best_t and (pos_s - t) <= _BOX_HOLD_S:
            best_t = t
            best = boxes
    return best or []


def _draw_overlay(frame, track: list, pos_s: float) -> None:
    for x1, y1, x2, y2, label, conf in _boxes_at(track, pos_s):
        color = (60, 200, 60) if label == "person" else (40, 170, 255)
        p1 = (int(x1), int(y1))
        p2 = (int(x2), int(y2))
        cv2.rectangle(frame, p1, p2, color, 2)
        cv2.putText(frame, f"{label} {conf:.2f}", (p1[0], max(12, p1[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)


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
        # Detection overlay: a clip-relative box track + on/off toggle. Used by
        # the Events view to draw bounding boxes as a recorded event plays.
        self._overlay_track: list | None = None
        self._overlay_on = False
        # Written from the UI thread, read here; GIL-atomic, no mutex needed.
        self._display_w = 0
        self._display_h = 0
        self._frame_in_flight = False

    # Public API (call from GUI thread)

    def set_display_size(self, w: int, h: int) -> None:
        """Tile size for worker-side frame scaling (see app.core.frames)."""
        self._display_w = max(0, int(w))
        self._display_h = max(0, int(h))

    def ack_frame(self) -> None:
        """The UI consumed the last emitted frame; allow the next one."""
        self._frame_in_flight = False

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
            self._speed = max(0.25, min(16.0, s))

    def seek_seconds(self, offset_s: float) -> None:
        with QMutexLocker(self._mutex):
            self._pending_seek_ms = max(0.0, offset_s * 1000.0)

    def set_overlay(self, track: list | None) -> None:
        """Set the clip-relative box track: [(t, [(x1,y1,x2,y2,label,conf)..])..]"""
        with QMutexLocker(self._mutex):
            self._overlay_track = track

    def set_overlay_enabled(self, on: bool) -> None:
        with QMutexLocker(self._mutex):
            self._overlay_on = on

    def request_stop(self) -> None:
        with QMutexLocker(self._mutex):
            self._stop = True

    # Worker loop

    def run(self) -> None:
        bus.info(self._label, "playback player started")
        cap: cv2.VideoCapture | None = None
        frame_interval_ms = 1000.0 / self.DEFAULT_FPS
        last_pos_t = time.monotonic()
        next_due = time.monotonic()  # deadline of the next frame emit

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
                overlay_on = self._overlay_on
                track = self._overlay_track

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
                last_pos_t = time.monotonic()
                next_due = time.monotonic()
                continue

            if cap is None:
                self.msleep(50)
                continue

            if seek_ms is not None:
                cap.set(cv2.CAP_PROP_POS_MSEC, seek_ms)
                next_due = time.monotonic()

            if paused:
                next_due = time.monotonic()
                self.msleep(40)
                continue

            ok, frame = cap.read()
            now = time.monotonic()
            if not ok or frame is None:
                self.state_changed.emit("eof")
                with QMutexLocker(self._mutex):
                    self._paused = True
                continue

            pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            if overlay_on and track:
                # Boxes are in original pixel coords; draw before scaling.
                _draw_overlay(frame, track, pos_ms / 1000.0)
            if not self._frame_in_flight:
                self._frame_in_flight = True
                self.frame_ready.emit(
                    to_qimage(fit_to(frame, self._display_w, self._display_h)))
            if now - last_pos_t >= 0.25:
                self.position_changed.emit(pos_ms / 1000.0)
                last_pos_t = now

            # Deadline pacing: sleep only the remainder after decode/scale, so
            # actual playback speed matches the requested one. If we fell far
            # behind (slow open, debugger pause), resnap instead of bursting.
            next_due += max(0.005, (frame_interval_ms / 1000.0) / max(0.1, speed))
            now = time.monotonic()
            if next_due < now - 0.25:
                next_due = now
            if next_due > now:
                self.msleep(int((next_due - now) * 1000))

        if cap is not None:
            cap.release()
        bus.info(self._label, "playback player stopped")
        self.state_changed.emit("stopped")
