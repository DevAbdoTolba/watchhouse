"""Index the recordings/ folder.

ffmpeg names files via `-strftime 1` as `YYYY-MM-DDTHH-MM-SS.mp4`,
so we can recover the start wall-clock from the filename alone. The
duration is read from the MP4 container on demand (cv2.VideoCapture
exposes `CAP_PROP_POS_MSEC` after seeking to the end). For speed we
estimate from file size when displaying many clips at once and only
query the real duration when something actually starts playing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Clip:
    camera_id: int
    start_at: datetime  # local time (matches the filename)
    path: Path
    size_bytes: int
    estimated_duration_s: float  # rough; refined on play

    @property
    def end_at_estimated(self) -> datetime:
        return self.start_at + timedelta(seconds=self.estimated_duration_s)


def _parse_clip_name(p: Path) -> datetime | None:
    """Filenames look like `2026-05-16T01-00-31.mp4`."""
    try:
        return datetime.strptime(p.stem, "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None


def _estimate_duration_s(size_bytes: int) -> float:
    """Very rough size-to-duration heuristic for sub-streams (~50 KB/s).

    Good enough for laying out a timeline strip; the real duration is
    fetched from the MP4 when the player starts.
    """
    if size_bytes <= 0:
        return 0.0
    return max(1.0, size_bytes / 50_000.0)


def scan(recording_dir: Path) -> dict[int, list[Clip]]:
    """Return {camera_id: sorted list of clips} for everything currently
    on disk. Cheap to call; the caller can re-scan whenever the UI
    needs to refresh."""
    out: dict[int, list[Clip]] = {}
    if not recording_dir.is_dir():
        return out

    for cam_dir in sorted(recording_dir.iterdir()):
        if not cam_dir.is_dir() or not cam_dir.name.startswith("cam"):
            continue
        try:
            cam_id = int(cam_dir.name[3:])
        except ValueError:
            continue
        clips: list[Clip] = []
        for mp4 in cam_dir.glob("*.mp4"):
            start = _parse_clip_name(mp4)
            if start is None:
                continue
            try:
                stat = mp4.stat()
            except OSError:
                continue
            clips.append(
                Clip(
                    camera_id=cam_id,
                    start_at=start,
                    path=mp4,
                    size_bytes=stat.st_size,
                    estimated_duration_s=_estimate_duration_s(stat.st_size),
                )
            )
        clips.sort(key=lambda c: c.start_at)
        out[cam_id] = clips
    return out


def clips_for_day(library: dict[int, list[Clip]], cam_id: int, day: _date) -> list[Clip]:
    return [c for c in library.get(cam_id, []) if c.start_at.date() == day]


def dates_with_clips(library: dict[int, list[Clip]]) -> set[_date]:
    out: set[_date] = set()
    for clips in library.values():
        for c in clips:
            out.add(c.start_at.date())
    return out


def find_clip_at(clips: Iterable[Clip], when: datetime) -> tuple[Clip, float] | None:
    """Find the clip that contains `when` and return (clip, offset_seconds)
    into the clip. Returns None if no clip contains that moment."""
    for c in clips:
        end = c.end_at_estimated
        if c.start_at <= when < end:
            return c, max(0.0, (when - c.start_at).total_seconds())
    return None
