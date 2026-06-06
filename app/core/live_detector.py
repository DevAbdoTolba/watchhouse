"""Real-time alert tier + instant quick-clip (pre-event ring buffer).

Two jobs, both off the LIVE preview frames the tiles already decode - so
nothing here touches the recorder or the 15-minute segment analyzer (those
remain the full-quality, authoritative evidence path):

1. Instant alert: sample frames, run yolov8n, fire `live_alert` (photo+title)
   the moment a person/vehicle appears - seconds, not minutes.

2. Instant quick-clip (the pre-event buffer pattern used by Blue Iris /
   Frigate / Milestone): every armed camera's recent frames are kept in a
   small rolling in-memory buffer (downscaled JPEG, tiny RAM). On a detection
   we take that pre-roll, keep appending for a post-roll window, and encode a
   ~30s clip on a worker thread - saved as a normal event folder and emitted
   via `quick_clip_ready` so it can be pushed to Telegram in ~30s instead of
   waiting up to 15 minutes for a segment to finalize.

The fast path is best-effort: if a quick clip fails to encode, the alert still
went out and the segment tier still produces the full clip later.
"""

from __future__ import annotations

import tempfile
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from app.core.detect import Detector
from app.core import detect_regions as dr
from app.core.log import bus


def _qimage_to_bgr(image: QImage):
    """QImage -> contiguous HxWx3 BGR uint8 ndarray (a copy). None on failure."""
    import numpy as np

    try:
        img = image.convertToFormat(QImage.Format.Format_BGR888)
        w, h = img.width(), img.height()
        if w <= 0 or h <= 0:
            return None
        ptr = img.constBits()
        arr = np.frombuffer(ptr, dtype=np.uint8, count=h * img.bytesPerLine())
        arr = arr.reshape(h, img.bytesPerLine())[:, : w * 3].reshape(h, w, 3)
        return arr.copy()
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


class _EncodeSignals(QObject):
    ready = Signal(int, str)  # cam_id, event_folder ("" on failure)


class _EncodeTask(QRunnable):
    """Encode captured JPEG frames into a quick-clip event folder, off the UI
    thread. Writes cam<N>.mp4 + thumb.jpg + event.json so the events list, the
    playback view and the Telegram reply path all treat it like any event."""

    def __init__(self, cam_id: int, jpegs: list, fps: float, events_dir: Path,
                 peak_person: int, peak_vehicle: int, person_conf: float,
                 vehicle_conf: float, start_at: datetime, thumb_bgr,
                 signals: _EncodeSignals) -> None:
        super().__init__()
        self._cam_id = cam_id
        self._jpegs = jpegs
        self._fps = max(1.0, fps)
        self._events_dir = Path(events_dir)
        self._pp = peak_person
        self._pv = peak_vehicle
        self._pc = person_conf
        self._vc = vehicle_conf
        self._start_at = start_at
        self._thumb = thumb_bgr
        self._signals = signals

    def run(self) -> None:
        folder = ""
        try:
            folder = self._encode()
        except Exception as e:
            bus.warn("LIVE", f"quick-clip encode raised: {e!s}")
            folder = ""
        self._signals.ready.emit(self._cam_id, folder)

    def _encode(self) -> str:
        import json

        import cv2
        import numpy as np

        from app.core.events import _unique_dir, label_for

        if not self._jpegs:
            return ""
        frames = []
        for b in self._jpegs:
            img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                frames.append(img)
        if not frames:
            return ""
        h, w = frames[0].shape[:2]
        frames = [f if f.shape[:2] == (h, w)
                  else cv2.resize(f, (w, h), interpolation=cv2.INTER_AREA)
                  for f in frames]

        label = label_for(self._pp, self._pv)
        day = self._start_at.strftime("%Y-%m-%d")
        cam_dir = self._events_dir / day / f"cam{self._cam_id}"
        cam_dir.mkdir(parents=True, exist_ok=True)
        folder = _unique_dir(cam_dir, f"{self._start_at:%H-%M-%S}_live_{label}")
        folder.mkdir(parents=True, exist_ok=True)

        out = folder / f"cam{self._cam_id}.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        vw = cv2.VideoWriter(str(out), fourcc, self._fps, (w, h))
        if not vw.isOpened():
            return ""
        for f in frames:
            vw.write(f)
        vw.release()
        if not (out.is_file() and out.stat().st_size > 0):
            return ""

        if self._thumb is not None:
            try:
                cv2.imwrite(str(folder / "thumb.jpg"), self._thumb)
            except Exception:
                pass

        dur = len(frames) / self._fps
        meta = {
            "start_at": self._start_at.isoformat(timespec="seconds"),
            "trigger_cam": self._cam_id,
            "detecting_cams": [self._cam_id],
            "label": label,
            "peak_person": self._pp,
            "peak_vehicle": self._pv,
            "person_conf": round(self._pc, 4),
            "vehicle_conf": round(self._vc, 4),
            "duration_s": round(dur, 2),
            "cams": [self._cam_id],
            "live": True,
            "tracks": [],
        }
        try:
            (folder / "event.json").write_text(json.dumps(meta, indent=2),
                                               encoding="utf-8")
        except OSError:
            pass
        return str(folder)


class LiveDetector(QObject):
    """Real-time person/vehicle alerts + instant quick-clip off live frames."""

    live_alert = Signal(int, str, str)      # cam_id, title, thumb_path
    quick_clip_ready = Signal(int, str)     # cam_id, event_folder

    _BUFFER_WIDTH = 960  # downscale buffered frames so RAM stays bounded

    def __init__(self, model_path: Path, armed: set[int],
                 conf: float = 0.40, cooldown_s: float = 45.0,
                 clips_dir: Path | None = None,
                 quick_clip_enabled: bool = True,
                 pre_roll_s: float = 10.0, post_roll_s: float = 20.0,
                 max_clip_s: float = 30.0, clip_retention: int = 5000,
                 person_conf: float = 0.0, min_box_frac: float = 0.0,
                 person_conf_by_cam: dict | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._detector = Detector(model_path, conf_threshold=conf,
                                  min_person_conf=person_conf,
                                  min_box_frac=min_box_frac)
        # Per-camera 'person' floor for cameras with a confident static FP.
        self._person_floor_by_cam = dict(person_conf_by_cam or {})
        self._armed = set(armed)
        self._cooldown = max(1.0, cooldown_s)
        # Quick clips live in their OWN dir (NOT events/), so the Events gallery -
        # which scans events/ - stays pure segment-tier (4 cams, dynamic length,
        # stitched). These are Telegram-only, reply-fetchable, retention-bounded.
        self._clips_dir = Path(clips_dir) if clips_dir else None
        self._clip_retention = max(1, int(clip_retention))
        self._quick = quick_clip_enabled and self._clips_dir is not None
        self._pre_roll = max(1.0, pre_roll_s)
        # post_roll is now the grace AFTER movement stops; max_clip is the hard
        # ceiling so a long presence is one capped clip, not an endless one.
        self._post_roll = max(1.0, post_roll_s)
        self._max_clip = max(self._post_roll + 1.0, max_clip_s)
        self._pool = QThreadPool.globalInstance()
        self._signals = _Signals()
        self._signals.done.connect(self._on_done)
        self._encode_signals = _EncodeSignals()
        self._encode_signals.ready.connect(self._on_encoded)
        self._busy = False                      # one inference in flight at a time
        self._last_alert: dict[int, float] = {}
        # Per-camera detect rectangle (normalized). A live detection only counts
        # if its box overlaps the camera's region; segment events are unaffected.
        self._regions: dict[int, tuple] = {}
        # Pre-event ring buffer + in-flight captures, per camera.
        self._buffers: dict[int, deque] = {}    # cam -> deque[(monotonic_t, jpeg)]
        self._capturing: dict[int, dict] = {}   # cam -> capture state
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
                             f"cooldown={self._cooldown:g}s, armed={sorted(self._armed)}, "
                             f"quick_clip={'on' if self._quick else 'off'} "
                             f"pre={self._pre_roll:g}s grace={self._post_roll:g}s "
                             f"max={self._max_clip:g}s dynamic)")

    def set_armed(self, armed: set[int]) -> None:
        self._armed = set(armed)

    def set_regions(self, regions: dict) -> None:
        """{cam_id: (x0,y0,x1,y1)} normalized detect rectangles (live tier)."""
        self._regions = dict(regions or {})

    @Slot(int, QImage)
    def submit(self, cam_id: int, image: QImage) -> None:
        if not self.enabled or cam_id not in self._armed:
            return
        frame = _qimage_to_bgr(image)
        if frame is None:
            return
        # Buffer EVERY frame (not gated by inference), so the pre-roll is intact.
        if self._quick:
            self._buffer_frame(cam_id, frame)
        if self._busy:
            return
        self._busy = True
        self._pool.start(_InferTask(self._detector, cam_id, frame, self._signals))

    # --- Pre-event ring buffer ---

    def _buffer_frame(self, cam_id: int, frame) -> None:
        import cv2

        try:
            h, w = frame.shape[:2]
            if w > self._BUFFER_WIDTH:
                s = self._BUFFER_WIDTH / float(w)
                frame = cv2.resize(frame, (self._BUFFER_WIDTH, int(round(h * s))),
                                   interpolation=cv2.INTER_AREA)
            ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ok:
                return
            jpeg = enc.tobytes()
        except Exception:
            return
        now = time.monotonic()
        buf = self._buffers.setdefault(cam_id, deque())
        buf.append((now, jpeg))
        # Evict frames older than the pre-roll window.
        cutoff = now - self._pre_roll
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        # Feed an in-flight capture too. While movement keeps extending `until`
        # the frames accumulate; once movement stops and `until` passes, the
        # next frame closes the clip -> dynamic length that matches the movement.
        cap = self._capturing.get(cam_id)
        if cap is not None:
            if now <= cap["until"]:
                cap["jpegs"].append((now, jpeg))
            else:
                self._finalize_capture(cam_id)

    @Slot(int, object, object)
    def _on_done(self, cam_id: int, frame, dets) -> None:
        self._busy = False
        if not dets:
            return
        # Per-camera 'person' floor: drop low-confidence people on cameras with a
        # confident static false positive (e.g. cam4's bin bags peak ~0.76).
        floor = self._person_floor_by_cam.get(cam_id, 0.0)
        if floor:
            dets = [d for d in dets
                    if not (d.is_person and d.confidence < floor)]
            if not dets:
                return
        # Detect-zone filter: keep only detections overlapping this camera's
        # rectangle, so an edge sliver (e.g. just the toes) doesn't fire.
        region = self._regions.get(cam_id)
        if region is not None and frame is not None:
            h, w = frame.shape[:2]
            if w and h:
                dets = [d for d in dets if dr.overlaps(
                    region, d.x1 / w, d.y1 / h, d.x2 / w, d.y2 / h)]
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

        # Dynamic clip: while movement keeps being seen, hold the clip open -
        # push the close time to grace (post_roll) after THIS sighting, capped at
        # max_clip total. This runs even during the photo cooldown, so a long
        # presence grows ONE clip instead of a fixed window or a flood of clips.
        cap = self._capturing.get(cam_id)
        if cap is not None:
            cap["until"] = min(now + self._post_roll,
                               cap["start_mono"] + self._max_clip)
            cap["pp"] = max(cap["pp"], persons)
            cap["pv"] = max(cap["pv"], sum(vehicles.values()))
            cap["pc"] = max(cap["pc"],
                            max((d.confidence for d in dets if d.is_person), default=0.0))
            cap["vc"] = max(cap["vc"],
                            max((d.confidence for d in dets if d.is_vehicle), default=0.0))

        if now - self._last_alert.get(cam_id, 0.0) < self._cooldown:
            return  # debounce a lingering subject into one alert (photo + new clip)
        self._last_alert[cam_id] = now

        title = _title(persons, vehicles)
        thumb_bgr = _draw_thumb(frame, dets)
        thumb_path = ""
        try:
            import cv2
            out = self._thumb_dir / f"live_cam{cam_id}_{int(now * 1000)}.jpg"
            if cv2.imwrite(str(out), thumb_bgr):
                thumb_path = str(out)
        except Exception as e:
            bus.warn("LIVE", f"thumb write failed: {e!s}")
        bus.info("LIVE", f"cam{cam_id}: {title} (real-time)")
        self.live_alert.emit(cam_id, title, thumb_path)

        if self._quick and cam_id not in self._capturing:
            self._start_capture(cam_id, persons, vehicles, dets, thumb_bgr)

    # --- Quick-clip capture ---

    def _start_capture(self, cam_id: int, persons: int, vehicles: dict,
                       dets, thumb_bgr) -> None:
        now = time.monotonic()
        pre = list(self._buffers.get(cam_id, ()))  # snapshot of pre-roll frames
        pc = max((d.confidence for d in dets if d.is_person), default=0.0)
        vc = max((d.confidence for d in dets if d.is_vehicle), default=0.0)
        self._capturing[cam_id] = {
            "jpegs": pre,                       # (t, jpeg); movement appends here
            "start_mono": now,                  # monotonic start, for the max cap
            "until": now + self._post_roll,     # extended on each fresh sighting
            "start_at": datetime.now(),
            "pp": persons,
            "pv": sum(vehicles.values()),
            "pc": pc,
            "vc": vc,
            "thumb": thumb_bgr,
        }
        # Normal close is frame-driven (in _buffer_frame, when movement stops and
        # `until` passes). This timer is only a backstop so a stalled live stream
        # can't leave the clip open forever.
        backstop_ms = int((self._max_clip + self._post_roll + 1.0) * 1000)
        QTimer.singleShot(backstop_ms, lambda c=cam_id: self._finalize_capture(c))

    def _finalize_capture(self, cam_id: int) -> None:
        cap = self._capturing.pop(cam_id, None)
        if not cap:
            return
        frames = cap["jpegs"]
        if len(frames) < 2:
            bus.warn("LIVE", f"cam{cam_id}: quick clip skipped (too few frames)")
            return
        ts = [t for t, _ in frames]
        span = max(0.5, ts[-1] - ts[0])
        fps = max(1.0, min(30.0, len(frames) / span))
        jpegs = [b for _, b in frames]
        self._pool.start(_EncodeTask(
            cam_id, jpegs, fps, self._clips_dir,
            cap["pp"], cap["pv"], cap["pc"], cap["vc"],
            cap["start_at"], cap["thumb"], self._encode_signals,
        ))

    @Slot(int, str)
    def _on_encoded(self, cam_id: int, folder: str) -> None:
        if folder:
            bus.info("LIVE", f"cam{cam_id}: quick clip ready -> {folder}")
            self.quick_clip_ready.emit(cam_id, folder)
            self._prune_live_clips()
        else:
            bus.warn("LIVE", f"cam{cam_id}: quick clip encode failed")

    def _prune_live_clips(self) -> None:
        """Keep only the most recent `clip_retention` quick-clip folders so the
        live_clips dir stays bounded (they're Telegram-only, not gallery events)."""
        if not self._clips_dir or not self._clips_dir.is_dir():
            return
        try:
            folders = [p for p in self._clips_dir.glob("*/cam*/*") if p.is_dir()]
            if len(folders) <= self._clip_retention:
                return
            folders.sort(key=lambda p: p.stat().st_mtime)
            import shutil
            for old in folders[:len(folders) - self._clip_retention]:
                shutil.rmtree(old, ignore_errors=True)
        except OSError as e:
            bus.warn("LIVE", f"live-clip prune failed: {e!s}")
