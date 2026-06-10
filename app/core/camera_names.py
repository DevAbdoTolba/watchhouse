"""Persisted per-camera display names (user-editable).

A small JSON file next to the resolved .env, mirroring ip_cache.py, so a
user's friendly names ("Front Door", "Garage") survive restarts and travel
with the credentials in a frozen-exe install. Names feed the tile header
and the Telegram alert captions; a blank/unset name falls back to the
camera's built-in location label.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.log import bus

NAMES_FILENAME = ".cctv-camera-names.json"
MAX_NAME_LEN = 40


def _names_path(anchor: Path | None) -> Path:
    """Stored alongside the loaded .env, falling back to the cwd."""
    base = anchor.parent if (anchor is not None and anchor.is_file()) else Path.cwd()
    return base / NAMES_FILENAME


def clean_name(value: str) -> str:
    """Trim, collapse internal whitespace/newlines, and cap length."""
    if not value:
        return ""
    return " ".join(value.split())[:MAX_NAME_LEN].strip()


def display_name(camera, names: dict[int, str]) -> str:
    """The one fallback chain for a camera's human label, shared by the tile
    headers, the playback view, and the Telegram captions: custom name if
    set, else the camera's built-in location, else its label."""
    return (
        names.get(camera.index)
        or getattr(camera, "location", None)
        or getattr(camera, "label", None)
        or f"camera {camera.index}"
    )


def load(anchor: Path | None) -> dict[int, str]:
    """Return {camera_index: name}; missing/corrupt file -> {}."""
    path = _names_path(anchor)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        bus.warn("CFG", f"failed to read {path.name}: {e!s}")
        return {}
    out: dict[int, str] = {}
    for k, v in (raw.get("names", {}) or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        name = clean_name(str(v))
        if name:
            out[idx] = name
    return out


def save(anchor: Path | None, names: dict[int, str]) -> bool:
    """Persist {index: name}. Blank names are dropped. Silent no-op if the
    target isn't writable."""
    path = _names_path(anchor)
    payload = {
        "names": {
            str(k): clean_name(v) for k, v in names.items() if clean_name(v)
        }
    }
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        bus.info("CFG", f"saved camera names to {path.name}")
        return True
    except OSError as e:
        bus.warn("CFG", f"failed to write {path.name}: {e!s}")
        return False
