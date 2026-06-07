"""SegmentAnalyzer: object detection over finalized recording segments.

When the recorder opens a new segment for a camera, the previous one is
finalized on disk (ffmpeg writes the moov atom on segment close). The
RecorderSupervisor emits `segment_closed(path)` for it; we enqueue the
path and process segments one at a time on this worker thread: open with
cv2, sample one frame every `sample_seconds` of footage, run the ONNX
detector, and aggregate per-segment detection counts.

v0.4.0 only *reported* findings (console + a running tally for the status
bar). v0.4.2 turns detections into extracted event clips + snapshots: while
scanning, person/vehicle frames are streamed into `events.PendingEvent`
accumulators that merge continuous presence (so a stationary person is one
event, not hundreds), and each completed event is cut into a margin-padded
clip with a thumbnail under recordings/events/.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
from PySide6.QtCore import QThread, Signal

from app.core import detect_regions as dr
from app.core import detlog
from app.core import events as evt
from app.core.detect import Detector
from app.core.event_stitcher import EventStitcher, SegmentScan
from app.core.log import bus


def _cam_from_path(path: Path) -> int:
    name = path.parent.name
    if name.startswith("cam"):
        try:
            return int(name[3:])
        except ValueError:
            pass
    return 0


def _seg_start_from_path(path: Path) -> datetime | None:
    """Segments are named `2026-05-29T14-15-01.mp4` - the wall-clock start."""
    try:
        return datetime.strptime(path.stem, "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None


@dataclass
class SegmentResult:
    path: Path
    cam_id: int
    frames_sampled: int
    person_frames: int   # sampled frames containing >=1 person
    vehicle_frames: int
    max_persons: int     # peak person count in a single sampled frame
    max_vehicles: int
    elapsed_s: float
    events_extracted: int = 0

    @property
    def had_activity(self) -> bool:
        return self.person_frames > 0 or self.vehicle_frames > 0


class SegmentAnalyzer(QThread):
    segment_analyzed = Signal(object)   # SegmentResult
    totals_changed = Signal(int, int)   # cumulative person_segments, vehicle_segments
    event_extracted = Signal(object)    # events.EventClip

    def __init__(
        self,
        model_path: Path,
        conf: float = 0.35,
        person_conf: float = 0.0,
        person_conf_by_cam: dict | None = None,
        min_box_frac: float = 0.0,
        sample_seconds: float = 1.0,
        event_cfg: "evt.EventConfig | None" = None,
        events_dir: Path | None = None,
        recording_dir: Path | None = None,
        cam_ids: list[int] | None = None,
        armed: set[int] | None = None,
        max_duration_s: float = 120.0,
        hold_timeout_s: float = 1800.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._detector = Detector(model_path, conf_threshold=conf,
                                  min_person_conf=person_conf,
                                  min_box_frac=min_box_frac)
        # Per-camera 'person' floor for cameras with a confident static FP
        # (e.g. cam4's bin bags). Applied here since the Detector is shared.
        self._person_floor_by_cam = dict(person_conf_by_cam or {})
        self._sample_seconds = max(0.2, sample_seconds)
        self._event_cfg = event_cfg or evt.EventConfig()
        self._max_duration_s = max_duration_s
        self._hold_timeout_s = hold_timeout_s
        self._events_dir = events_dir
        self._recording_dir = recording_dir
        self._cam_ids = list(cam_ids) if cam_ids else []
        # Cameras that may *trigger* events. None = all armed (the default
        # on startup). Set live from the UI; reads are atomic enough.
        self._armed = set(armed) if armed is not None else None
        # Per-camera detect box (normalized rect). Mirrors the live tier: it
        # does NOT change what gets recorded (full evidence), only flags whether
        # an event's activity touched the box, so the notifier can skip pushes
        # for out-of-box activity. Empty = no boxes (everything counts).
        self._regions: dict[int, tuple] = {}
        self._queue: "queue.Queue[Path | None]" = queue.Queue()
        self._stop = False
        self._person_segments = 0
        self._vehicle_segments = 0
        # Cross-segment carry-state. The writer is bound to this analyzer's
        # output config; emit pushes finalized clips out via the Qt signal.
        # A lock guards the stitcher because flush_all() can be called from the
        # UI thread (window close) while the worker thread is otherwise idle.
        self._stitch_lock = threading.Lock()
        self._stitcher = EventStitcher(
            cfg=self._event_cfg,
            writer=self._write_chain,
            emit=self._on_clip,
            max_duration_s=self._max_duration_s,
            hold_timeout_s=self._hold_timeout_s,
        )

    def set_armed(self, armed: set[int]) -> None:
        """Update which cameras trigger event extraction (live, thread-safe)."""
        self._armed = set(armed)

    def set_regions(self, regions: dict) -> None:
        """Update per-camera detect boxes (live, thread-safe). Same boxes the
        live tier uses; here they only flag whether an event touched the box."""
        self._regions = dict(regions or {})

    def _in_region(self, cam_id: int, frame, dets: list) -> bool:
        """True if any person/vehicle detection overlaps this camera's box, or
        the camera has no box. Used only to flag the event for notification -
        recording stays full-frame regardless."""
        region = self._regions.get(cam_id)
        if region is None:
            return True
        h, w = frame.shape[:2]
        if not (w and h):
            return True
        return any(
            dr.overlaps(region, d.x1 / w, d.y1 / h, d.x2 / w, d.y2 / h)
            for d in dets if d.is_person or d.is_vehicle
        )

    def _write_chain(self, parts: list, presence_state: str = "single",
                     presence_seconds: float = 0.0,
                     session_id: str = "", session_index: int = 0,
                     session_final: bool = False) -> "evt.EventClip | None":
        try:
            return evt.write_event_chain(
                parts, self._events_dir, self._event_cfg,
                self._recording_dir, self._cam_ids,
                presence_state=presence_state,
                presence_seconds=presence_seconds,
                session_id=session_id,
                session_index=session_index,
                session_final=session_final,
            )
        except Exception as e:
            name = parts[0].path.name if parts else "?"
            bus.error("EVT", f"event chain write failed near {name}: {e!s}")
            return None

    def _on_clip(self, clip) -> None:
        if clip is None:
            return
        span = clip.duration_s
        cams = "+".join(f"cam{c}" for c in clip.cams_captured) or "none"
        bus.info(
            "EVT",
            f"cam{clip.cam_id} triggered {clip.start_at:%H:%M:%S} {clip.label}  "
            f"{span:.0f}s clip  captured [{cams}]  {clip.folder.name}",
        )
        self.event_extracted.emit(clip)

    def flush_all(self) -> None:
        """Finalize every held (open) event. Called on analyzer stop and from
        the main window's close handler BEFORE the recorder stops, so a tail
        with no following segment still becomes an event."""
        with self._stitch_lock:
            try:
                self._stitcher.flush_all()
            except Exception as e:
                bus.error("EVT", f"flush_all failed: {e!s}")

    def enqueue(self, path: str) -> None:
        if not self._stop:
            self._queue.put(Path(path))

    def request_stop(self) -> None:
        self._stop = True
        self._queue.put(None)  # unblock the get()

    def run(self) -> None:
        if not self._detector.available:
            bus.warn(
                "AI",
                f"detector model not found ({self._detector._model_path.name}); "
                "analysis disabled for this session",
            )
            return
        bus.info("AI", "segment analyzer started (yolov8n, CPU)")
        while not self._stop:
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if path is None:
                break
            try:
                self._analyze(path)
            except Exception as e:  # never let one bad file kill the thread
                bus.error("AI", f"analyze failed for {path.name}: {e!s}")
        # Drain any still-open held events so the last tail isn't lost on stop.
        self.flush_all()
        bus.info("AI", "segment analyzer stopped")

    def _analyze(self, path: Path) -> None:
        if not path.is_file():
            return
        cam_id = _cam_from_path(path)
        # Disarmed cameras still record (rolling buffer) and can be pulled into
        # another camera's event, but they never *trigger* detection here.
        armed = self._armed
        if armed is not None and cam_id not in armed:
            return
        seg_start = _seg_start_from_path(path)
        t0 = time.monotonic()
        cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            bus.warn("AI", f"cam{cam_id}: could not open {path.name} for analysis")
            cap.release()
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        step = max(1, int(round(fps * self._sample_seconds))) if fps > 0 else 25
        eff_step_s = (step / fps) if fps > 0 else self._sample_seconds

        extract = (
            self._event_cfg.enabled
            and self._events_dir is not None
            and self._recording_dir is not None
            and seg_start is not None
            and cam_id > 0
        )
        # Hits closer than the merge gap (plus one sampling interval of slack)
        # are treated as the same continuous presence.
        merge_gap = self._event_cfg.merge_gap_s + eff_step_s
        pending: evt.PendingEvent | None = None
        completed: list[evt.PendingEvent] = []

        frames_sampled = person_frames = vehicle_frames = 0
        max_persons = max_vehicles = 0

        person_floor = self._person_floor_by_cam.get(cam_id, 0.0)
        # Detection-history accumulators (written once per segment to detlog).
        seen_kept: list[float] = []
        seen_camfloor: list[float] = []
        seen_global: list[float] = []

        def _consume(frame, offset_s: float) -> None:
            nonlocal pending, frames_sampled, person_frames, vehicle_frames
            nonlocal max_persons, max_vehicles
            dets, rejected = self._detector.detect(frame, with_rejected=True)
            for d, _reason in rejected:
                if d.is_person:
                    seen_global.append(d.confidence)
            if person_floor:
                survivors = []
                for d in dets:
                    if d.is_person and d.confidence < person_floor:
                        seen_camfloor.append(d.confidence)
                    else:
                        survivors.append(d)
                dets = survivors
            for d in dets:
                if d.is_person:
                    seen_kept.append(d.confidence)
            n_person = sum(1 for d in dets if d.is_person)
            n_vehicle = sum(1 for d in dets if d.is_vehicle)
            frames_sampled += 1
            person_frames += 1 if n_person else 0
            vehicle_frames += 1 if n_vehicle else 0
            max_persons = max(max_persons, n_person)
            max_vehicles = max(max_vehicles, n_vehicle)
            if extract and (n_person or n_vehicle):
                if pending is not None and (offset_s - pending.t_end) > merge_gap:
                    completed.append(pending)
                    pending = None
                if pending is None:
                    pending = evt.PendingEvent(
                        cam_id=cam_id, t_start=offset_s, t_end=offset_s
                    )
                pending.add(offset_s, n_person, n_vehicle, frame.copy(), dets)
                # Recording stays full-frame; this only marks whether the
                # activity touched the detect box, so the notifier can skip
                # out-of-box pushes (the event is still saved to the gallery).
                if self._in_region(cam_id, frame, dets):
                    pending.in_region = True

        idx = 0
        last_idx = -1
        last_sampled_idx = -1
        while not self._stop:
            if not cap.grab():
                break
            last_idx = idx
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    offset_s = (idx / fps) if fps > 0 else (idx * self._sample_seconds)
                    _consume(frame, offset_s)
                    last_sampled_idx = idx
            idx += 1

        # Tail-loss guard (Part A): the regular stride skips the frames between
        # the last sampled multiple of `step` and EOF, so a detection in the
        # final second could be missed entirely. Re-read the very last decoded
        # frame and sample it too. We seek rather than reuse the buffer because
        # the loop's exit `grab()` already failed (nothing left to retrieve).
        if not self._stop and last_idx >= 0 and last_idx != last_sampled_idx:
            tail_frame = None
            if cap.set(cv2.CAP_PROP_POS_FRAMES, float(last_idx)):
                ok, tail_frame = cap.read()
                if not (ok and tail_frame is not None):
                    tail_frame = None
            if tail_frame is not None:
                offset_s = (last_idx / fps) if fps > 0 else (last_idx * self._sample_seconds)
                _consume(tail_frame, offset_s)

        cap.release()
        if pending is not None:
            completed.append(pending)

        # Duration from actual frames decoded (EOF inclusive), not a sampled
        # estimate - so the clip extractor's EOF check lines up with reality.
        total_frames = last_idx + 1 if last_idx >= 0 else 0
        duration_s = (total_frames / fps) if fps > 0 else (total_frames * self._sample_seconds)

        events_extracted = 0
        if extract and seg_start is not None:
            scan = SegmentScan(
                cam_id=cam_id, path=path, seg_start=seg_start,
                duration_s=duration_s, events=completed,
            )
            with self._stitch_lock:
                clips = self._stitcher.feed(scan)
            events_extracted = len(clips)

        # Detection history: one line per analyzed segment, so "nothing detected"
        # vs "nothing analyzing" (app down) vs "threshold too high" is visible.
        seg_iso = seg_start.isoformat(timespec="seconds") if seg_start else path.stem
        detlog.segment(seg_iso, cam_id, frames_sampled, seen_kept,
                       seen_camfloor, seen_global, events_extracted, person_floor)

        result = SegmentResult(
            path=path,
            cam_id=cam_id,
            frames_sampled=frames_sampled,
            person_frames=person_frames,
            vehicle_frames=vehicle_frames,
            max_persons=max_persons,
            max_vehicles=max_vehicles,
            elapsed_s=time.monotonic() - t0,
            events_extracted=events_extracted,
        )
        if person_frames:
            self._person_segments += 1
        if vehicle_frames:
            self._vehicle_segments += 1

        bus.info(
            "AI",
            f"cam{cam_id}: {path.name}  sampled {frames_sampled}f in {result.elapsed_s:.1f}s  "
            f"person {person_frames}f (max {max_persons})  "
            f"vehicle {vehicle_frames}f (max {max_vehicles})  "
            f"events {events_extracted}",
        )
        self.segment_analyzed.emit(result)
        self.totals_changed.emit(self._person_segments, self._vehicle_segments)
