"""Per-camera StreamReader thread + bounded drop-oldest FrameQueue.

Phase 1 Plan 01-01 foundation. Wraps a ``FrameSource`` (either the live
``RtspFrameSource`` or the offline ``Mp4FrameSource``) in a dedicated
``threading.Thread`` that continuously calls ``frame_source.read()`` and pushes
successful decodes into a ``FrameQueue(maxlen=2)``.

Key contracts (from ``.planning/phases/01-multi-stream-ingest-reconnect/01-CONTEXT.md``):

* **D-01** — Threading model. No asyncio, no multiprocessing.
* **D-03** — Per-camera queue is ``collections.deque(maxlen=2)`` with
  drop-oldest semantics; slow consumers never back-pressure a reader.
* **D-04** — Post-open 5-frame drop is already implemented inside
  ``_BaseCvCapture.read()`` via ``_DROP_FRAMES_AFTER_OPEN=5``. This reader
  does NOT re-implement the drop; it simply consumes ``(False, None)`` for
  those first 5 calls without entering reconnect (Plan 02 owns reconnect).
* **D-05** — Green-frame detection lives inside ``_BaseCvCapture.read()``
  (see ``home_cctv.ingest.frame_quality``). Reader loop never re-checks.
* **D-10** — All log statements use the sanitized target or just the
  ``camera_id``. Raw credential URLs are masked via
  ``_sanitized_target()`` on the source before interpolation.
* **D-12** — Same reader loop must work for ``RtspFrameSource`` (live) and
  ``Mp4FrameSource`` (file). File sources exit on EOF; live sources never
  exit on decode errors.

Not in this plan:

* Watchdog + stuck-read detection (Plan 02 / ING-02).
* Exponential-backoff reconnect on ``open()`` failure (Plan 02 / ING-03).
* Heartbeat composition into the reader (Plan 03 / IngestSupervisor).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional, Tuple

import numpy as np

from home_cctv.ingest.capture import FrameSource

_LOG = logging.getLogger("home_cctv.ingest.stream_reader")

# D-03 — drop-oldest deque maxlen. Hard cap; no production code path allows
# a larger value (T-01-03 mitigation).
_DEFAULT_QUEUE_MAXLEN: int = 2


class FrameQueue:
    """Bounded drop-oldest queue built on ``collections.deque(maxlen=2)``.

    ``put()`` always succeeds; if the deque is already at ``maxlen`` the
    oldest item is evicted by ``deque.append`` and ``drop_count`` is
    incremented. ``drop_count`` and ``push_count`` are observed by the
    Phase 1 heartbeat to report sustained slow-consumer pressure.

    Thread-safe: all mutators + ``get_latest``/``pop_oldest``/``__len__``
    take a single internal lock. A ``StreamReader`` produces; any consumer
    (Phase 2 ``InferenceWorker``) may read concurrently.
    """

    def __init__(self, maxlen: int = _DEFAULT_QUEUE_MAXLEN) -> None:
        self._dq: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.drop_count: int = 0
        self.push_count: int = 0

    # ----------------------------------------------------------------- mutate
    def put(self, item: Tuple[np.ndarray, float]) -> None:
        """Enqueue an ``(ndarray, monotonic_ts)`` tuple.

        If the deque is full, the append triggers a drop-oldest eviction;
        ``drop_count`` is incremented to expose that to the heartbeat.
        """
        with self._lock:
            if len(self._dq) == self._dq.maxlen:
                self.drop_count += 1
            self._dq.append(item)
            self.push_count += 1

    # ------------------------------------------------------------------- read
    def get_latest(self) -> Optional[Tuple[np.ndarray, float]]:
        """Return the newest queued tuple without removing it.

        Used by Phase 2 ``InferenceWorker`` which always wants the most
        recent frame (the older of two in-flight frames is stale).
        """
        with self._lock:
            if not self._dq:
                return None
            return self._dq[-1]

    def pop_oldest(self) -> Optional[Tuple[np.ndarray, float]]:
        """Remove and return the oldest queued tuple, or ``None`` if empty."""
        with self._lock:
            if not self._dq:
                return None
            return self._dq.popleft()

    def __len__(self) -> int:
        with self._lock:
            return len(self._dq)


class StreamReader(threading.Thread):
    """Daemon thread that drives a ``FrameSource`` to completion.

    Lifecycle:

    1. ``start()`` kicks off the daemon thread.
    2. ``run()`` calls ``frame_source.open()``. On open failure, log and exit
       — Plan 02 extends this with reconnect.
    3. Loop ``while not stop_event.is_set()``: call ``frame_source.read()``;
       on success push ``(frame, time.monotonic())`` onto the queue.
    4. On ``is_file_source`` sources with at least one frame decoded + at
       least one decode error, exit the loop (EOF heuristic).
    5. Live sources never exit on decode errors — only stop_event ends them.
    6. ``finally: frame_source.release()`` — guarantees no capture is leaked
       even if the loop body raises.

    Consumers should read via ``self.queue.get_latest()``. The reader never
    drops on the producer side; it always pushes into the drop-oldest deque.
    """

    def __init__(
        self,
        *,
        camera_id: str,
        frame_source: FrameSource,
        stop_event: threading.Event,
        queue: Optional[FrameQueue] = None,
    ) -> None:
        super().__init__(name=f"StreamReader[{camera_id}]", daemon=True)
        self.camera_id = camera_id
        self.frame_source = frame_source
        self.stop_event = stop_event
        self.queue = queue if queue is not None else FrameQueue(
            maxlen=_DEFAULT_QUEUE_MAXLEN
        )

    # ----------------------------------------------------------------- helpers
    def _sanitized_target(self) -> str:
        """Return the frame source's credential-masked target string.

        Every ``_BaseCvCapture`` subclass implements ``_sanitized_target``;
        ``RtspFrameSource`` strips the password, ``Mp4FrameSource`` passes the
        path through unchanged. Using this helper keeps D-10 (credential
        masking) enforced at the call site.
        """
        sanitizer = getattr(self.frame_source, "_sanitized_target", None)
        if callable(sanitizer):
            try:
                return str(sanitizer())
            except Exception:  # pragma: no cover — defensive
                pass
        return f"source_id={self.frame_source.source_id}"

    # ---------------------------------------------------------------- run loop
    def run(self) -> None:  # noqa: D401 — threading.Thread override
        # Open guard. Plan 02 wraps this in reconnect-with-backoff; Plan 01
        # simply logs-and-exits on first open failure so the contract is
        # observable from day one.
        try:
            self.frame_source.open()
        except Exception as exc:
            _LOG.error(
                "reader_open_failed camera_id=%s target=%s err=%r",
                self.camera_id,
                self._sanitized_target(),
                exc,
                extra={"camera_id": self.camera_id},
            )
            return

        is_file_source = bool(
            getattr(self.frame_source, "is_file_source", False)
        )
        _LOG.info(
            "reader_started camera_id=%s target=%s is_file_source=%s",
            self.camera_id,
            self._sanitized_target(),
            is_file_source,
            extra={"camera_id": self.camera_id},
        )

        try:
            while not self.stop_event.is_set():
                ok, frame = self.frame_source.read()
                if ok and frame is not None:
                    self.queue.put((frame, time.monotonic()))
                    continue
                # Not ok. Decide whether this is EOF (file source) or a
                # transient live-stream hiccup we should keep grinding on.
                if is_file_source:
                    stats = self.frame_source.stats
                    if stats.decode_errors > 0 and stats.frames_decoded > 0:
                        _LOG.info(
                            "reader_eof camera_id=%s frames_decoded=%d "
                            "decode_errors=%d",
                            self.camera_id,
                            stats.frames_decoded,
                            stats.decode_errors,
                            extra={"camera_id": self.camera_id},
                        )
                        break
                # Live source: never exit here. D-12 / CONTEXT §'New Facts' #2.
                # Post-open 5-frame drop also surfaces here as (False, None);
                # we just loop — no reconnect, no sleep. Plan 02 adds backoff.
        finally:
            try:
                self.frame_source.release()
            except Exception as exc:  # pragma: no cover — defensive
                _LOG.warning(
                    "reader_release_failed camera_id=%s err=%r",
                    self.camera_id,
                    exc,
                    extra={"camera_id": self.camera_id},
                )
            _LOG.info(
                "reader_stopped camera_id=%s",
                self.camera_id,
                extra={"camera_id": self.camera_id},
            )


__all__ = ["FrameQueue", "StreamReader"]
