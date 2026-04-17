"""Per-camera health heartbeat emitter.

Phase 1 Plan 01-01 Task 2. Reads a ``CaptureStats`` + ``FrameQueue`` pair and
emits one structured INFO line per cadence tick through the existing
``home_cctv`` logger — which already carries ``CredentialMaskFilter`` from
``obs.logging_setup`` (T-01-01 mitigation).

Per CONTEXT §G-04 the line format is:

    heartbeat fps=X.Y frames_decoded=N frames_corrupted=M decode_errors=E \
        drop_rate=P.P% last_read_age_s=A.A state=<S>

where ``<S>`` is one of ``starting | healthy | degraded | stalled``.

Phase 1 does **not** write ``metrics.json``; that's Phase 4 / OPS-02. The
heartbeat carries the same per-camera payload so the metrics writer will be a
reshape, not a remeasure.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from home_cctv.ingest.capture import CaptureStats
from home_cctv.ingest.stream_reader import FrameQueue

# -------- Constants -----------------------------------------------------------

#: CONTEXT §G-04 — one heartbeat line per camera every 30 seconds.
DEFAULT_HEARTBEAT_CADENCE_S: float = 30.0

#: If ``now - stats.last_frame_monotonic`` exceeds this, the reader is stalled.
STALL_THRESHOLD_S: float = 10.0

#: If ``measured_fps`` drops below this fraction of ``expected_fps`` the reader
#: is classified as degraded (used once a first frame has been decoded).
DEGRADED_FPS_RATIO: float = 0.5

# Possible states for the heartbeat line (kept as string constants for
# deterministic matching by downstream consumers + tests).
# The rendered line ends with one of: state=starting, state=healthy,
# state=degraded, state=stalled — deliberately greppable for acceptance tests.
STATE_STARTING = "starting"
STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"
STATE_STALLED = "stalled"


@dataclass
class HeartbeatSample:
    """Snapshot of one heartbeat tick.

    Fields map 1:1 to the line template. The heartbeat emitter builds one of
    these per tick, passes it through ``format_line``, and logs the result.
    Phase 4 (metrics.json) will serialise this same dataclass to JSON.
    """

    fps: float
    frames_decoded: int
    frames_corrupted: int
    decode_errors: int
    drop_rate_pct: float
    last_read_age_s: Optional[float]
    state: str  # one of STATE_STARTING | STATE_HEALTHY | STATE_DEGRADED | STATE_STALLED


def format_line(sample: HeartbeatSample) -> str:
    """Render a ``HeartbeatSample`` to the exact CONTEXT §G-04 template.

    ``last_read_age_s`` renders as ``"unknown"`` when ``None`` (starting
    state), otherwise ``{value:.2f}``.
    """
    if sample.last_read_age_s is None:
        age_str = "unknown"
    else:
        age_str = f"{sample.last_read_age_s:.2f}"
    return (
        f"heartbeat fps={sample.fps} "
        f"frames_decoded={sample.frames_decoded} "
        f"frames_corrupted={sample.frames_corrupted} "
        f"decode_errors={sample.decode_errors} "
        f"drop_rate={sample.drop_rate_pct}% "
        f"last_read_age_s={age_str} "
        f"state={sample.state}"
    )


def compute_sample(
    stats: CaptureStats,
    queue: FrameQueue,
    *,
    expected_fps: float,
    now: float,
) -> HeartbeatSample:
    """Build a :class:`HeartbeatSample` from the reader's two observables.

    State is computed purely from ``CaptureStats`` — no timers, no prior
    samples — which keeps the classification deterministic and cheap to test.
    """
    last_read_age_s: Optional[float]
    if stats.last_frame_monotonic is None:
        state = STATE_STARTING
        last_read_age_s = None
    else:
        age = now - stats.last_frame_monotonic
        last_read_age_s = age
        if age > STALL_THRESHOLD_S:
            state = STATE_STALLED
        elif (
            stats.measured_fps > 0.0
            and stats.measured_fps < DEGRADED_FPS_RATIO * expected_fps
        ):
            state = STATE_DEGRADED
        else:
            state = STATE_HEALTHY

    drop_rate_pct = round(
        queue.drop_count / max(1, queue.push_count) * 100.0, 1
    )

    return HeartbeatSample(
        fps=round(stats.measured_fps, 2),
        frames_decoded=stats.frames_decoded,
        frames_corrupted=stats.frames_corrupted,
        decode_errors=stats.decode_errors,
        drop_rate_pct=drop_rate_pct,
        last_read_age_s=last_read_age_s,
        state=state,
    )


class CameraHeartbeat:
    """Periodic per-camera heartbeat emitter.

    One ``CameraHeartbeat`` per reader. Owns a daemon thread that wakes up
    every ``cadence_s`` seconds (via ``stop_event.wait(cadence_s)``), samples
    the ``CaptureStats`` + ``FrameQueue`` snapshot, and logs one INFO line
    through a ``logging.LoggerAdapter`` tagged with ``camera_id`` so the
    existing bracketed-prefix formatter renders ``[cam1:exterior_red]``.

    The heartbeat does NOT own ``stop_event`` — the caller (Plan 03
    ``IngestSupervisor``) flips it when shutting the pipeline down. Using
    ``stop_event.wait(cadence)`` means the loop exits promptly (within one
    cadence tick or less) when stop is signalled.
    """

    def __init__(
        self,
        *,
        camera_id: str,
        stats: CaptureStats,
        queue: FrameQueue,
        expected_fps: float,
        stop_event: threading.Event,
        cadence_s: float = DEFAULT_HEARTBEAT_CADENCE_S,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.camera_id = camera_id
        self.stats = stats
        self.queue = queue
        self.expected_fps = expected_fps
        self.stop_event = stop_event
        self.cadence_s = cadence_s
        base_logger = logger or logging.getLogger("home_cctv.heartbeat")
        self._adapter = logging.LoggerAdapter(
            base_logger, {"camera_id": camera_id}
        )
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Spawn the heartbeat daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"Heartbeat[{self.camera_id}]",
            daemon=True,
        )
        self._thread.start()

    def join(self, timeout: float = 1.0) -> None:
        """Join the heartbeat thread. Caller owns stop_event."""
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def emit_now(self) -> None:
        """Emit one heartbeat immediately. Useful for tests + boot banner."""
        self._emit()

    # --------------------------------------------------------------------- run
    def _run(self) -> None:
        # ``stop_event.wait(cadence)`` returns True as soon as stop fires, so
        # the loop exits within one cadence tick without a busy poll.
        while not self.stop_event.wait(self.cadence_s):
            try:
                self._emit()
            except Exception as exc:  # pragma: no cover — defensive
                self._adapter.warning(
                    "heartbeat_emit_failed err=%r", exc
                )

    def _emit(self) -> None:
        import time as _time  # local to keep module top-level import surface tiny

        sample = compute_sample(
            self.stats,
            self.queue,
            expected_fps=self.expected_fps,
            now=_time.monotonic(),
        )
        self._adapter.info(format_line(sample))


__all__ = [
    "CameraHeartbeat",
    "HeartbeatSample",
    "DEFAULT_HEARTBEAT_CADENCE_S",
    "STALL_THRESHOLD_S",
    "DEGRADED_FPS_RATIO",
    "STATE_STARTING",
    "STATE_HEALTHY",
    "STATE_DEGRADED",
    "STATE_STALLED",
    "compute_sample",
    "format_line",
]
