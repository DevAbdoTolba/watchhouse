"""Real-time alert tier: detect on the LIVE preview frames.

This is the fast, instant-notification path, separate from the segment
analyzer. The segment analyzer is inherently delayed (it only sees a
recording segment after it finalizes, up to the segment length later); this
tier taps the frames already decoded for the on-screen tiles, samples them at
a low rate, runs the same yolov8n detector, and fires an alert the moment a
person/vehicle appears - so "someone is here NOW" arrives in seconds.

Design:
- Frames arrive (throttled to ~2 fps) on the GUI thread via each tile's
  `frame_tapped` signal. We convert the newest one to BGR and hand it to a
  single-slot QThreadPool runnable for inference, dropping frames while a
  detection is already in flight (so CPU never piles up).
- Only ARMED cameras are processed (mirrors the segment-tier arming).
- Per-camera cooldown debounces a lingering person into one alert.
- On a fresh detection we draw a thumbnail and emit `live_alert`; MainWindow
  routes that to the Telegram notifier's instant path.

Nothing here touches recording or the segment pipeline - it is purely additive.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage

from app.core.detect import Detector
from app.core.log import bus


def _qimage_to_bgr(image: QImage):
    """QImage -> contiguous HxWx3 BGR uint8 ndarray (a copy). None on failure."""
    import numpy as np

    try:
        img = image.convertToFormat(QImage.Format.Format_BGR888)
        w, h, bpl = img.width(), img.height(), img.bytesPerLine()
        if w <= 0 or h <= 0:
            return None
        buf = img.constBits()
        arr = np.frombuffer(buf, dtype=np.uint8, count=h * bpl).reshape(h, bpl)
        return arr[:, : w * 3].reshape(h, w, 3).copy()
    except Exception:
        return None


def _title(persons: int, vehicles: dict[str, int]) -> str:
    parts: list[str] = []
    if persons:
        parts.append("Person" if persons == 1 else f"{persons} persons")
    for label, n in vehicles.items():
        parts.append(label if n == 1 else f"{n} {label}s")
    return ", ".join(parts) if parts else "Movement"


def _draw_thumb(frame, dets, max_width: int = 640):
    import cv2

    img = frame.copy()
    h, w = img.shape[:2]
    scale = 1.0
    if w > max_width:
        scale = max_width / float(w)
        img = cv2.resize(img, (max_width, int(round(h * scale))),
                         interpolation=cv2.INTER_AREA)
    for d in dets:
        color = (60, 200, 60) if d.is_person else (40, 170, 255)
        p1 = (int(d.x1 * scale), int(d.y1 * scale))
        p2 = (int(d.x2 * scale), int(d.y2 * scale))
        cv2.rectangle(img, p1, p2, color, 2)
        cv2.putText(img, f"{d.label} {d.confidence:.2f}",
                    (p1[0], max(12, p1[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return img


class _Signals(QObject):
    done = Signal(int, object, object)  # cam_id, frame_bgr, detections


class _InferTask(QRunnable):
    def __init__(self, detector: Detector, cam_id: int, frame, signals: _Signals):
        super().__init__()
        self._detector = detector
        self._cam_id = cam_id
        self._frame = frame
        self._signals = signals

    def run(self) -> None:
        try:
            dets = self._detector.detect(self._frame)
        except Exception as e:
            bus.warn("LIVE", f"detect failed: {e!s}")
            dets = []
        self._signals.done.emit(self._cam_id, self._frame, dets)


class LiveDetector(QObject):
    """Real-time person/vehicle alerts off the live preview frames."""

    # cam_id, title, thumb_path
    live_alert = Signal(int, str, str)

    def __init__(self, model_path: Path, armed: set[int],
                 conf: float = 0.40, cooldown_s: float = 45.0,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._detector = Detector(model_path, conf_threshold=conf)
        self._armed = set(armed)
        self._cooldown = max(1.0, cooldown_s)
        self._pool = QThreadPool.globalInstance()
        self._signals = _Signals()
        self._signals.done.connect(self._on_done)
        self._busy = False                      # one inference in flight at a time
        self._last_alert: dict[int, float] = {}
        self._thumb_dir = Path(tempfile.gettempdir()) / "watchhouse_live"
        try:
            self._thumb_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._thumb_dir = Path(tempfile.gettempdir())
        self.enabled = self._detector.available
        if not self.enabled:
            bus.warn("LIVE", "model missing; real-time alerts disabled")
        else:
            bus.info("LIVE", f"real-time alerts ready (conf={conf:g}, "
                             f"cooldown={self._cooldown:g}s, armed={sorted(self._armed)})")

    def set_armed(self, armed: set[int]) -> None:
        self._armed = set(armed)

    @Slot(int, QImage)
    def submit(self, cam_id: int, image: QImage) -> None:
        if not self.enabled or self._busy or cam_id not in self._armed:
            return
        frame = _qimage_to_bgr(image)
        if frame is None:
            return
        self._busy = True
        self._pool.start(_InferTask(self._detector, cam_id, frame, self._signals))

    @Slot(int, object, object)
    def _on_done(self, cam_id: int, frame, dets) -> None:
        self._busy = False
        if not dets:
            return
        persons = sum(1 for d in dets if d.is_person)
        vehicles: dict[str, int] = {}
        for d in dets:
            if d.is_vehicle:
                vehicles[d.label] = vehicles.get(d.label, 0) + 1
        if not persons and not vehicles:
            return
        now = time.monotonic()
        if now - self._last_alert.get(cam_id, 0.0) < self._cooldown:
            return  # debounce a lingering subject into one alert
        self._last_alert[cam_id] = now

        title = _title(persons, vehicles)
        thumb_path = ""
        try:
            import cv2
            out = self._thumb_dir / f"live_cam{cam_id}_{int(now * 1000)}.jpg"
            if cv2.imwrite(str(out), _draw_thumb(frame, dets)):
                thumb_path = str(out)
        except Exception as e:
            bus.warn("LIVE", f"thumb write failed: {e!s}")
        bus.info("LIVE", f"cam{cam_id}: {title} (real-time)")
        self.live_alert.emit(cam_id, title, thumb_path)
