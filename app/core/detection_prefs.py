"""Per-camera person-confidence floors, editable in the UI and persisted.

A floor is the minimum confidence a 'person' detection must reach on a given
camera to count. 0 (or unset) = uncapped: keep every person the model reports
(at its base ~0.35 threshold). A noisy view — e.g. a door camera that calls a
pile of rubbish bags a person at 0.4-0.76 — gets a high floor (0.78) so only
real, confident people pass, while quiet interior cameras stay uncapped so a
faint-but-real person is never silently dropped.

Stored in <env_dir>/.cctv-detection.json (gitignored), mirroring camera_links.
Values here OVERRIDE the .env DETECTION_PERSON_CONF_BY_CAM baseline.
"""

from __future__ import annotations

import json
from pathlib import Path

_FILE = ".cctv-detection.json"
MAX_FLOOR = 0.95  # above this nothing would ever pass; clamp for sanity


def _path(env_path) -> Path:
    base = Path(env_path).parent if env_path else Path.cwd()
    return base / _FILE


def _clamp(v) -> float:
    try:
        return max(0.0, min(MAX_FLOOR, float(v)))
    except (TypeError, ValueError):
        return 0.0


def load(env_path) -> dict[int, float]:
    """Return {cam_id: floor}. Only floors > 0 are meaningful (0 = uncapped)."""
    try:
        data = json.loads(_path(env_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("person_conf_by_cam") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[int, float] = {}
    for k, v in raw.items():
        try:
            floor = _clamp(v)
        except (TypeError, ValueError):
            continue
        if floor > 0.0:
            out[int(k)] = floor
    return out


def save(env_path, floors: dict[int, float]) -> None:
    """Persist {cam_id: floor}; entries at 0 are dropped (uncapped is the default
    and need not be stored)."""
    clean = {str(int(c)): _clamp(f) for c, f in floors.items() if _clamp(f) > 0.0}
    try:
        _path(env_path).write_text(
            json.dumps({"person_conf_by_cam": clean}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
