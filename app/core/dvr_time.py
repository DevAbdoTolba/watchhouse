"""DVR time offset — make every time the user SEES match the DVR's clock.

The DVR doesn't observe daylight-saving, so its burned-in overlay runs e.g. 1 h
behind the PC. This is a pure DISPLAY transform: a single configurable offset
(DVR_TIME_OFFSET_MINUTES, e.g. -60) is added wherever a timestamp is rendered
for a human — Telegram captions, the events list, the timeline axis, clocks.

Internals (recording filenames, segment lookup, pruning, seeking, timeline
POSITIONING) stay on the raw PC clock, so nothing breaks; only the labels move.
Set the offset once at startup from Settings; default 0 = no shift.
"""

from __future__ import annotations

from datetime import datetime, timedelta

_offset_min: float = 0.0
_DAY = 86_400.0


def set_offset_minutes(minutes) -> None:
    global _offset_min
    try:
        _offset_min = float(minutes)
    except (TypeError, ValueError):
        _offset_min = 0.0


def offset_minutes() -> float:
    return _offset_min


def shift(dt: "datetime | None") -> "datetime | None":
    """A datetime shifted into DVR display time (None passes through)."""
    if dt is None or _offset_min == 0.0:
        return dt
    return dt + timedelta(minutes=_offset_min)


def now() -> datetime:
    """Current wall-clock in DVR display time (for live 'when' / status clock)."""
    return datetime.now() + timedelta(minutes=_offset_min)


def shift_seconds_of_day(s: float) -> float:
    """Shift a seconds-of-day value (timeline axis tick) into DVR time, wrapped
    to [0, 86400). Positioning uses the raw value; only the LABEL uses this."""
    if _offset_min == 0.0:
        return s
    return (s + _offset_min * 60.0) % _DAY
