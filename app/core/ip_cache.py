"""Tiny JSON file keeping the last-N DVR IPs that worked.

Sits next to the resolved .env so the cache moves with the credentials,
and so frozen-exe installations stay self-contained.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.log import bus


CACHE_FILENAME = ".cctv-known-dvrs.json"
MAX_ENTRIES = 5


@dataclass(frozen=True)
class IpRecord:
    ip: str
    last_seen: float


def _cache_path(anchor: Path | None) -> Path:
    """Cache lives alongside the loaded .env, falling back to the cwd."""
    base = anchor.parent if (anchor is not None and anchor.is_file()) else Path.cwd()
    return base / CACHE_FILENAME


def load(anchor: Path | None) -> list[IpRecord]:
    path = _cache_path(anchor)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        bus.warn("CACHE", f"failed to read {path.name}: {e!s}")
        return []
    out: list[IpRecord] = []
    for entry in raw.get("history", []):
        ip = entry.get("ip")
        ts = entry.get("last_seen", 0.0)
        if isinstance(ip, str) and ip.count(".") == 3:
            out.append(IpRecord(ip=ip, last_seen=float(ts)))
    return out


def record_hit(anchor: Path | None, ip: str) -> None:
    """Add or refresh `ip` at the head of the history. Keeps MAX_ENTRIES,
    MRU first. Silently no-ops if the file isn't writable."""
    path = _cache_path(anchor)
    existing = load(anchor)
    filtered = [r for r in existing if r.ip != ip]
    new = [IpRecord(ip=ip, last_seen=time.time()), *filtered][:MAX_ENTRIES]
    payload = {
        "history": [{"ip": r.ip, "last_seen": r.last_seen} for r in new],
    }
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        bus.debug("CACHE", f"recorded {ip} in {path.name}")
    except OSError as e:
        bus.warn("CACHE", f"failed to write {path.name}: {e!s}")
