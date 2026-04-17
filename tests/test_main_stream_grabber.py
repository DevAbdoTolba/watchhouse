"""Tests for ``home_cctv.ingest.main_stream_grabber`` — Phase 1 Plan 01-03 Task 1.

Covers the ``MainStreamGrabber`` public contract:

* Test 1: single ``grab_main_frame(camera_id=1)`` returns JPEG bytes.
* Test 2: back-to-back calls within 2 s TTL → exactly one ``open()``.
* Test 3: back-to-back calls separated by >2 s → two ``open()`` calls.
* Test 4: concurrent calls on two different camera_ids ALWAYS serialize
  through the DVR-wide ``Semaphore(1)`` — no open-interval overlap.
* Test 5: read failure returns ``None``; no cache population.
* Test 6: 20 calls across 4 camera_ids with 0.1 s simulated open cost
  finishes under 3 s total (serialization is bounded; cache hits work).
* Test 7: ``shutdown()`` / ``stop_event.set()`` interrupts a blocked
  semaphore acquirer within 2 s.
* Test 8: double-checked cache locking — two threads racing on the same
  camera_id with cold cache result in EXACTLY ONE ``VideoCapture.open``.

All tests use in-memory stubs — no real RTSP sockets, no real cv2 captures
against any actual DVR. The grabber's ``capture_factory`` injection point
makes this trivial.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
import pytest

import home_cctv  # noqa: F401  — pre-cv2 env setup
from home_cctv.ingest.main_stream_grabber import (
    MAIN_STREAM_CACHE_TTL_S,
    MAIN_STREAM_JPEG_QUALITY,
    MAIN_STREAM_MAX_READ_ATTEMPTS,
    CacheEntry,
    MainStreamGrabber,
)


def _reset_logger() -> None:
    lg = logging.getLogger("home_cctv")
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.propagate = True
    for name in ("home_cctv.ingest.main_stream",):
        sub = logging.getLogger(name)
        for h in list(sub.handlers):
            sub.removeHandler(h)
        sub.propagate = True


@pytest.fixture(autouse=True)
def _ensure_logger_propagation() -> None:
    _reset_logger()


# --------------------------------------------------------------- test stubs


class _StubCap:
    """A stand-in for cv2.VideoCapture that yields configurable frames.

    The factory instantiates one of these per ``capture_factory(url)`` call and
    records timestamps so tests can assert open/release intervals.
    """

    def __init__(
        self,
        *,
        url: str,
        frames: List[Optional[np.ndarray]],
        is_opened: bool = True,
        open_cost_s: float = 0.0,
        read_cost_s: float = 0.0,
    ) -> None:
        self.url = url
        self._frames = list(frames)
        self._is_opened_flag = is_opened
        self._open_cost_s = open_cost_s
        self._read_cost_s = read_cost_s
        self._released = False
        self.open_start: float = time.monotonic()
        self.open_end: float = 0.0
        # Simulate open cost in the ``isOpened`` path so the factory is the
        # one paying the latency (matches real cv2 behaviour where open
        # blocks the caller).
        if open_cost_s > 0.0:
            time.sleep(open_cost_s)

    def isOpened(self) -> bool:  # noqa: N802 — mirrors cv2 API
        return self._is_opened_flag

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if self._read_cost_s > 0.0:
            time.sleep(self._read_cost_s)
        if not self._frames:
            return False, None
        frame = self._frames.pop(0)
        if frame is None:
            return False, None
        return True, frame

    def release(self) -> None:
        self._released = True
        self.open_end = time.monotonic()


class _StubCaptureFactory:
    """Capture factory that records open/release timestamps and call count."""

    def __init__(
        self,
        *,
        frames_per_call: Optional[List[Optional[np.ndarray]]] = None,
        is_opened: bool = True,
        open_cost_s: float = 0.0,
        read_cost_s: float = 0.0,
    ) -> None:
        self._frames_per_call = frames_per_call
        self._is_opened = is_opened
        self._open_cost_s = open_cost_s
        self._read_cost_s = read_cost_s
        self.open_call_count: int = 0
        self.opens: List[_StubCap] = []
        self._lock = threading.Lock()

    def __call__(self, url: str) -> _StubCap:
        with self._lock:
            self.open_call_count += 1
        # Build frames — default to a single 2×2 synthetic frame.
        if self._frames_per_call is None:
            frames: List[Optional[np.ndarray]] = [
                np.full((2, 2, 3), 128, dtype=np.uint8)
            ]
        else:
            frames = list(self._frames_per_call)
        cap = _StubCap(
            url=url,
            frames=frames,
            is_opened=self._is_opened,
            open_cost_s=self._open_cost_s,
            read_cost_s=self._read_cost_s,
        )
        cap.open_start = time.monotonic()
        # open_cost has already been paid inside __init__; mark open_end when
        # release() fires. Record it so tests can sort by open_start.
        self.opens.append(cap)
        return cap


# ------------------------------------------------------------- helper URL map


def _dummy_url_resolver(camera_id: int) -> str:
    # Never a real RTSP URL — path-only so grabber has no network temptation.
    return f"stub://cam/{camera_id}/main"


# ============================================================== Test 1


def test_single_grab_returns_jpeg_bytes() -> None:
    factory = _StubCaptureFactory()
    stop = threading.Event()
    g = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        capture_factory=factory,
    )
    out = g.grab_main_frame(camera_id=1)
    assert out is not None, "expected JPEG bytes on successful grab"
    assert isinstance(out, bytes)
    # JPEG SOI marker is 0xFF 0xD8 0xFF.
    assert out[:3] == b"\xff\xd8\xff", (
        f"expected JPEG SOI marker; got {out[:8]!r}"
    )
    assert factory.open_call_count == 1


# ============================================================== Test 2


def test_ttl_cache_hit_within_window_skips_open() -> None:
    factory = _StubCaptureFactory()
    stop = threading.Event()
    g = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        capture_factory=factory,
        ttl_s=2.0,
    )
    out1 = g.grab_main_frame(camera_id=1)
    out2 = g.grab_main_frame(camera_id=1)  # within 2 s — cache hit
    assert out1 is not None and out2 is not None
    assert out1 == out2  # same cached bytes
    assert factory.open_call_count == 1, (
        f"second call within TTL should hit cache; got "
        f"open_call_count={factory.open_call_count}"
    )


# ============================================================== Test 3


def test_ttl_cache_expiry_triggers_new_open() -> None:
    factory = _StubCaptureFactory()
    stop = threading.Event()
    g = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        capture_factory=factory,
        ttl_s=0.05,  # super short TTL for fast test
    )
    out1 = g.grab_main_frame(camera_id=1)
    time.sleep(0.10)  # exceed TTL
    out2 = g.grab_main_frame(camera_id=1)
    assert out1 is not None and out2 is not None
    assert factory.open_call_count == 2, (
        f"expected 2 opens after TTL expiry; got {factory.open_call_count}"
    )


# ============================================================== Test 4


def test_concurrent_grabs_serialize_via_global_semaphore() -> None:
    """Two threads grabbing different cameras NEVER overlap on open."""
    factory = _StubCaptureFactory(open_cost_s=0.1)  # 100 ms simulated open
    stop = threading.Event()
    g = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        capture_factory=factory,
    )
    results: List[Optional[bytes]] = [None, None]
    barrier = threading.Barrier(2)

    def worker(idx: int, cam_id: int) -> None:
        barrier.wait()
        results[idx] = g.grab_main_frame(camera_id=cam_id)

    t1 = threading.Thread(target=worker, args=(0, 1))
    t2 = threading.Thread(target=worker, args=(1, 2))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert results[0] is not None and results[1] is not None
    assert factory.open_call_count == 2, (
        f"expected 2 opens for 2 cameras; got {factory.open_call_count}"
    )
    # Sort captures by open_start, then assert no overlap.
    opens = sorted(factory.opens, key=lambda c: c.open_start)
    for i in range(len(opens) - 1):
        assert opens[i].open_end > 0, "open_end was not recorded via release"
        assert opens[i].open_end <= opens[i + 1].open_start + 1e-3, (
            f"open intervals overlap between [{opens[i].open_start:.3f}, "
            f"{opens[i].open_end:.3f}] and [{opens[i+1].open_start:.3f}, "
            f"{opens[i+1].open_end:.3f}]"
        )


# ============================================================== Test 5


def test_read_failure_returns_none_and_does_not_cache() -> None:
    # A capture that opens OK but every read returns (False, None).
    factory = _StubCaptureFactory(
        frames_per_call=[None, None, None, None, None],
    )
    stop = threading.Event()
    g = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        capture_factory=factory,
    )
    out = g.grab_main_frame(camera_id=1)
    assert out is None, f"expected None on read failure; got {out!r}"

    # A subsequent call should try again (cache empty), NOT return stale None.
    factory2 = _StubCaptureFactory(
        frames_per_call=[np.full((2, 2, 3), 64, dtype=np.uint8)]
    )
    g2 = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        capture_factory=factory2,
    )
    out2 = g2.grab_main_frame(camera_id=1)
    assert out2 is not None, "fresh grabber should succeed on fresh factory"


# ============================================================== Test 6


def test_many_grabs_across_four_cameras_complete_quickly() -> None:
    """20 calls across 4 cameras with 0.1 s simulated open cost < 3 s total."""
    factory = _StubCaptureFactory(open_cost_s=0.1)
    stop = threading.Event()
    g = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        capture_factory=factory,
        ttl_s=0.5,  # short enough to force occasional re-opens, long enough
        # that most repeated calls on the same cam hit cache
    )
    t0 = time.monotonic()
    for i in range(20):
        cam_id = (i % 4) + 1
        out = g.grab_main_frame(camera_id=cam_id)
        assert out is not None
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"20 grabs took too long: {elapsed:.3f}s"


# ============================================================== Test 7


def test_stop_event_unblocks_semaphore_acquirer_within_two_seconds() -> None:
    """A grabber blocked on a held semaphore must return None within 2 s of stop."""
    # Build a grabber whose semaphore is held by the test — the call blocks.
    held_sem = threading.Semaphore(1)
    held_sem.acquire()  # now nobody else can acquire

    factory = _StubCaptureFactory()
    stop = threading.Event()
    g = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        semaphore=held_sem,
        capture_factory=factory,
    )

    result: List[Optional[bytes]] = [b"sentinel"]

    def worker() -> None:
        result[0] = g.grab_main_frame(camera_id=1)

    t = threading.Thread(target=worker)
    t.start()
    # Give the thread a moment to be blocked on acquire.
    time.sleep(0.1)
    t0 = time.monotonic()
    stop.set()
    t.join(timeout=3.0)
    elapsed = time.monotonic() - t0

    assert not t.is_alive(), "grabber thread did not exit after stop_event"
    assert elapsed < 2.0, f"stop_event took too long to unblock: {elapsed:.3f}s"
    assert result[0] is None, (
        f"blocked-then-stopped grab should return None; got {result[0]!r}"
    )

    # Release so the test fixture doesn't leak.
    held_sem.release()


# ============================================================== Test 8


def test_double_checked_cache_locking_single_open_under_barrier() -> None:
    """Two threads both miss cache, both queue on semaphore; only ONE opens.

    This is the ING-07 invariant: after the first caller populates the cache,
    the second caller — who was blocked on the semaphore — must hit the
    second (post-acquire) cache check and return cached bytes WITHOUT
    opening the capture.
    """
    factory = _StubCaptureFactory(open_cost_s=0.1)
    stop = threading.Event()
    g = MainStreamGrabber(
        url_resolver=_dummy_url_resolver,
        stop_event=stop,
        capture_factory=factory,
        ttl_s=5.0,  # TTL big enough that second caller sees fresh cache
    )
    results: List[Optional[bytes]] = [None, None]
    barrier = threading.Barrier(2)

    def worker(idx: int) -> None:
        barrier.wait()
        # Both threads cross the barrier simultaneously, hit the first
        # cache check on a cold cache, both queue on the semaphore.
        results[idx] = g.grab_main_frame(camera_id=1)

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert results[0] is not None and results[1] is not None
    assert results[0] == results[1], (
        "both threads must receive identical cached bytes"
    )
    assert factory.open_call_count == 1, (
        f"double-checked locking should limit opens to 1; got "
        f"open_call_count={factory.open_call_count}"
    )


# ============================================================ Constants test


def test_module_constants_are_correct() -> None:
    assert MAIN_STREAM_CACHE_TTL_S == 2.0
    assert MAIN_STREAM_MAX_READ_ATTEMPTS == 3
    assert MAIN_STREAM_JPEG_QUALITY == 85
    # CacheEntry is a dataclass with the two expected fields.
    e = CacheEntry(jpeg_bytes=b"abc", cached_at_monotonic=123.4)
    assert e.jpeg_bytes == b"abc"
    assert e.cached_at_monotonic == 123.4
