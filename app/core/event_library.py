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
from datetime import date as _date, datetime
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
    clips: dict[int, Path] = field(default_factory=dict)  # cam_id -> clip path
    # Playback overlay boxes for the trigger camera, clip-relative seconds:
    # [(t, [(x1, y1, x2, y2, label, conf), ...]), ...]
    tracks: list = field(default_factory=list)

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
    tracks: list = []

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
        clips=clips,
        tracks=tracks,
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


def events_for_day(events: list[EventRecord], day: _date) -> list[EventRecord]:
    return [e for e in events if e.start_at.date() == day]


def event_dates(events: list[EventRecord]) -> set[_date]:
    return {e.start_at.date() for e in events}
