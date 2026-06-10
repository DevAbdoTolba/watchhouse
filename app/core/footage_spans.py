"""Pure span math for the timeline's blue (pinned / kept) layer.

Extracted from PlaybackView (R3): given the scanned clip library and the
persisted Pins, compute which wall-clock spans are locked per camera, and
which sub-ranges of a user-framed window actually hold footage. No Qt —
plain functions over the Clip records, fully unit-testable.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def pinned_spans_by_cam(library: dict, pins, cameras, imported_dir) -> dict:
    """Blue-layer spans per camera: imported clips (per camera) + pinned
    ranges + the live keep window (the last two apply to every camera)."""
    out: dict[int, list] = {}
    try:
        imported_root = str(Path(imported_dir).resolve()).lower()
    except OSError:
        imported_root = ""
    for cam, clips in library.items():
        for c in clips:
            try:
                if imported_root and imported_root in str(
                        Path(c.path).resolve()).lower():
                    out.setdefault(cam, []).append(
                        (c.start_at, c.end_at_estimated))
            except OSError:
                continue
    spans = pins.all_spans(datetime.now())
    if spans:
        for cam_obj in cameras:
            out.setdefault(cam_obj.index, []).extend(spans)
    return out


def recorded_subranges(library: dict, start, end) -> list:
    """Intersections of [start, end] with clips that actually exist (any
    camera), so locking covers real footage only and never empty gaps."""
    spans = []
    for clips in library.values():
        for c in clips:
            lo = max(start, c.start_at)
            hi = min(end, c.end_at_estimated)
            if hi > lo:
                spans.append((lo, hi))
    return spans
