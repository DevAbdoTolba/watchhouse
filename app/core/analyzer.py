"""SegmentAnalyzer: object detection over finalized recording segments.

When the recorder opens a new segment for a camera, the previous one is
finalized on disk (ffmpeg writes the moov atom on segment close). The
RecorderSupervisor emits `segment_closed(path)` for it; we enqueue the
path and process segments one at a time on this worker thread: open with
cv2, sample one frame every `sample_seconds` of footage, run the ONNX
detector, and aggregate per-segment detection counts.

v0.4.0 only *reports* findings (console + a running tally for the status
bar). v0.4.1 turns detections into extracted event clips + snapshots.
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
from PySide6.QtCore import QThread, Signal

from app.core.detect import Detector
from app.core.log import bus


def _cam_from_path(path: Path) -> int:
    name = path.parent.name
    if name.startswith("cam"):
        try:
            return int(name[3:])
        except ValueError:
            pass
    return 0


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

    @property
    def had_activity(self) -> bool:
        return self.person_frames > 0 or self.vehicle_frames > 0


class SegmentAnalyzer(QThread):
    segment_analyzed = Signal(object)   # SegmentResult
    totals_changed = Signal(int, int)   # cumulative person_segments, vehicle_segments

    def __init__(
        self,
        model_path: Path,
        conf: float = 0.35,
        sample_seconds: float = 1.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._detector = Detector(model_path, conf_threshold=conf)
        self._sample_seconds = max(0.2, sample_seconds)
        self._queue: "queue.Queue[Path | None]" = queue.Queue()
        self._stop = False
        self._person_segments = 0
        self._vehicle_segments = 0

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
        bus.info("AI", "segment analyzer stopped")

    def _analyze(self, path: Path) -> None:
        if not path.is_file():
            return
        cam_id = _cam_from_path(path)
        t0 = time.monotonic()
        cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            bus.warn("AI", f"cam{cam_id}: could not open {path.name} for analysis")
            cap.release()
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        step = max(1, int(round(fps * self._sample_seconds))) if fps > 0 else 25

        frames_sampled = person_frames = vehicle_frames = 0
        max_persons = max_vehicles = 0
        idx = 0
        while not self._stop:
            if not cap.grab():
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    dets = self._detector.detect(frame)
                    n_person = sum(1 for d in dets if d.is_person)
                    n_vehicle = sum(1 for d in dets if d.is_vehicle)
                    frames_sampled += 1
                    person_frames += 1 if n_person else 0
                    vehicle_frames += 1 if n_vehicle else 0
                    max_persons = max(max_persons, n_person)
                    max_vehicles = max(max_vehicles, n_vehicle)
            idx += 1
        cap.release()

        result = SegmentResult(
            path=path,
            cam_id=cam_id,
            frames_sampled=frames_sampled,
            person_frames=person_frames,
            vehicle_frames=vehicle_frames,
            max_persons=max_persons,
            max_vehicles=max_vehicles,
            elapsed_s=time.monotonic() - t0,
        )
        if person_frames:
            self._person_segments += 1
        if vehicle_frames:
            self._vehicle_segments += 1

        bus.info(
            "AI",
            f"cam{cam_id}: {path.name}  sampled {frames_sampled}f in {result.elapsed_s:.1f}s  "
            f"person {person_frames}f (max {max_persons})  "
            f"vehicle {vehicle_frames}f (max {max_vehicles})",
        )
        self.segment_analyzed.emit(result)
        self.totals_changed.emit(self._person_segments, self._vehicle_segments)
