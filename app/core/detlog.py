"""Detection history log — a plain-text record of what the analyzer SAW.

One line per analyzed segment (per armed camera), listing the person-detection
confidences that were KEPT, those DROPPED by the per-camera floor, and those
DROPPED by the global guards (conf/box) — plus whether an event was written.

Why: there was no way to tell "the threshold is too high and missing people"
from "the app/analyzer isn't running at all" (both look like an empty events
folder). This log makes the difference obvious: no lines = nothing analyzing;
lines with kept=[] but dropped=[0.7,...] = threshold too high; lines with
seen=[] = the model simply saw nothing.

Appends to <recording_dir>/detections.log. Thread-safe, best-effort, never
raises into the analyzer.
"""

from __future__ import annotations

import threading
from pathlib import Path

_path: "Path | None" = None
_lock = threading.Lock()
_MAX_BYTES = 5 * 1024 * 1024  # rotate at ~5 MB so it can't grow forever


def set_path(p) -> None:
    global _path
    _path = Path(p) if p else None


def log(line: str) -> None:
    if _path is None:
        return
    with _lock:
        try:
            if _path.exists() and _path.stat().st_size > _MAX_BYTES:
                _path.replace(_path.with_suffix(".log.1"))
            with _path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _fmt(confs) -> str:
    return "[" + ",".join(f"{c:.2f}" for c in sorted(confs, reverse=True)) + "]"


def segment(seg_iso: str, cam: int, frames: int, kept, cam_floor_dropped,
            global_dropped, events: int, floor: float) -> None:
    """One per-segment summary line."""
    log(f"{seg_iso} cam{cam} frames={frames} "
        f"kept={_fmt(kept)} "
        f"drop_camfloor(<{floor:g})={_fmt(cam_floor_dropped)} "
        f"drop_global={_fmt(global_dropped)} "
        f"events={events}")
