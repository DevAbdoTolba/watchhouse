"""Index the recordings/ folder.

Two sources are merged into one library:

  1. recordings/cam{N}/YYYY-MM-DDTHH-MM-SS.mp4  - written by Watchhouse
     itself via the segment recorder.
  2. recordings/imported/<anything>.mp4         - clips you exported
     out of the BitVision desktop client (or any other MP4 you drop
     in by hand). Filenames are tried against several common
     BitVision/Cantonk patterns to recover the camera index and
     start wall-clock; if nothing matches, the file is still
     surfaced under camera 0 with its mtime as the start time so
     you can still scrub through it.
"""

from __future__ import annotations

import re
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


# Patterns we try to recognise on imported clips, in order. Each yields
# (camera_id, datetime). Common BitVision / Cantonk / generic NVR
# export naming.
_IMPORT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # BitVision desktop:  ch01_20260516_010734.mp4   /   ch1_20260516_010734.mp4
    (re.compile(r"^ch(\d{1,2})[_-](\d{8})[_-](\d{6})", re.IGNORECASE), "ch_compact"),
    # Cantonk export:     20260516_010734_ch01.mp4
    (re.compile(r"^(\d{8})[_-](\d{6})[_-]ch(\d{1,2})", re.IGNORECASE), "compact_ch"),
    # Hikvision-ish:      cam01_2026-05-16_01-07-34.mp4
    (re.compile(r"^cam(\d{1,2})[_-](\d{4}-\d{2}-\d{2})[_-](\d{2}-\d{2}-\d{2})", re.IGNORECASE), "cam_iso"),
    # Watchhouse own (already handled by _parse_clip_name but harmless to also match)
    (re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}-\d{2}-\d{2})", re.IGNORECASE), "iso_only"),
]


def _parse_imported_name(p: Path) -> tuple[int, datetime] | None:
    name = p.stem
    for pat, kind in _IMPORT_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        try:
            if kind == "ch_compact":
                cam = int(m.group(1))
                d = m.group(2); t = m.group(3)
                return cam, datetime.strptime(d + t, "%Y%m%d%H%M%S")
            if kind == "compact_ch":
                d = m.group(1); t = m.group(2); cam = int(m.group(3))
                return cam, datetime.strptime(d + t, "%Y%m%d%H%M%S")
            if kind == "cam_iso":
                cam = int(m.group(1))
                return cam, datetime.strptime(m.group(2) + " " + m.group(3), "%Y-%m-%d %H-%M-%S")
            if kind == "iso_only":
                # Camera unknown for a bare ISO date; assume cam 0
                return 0, datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H-%M-%S")
        except ValueError:
            continue
    return None


def _scan_imported(imported_dir: Path) -> list[tuple[int, Clip]]:
    """Walk recordings/imported/ recursively, returning (cam_id, Clip) pairs.

    Files whose name we cannot parse fall back to (cam 0, file mtime).
    Lazily creates the imported/ directory the first time we look for
    it so the user has a visible destination to drop files into."""
    out: list[tuple[int, Clip]] = []
    if not imported_dir.exists():
        try:
            imported_dir.mkdir(parents=True, exist_ok=True)
            (imported_dir / "README.txt").write_text(
                "Drop DVR-exported MP4 clips here.\n\n"
                "Recognised filename patterns:\n"
                "  ch01_20260516_010734.mp4   (BitVision desktop)\n"
                "  20260516_010734_ch01.mp4   (Cantonk)\n"
                "  cam01_2026-05-16_01-07-34.mp4\n"
                "  2026-05-16T01-07-34.mp4    (Watchhouse-native)\n\n"
                "Anything else is still indexed under camera 0 with the file's\n"
                "modified-time as the start time, so you can still scrub it.\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return out
    if not imported_dir.is_dir():
        return out
    for mp4 in imported_dir.rglob("*.mp4"):
        try:
            stat = mp4.stat()
        except OSError:
            continue
        parsed = _parse_imported_name(mp4)
        if parsed is None:
            cam_id = 0
            start = datetime.fromtimestamp(stat.st_mtime)
        else:
            cam_id, start = parsed
        out.append((
            cam_id,
            Clip(
                camera_id=cam_id,
                start_at=start,
                path=mp4,
                size_bytes=stat.st_size,
                estimated_duration_s=_estimate_duration_s(stat.st_size),
            ),
        ))
    return out


def scan(recording_dir: Path) -> dict[int, list[Clip]]:
    """Return {camera_id: sorted list of clips} for everything currently
    on disk. Includes both Watchhouse-recorded clips (recordings/camN/)
    and user-imported clips (recordings/imported/). Camera 0 is the
    catch-all for imported clips whose camera index couldn't be parsed."""
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
        out.setdefault(cam_id, []).extend(clips)

    # Merge in the imported folder
    for cam_id, clip in _scan_imported(recording_dir / "imported"):
        out.setdefault(cam_id, []).append(clip)

    for cam_id in out:
        out[cam_id].sort(key=lambda c: c.start_at)
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
