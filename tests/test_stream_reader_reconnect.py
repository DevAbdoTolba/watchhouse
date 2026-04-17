"""Tests for StreamReader reconnect loop — Phase 1 Plan 01-02 Task 3.

Covers:

* Test 1: ``open()`` fails N times → N ``reconnect_attempt`` log lines → success.
* Test 2: ``read()`` returns ``(False, None)`` many times, then external
  ``release()`` (simulating watchdog); reader detects via ``is_open == False``
  and reconnects.
* Test 3: Sustained-health reset — after 65 s of healthy reads (mocked
  monotonic), backoff resets to 1 s / attempt 0.
* Test 4: Post-open 5-frame drop preserved across reconnect — a real
  ``Mp4FrameSource`` reopen produces 5 more ``frames_corrupted`` before the
  next successful push.
* Test 5: ``stop_event`` interrupts backoff sleep within the delay + 0.1 s.
* Test 6: Structured reconnect log line format.
* Test 7: MP4 sources disable reconnect — EOF exits cleanly.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pytest

import home_cctv  # noqa: F401  — pre-cv2 env setup
from home_cctv.ingest.backoff import JitteredBackoff
from home_cctv.ingest.capture import CaptureStats, Mp4FrameSource
from home_cctv.ingest.stream_reader import FrameQueue, StreamReader
from home_cctv.obs.logging_setup import configure_logging

FIXTURES = Path(__file__).parent / "fixtures"


def _reset_logger() -> None:
    """Clear handlers + restore propagate=True so caplog works.

    Phase 0 ``configure_logging`` sets propagate=False on the ``home_cctv``
    logger tree to prevent double-output through the root. Earlier tests
    (e.g. test_heartbeat) invoke it and the flag persists across tests,
    which blocks caplog. Reset here so this module's caplog assertions are
    isolated from test ordering.
    """
    lg = logging.getLogger("home_cctv")
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.propagate = True
    for name in ("home_cctv.ingest.stream_reader",):
        sub = logging.getLogger(name)
        for h in list(sub.handlers):
            sub.removeHandler(h)
        sub.propagate = True


@pytest.fixture(autouse=True)
def _ensure_logger_propagation() -> None:
    """Reset the home_cctv logger state before each test in this module."""
    _reset_logger()


# ---------------------------------------------------------------- test stubs


class _FlakyStubSource:
    """Live-source stub with configurable open/read failure counts.

    ``open_fail_count`` — the first N calls to ``open()`` raise ``RuntimeError``.
    ``read_fail_count`` — while it's positive, ``read()`` returns ``(False, None)``
    and increments ``stats.decode_errors``; after it reaches 0, returns a
    synthetic frame.

    ``is_open`` reflects external state (opens on successful ``open()``,
    closes on ``release()``).
    """

    is_file_source: bool = False

    def __init__(
        self,
        *,
        source_id: str,
        open_fail_count: int = 0,
        read_fail_count: int = 0,
        succeed_after_first_open: bool = True,
    ) -> None:
        self.source_id = source_id
        self.stats = CaptureStats()
        self._open_fail_budget = open_fail_count
        self._read_fail_budget = read_fail_count
        self._opened = False
        self._released = False
        self.open_calls: int = 0
        self.read_calls: int = 0
        self.release_calls: int = 0
        self._succeed_after_first_open = succeed_after_first_open

    @property
    def is_open(self) -> bool:
        return self._opened and not self._released

    def open(self) -> None:
        self.open_calls += 1
        if self._open_fail_budget > 0:
            self._open_fail_budget -= 1
            raise RuntimeError(f"simulated open failure on attempt {self.open_calls}")
        self._opened = True
        self._released = False
        self.stats.first_frame_monotonic = None  # fresh open

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        self.read_calls += 1
        if not self.is_open:
            return False, None
        if self._read_fail_budget > 0:
            self._read_fail_budget -= 1
            self.stats.decode_errors += 1
            return False, None
        # Produce a synthetic frame and bump stats like _BaseCvCapture would.
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        now = time.monotonic()
        if self.stats.first_frame_monotonic is None:
            self.stats.first_frame_monotonic = now
        self.stats.last_frame_monotonic = now
        self.stats.frames_decoded += 1
        return True, frame

    def release(self) -> None:
        self.release_calls += 1
        self._released = True


# ---------------------------------------------------- Test 1: open() fails 3x


def test_reconnect_after_three_open_failures(caplog: pytest.LogCaptureFixture) -> None:
    fs = _FlakyStubSource(
        source_id="cam-flaky", open_fail_count=3, read_fail_count=0
    )
    stop = threading.Event()
    backoff = JitteredBackoff(rng=__import__("random").Random(42))
    reader = StreamReader(
        camera_id="cam-flaky",
        frame_source=fs,
        stop_event=stop,
        backoff=backoff,
    )
    caplog.set_level(logging.INFO, logger="home_cctv.ingest.stream_reader")
    reader.start()
    # Let it retry — with rng=42 the delays are small (always ≤ ceiling).
    # Give it enough wall time to burn 3 retries (≤ 1+2+4 = 7 s worst case,
    # actual expected ~3 s with seeded RNG).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and fs.open_calls < 4:
        time.sleep(0.05)
    # Now the 4th open should have succeeded, reader is in inner loop.
    # Stop it to let test finish.
    stop.set()
    reader.join(timeout=2.0)

    assert fs.open_calls >= 4, (
        f"expected at least 4 open attempts (3 fail + 1 success); "
        f"got {fs.open_calls}"
    )
    # At least 3 reconnect_attempt log lines.
    reconnects = [
        r for r in caplog.records if "reconnect_attempt" in r.getMessage()
    ]
    assert len(reconnects) >= 3, (
        f"expected ≥3 reconnect_attempt log lines; got {len(reconnects)}"
    )


# ------------------------- Test 2: read fails, external release, is_open=False


def test_reader_detects_external_release_via_is_open() -> None:
    # No open failure, just 20 read failures then external release.
    fs = _FlakyStubSource(
        source_id="cam-watchdog", open_fail_count=0, read_fail_count=10**9
    )
    stop = threading.Event()
    reader = StreamReader(
        camera_id="cam-watchdog",
        frame_source=fs,
        stop_event=stop,
    )
    reader.start()
    # Wait for first open() call.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and fs.open_calls < 1:
        time.sleep(0.02)
    assert fs.open_calls == 1
    assert fs.is_open is True

    # Simulate watchdog calling release() from another thread.
    fs.release()
    assert fs.is_open is False

    # Give the reader time to detect is_open=False and begin reconnect.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and fs.open_calls < 2:
        time.sleep(0.02)

    assert fs.open_calls >= 2, (
        f"reader failed to reconnect after external release; "
        f"open_calls={fs.open_calls}"
    )

    stop.set()
    reader.join(timeout=2.0)


# ------------------------------- Test 3: sustained-health reset to 1 s


def test_sustained_health_resets_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """After 65 simulated seconds of healthy reads, backoff resets to 1 s."""
    # Use a JitteredBackoff that's been advanced so we can observe reset.
    backoff = JitteredBackoff(rng=__import__("random").Random(0))
    # Pre-advance to simulate prior failures.
    backoff.next_delay()  # ceiling → 2
    backoff.next_delay()  # ceiling → 4
    assert backoff.current_ceiling_s == 4.0
    assert backoff.attempt == 2

    fs = _FlakyStubSource(source_id="cam-healthy", open_fail_count=0, read_fail_count=0)
    stop = threading.Event()

    # Monkeypatch time.monotonic inside stream_reader to drive a simulated clock.
    sim_t = [1000.0]

    def fake_mono() -> float:
        # Each call advances by 1 s — after ~65 calls from the first read,
        # 60 s window elapses.
        sim_t[0] += 1.0
        return sim_t[0]

    # Patch the module-level time reference used by StreamReader.
    import home_cctv.ingest.stream_reader as sr
    monkeypatch.setattr(sr.time, "monotonic", fake_mono)

    reader = StreamReader(
        camera_id="cam-healthy",
        frame_source=fs,
        stop_event=stop,
        backoff=backoff,
    )
    reader.start()
    # Wait until the reader has pushed at least 70 frames (each push advances
    # simulated time by ≥1 s).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and fs.stats.frames_decoded < 70:
        time.sleep(0.02)
    stop.set()
    reader.join(timeout=2.0)

    assert fs.stats.frames_decoded >= 70, (
        f"expected ≥70 decoded frames; got {fs.stats.frames_decoded}"
    )
    # Backoff should have been reset.
    assert backoff.current_ceiling_s == 1.0, (
        f"sustained-health reset did not fire; ceiling={backoff.current_ceiling_s}"
    )
    assert backoff.attempt == 0


# ------------------ Test 4: post-open 5-frame drop preserved across reconnect


def test_post_open_five_frame_drop_preserved_across_reconnect() -> None:
    """An Mp4FrameSource reopen must re-enter the 5-frame post-open drop."""
    fs = Mp4FrameSource(
        str(FIXTURES / "sample_720p25.mp4"), source_id="cam0:mp4"
    )
    fs.open()
    # Read enough frames to get past the initial post-open drop.
    for _ in range(50):
        ok, _ = fs.read()
        if ok:
            break
    frames_before = fs.stats.frames_decoded
    corrupted_before = fs.stats.frames_corrupted
    # Reopen (simulating watchdog release + reader reconnect).
    fs.release()
    fs.open()
    # The next 5 reads must all return (False, None) and bump frames_corrupted.
    corrupted_bumps = 0
    for _ in range(5):
        ok, _ = fs.read()
        assert ok is False, "post-open drop should tag first 5 frames as bad"
        corrupted_bumps += 1
    # frames_corrupted should have advanced by exactly 5.
    assert fs.stats.frames_corrupted - corrupted_before == 5, (
        f"expected 5 frames_corrupted bumps post-reopen; got "
        f"{fs.stats.frames_corrupted - corrupted_before}"
    )
    # frames_decoded must NOT have advanced during the drop window.
    assert fs.stats.frames_decoded == frames_before
    fs.release()


# ------------------------ Test 5: stop_event interrupts backoff sleep


def test_stop_event_interrupts_backoff_sleep() -> None:
    """During a backoff sleep, stop_event.set() must end the reader fast."""
    fs = _FlakyStubSource(
        source_id="cam-backoff", open_fail_count=10**9, read_fail_count=0
    )
    stop = threading.Event()
    # Seed backoff to force long-ish delays: use a deterministic RNG that
    # yields delays near ceiling. But easier: after a few failures ceiling
    # is already 8-16 s; we just need to prove stop_event interrupts.
    backoff = JitteredBackoff(rng=__import__("random").Random(1))
    # Pre-advance to get to ceiling=16+ and then ceiling=30.
    for _ in range(6):
        backoff.next_delay()
    # Ceiling is now 30, so next delay could be up to 30 s.

    reader = StreamReader(
        camera_id="cam-backoff",
        frame_source=fs,
        stop_event=stop,
        backoff=backoff,
    )
    reader.start()
    # Wait until the reader is in the backoff sleep — that happens after the
    # first open() failure. Give it 0.5 s to fail once.
    time.sleep(0.3)
    assert fs.open_calls >= 1

    t0 = time.monotonic()
    stop.set()
    reader.join(timeout=3.0)
    elapsed = time.monotonic() - t0
    assert not reader.is_alive(), "reader did not exit within 3 s of stop_event"
    assert elapsed < 1.5, (
        f"stop_event did not interrupt backoff sleep promptly: {elapsed:.3f}s"
    )


# ------------------------------- Test 6: structured log line format


def test_reconnect_log_line_format(caplog: pytest.LogCaptureFixture) -> None:
    fs = _FlakyStubSource(
        source_id="cam-log", open_fail_count=2, read_fail_count=0
    )
    stop = threading.Event()
    backoff = JitteredBackoff(rng=__import__("random").Random(0))
    reader = StreamReader(
        camera_id="cam-log",
        frame_source=fs,
        stop_event=stop,
        backoff=backoff,
    )
    caplog.set_level(logging.WARNING, logger="home_cctv.ingest.stream_reader")
    reader.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and fs.open_calls < 3:
        time.sleep(0.02)
    stop.set()
    reader.join(timeout=2.0)

    pattern = re.compile(
        r"reconnect_attempt camera_id=\S+ attempt=\d+ "
        r"delay_s=\d+\.\d+ ceiling_s=\d+\.\d+"
    )
    matches = [r for r in caplog.records if pattern.search(r.getMessage())]
    assert len(matches) >= 2, (
        f"expected ≥2 matching reconnect log lines; got {len(matches)}\n"
        f"records={[r.getMessage() for r in caplog.records]}"
    )


# --------------------- Test 7: MP4 source disables reconnect (clean EOF)


def test_mp4_source_disables_reconnect_on_eof() -> None:
    fs = Mp4FrameSource(
        str(FIXTURES / "sample_720p25.mp4"), source_id="cam0:mp4"
    )
    stop = threading.Event()
    reader = StreamReader(
        camera_id="cam0:mp4",
        frame_source=fs,
        stop_event=stop,
    )
    t0 = time.monotonic()
    reader.start()
    reader.join(timeout=15.0)
    elapsed = time.monotonic() - t0
    assert not reader.is_alive(), f"MP4 reader did not self-exit ({elapsed:.2f}s)"
    assert stop.is_set() is False, (
        "MP4 reader should self-exit via EOF heuristic, not via stop_event"
    )
    assert fs.stats.frames_decoded > 0
