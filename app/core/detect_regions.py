"""Per-camera live-alert detect region — the rectangle you drag on the tile.

A rectangle in normalized [0..1] image coords (x0, y0, x1, y1). A LIVE detection
only counts when its box overlaps this rectangle, so an edge sliver (a toe at the
very bottom of frame) doesn't fire an alert — while recording and the saved frame
stay full. Persists to <env_dir>/.cctv-detect-regions.json (gitignored).

Only the live-alert tier uses this; the segment/event analyzer is unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

_FILE = ".cctv-detect-regions.json"
DEFAULT = (0.15, 0.15, 0.85, 0.85)   # 15% inset on every edge
_MIN = 0.05                          # smallest allowed width/height


def _path(env_path) -> Path:
    base = Path(env_path).parent if env_path else Path.cwd()
    return base / _FILE


def norm_rect(r) -> tuple:
    """Clamp to [0,1], order the corners, and keep a minimum size."""
    x0, y0, x1, y1 = (float(v) for v in r)
    x0, x1 = sorted((min(max(x0, 0.0), 1.0), min(max(x1, 0.0), 1.0)))
    y0, y1 = sorted((min(max(y0, 0.0), 1.0), min(max(y1, 0.0), 1.0)))
    if x1 - x0 < _MIN:
        x1 = min(1.0, x0 + _MIN)
    if y1 - y0 < _MIN:
        y1 = min(1.0, y0 + _MIN)
    return (x0, y0, x1, y1)


def load(env_path) -> dict:
    """{cam_index: (x0,y0,x1,y1)} for cameras with a saved region."""
    out: dict[int, tuple] = {}
    try:
        data = json.loads(_path(env_path).read_text(encoding="utf-8"))
        for k, v in data.items():
            try:
                if len(v) == 4:
                    out[int(k)] = norm_rect(v)
            except (ValueError, TypeError):
                continue
    except (OSError, ValueError):
        pass
    return out


def save(env_path, regions: dict) -> None:
    data = {str(k): list(v) for k, v in regions.items()}
    try:
        _path(env_path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def overlaps(region, bx0, by0, bx1, by1) -> bool:
    """Does a normalized detection box (bx0..bx1, by0..by1) overlap `region`?"""
    rx0, ry0, rx1, ry1 = region
    return bx0 < rx1 and bx1 > rx0 and by0 < ry1 and by1 > ry0
