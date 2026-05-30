"""Index the recordings/events/ tree written by the event extractor.

Layout produced by app.core.events.write_event:

    events/2026-05-30/cam1/00-13-02_person_x2_vehicle_x1/
        cam1.mp4   cam2.mp4   cam3.mp4   cam4.mp4
        thumb.jpg

Each leaf folder is one event: a wall-clock moment, the camera that
triggered it, a person/vehicle count label, a thumbnail of the peak frame,
and a clip per camera (all angles of the same window). This module turns
that tree into a sorted list of EventRecord for the playback Events view.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from pathlib import Path


_PERSON_RE = re.compile(r"person_x(\d+)")
_VEHICLE_RE = re.compile(r"vehicle_x(\d+)")


@dataclass(frozen=True)
class EventRecord:
    start_at: datetime          # when the activity began (wall clock)
    trigger_cam: int            # camera that detected the event
    label: str                  # e.g. "person_x2_vehicle_x1"
    peak_person: int
    peak_vehicle: int
    person_conf: float          # peak human detection confidence 0..1 (0 if unknown)
    vehicle_conf: float
    folder: Path
    thumb: Path | None
    duration_s: float = 0.0     # clip length in seconds (0 if unknown)
    clips: dict[int, Path] = field(default_factory=dict)  # cam_id -> clip path
    # Playback overlay boxes for the trigger camera, clip-relative seconds:
    # [(t, [(x1, y1, x2, y2, label, conf), ...]), ...]
    tracks: list = field(default_factory=list)
    # Presence-session grouping (v0.4.21). All cap-split chunks of one
    # continuous presence share `session_id`; ordered by `session_index`;
    # `session_final` marks the last chunk. Older events lack these keys and
    # default to a singleton session keyed on the event's own folder.
    session_id: str = ""
    session_index: int = 0
    session_final: bool = False

    @property
    def person_pct(self) -> int:
        return round(self.person_conf * 100)

    @property
    def pretty(self) -> str:
        parts = []
        if self.peak_person:
            parts.append(f"{self.peak_person} person" + ("s" if self.peak_person != 1 else ""))
        if self.peak_vehicle:
            parts.append(f"{self.peak_vehicle} vehicle" + ("s" if self.peak_vehicle != 1 else ""))
        return ", ".join(parts) if parts else "activity"


def _parse_folder(folder: Path, day: _date, trigger_cam: int) -> EventRecord | None:
    # Folder name: "HH-MM-SS_<label>"
    name = folder.name
    if "_" not in name:
        return None
    time_part, _, label = name.partition("_")
    try:
        t = datetime.strptime(time_part, "%H-%M-%S").time()
    except ValueError:
        return None
    start_at = datetime.combine(day, t)

    clips: dict[int, Path] = {}
    for mp4 in folder.glob("cam*.mp4"):
        m = re.match(r"cam(\d+)\.mp4", mp4.name, re.IGNORECASE)
        if m:
            clips[int(m.group(1))] = mp4
    if not clips:
        return None

    thumb = folder / "thumb.jpg"
    pm = _PERSON_RE.search(label)
    vm = _VEHICLE_RE.search(label)
    peak_person = int(pm.group(1)) if pm else 0
    peak_vehicle = int(vm.group(1)) if vm else 0
    person_conf = vehicle_conf = 0.0
    duration_s = 0.0
    tracks: list = []
    # Default each ungrouped event to its own singleton session so old events
    # (pre-session) still bucket cleanly. The folder path is stable + unique.
    session_id = str(folder)
    session_index = 0
    session_final = True

    # Richer metadata (incl. confidence + overlay boxes) comes from the sidecar
    # when present; older events predate it and fall back to folder-name counts.
    meta_path = folder / "event.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            peak_person = int(meta.get("peak_person", peak_person))
            peak_vehicle = int(meta.get("peak_vehicle", peak_vehicle))
            person_conf = float(meta.get("person_conf", 0.0))
            vehicle_conf = float(meta.get("vehicle_conf", 0.0))
            duration_s = float(meta.get("duration_s", 0.0))
            sid = meta.get("session_id")
            if sid:
                session_id = str(sid)
            session_index = int(meta.get("session_index", 0))
            session_final = bool(meta.get("session_final", True))
            for entry in meta.get("tracks", []):
                boxes = [
                    (float(b[0]), float(b[1]), float(b[2]), float(b[3]),
                     str(b[4]), float(b[5]))
                    for b in entry.get("b", [])
                ]
                tracks.append((float(entry.get("t", 0.0)), boxes))
        except (ValueError, OSError, IndexError, TypeError):
            pass

    return EventRecord(
        start_at=start_at,
        trigger_cam=trigger_cam,
        label=label,
        peak_person=peak_person,
        peak_vehicle=peak_vehicle,
        person_conf=person_conf,
        vehicle_conf=vehicle_conf,
        folder=folder,
        thumb=thumb if thumb.is_file() else None,
        duration_s=duration_s,
        clips=clips,
        tracks=tracks,
        session_id=session_id,
        session_index=session_index,
        session_final=session_final,
    )


def scan_events(events_dir: Path) -> list[EventRecord]:
    """Walk events/<date>/cam<N>/<time>_<label>/ and return all events,
    newest first."""
    out: list[EventRecord] = []
    if not events_dir.is_dir():
        return out
    for day_dir in events_dir.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        for cam_dir in day_dir.iterdir():
            if not cam_dir.is_dir() or not cam_dir.name.lower().startswith("cam"):
                continue
            try:
                trigger_cam = int(cam_dir.name[3:])
            except ValueError:
                continue
            for ev_dir in cam_dir.iterdir():
                if not ev_dir.is_dir():
                    continue
                rec = _parse_folder(ev_dir, day, trigger_cam)
                if rec is not None:
                    out.append(rec)
    out.sort(key=lambda e: e.start_at, reverse=True)
    return out


@dataclass(frozen=True)
class EventSession:
    """A presence grouped from its cap-split member events (v0.4.21).

    A long continuous presence is force-split into many ~2-minute events for
    frequent notifications, but it is ONE presence to the user. This rolls the
    members that share a `session_id` back up into a single browsable unit:
    one row in the Events list, and the unit that "Save full clip" stitches.
    """

    session_id: str
    trigger_cam: int
    start_at: datetime          # earliest member start
    end_at: datetime            # latest member's start + its duration
    total_seconds: float        # summed member durations (true presence length)
    count: int
    peak_person: int
    peak_vehicle: int
    thumb: Path | None          # first member's thumbnail
    members: list[EventRecord]  # ordered by start_at (== session_index order)

    @property
    def pretty(self) -> str:
        parts = []
        if self.peak_person:
            parts.append(f"{self.peak_person} person" + ("s" if self.peak_person != 1 else ""))
        if self.peak_vehicle:
            parts.append(f"{self.peak_vehicle} vehicle" + ("s" if self.peak_vehicle != 1 else ""))
        return ", ".join(parts) if parts else "activity"

    @property
    def duration_label(self) -> str:
        secs = int(round(self.total_seconds))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m"
        if m:
            return f"{m}m {s:02d}s"
        return f"{s}s"


def group_sessions(events: list[EventRecord]) -> list[EventSession]:
    """Bucket events by `session_id` and roll each bucket up into an
    EventSession. Returns sessions newest first (by start_at)."""
    buckets: dict[str, list[EventRecord]] = {}
    for ev in events:
        buckets.setdefault(ev.session_id or str(ev.folder), []).append(ev)

    sessions: list[EventSession] = []
    for sid, members in buckets.items():
        members.sort(key=lambda e: (e.session_index, e.start_at))
        first = members[0]
        total = sum(m.duration_s for m in members)
        last = members[-1]
        end_at = last.start_at + timedelta(seconds=last.duration_s)
        sessions.append(EventSession(
            session_id=sid,
            trigger_cam=first.trigger_cam,
            start_at=first.start_at,
            end_at=end_at,
            total_seconds=total,
            count=len(members),
            peak_person=max(m.peak_person for m in members),
            peak_vehicle=max(m.peak_vehicle for m in members),
            thumb=first.thumb,
            members=members,
        ))
    sessions.sort(key=lambda s: s.start_at, reverse=True)
    return sessions


def sessions_for_day(events: list[EventRecord], day: _date) -> list[EventSession]:
    return group_sessions(events_for_day(events, day))


def events_for_day(events: list[EventRecord], day: _date) -> list[EventRecord]:
    return [e for e in events if e.start_at.date() == day]


def event_dates(events: list[EventRecord]) -> set[_date]:
    return {e.start_at.date() for e in events}
