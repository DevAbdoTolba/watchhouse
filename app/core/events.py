"""Event extraction: turn detection hits into browsable evidence clips.

The SegmentAnalyzer samples a finalized recording segment ~1 fps and flags
frames containing a person or vehicle. This module groups those flagged
frames into continuous *presence intervals* - so a person who stands still
for two minutes is one event, not 120 - pads each interval with a
configurable pre-roll / post-roll safety margin, and cuts a slim clip out
of the parent segment with a stream copy (zero re-encode). A thumbnail of
the peak-activity frame, with detection boxes drawn on it, is saved beside
the clip.

Events land in a hand-browsable tree that lives OUTSIDE the rolling capture
buffer, so they survive after the parent segment ages out of the 90-minute
window:

    <recording_dir>/events/2026-05-29/cam1/14-17-32_person_x2/
        clip.mp4
        thumb.jpg

This folder is never touched by the retention pruner.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from app.core.detect import Detection

from app.core.log import bus


def _ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@dataclass(frozen=True)
class EventConfig:
    enabled: bool = True
    pre_roll_s: float = 5.0          # safety margin before activity starts
    post_roll_s: float = 5.0         # safety margin after activity ends
    merge_gap_s: float = 5.0         # hits within this gap belong to one event
    min_hits: int = 2                # drop single-frame blips (noise filter)
    thumb_max_width: int = 640


@dataclass
class PendingEvent:
    """Accumulates a single continuous presence as the analyzer streams hits.

    `t_start`/`t_end` are offsets in seconds into the parent segment. The
    "best" frame is the sampled frame with the most detections seen so far -
    that becomes the thumbnail.
    """

    cam_id: int
    t_start: float
    t_end: float
    hit_count: int = 0
    peak_person: int = 0
    peak_vehicle: int = 0
    peak_person_conf: float = 0.0   # best person confidence across the event
    peak_vehicle_conf: float = 0.0
    _best_score: int = -1
    best_frame: "np.ndarray | None" = None
    best_dets: list = field(default_factory=list)
    # Per-sample boxes for the playback overlay: (segment_offset_s, [Detection]).
    track: list = field(default_factory=list)

    def add(self, offset_s: float, n_person: int, n_vehicle: int,
            frame: "np.ndarray", dets: list) -> None:
        self.t_end = offset_s
        self.hit_count += 1
        self.peak_person = max(self.peak_person, n_person)
        self.peak_vehicle = max(self.peak_vehicle, n_vehicle)
        self.track.append((offset_s, dets))
        for d in dets:
            if d.is_person:
                self.peak_person_conf = max(self.peak_person_conf, d.confidence)
            elif d.is_vehicle:
                self.peak_vehicle_conf = max(self.peak_vehicle_conf, d.confidence)
        score = n_person + n_vehicle
        if score > self._best_score:
            self._best_score = score
            self.best_frame = frame
            self.best_dets = dets


@dataclass(frozen=True)
class EventClip:
    folder: Path
    cam_id: int                  # the camera that *triggered* the event
    cams_captured: list[int]     # every camera a clip was cut for
    thumb_path: Path | None
    start_at: datetime           # wall-clock when the activity began
    duration_s: float            # length of the extracted clip (with margins)
    peak_person: int
    peak_vehicle: int
    peak_person_conf: float
    peak_vehicle_conf: float
    label: str


def _seg_start(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem, "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None


def _segment_covering(cam_dir: Path, when: datetime) -> tuple[datetime, Path] | None:
    """The segment file on this camera that contains wall-clock `when` - i.e.
    the one with the greatest start time <= `when`. All cameras roll on the
    same clock-aligned boundary, so the sibling of a given segment shares the
    same 15-minute block even though its filename second may differ by ~1s."""
    if not cam_dir.is_dir():
        return None
    best: tuple[datetime, Path] | None = None
    for mp4 in cam_dir.glob("*.mp4"):
        st = _seg_start(mp4)
        if st is None or st > when:
            continue
        if best is None or st > best[0]:
            best = (st, mp4)
    return best


def label_for(peak_person: int, peak_vehicle: int) -> str:
    parts: list[str] = []
    if peak_person:
        parts.append(f"person_x{peak_person}")
    if peak_vehicle:
        parts.append(f"vehicle_x{peak_vehicle}")
    return "_".join(parts) if parts else "object"


def _unique_dir(parent: Path, name: str) -> Path:
    cand = parent / name
    i = 2
    while cand.exists():
        cand = parent / f"{name}_{i}"
        i += 1
    return cand


def _draw_thumb(frame: "np.ndarray", dets: list, max_width: int) -> "np.ndarray":
    import cv2

    img = frame.copy()
    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / float(w)
        img = cv2.resize(img, (max_width, int(round(h * scale))),
                         interpolation=cv2.INTER_AREA)
    else:
        scale = 1.0
    for d in dets:
        # BGR: green for people, amber for vehicles.
        color = (60, 200, 60) if d.is_person else (40, 170, 255)
        p1 = (int(d.x1 * scale), int(d.y1 * scale))
        p2 = (int(d.x2 * scale), int(d.y2 * scale))
        cv2.rectangle(img, p1, p2, color, 2)
        text = f"{d.label} {d.confidence:.2f}"
        cv2.putText(img, text, (p1[0], max(12, p1[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return img


def _cut_clip(src: Path, dst: Path, start_s: float, dur_s: float) -> bool:
    """Stream-copy a sub-clip out of `src`. No re-encode (cuts at the nearest
    prior keyframe, which the pre-roll margin already absorbs)."""
    exe = _ffmpeg_path()
    if not exe:
        return False
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_s:.3f}", "-i", str(src),
        "-t", f"{dur_s:.3f}",
        "-c", "copy", "-map", "0:v:0",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            timeout=120,
        )
        return r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0
    except Exception:
        return False


def write_event(
    segment_path: Path,
    seg_start: datetime,
    duration_s: float,
    ev: PendingEvent,
    events_dir: Path,
    cfg: EventConfig,
    recording_dir: Path,
    cam_ids: list[int],
) -> EventClip | None:
    """Materialize one PendingEvent as an evidence folder.

    The event is detected on a single (triggering) camera, but a clip is cut
    for *every* camera over the same wall-clock window - so reviewing an event
    shows all four angles of the moment, not just the one that tripped. Each
    sibling clip carries the same pre-roll / post-roll safety margin.

    Returns the EventClip, or None if the event was below the noise floor
    (`min_hits`).
    """
    import cv2

    if ev.hit_count < cfg.min_hits:
        return None

    clip_start = max(0.0, ev.t_start - cfg.pre_roll_s)
    raw_end = ev.t_end + cfg.post_roll_s
    clip_end = min(duration_s, raw_end) if duration_s > 0 else raw_end
    clip_dur = max(0.5, clip_end - clip_start)

    activity_wall = seg_start + timedelta(seconds=ev.t_start)
    window_start = seg_start + timedelta(seconds=clip_start)  # absolute, incl. pre-roll
    label = label_for(ev.peak_person, ev.peak_vehicle)

    day = activity_wall.strftime("%Y-%m-%d")
    cam_dir = events_dir / day / f"cam{ev.cam_id}"
    try:
        cam_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        bus.error("EVT", f"cannot create {cam_dir}: {e!s}")
        return None
    folder = _unique_dir(cam_dir, f"{activity_wall.strftime('%H-%M-%S')}_{label}")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        bus.error("EVT", f"cannot create {folder}: {e!s}")
        return None

    thumb_path: Path | None = folder / "thumb.jpg"
    if ev.best_frame is not None:
        try:
            cv2.imwrite(str(thumb_path), _draw_thumb(ev.best_frame, ev.best_dets, cfg.thumb_max_width))
        except Exception as e:
            bus.warn("EVT", f"thumbnail write failed for {folder.name}: {e!s}")
            thumb_path = None
    else:
        thumb_path = None

    cams_captured: list[int] = []
    for cam_id in cam_ids:
        out = folder / f"cam{cam_id}.mp4"
        if cam_id == ev.cam_id:
            src, offset = segment_path, clip_start
        else:
            found = _segment_covering(recording_dir / f"cam{cam_id}", window_start)
            if found is None:
                bus.warn("EVT", f"cam{cam_id}: no segment covering {window_start:%H:%M:%S} (skipped)")
                continue
            sib_start, src = found
            offset = (window_start - sib_start).total_seconds()
            if offset < 0 or offset > 17 * 60:
                bus.warn("EVT", f"cam{cam_id}: segment offset {offset:.0f}s out of range (skipped)")
                continue
        if _cut_clip(src, out, offset, clip_dur):
            cams_captured.append(cam_id)
        else:
            bus.warn("EVT", f"cam{cam_id}: clip cut failed for {folder.name}")

    # Per-sample boxes for the trigger camera, in *clip-relative* seconds, so
    # the playback overlay can draw them as the clip plays. Coords are clip
    # pixels (the clip is a stream copy of the segment, same resolution).
    tracks = []
    for off, dets in ev.track:
        rel = off - clip_start
        if rel < 0:
            continue
        boxes = [
            [round(d.x1, 1), round(d.y1, 1), round(d.x2, 1), round(d.y2, 1),
             d.label, round(d.confidence, 3)]
            for d in dets if d.is_person or d.is_vehicle
        ]
        if boxes:
            tracks.append({"t": round(rel, 2), "b": boxes})

    # Sidecar metadata for the Events view (confidence-based filtering, etc.).
    meta = {
        "start_at": activity_wall.isoformat(timespec="seconds"),
        "trigger_cam": ev.cam_id,
        "label": label,
        "peak_person": ev.peak_person,
        "peak_vehicle": ev.peak_vehicle,
        "person_conf": round(ev.peak_person_conf, 4),
        "vehicle_conf": round(ev.peak_vehicle_conf, 4),
        "duration_s": round(clip_dur, 2),
        "cams": cams_captured,
        "tracks": tracks,
    }
    try:
        (folder / "event.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as e:
        bus.warn("EVT", f"could not write event.json for {folder.name}: {e!s}")

    return EventClip(
        folder=folder,
        cam_id=ev.cam_id,
        cams_captured=cams_captured,
        thumb_path=thumb_path,
        start_at=activity_wall,
        duration_s=clip_dur,
        peak_person=ev.peak_person,
        peak_vehicle=ev.peak_vehicle,
        peak_person_conf=ev.peak_person_conf,
        peak_vehicle_conf=ev.peak_vehicle_conf,
        label=label,
    )
