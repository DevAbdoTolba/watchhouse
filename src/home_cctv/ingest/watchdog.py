"""Per-camera read watchdog — Phase 1 Plan 01-02 Task 2.

A ``ReadWatchdog`` runs in its own daemon thread alongside each ``StreamReader``.
Every ``check_interval_s`` (default 1 s) it inspects ``CaptureStats.last_frame_monotonic``
and, if ``now - last > threshold_s`` (default 10 s), calls
``frame_source.release()`` from the watchdog thread. The reader's blocked
``cap.read()`` then returns ``(False, None)`` and the reader's outer loop
detects the release via the ``FrameSource.is_open`` Protocol property and
enters reconnect-with-backoff (Task 3).

This is the only reliable way to unblock a stuck ``cv2.VideoCapture.read()``
on the FFmpeg backend in WSL2 — see ``.planning/research/PITFALLS.md §1.1``
and CONTEXT §D-08 / §G-01. It is "technically undefined but reliable in
practice" per CONTEXT G-01; the alternatives (``ThreadPoolExecutor.submit``
with ``.result(timeout=...)`` or process-per-camera) were explicitly
rejected because they contradict D-01 (thread-only concurrency model).

Idempotency across stall episodes:

* The watchdog stores ``_last_release_at_frame_ts`` = the ``last_frame_monotonic``
  value it last released against. Until the reader produces a new frame
  (changing that value), subsequent ticks treat the stall as the *same episode*
  and do NOT double-fire a release.
* Once the reader recovers and advances ``last_frame_monotonic``, the
  watchdog is re-armed naturally — a fresh stall triggers a fresh release.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from home_cctv.ingest.capture import CaptureStats, FrameSource

_LOG = logging.getLogger("home_cctv.ingest.watchdog")

#: CONTEXT §D-08 — every stuck ``cap.read()`` must be force-released within 10 s.
DEFAULT_STALL_THRESHOLD_S: float = 10.0

#: Poll cadence. 1 s is enough resolution for a 10-s threshold.
DEFAULT_CHECK_INTERVAL_S: float = 1.0


class ReadWatchdog(threading.Thread):
    """Daemon thread that force-releases a stuck ``FrameSource``.

    Lifecycle:

    1. ``start()`` spawns the daemon thread.
    2. Every ``check_interval_s`` seconds the thread inspects
       ``stats.last_frame_monotonic``:
        * ``None`` → pre-first-frame grace; skip.
        * equal to ``_last_release_at_frame_ts`` → same stall episode we
          already released against; skip (prevents double-fire).
        * ``now - last > threshold_s`` → fire: increment ``hang_events`` and
          ``release_count``, log a warning, call ``frame_source.release()``,
          update ``_last_release_at_frame_ts``.
    3. ``stop_event.wait(check_interval_s)`` is the sleep mechanism, so
       ``stop_event.set()`` ends the thread within one tick.
    """

    def __init__(
        self,
        *,
        camera_id: str,
        frame_source: FrameSource,
        stats: CaptureStats,
        stop_event: threading.Event,
        threshold_s: float = DEFAULT_STALL_THRESHOLD_S,
        check_interval_s: float = DEFAULT_CHECK_INTERVAL_S,
    ) -> None:
        super().__init__(name=f"Watchdog[{camera_id}]", daemon=True)
        self.camera_id = camera_id
        self.frame_source = frame_source
        self.stats = stats
        self.stop_event = stop_event
        self.threshold_s = threshold_s
        self.check_interval_s = check_interval_s
        self.release_count: int = 0
        self._last_release_at_frame_ts: Optional[float] = None

    def run(self) -> None:  # noqa: D401 — threading.Thread override
        # ``stop_event.wait(check_interval)`` returns True as soon as stop
        # fires, so the loop exits within one tick without a busy poll.
        while not self.stop_event.wait(self.check_interval_s):
            last = self.stats.last_frame_monotonic
            if last is None:
                # Pre-first-frame grace — reader hasn't decoded anything yet.
                continue
            if last == self._last_release_at_frame_ts:
                # Same stall episode we already released against. The reader
                # has not produced a new frame since our last release, so
                # firing again would be a double-tap on the same event.
                continue
            age = time.monotonic() - last
            if age > self.threshold_s:
                self.stats.hang_events += 1
                self.release_count += 1
                self._last_release_at_frame_ts = last
                _LOG.warning(
                    "watchdog_release camera_id=%s age_s=%.2f "
                    "threshold_s=%.2f hang_events=%d",
                    self.camera_id,
                    age,
                    self.threshold_s,
                    self.stats.hang_events,
                )
                try:
                    self.frame_source.release()
                except Exception as exc:  # pragma: no cover — defensive
                    # CONTEXT G-01 acknowledges cross-thread release is
                    # "technically undefined" — swallow any backend
                    # exception so the watchdog itself doesn't die.
                    _LOG.warning(
                        "watchdog_release_failed camera_id=%s err=%r",
                        self.camera_id,
                        exc,
                    )


__all__ = [
    "ReadWatchdog",
    "DEFAULT_STALL_THRESHOLD_S",
    "DEFAULT_CHECK_INTERVAL_S",
]
