"""Permanent 'pinned' footage — ranges the retention pruner must NEVER delete.

Blue-layer footage comes from three places (see the timeline roadmap):
  1. manually-imported clips (the imported/ folder — already pruner-protected);
  2. a recording range the user pinned ('keep this');
  3. a live 'keep new recording' flag — everything from `keep_from` onward is
     kept, with a running size/length the UI shows.

State persists to <env_dir>/.cctv-pins.json (gitignored). Ranges are wall-clock
and global (apply to every camera), matching how a pinned moment or a kept
window spans all four angles at once.

The pruner reads this on every sweep; the UI mutates it. Both go through this
one module so the "never delete pinned" rule lives in exactly one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_FILE = ".cctv-pins.json"


def _state_path(env_path) -> Path:
    base = Path(env_path).parent if env_path else Path.cwd()
    return base / _FILE


@dataclass
class Pins:
    path: Path
    ranges: list = field(default_factory=list)   # [(start dt, end dt)]
    keep_from: datetime | None = None            # live "keep everything since"

    @classmethod
    def load(cls, env_path) -> "Pins":
        p = _state_path(env_path)
        ranges: list = []
        keep_from = None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for r in data.get("ranges", []):
                try:
                    ranges.append((datetime.fromisoformat(r["start"]),
                                   datetime.fromisoformat(r["end"])))
                except (KeyError, ValueError, TypeError):
                    continue
            kf = data.get("keep_from")
            keep_from = datetime.fromisoformat(kf) if kf else None
        except (OSError, ValueError):
            pass
        return cls(p, ranges, keep_from)

    def save(self) -> None:
        data = {
            "ranges": [
                {"start": s.isoformat(timespec="seconds"),
                 "end": e.isoformat(timespec="seconds")} for s, e in self.ranges
            ],
            "keep_from": (self.keep_from.isoformat(timespec="seconds")
                          if self.keep_from else None),
        }
        try:
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    # --- mutations (UI side) ---

    def add_range(self, start: datetime, end: datetime) -> None:
        if end < start:
            start, end = end, start
        self.ranges.append((start, end))
        self.save()

    def set_keep_from(self, when: datetime) -> None:
        self.keep_from = when
        self.save()

    def stop_keep(self, now: datetime) -> None:
        """Freeze the live-kept window [keep_from, now] into a permanent range so
        it survives after the flag is turned off, then clear the flag."""
        if self.keep_from is not None:
            self.ranges.append((self.keep_from, now))
            self.keep_from = None
            self.save()

    # --- queries (pruner + UI) ---

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """True if [start, end] intersects any pinned range or the live keep
        window — i.e. a segment touching it must be kept."""
        # `end` is the segment's exclusive end, so a segment ending exactly at
        # keep_from holds no kept footage -> strict '>'.
        if self.keep_from is not None and end > self.keep_from:
            return True
        return any(s <= end and start <= e for s, e in self.ranges)

    def all_spans(self, now: datetime | None = None) -> list:
        """Every blue span for drawing: fixed ranges + the open keep window."""
        spans = list(self.ranges)
        if self.keep_from is not None:
            spans.append((self.keep_from, now or self.keep_from))
        return spans
