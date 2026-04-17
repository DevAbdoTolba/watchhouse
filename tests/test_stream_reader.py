"""Tests for ``home_cctv.ingest.stream_reader`` — Phase 1 Plan 01 Task 1.

Covers:

* FrameQueue drop-oldest semantics (maxlen=2, observable drop_count).
* StreamReader thread lifecycle against an ``Mp4FrameSource`` fixture.
* StreamReader tuple shape: ``(ndarray, monotonic_ts)``.
* Prompt exit on ``stop_event.set()`` (daemon cleanup).
* Per-camera LoggerAdapter prefix propagation.
* File-source EOF heuristic: exit when ``is_file_source`` AND
  ``decode_errors > 0`` AND ``frames_decoded > 0``.
* Live-source resilience: decode errors alone must NOT end the loop.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pytest

import home_cctv  # noqa: F401  — pre-cv2 env setup
from home_cctv.ingest.capture import CaptureStats, Mp4FrameSource
from home_cctv.ingest.stream_reader import FrameQueue, StreamReader
from home_cctv.obs.logging_setup import configure_logging

FIXTURES = Path(__file__).parent / "fixtures"


def _reset_logger() -> None:
    lg = logging.getLogger("home_cctv")
    for h in list(lg.handlers):
        lg.removeHandler(h)


# ------------------------------------------------------------------ FrameQueue


def test_framequeue_maxlen_is_two_and_drops_oldest() -> None:
    q = FrameQueue(maxlen=2)
    # Push 10 unique sentinels; only the last 2 should survive; drop_count = 8.
    for i in range(10):
        frame = np.full((4, 4, 3), i, dtype=np.uint8)
        q.put((frame, float(i)))
    assert len(q) == 2
    assert q.push_count == 10
    assert q.drop_count == 8
    latest = q.get_latest()
    assert latest is not None
    _, ts = latest
    assert ts == 9.0


def test_framequeue_get_latest_and_pop_oldest_empty() -> None:
    q = FrameQueue(maxlen=2)
    assert q.get_latest() is None
    assert q.pop_oldest() is None


# ----------------------------------------------------------- StreamReader core


class _StubLiveSource:
    """Minimal ``FrameSource`` that mimics a live RTSP feed.

    ``read()`` returns ``(False, None)`` ``decode_error_count`` times, then
    blocks on ``block_event`` forever (caller must set ``stop_event`` on the
    reader to unblock).
    """

    is_file_source: bool = False

    def __init__(self, *, source_id: str, decode_error_count: int) -> None:
        self.source_id = source_id
        self.stats = CaptureStats()
        self._decode_error_count = decode_error_count
        self._errors_emitted = 0
        self._block_event = threading.Event()
        self._opened = False
        self.released = 0

    def open(self) -> None:
        self._opened = True

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if self._errors_emitted < self._decode_error_count:
            self._errors_emitted += 1
            self.stats.decode_errors += 1
            return False, None
        # Past the error budget — block until unblocked, then report failure.
        self._block_event.wait(timeout=0.05)
        self.stats.decode_errors += 1
        return False, None

    def release(self) -> None:
        self.released += 1
        self._block_event.set()


def test_stream_reader_emits_ndarray_and_monotonic_ts_from_mp4() -> None:
    fs = Mp4FrameSource(
        str(FIXTURES / "sample_720p25.mp4"), source_id="cam0:mp4"
    )
    stop = threading.Event()
    reader = StreamReader(
        camera_id="cam0:mp4", frame_source=fs, stop_event=stop
    )
    t0 = time.monotonic()
    reader.start()
    # Let the MP4 play to EOF; is_file_source heuristic should self-exit.
    reader.join(timeout=10.0)
    t1 = time.monotonic()
    assert not reader.is_alive(), "reader did not exit on EOF"
    item = reader.queue.get_latest()
    assert item is not None, "expected at least one queued frame"
    frame, ts = item
    assert isinstance(frame, np.ndarray)
    assert frame.ndim == 3
    # ts is a time.monotonic() value captured at enqueue — must be within
    # the window the reader actually ran in.
    assert t0 <= ts <= t1
    # Sanity: the reader actually pushed some frames through the drop-oldest
    # deque during playback.
    assert reader.queue.push_count >= 1


def test_stream_reader_drop_oldest_on_no_consumer() -> None:
    """Pushing 10+ frames with a queue size of 2 leaves exactly 2."""
    q = FrameQueue(maxlen=2)
    for i in range(10):
        frame = np.full((4, 4, 3), i, dtype=np.uint8)
        q.put((frame, float(i)))
    assert len(q) == 2
    assert q.drop_count == 8


def test_stream_reader_exits_within_half_second_on_stop_event() -> None:
    fs = _StubLiveSource(source_id="cam-live", decode_error_count=0)
    stop = threading.Event()
    reader = StreamReader(
        camera_id="cam-live", frame_source=fs, stop_event=stop
    )
    reader.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    stop.set()
    reader.join(timeout=1.0)
    elapsed = time.monotonic() - t0
    assert not reader.is_alive(), "reader did not exit after stop_event.set()"
    assert elapsed < 0.5, f"stop took too long: {elapsed:.3f}s"


def test_stream_reader_is_not_alive_after_join() -> None:
    fs = Mp4FrameSource(
        str(FIXTURES / "sample_720p25.mp4"), source_id="cam0:mp4"
    )
    stop = threading.Event()
    reader = StreamReader(
        camera_id="cam0:mp4", frame_source=fs, stop_event=stop
    )
    reader.start()
    stop.set()
    reader.join(timeout=1.0)
    assert reader.is_alive() is False


def test_stream_reader_uses_per_camera_logger_prefix(tmp_path: Path) -> None:
    """Reader log lines must carry the bracketed ``[cam0:mp4]`` prefix."""
    _reset_logger()
    configure_logging(tmp_path)

    fs = Mp4FrameSource(
        str(FIXTURES / "sample_720p25.mp4"), source_id="cam0:mp4"
    )
    stop = threading.Event()
    reader = StreamReader(
        camera_id="cam0:mp4", frame_source=fs, stop_event=stop
    )
    reader.start()
    reader.join(timeout=10.0)
    assert not reader.is_alive()
    # Flush handlers so rotating file handler writes to disk.
    for h in logging.getLogger("home_cctv").handlers:
        h.flush()

    log_file = Path(tmp_path).resolve() / "home_cctv.log"
    text = log_file.read_text(encoding="utf-8")
    assert "[cam0:mp4] reader_started" in text, (
        f"expected bracketed camera prefix on reader_started line; got:\n{text}"
    )


def test_stream_reader_exits_on_mp4_eof() -> None:
    """is_file_source=True + decode_errors>0 + frames_decoded>0 => exit."""
    fs = Mp4FrameSource(
        str(FIXTURES / "sample_720p25.mp4"), source_id="cam0:mp4"
    )
    stop = threading.Event()
    reader = StreamReader(
        camera_id="cam0:mp4", frame_source=fs, stop_event=stop
    )
    reader.start()
    reader.join(timeout=10.0)
    # Reader must self-exit without anyone flipping stop_event.
    assert not reader.is_alive(), "reader failed to exit on MP4 EOF"
    assert stop.is_set() is False, (
        "reader should self-exit via is_file_source heuristic, "
        "not by setting stop_event"
    )
    assert fs.stats.frames_decoded > 0
    assert fs.stats.decode_errors > 0


def test_stream_reader_live_source_does_not_exit_on_decode_errors() -> None:
    """A live source (is_file_source=False) must NOT exit on decode_errors."""
    fs = _StubLiveSource(source_id="cam-live", decode_error_count=20)
    stop = threading.Event()
    reader = StreamReader(
        camera_id="cam-live", frame_source=fs, stop_event=stop
    )
    reader.start()
    # Give the reader enough time to exhaust the 20 decode errors; it should
    # NOT have exited yet.
    time.sleep(0.25)
    assert reader.is_alive(), (
        "live reader exited prematurely on decode errors — "
        "D-12 / CONTEXT §'New Facts' #2 violated"
    )
    # Only stop_event should end the live reader.
    stop.set()
    reader.join(timeout=1.0)
    assert not reader.is_alive()
    # Sanity: release was called in the finally branch.
    assert fs.released >= 1
