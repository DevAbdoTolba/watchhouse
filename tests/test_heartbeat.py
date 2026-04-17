"""Tests for ``home_cctv.obs.heartbeat`` — Phase 1 Plan 01-01 Task 2.

Covers:

* ``format_line`` renders the exact CONTEXT §G-04 template.
* State classification: starting / healthy / degraded / stalled.
* ``drop_rate_pct`` rounds to 1 decimal.
* Credential masking survives through the heartbeat logging path
  (T-01-01 mitigation).
* Cadence: ``CameraHeartbeat.start()`` fires approximately once per cadence
  tick and stops emitting after ``stop_event`` is set.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from home_cctv.ingest.capture import CaptureStats
from home_cctv.ingest.stream_reader import FrameQueue
from home_cctv.obs.heartbeat import (
    DEFAULT_HEARTBEAT_CADENCE_S,
    STALL_THRESHOLD_S,
    CameraHeartbeat,
    HeartbeatSample,
    compute_sample,
    format_line,
)
from home_cctv.obs.logging_setup import configure_logging


def _reset_logger() -> None:
    lg = logging.getLogger("home_cctv")
    for h in list(lg.handlers):
        lg.removeHandler(h)


def _push_frames(queue: FrameQueue, n: int) -> None:
    for i in range(n):
        queue.put((np.zeros((2, 2, 3), dtype=np.uint8), float(i)))


# ---------------------------------------------------------- constants + shape


def test_default_cadence_is_30_seconds() -> None:
    assert DEFAULT_HEARTBEAT_CADENCE_S == 30.0


def test_stall_threshold_is_10_seconds() -> None:
    assert STALL_THRESHOLD_S == 10.0


# ------------------------------------------------------------ sample + format


def _healthy_stats(
    *, frames_decoded: int = 150, first: float = 100.0, last: float = 110.0
) -> CaptureStats:
    """Build a CaptureStats representing a healthy 15 fps stream over 10 s."""
    stats = CaptureStats()
    stats.frames_decoded = frames_decoded
    stats.frames_corrupted = 2
    stats.decode_errors = 1
    stats.first_frame_monotonic = first
    stats.last_frame_monotonic = last
    return stats


def test_format_line_healthy_shape() -> None:
    """Test 1: exact template on a healthy reader."""
    stats = _healthy_stats()  # measured_fps = (150-1)/10 = 14.9
    queue = FrameQueue(maxlen=2)
    _push_frames(queue, 4)  # push 4 → drop 2, push 4 total, drop_rate 50.0%
    sample = compute_sample(
        stats, queue, expected_fps=15.0, now=stats.last_frame_monotonic + 0.5
    )
    line = format_line(sample)
    expected = (
        f"heartbeat fps={sample.fps} frames_decoded={sample.frames_decoded} "
        f"frames_corrupted={sample.frames_corrupted} "
        f"decode_errors={sample.decode_errors} "
        f"drop_rate={sample.drop_rate_pct}% "
        f"last_read_age_s={sample.last_read_age_s:.2f} state=healthy"
    )
    assert line == expected
    assert "heartbeat fps=" in line
    assert " state=healthy" in line


def test_starting_state_when_no_frames_yet() -> None:
    """Test 2: last_frame_monotonic is None => state=starting, age=unknown."""
    stats = CaptureStats()
    queue = FrameQueue(maxlen=2)
    sample = compute_sample(
        stats, queue, expected_fps=15.0, now=100.0
    )
    assert sample.state == "starting"
    assert sample.last_read_age_s is None
    line = format_line(sample)
    assert "last_read_age_s=unknown" in line
    assert " state=starting" in line


def test_stalled_state_when_age_exceeds_threshold() -> None:
    """Test 3: now - last_frame_monotonic > 10 s => state=stalled."""
    stats = _healthy_stats()
    queue = FrameQueue(maxlen=2)
    _push_frames(queue, 2)
    # 12 s since last frame
    sample = compute_sample(
        stats, queue, expected_fps=15.0, now=stats.last_frame_monotonic + 12.0
    )
    assert sample.state == "stalled"


def test_degraded_state_when_fps_below_half_expected() -> None:
    """Test 4: measured_fps < 0.5 * expected => state=degraded."""
    stats = CaptureStats()
    stats.frames_decoded = 30  # 29 frame deltas
    stats.frames_corrupted = 0
    stats.decode_errors = 0
    stats.first_frame_monotonic = 100.0
    stats.last_frame_monotonic = 110.0
    # measured_fps = 29 / 10 = 2.9, expected 15 → below 7.5
    queue = FrameQueue(maxlen=2)
    _push_frames(queue, 1)
    sample = compute_sample(
        stats, queue, expected_fps=15.0, now=110.2
    )
    assert sample.state == "degraded"


def test_drop_rate_rounding_to_one_decimal() -> None:
    """Test 5: drop_rate_pct = drop_count / push_count * 100, 1 decimal."""
    stats = _healthy_stats()
    queue = FrameQueue(maxlen=2)
    # 3 pushes → 1 eviction after the 3rd. drop_count=1, push_count=3.
    _push_frames(queue, 3)
    assert queue.drop_count == 1
    assert queue.push_count == 3
    sample = compute_sample(
        stats, queue, expected_fps=15.0, now=stats.last_frame_monotonic + 0.1
    )
    # 1/3 * 100 = 33.333... → 33.3
    assert sample.drop_rate_pct == 33.3


def test_drop_rate_zero_push_does_not_divide_by_zero() -> None:
    stats = _healthy_stats()
    queue = FrameQueue(maxlen=2)
    sample = compute_sample(
        stats, queue, expected_fps=15.0, now=stats.last_frame_monotonic + 0.1
    )
    assert sample.drop_rate_pct == 0.0


# -------------------------------------------------- credential masking (T-01-01)


def test_heartbeat_line_is_credential_masked(tmp_path: Path) -> None:
    """Test 6: even if a caller somehow includes an RTSP URL with a password,
    the CredentialMaskFilter applied by configure_logging must mask it before
    the record reaches any handler.
    """
    _reset_logger()
    adapter = configure_logging(tmp_path, camera_id="cam1:exterior_red")
    # Emit a heartbeat-ish line that accidentally includes a cred URL.
    adapter.info(
        "heartbeat fps=15.0 frames_decoded=10 target=rtsp://bob:secret@10.0.0.1/1 "
        "state=healthy"
    )
    for h in adapter.logger.handlers:
        h.flush()
    log_file = Path(tmp_path).resolve() / "home_cctv.log"
    text = log_file.read_text(encoding="utf-8")
    assert "bob:***@10.0.0.1" in text
    assert "secret" not in text


# --------------------------------------------------------- cadence + lifecycle


def test_cameraheartbeat_fires_at_configured_cadence(tmp_path: Path) -> None:
    """Test 7: at cadence=0.1 s, ≥3 lines appear in ~0.35 s; 0 after stop."""
    _reset_logger()
    configure_logging(tmp_path, camera_id="cam1:exterior_red")

    stats = _healthy_stats()
    queue = FrameQueue(maxlen=2)
    _push_frames(queue, 5)
    stop = threading.Event()
    hb = CameraHeartbeat(
        camera_id="cam1:exterior_red",
        stats=stats,
        queue=queue,
        expected_fps=15.0,
        stop_event=stop,
        cadence_s=0.1,
    )
    hb.start()
    time.sleep(0.35)

    # Count heartbeat lines so far.
    for h in logging.getLogger("home_cctv").handlers:
        h.flush()
    log_file = Path(tmp_path).resolve() / "home_cctv.log"
    text_before = log_file.read_text(encoding="utf-8")
    lines_before = [
        ln for ln in text_before.splitlines() if "heartbeat fps=" in ln
    ]
    assert len(lines_before) >= 3, (
        f"expected ≥3 heartbeat lines at cadence=0.1s; got {len(lines_before)}"
    )

    stop.set()
    hb.join(timeout=1.0)
    time.sleep(0.3)
    for h in logging.getLogger("home_cctv").handlers:
        h.flush()
    text_after = log_file.read_text(encoding="utf-8")
    lines_after = [
        ln for ln in text_after.splitlines() if "heartbeat fps=" in ln
    ]
    # At most one extra emission could slip through between time.sleep(0.35)
    # snapshot above and stop.set(); must not be open-ended growth.
    assert len(lines_after) - len(lines_before) <= 2, (
        f"heartbeat kept emitting after stop: "
        f"before={len(lines_before)} after={len(lines_after)}"
    )


def test_heartbeat_sample_fields_populated() -> None:
    """HeartbeatSample carries all fields the heartbeat line references."""
    s = HeartbeatSample(
        fps=14.9,
        frames_decoded=100,
        frames_corrupted=1,
        decode_errors=2,
        drop_rate_pct=5.5,
        last_read_age_s=0.1,
        state="healthy",
    )
    assert s.fps == 14.9
    assert s.state == "healthy"
    assert s.last_read_age_s == 0.1


def test_emit_now_writes_one_line(tmp_path: Path) -> None:
    _reset_logger()
    configure_logging(tmp_path, camera_id="cam0:mp4")
    stats = _healthy_stats()
    queue = FrameQueue(maxlen=2)
    _push_frames(queue, 1)
    stop = threading.Event()
    hb = CameraHeartbeat(
        camera_id="cam0:mp4",
        stats=stats,
        queue=queue,
        expected_fps=15.0,
        stop_event=stop,
    )
    hb.emit_now()
    for h in logging.getLogger("home_cctv").handlers:
        h.flush()
    text = (Path(tmp_path).resolve() / "home_cctv.log").read_text(
        encoding="utf-8"
    )
    assert "[cam0:mp4] heartbeat fps=" in text
