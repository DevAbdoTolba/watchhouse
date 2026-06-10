"""Lightweight runtime performance probes (source "PERF" on the log bus).

Performance claims need numbers, not impressions: BUG-012 hid for weeks
because nothing measured the UI thread. These probes are always-on but
cheap — a half-second timer tick and a few counters — and surface only
through the admin console (Ctrl+L, filter PERF) and the tile tooltips.
No new chrome.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, Qt, QTimer

from app.core.log import bus

_TICK_MS = 500
_REPORT_TICKS = 60      # one summary line per ~30 s window
_WARN_LAG_MS = 200.0    # a tick this late means the UI thread stalled


class UiLoopProbe(QObject):
    """Measures GUI event-loop responsiveness from inside the loop itself.

    A periodic timer records how late each tick fires versus its schedule;
    lag spikes mean something blocked the UI thread (paint storms, disk
    walks, synchronous I/O). One avg/max summary line per window; WARN when
    the worst tick crossed _WARN_LAG_MS, so stalls stand out in the console
    even when nobody is watching live."""

    def __init__(self, parent: QObject | None = None,
                 report_ticks: int = _REPORT_TICKS) -> None:
        super().__init__(parent)
        self._report_ticks = max(1, report_ticks)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._last: float | None = None
        self._ticks = 0
        self._lag_sum = 0.0
        self._lag_max = 0.0

    def start(self) -> None:
        if self._timer.isActive():
            return  # showEvent can fire repeatedly (restore from minimize)
        self._last = time.monotonic()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def worst_lag_ms(self) -> float:
        """Worst tick lag seen in the current window."""
        return self._lag_max

    def _tick(self) -> None:
        now = time.monotonic()
        if self._last is not None:
            lag_ms = max(0.0, (now - self._last) * 1000.0 - _TICK_MS)
            self._lag_sum += lag_ms
            self._lag_max = max(self._lag_max, lag_ms)
            self._ticks += 1
        self._last = now
        if self._ticks >= self._report_ticks:
            self._report()

    def _report(self) -> None:
        avg = self._lag_sum / max(1, self._ticks)
        line = (f"ui-loop lag avg {avg:.1f} ms, max {self._lag_max:.0f} ms "
                f"over {self._ticks} ticks")
        if self._lag_max >= _WARN_LAG_MS:
            bus.warn("PERF", line)
        else:
            bus.info("PERF", line)
        self._ticks = 0
        self._lag_sum = 0.0
        self._lag_max = 0.0
