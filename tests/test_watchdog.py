"""Tests for ``home_cctv.ingest.watchdog`` — Phase 1 Plan 01-02 Task 2.

Covers the ``ReadWatchdog`` cross-thread forced-release mechanic:

* Healthy reader (fresh ``last_frame_monotonic``) → no release.
* Stale reader (no advance beyond threshold) → exactly one release per episode.
* ``hang_events`` increments each time the watchdog fires.
* ``stop_event`` terminates the watchdog within one check interval.
* Pre-first-frame grace (``last_frame_monotonic is None``) never triggers release.
* Re-arm: a second stall after recovery triggers a second release.
"""
from __future__ import annotations

import threading
import time

from home_cctv.ingest.capture import CaptureStats
from home_cctv.ingest.watchdog import (
    DEFAULT_CHECK_INTERVAL_S,
    DEFAULT_STALL_THRESHOLD_S,
    ReadWatchdog,
)


class _StubSource:
    """Minimal ``FrameSource`` stub for watchdog tests.

    ``release()`` is counted; it does not raise. ``CaptureStats`` is held by
    the test so it can freeze or advance ``last_frame_monotonic`` at will.
    """

    is_file_source: bool = False

    def __init__(self, *, source_id: str, stats: CaptureStats) -> None:
        self.source_id = source_id
        self.stats = stats
        self.release_count: int = 0

    @property
    def is_open(self) -> bool:  # Required by future FrameSource Protocol
        return True

    def open(self) -> None:  # pragma: no cover — not exercised here
        pass

    def read(self):  # pragma: no cover — not exercised here
        return False, None

    def release(self) -> None:
        self.release_count += 1


# ----------------------------------------------------------------- constants


def test_default_constants_correct() -> None:
    assert DEFAULT_STALL_THRESHOLD_S == 10.0
    assert DEFAULT_CHECK_INTERVAL_S == 1.0


# ---------------------------------------------------- Test 1: healthy => no release


def test_healthy_reader_never_triggers_release() -> None:
    stats = CaptureStats()
    stats.last_frame_monotonic = time.monotonic()
    src = _StubSource(source_id="cam0", stats=stats)
    stop = threading.Event()
    wd = ReadWatchdog(
        camera_id="cam0",
        frame_source=src,
        stats=stats,
        stop_event=stop,
        threshold_s=10.0,
        check_interval_s=0.05,
    )
    wd.start()
    # Continuously advance last_frame_monotonic faster than the threshold.
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.5:
        stats.last_frame_monotonic = time.monotonic()
        time.sleep(0.02)
    stop.set()
    wd.join(timeout=1.0)
    assert src.release_count == 0, (
        f"healthy reader should not be released; got {src.release_count}"
    )
    assert stats.hang_events == 0


# ----------------------------- Test 2: stale => exactly one release per episode


def test_stale_reader_releases_exactly_once_per_episode() -> None:
    stats = CaptureStats()
    # Pretend the reader got one frame far in the past.
    stats.last_frame_monotonic = time.monotonic() - 5.0
    src = _StubSource(source_id="cam0", stats=stats)
    stop = threading.Event()
    wd = ReadWatchdog(
        camera_id="cam0",
        frame_source=src,
        stats=stats,
        stop_event=stop,
        threshold_s=0.2,
        check_interval_s=0.05,
    )
    wd.start()
    # Run for 0.5 s without advancing last_frame_monotonic.
    time.sleep(0.5)
    stop.set()
    wd.join(timeout=1.0)
    assert src.release_count == 1, (
        f"expected exactly 1 release per stall episode; got {src.release_count}"
    )


# ----------------------------------------- Test 3: hang_events increments


def test_hang_events_increments_on_release() -> None:
    stats = CaptureStats()
    stats.last_frame_monotonic = time.monotonic() - 5.0
    src = _StubSource(source_id="cam0", stats=stats)
    stop = threading.Event()
    wd = ReadWatchdog(
        camera_id="cam0",
        frame_source=src,
        stats=stats,
        stop_event=stop,
        threshold_s=0.2,
        check_interval_s=0.05,
    )
    assert stats.hang_events == 0
    wd.start()
    time.sleep(0.4)
    stop.set()
    wd.join(timeout=1.0)
    assert stats.hang_events == 1
    assert src.release_count == 1


# ------------------------------------------------- Test 4: stop_event ends thread


def test_stop_event_terminates_watchdog_within_check_interval() -> None:
    stats = CaptureStats()
    stats.last_frame_monotonic = time.monotonic()
    src = _StubSource(source_id="cam0", stats=stats)
    stop = threading.Event()
    wd = ReadWatchdog(
        camera_id="cam0",
        frame_source=src,
        stats=stats,
        stop_event=stop,
        threshold_s=10.0,
        check_interval_s=0.1,
    )
    wd.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    stop.set()
    wd.join(timeout=1.0)
    elapsed = time.monotonic() - t0
    assert not wd.is_alive(), "watchdog did not exit after stop_event.set()"
    assert elapsed < 0.3, f"stop took too long: {elapsed:.3f}s"


# -------------------------------- Test 5: pre-first-frame grace (None) is idle


def test_prefirst_frame_grace_does_not_release() -> None:
    stats = CaptureStats()
    assert stats.last_frame_monotonic is None
    src = _StubSource(source_id="cam0", stats=stats)
    stop = threading.Event()
    wd = ReadWatchdog(
        camera_id="cam0",
        frame_source=src,
        stats=stats,
        stop_event=stop,
        threshold_s=0.1,
        check_interval_s=0.05,
    )
    wd.start()
    # Even after the nominal threshold, release must not fire because
    # last_frame_monotonic is still None.
    time.sleep(0.4)
    stop.set()
    wd.join(timeout=1.0)
    assert src.release_count == 0
    assert stats.hang_events == 0


# ---------- Test 6: re-arm — second stall after recovery triggers second release


def test_watchdog_rearms_on_recovery_then_second_stall() -> None:
    stats = CaptureStats()
    stats.last_frame_monotonic = time.monotonic() - 5.0
    src = _StubSource(source_id="cam0", stats=stats)
    stop = threading.Event()
    wd = ReadWatchdog(
        camera_id="cam0",
        frame_source=src,
        stats=stats,
        stop_event=stop,
        threshold_s=0.2,
        check_interval_s=0.05,
    )
    wd.start()
    # Episode 1 — stall → release #1.
    time.sleep(0.35)
    assert src.release_count == 1
    # Recovery — advance last_frame_monotonic to a FRESH value.
    stats.last_frame_monotonic = time.monotonic()
    # Hold healthy for a little while (watchdog should not fire again).
    for _ in range(5):
        stats.last_frame_monotonic = time.monotonic()
        time.sleep(0.04)
    assert src.release_count == 1, (
        "watchdog fired a second time during healthy window"
    )
    # Episode 2 — freeze last_frame_monotonic at the new fresh timestamp so it
    # goes stale again (a new episode, different from the first because
    # last_frame_monotonic value has advanced).
    frozen = stats.last_frame_monotonic
    time.sleep(0.4)
    # Verify the value didn't advance (sanity — nothing in test wrote to it).
    assert stats.last_frame_monotonic == frozen
    stop.set()
    wd.join(timeout=1.0)
    assert src.release_count == 2, (
        f"expected second release on second stall; got {src.release_count}"
    )
    assert stats.hang_events == 2
