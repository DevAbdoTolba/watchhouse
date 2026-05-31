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
import shutil
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
    # Presence lifecycle for notifications (v0.4.20). The stitcher tags each
    # emitted clip so the notifier can say arrived vs still-present vs left:
    #   "started" first clip of a new presence on a camera
    #   "ongoing" a cap-split continuation while the presence persists
    #   "ended"   the final clip of a presence (chain finalized for good)
    #   "single"  a short standalone event that started and ended in one emit
    # `presence_seconds` is the total wall span of the whole presence so far.
    presence_state: str = "single"
    presence_seconds: float = 0.0
    # Presence-session grouping (v0.4.21). All cap-split chunks of ONE
    # continuous presence share a `session_id`; `session_index` is their
    # 0-based order within the session; `session_final` is True on the last
    # emitted chunk. Lets the UI group + stitch a whole presence on demand
    # without auto-writing a giant continuous file.
    session_id: str = ""
    session_index: int = 0
    session_final: bool = False


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


def _cut_clip(src: Path, dst: Path, start_s: float, dur_s: float,
              to_eof: bool = False) -> bool:
    """Stream-copy a sub-clip out of `src`. No re-encode (cuts at the nearest
    prior keyframe, which the pre-roll margin already absorbs).

    `to_eof=True` omits `-t` so the copy runs through end-of-file. This is the
    tail-loss fix: when an event extends to the very end of a segment, clamping
    `-t` to a sampled-duration estimate could chop the genuine final second
    (the last frames aren't always sampled, so the measured duration can fall
    short of the true file length). Letting ffmpeg run to EOF guarantees the
    tail is kept; the post-roll margin never overshoots past real footage."""
    exe = _ffmpeg_path()
    if not exe:
        return False
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_s:.3f}", "-i", str(src),
    ]
    if not to_eof:
        cmd += ["-t", f"{dur_s:.3f}"]
    cmd += [
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


def _concat_clips(parts: list[Path], dst: Path) -> bool:
    """Join already-cut sub-clips end to end with the ffmpeg concat demuxer
    (stream copy, no re-encode). Single-part lists are just moved into place so
    the common case stays a plain rename. The interior joins carry no extra
    pre/post-roll - margins were applied only at the true outer edges when the
    parts were cut - so a stitched clip is the same length as the real event."""
    parts = [p for p in parts if p.is_file() and p.stat().st_size > 0]
    if not parts:
        return False
    if len(parts) == 1:
        try:
            if dst.exists():
                dst.unlink()
            # shutil.move, NOT Path.replace/os.replace: the scratch copy may sit
            # on %TEMP% (C:) while the user saves onto the data drive (D:).
            # os.replace raises WinError 17 across volumes and was silently
            # swallowed below, so every single-part export wrote nothing.
            shutil.move(str(parts[0]), str(dst))
            return True
        except OSError as e:
            bus.warn("EVT", f"concat(single): move {parts[0].name} -> {dst}: {e!s}")
            return False
    exe = _ffmpeg_path()
    if not exe:
        return False
    listing = dst.with_suffix(".concat.txt")
    try:
        listing.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8"
        )
    except OSError:
        return False
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart",
        str(dst),
    ]
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=flags, timeout=180,
        )
        ok = r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0
    except Exception:
        ok = False
    finally:
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            listing.unlink()
        except OSError:
            pass
    return ok


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
    # Tail-loss guard: if the (margin-padded) event runs to within a sampling
    # interval of the segment end, copy through EOF instead of clamping `-t`,
    # which could otherwise drop the genuine final frames.
    reaches_eof = duration_s > 0 and raw_end >= duration_s - 1.0

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
        cut_to_eof = False
        if cam_id == ev.cam_id:
            src, offset = segment_path, clip_start
            cut_to_eof = reaches_eof
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
        if _cut_clip(src, out, offset, clip_dur, to_eof=cut_to_eof):
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


@dataclass
class ChainPart:
    """One segment's contribution to a stitched (possibly multi-segment) event.

    `ev` carries *segment-local* offsets (seconds into `path`). When a single
    presence spans N consecutive segments the stitcher collects one ChainPart
    per segment; `write_event_chain` cuts each part with margins only at the
    chain's true outer edges and concatenates them into one clip per camera.
    """

    path: Path
    seg_start: datetime
    duration_s: float
    ev: PendingEvent


def _merge_pending(parts: list[ChainPart]) -> PendingEvent:
    """Roll up peak counts / confidences / best frame across all parts so the
    label, thumbnail and metadata describe the whole stitched event."""
    head = parts[0].ev
    merged = PendingEvent(cam_id=head.cam_id, t_start=head.t_start, t_end=head.t_end)
    for part in parts:
        ev = part.ev
        merged.hit_count += ev.hit_count
        merged.peak_person = max(merged.peak_person, ev.peak_person)
        merged.peak_vehicle = max(merged.peak_vehicle, ev.peak_vehicle)
        merged.peak_person_conf = max(merged.peak_person_conf, ev.peak_person_conf)
        merged.peak_vehicle_conf = max(merged.peak_vehicle_conf, ev.peak_vehicle_conf)
        if ev._best_score > merged._best_score:
            merged._best_score = ev._best_score
            merged.best_frame = ev.best_frame
            merged.best_dets = ev.best_dets
    return merged


def write_event_chain(
    parts: list[ChainPart],
    events_dir: Path,
    cfg: EventConfig,
    recording_dir: Path,
    cam_ids: list[int],
    presence_state: str = "single",
    presence_seconds: float = 0.0,
    session_id: str = "",
    session_index: int = 0,
    session_final: bool = False,
) -> EventClip | None:
    """Materialize a stitched event spanning one or more contiguous segments.

    For a single part this is equivalent to `write_event`, but it always cuts
    via the concat path so the output format is uniform. Pre-roll is applied
    only on the first part, post-roll only on the last; interior joins are
    butt-spliced. Sibling cameras are concatenated over the same per-part
    wall-clock windows. event.json records every source segment for
    reproducibility and idempotency.
    """
    import cv2

    parts = [p for p in parts if p.ev is not None]
    if not parts:
        return None

    total_hits = sum(p.ev.hit_count for p in parts)
    if total_hits < cfg.min_hits:
        return None

    merged = _merge_pending(parts)
    label = label_for(merged.peak_person, merged.peak_vehicle)

    first = parts[0]
    last = parts[-1]
    # Outer-edge margins, clamped to real footage on each end.
    head_start = max(0.0, first.ev.t_start - cfg.pre_roll_s)
    activity_wall = first.seg_start + timedelta(seconds=first.ev.t_start)
    window_start = first.seg_start + timedelta(seconds=head_start)
    last_raw_end = last.ev.t_end + cfg.post_roll_s
    last_reaches_eof = last.duration_s > 0 and last_raw_end >= last.duration_s - 1.0
    last_end = min(last.duration_s, last_raw_end) if last.duration_s > 0 else last_raw_end

    day = activity_wall.strftime("%Y-%m-%d")
    cam_dir = events_dir / day / f"cam{merged.cam_id}"
    folder = cam_dir / f"{activity_wall.strftime('%H-%M-%S')}_{label}"
    # Idempotency: the (cam, wall-clock start, label) tuple maps to exactly one
    # folder. If it already exists (duplicate segment_closed, re-processing) we
    # skip rather than spawn a "_2" twin.
    if folder.exists():
        bus.info("EVT", f"event {folder.name} already exists; skipping duplicate")
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        bus.error("EVT", f"cannot create {folder}: {e!s}")
        return None

    thumb_path: Path | None = folder / "thumb.jpg"
    if merged.best_frame is not None:
        try:
            cv2.imwrite(str(thumb_path),
                        _draw_thumb(merged.best_frame, merged.best_dets, cfg.thumb_max_width))
        except Exception as e:
            bus.warn("EVT", f"thumbnail write failed for {folder.name}: {e!s}")
            thumb_path = None
    else:
        thumb_path = None

    # Each part's [start, end] window in segment-local seconds. Pre/post-roll
    # only at the chain's outer edges; interior parts run edge to edge so the
    # concatenated stream is continuous with no double margins or gaps.
    n = len(parts)
    part_windows: list[tuple[float, float, bool]] = []
    for i, part in enumerate(parts):
        if i == 0:
            ps = head_start
        else:
            ps = 0.0
        if i == n - 1:
            pe = last_end
            eof = last_reaches_eof
        else:
            pe = part.duration_s
            eof = True  # interior part: copy through to its EOF, no clamp
        part_windows.append((ps, max(ps + 0.1, pe), eof))

    total_dur = sum(pe - ps for ps, pe, _ in part_windows)

    cams_captured: list[int] = []
    for cam_id in cam_ids:
        out = folder / f"cam{cam_id}.mp4"
        sub_clips: list[Path] = []
        ok_all = True
        for i, (part, (ps, pe, eof)) in enumerate(zip(parts, part_windows)):
            sub = folder / f"_{cam_id}_part{i}.mp4"
            if cam_id == merged.cam_id:
                src, off, dur = part.path, ps, pe - ps
                to_eof = eof
            else:
                # Sibling: find the segment covering this part's wall window.
                part_window_start = part.seg_start + timedelta(seconds=ps)
                found = _segment_covering(recording_dir / f"cam{cam_id}", part_window_start)
                if found is None:
                    ok_all = False
                    break
                sib_start, src = found
                off = (part_window_start - sib_start).total_seconds()
                if off < 0 or off > 17 * 60:
                    ok_all = False
                    break
                dur = pe - ps
                # Siblings always clamp with `-t`: a sibling segment may be a
                # different length than the trigger's, so "copy to EOF" can't be
                # safely mirrored across cameras.
                to_eof = False
            if not _cut_clip(src, sub, off, dur, to_eof=to_eof):
                ok_all = False
                break
            sub_clips.append(sub)
        if not ok_all:
            for sc in sub_clips:
                try:
                    sc.unlink()
                except OSError:
                    pass
            if cam_id == merged.cam_id:
                bus.warn("EVT", f"cam{cam_id}: clip cut failed for {folder.name}")
            else:
                bus.warn("EVT", f"cam{cam_id}: missing/short coverage for {folder.name} (skipped)")
            continue
        if _concat_clips(sub_clips, out):
            cams_captured.append(cam_id)
        else:
            bus.warn("EVT", f"cam{cam_id}: concat failed for {folder.name}")

    # Tracks: per-part boxes, offset to merged-clip-relative seconds. The merged
    # clip starts at `head_start` into part 0; each subsequent part's local time
    # maps to (cumulative duration of prior part windows) + (local - part_start).
    tracks = []
    cum = 0.0
    for i, (part, (ps, pe, _)) in enumerate(zip(parts, part_windows)):
        for off, dets in part.ev.track:
            rel = cum + (off - ps)
            if rel < 0 or off < ps or off > pe:
                continue
            boxes = [
                [round(d.x1, 1), round(d.y1, 1), round(d.x2, 1), round(d.y2, 1),
                 d.label, round(d.confidence, 3)]
                for d in dets if d.is_person or d.is_vehicle
            ]
            if boxes:
                tracks.append({"t": round(rel, 2), "b": boxes})
        cum += pe - ps

    source_segments = [
        {"path": str(part.path), "start": ps, "end": pe}
        for part, (ps, pe, _) in zip(parts, part_windows)
    ]

    meta = {
        "start_at": activity_wall.isoformat(timespec="seconds"),
        "trigger_cam": merged.cam_id,
        "label": label,
        "peak_person": merged.peak_person,
        "peak_vehicle": merged.peak_vehicle,
        "person_conf": round(merged.peak_person_conf, 4),
        "vehicle_conf": round(merged.peak_vehicle_conf, 4),
        "duration_s": round(total_dur, 2),
        "cams": cams_captured,
        "segments": len(parts),
        "source_segments": source_segments,
        "presence_state": presence_state,
        "presence_seconds": round(presence_seconds, 2),
        "session_id": session_id,
        "session_index": session_index,
        "session_final": session_final,
        "tracks": tracks,
    }
    try:
        (folder / "event.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as e:
        bus.warn("EVT", f"could not write event.json for {folder.name}: {e!s}")

    return EventClip(
        folder=folder,
        cam_id=merged.cam_id,
        cams_captured=cams_captured,
        thumb_path=thumb_path,
        start_at=activity_wall,
        duration_s=total_dur,
        peak_person=merged.peak_person,
        peak_vehicle=merged.peak_vehicle,
        peak_person_conf=merged.peak_person_conf,
        peak_vehicle_conf=merged.peak_vehicle_conf,
        label=label,
        presence_state=presence_state,
        presence_seconds=presence_seconds,
        session_id=session_id,
        session_index=session_index,
        session_final=session_final,
    )
