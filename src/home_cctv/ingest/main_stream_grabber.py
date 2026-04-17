"""On-demand main-stream frame grabber — Phase 1 Plan 01-03 Task 1.

Owns the DVR-wide ``threading.Semaphore(1)`` that serializes every main-stream
open across all 4 cameras (ING-08), plus a 2-s TTL cache keyed by ``camera_id``
that suppresses duplicate opens during burst triggers (ING-07). Exposes a
synchronous API per CONTEXT §G-03:

    grab_main_frame(camera_id: int) -> bytes | None

Internal flow (double-checked locking — see ING-07 invariant):

    1. First cache check (fast path) under ``_cache_lock``.
       On fresh hit: log ``main_grab_cache_hit`` and return the cached bytes.
    2. Acquire the semaphore with a short timeout in a loop gated by
       ``stop_event`` — any blocked acquirer returns ``None`` within 0.5 s
       of a SIGINT.
    3. Second cache check (re-check after acquire) under ``_cache_lock``.
       A burst caller that queued behind the first caller MUST find the
       fresh cache entry and return cached bytes WITHOUT re-opening the
       VideoCapture — log ``main_grab_cache_hit_after_wait`` and release the
       semaphore early.
    4. On second-check miss: short-lived ``open → read → release`` via
       ``_open_read_release``. Cache the JPEG bytes on success; the
       semaphore is released in ``finally``.

Everything stays in-memory — Phase 1 does NOT persist to SQLite or
``EVENT_IMAGE_DIR`` (D-11). Phase 3's ``TriggerEvaluator`` owns persistence.

Credential safety (T-03-04, D-10): every log line uses ``camera_id`` only.
The resolved ``url`` is never interpolated into a log message. The
``CredentialMaskFilter`` on the root logger provides belt-and-braces.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import cv2
import numpy as np

from home_cctv.ingest.flags import assert_capture_options_active

_LOG = logging.getLogger("home_cctv.ingest.main_stream")

# --------------------------------------------------------------------- constants

#: Cache entries expire after this many seconds (CONTEXT §D-09).
MAIN_STREAM_CACHE_TTL_S: float = 2.0

#: A single triggered main-stream open retries the ``cap.read()`` at most
#: this many times before giving up (bounded to prevent T-03-02 leaked cap).
MAIN_STREAM_MAX_READ_ATTEMPTS: int = 3

#: JPEG encode quality for the cached bytes — a sensible default for 720p/1080p.
MAIN_STREAM_JPEG_QUALITY: int = 85

#: How long a single ``semaphore.acquire`` attempt waits before polling
#: ``stop_event`` again. 0.5 s keeps SIGINT latency well under 2 s (T-03-05).
_SEMAPHORE_POLL_S: float = 0.5


# --------------------------------------------------------------------- cache


@dataclass
class CacheEntry:
    """One entry in the TTL cache, keyed by camera_id in the grabber."""

    jpeg_bytes: bytes
    cached_at_monotonic: float


# ------------------------------------------------------------- default factory


def _default_capture_factory(url: str) -> "cv2.VideoCapture":
    """Production capture factory — asserts env flags and constructs cv2.

    The ``assert_capture_options_active()`` call surfaces any accidental
    reordering of ``OPENCV_FFMPEG_CAPTURE_OPTIONS`` at construction time
    (PITFALLS §1.1) instead of letting the DVR return green frames.
    """
    assert_capture_options_active()
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG)


# ---------------------------------------------------------------- grabber


class MainStreamGrabber:
    """DVR-wide main-stream grabber with semaphore + TTL cache.

    Reentrant / lazy: no background thread of its own. Callers (Phase 3
    ``TriggerEvaluator``) invoke ``grab_main_frame(camera_id)`` directly from
    whatever thread they're running on. Serialization is provided by the
    ``Semaphore(1)`` — combined with the 4 persistent sub-stream sessions,
    the total concurrent DVR session count stays at 4 or 5, well under the
    empirically measured 6-session cap (PHASE0-REPORT).
    """

    def __init__(
        self,
        *,
        url_resolver: Callable[[int], str],
        stop_event: threading.Event,
        semaphore: Optional[threading.Semaphore] = None,
        ttl_s: float = MAIN_STREAM_CACHE_TTL_S,
        capture_factory: Optional[Callable[[str], "cv2.VideoCapture"]] = None,
    ) -> None:
        self.url_resolver = url_resolver
        self.stop_event = stop_event
        # Default: a fresh global Semaphore(1). Callers may pass their own
        # (tests, or a future multi-DVR setup with one semaphore per DVR).
        self.semaphore = semaphore if semaphore is not None else threading.Semaphore(1)
        self.ttl_s = float(ttl_s)
        self._capture_factory = (
            capture_factory if capture_factory is not None else _default_capture_factory
        )
        self._cache: Dict[int, CacheEntry] = {}
        self._cache_lock = threading.Lock()

    # ----------------------------------------------------------- public API
    def grab_main_frame(self, camera_id: int) -> Optional[bytes]:
        """Return JPEG bytes for ``camera_id`` or ``None`` on failure / stop.

        Blocks the caller for the duration of the open/read/release cycle
        (typically <3 s, bounded by the DVR handshake latency of ~3.4 s
        measured in PHASE0-REPORT). If another caller is already inside the
        critical section, waits for the semaphore. If the same camera was
        fetched within ``ttl_s`` seconds by either the caller or a concurrent
        thread, returns the cached bytes without re-opening.
        """
        now = time.monotonic()

        # ------ First cache check (fast path, pre-semaphore) --------------
        with self._cache_lock:
            entry = self._cache.get(camera_id)
            if entry is not None and (now - entry.cached_at_monotonic) < self.ttl_s:
                age = now - entry.cached_at_monotonic
                _LOG.info(
                    "main_grab_cache_hit camera_id=%s age_s=%.2f",
                    camera_id,
                    age,
                )
                return entry.jpeg_bytes

        # ------ Acquire semaphore with stop_event gating ------------------
        acquired = False
        while not self.stop_event.is_set():
            if self.semaphore.acquire(timeout=_SEMAPHORE_POLL_S):
                acquired = True
                break
        if not acquired:
            _LOG.info(
                "main_grab_stopped_before_acquire camera_id=%s",
                camera_id,
            )
            return None

        semaphore_released = False
        try:
            # ------ Second cache check (double-checked locking) -----------
            # Another caller may have populated the cache while we were
            # queued behind the semaphore. ING-07 invariant: we MUST NOT
            # re-open a VideoCapture in that case.
            now2 = time.monotonic()
            with self._cache_lock:
                entry = self._cache.get(camera_id)
                if (
                    entry is not None
                    and (now2 - entry.cached_at_monotonic) < self.ttl_s
                ):
                    age = now2 - entry.cached_at_monotonic
                    _LOG.info(
                        "main_grab_cache_hit_after_wait camera_id=%s age_s=%.2f",
                        camera_id,
                        age,
                    )
                    # Release the semaphore early so the next caller can
                    # proceed immediately without waiting for a no-op.
                    self.semaphore.release()
                    semaphore_released = True
                    return entry.jpeg_bytes

            # ------ Second-check miss: do the actual open/read/release ---
            jpeg_bytes = self._open_read_release(camera_id)
            if jpeg_bytes is None:
                return None
            with self._cache_lock:
                self._cache[camera_id] = CacheEntry(
                    jpeg_bytes=jpeg_bytes,
                    cached_at_monotonic=time.monotonic(),
                )
            return jpeg_bytes
        finally:
            if not semaphore_released:
                self.semaphore.release()

    # ---------------------------------------------------- internal helpers
    def _open_read_release(self, camera_id: int) -> Optional[bytes]:
        """Short-lived ``open → read → release`` cycle.

        The capture is ALWAYS released via ``finally`` — even on early
        return paths — so a failed open never leaks a file descriptor
        (T-03-02).
        """
        url = self.url_resolver(camera_id)
        _LOG.info("main_grab_open camera_id=%s", camera_id)
        cap = self._capture_factory(url)
        try:
            if not cap.isOpened():
                _LOG.warning("main_grab_open_failed camera_id=%s", camera_id)
                return None
            frame: Optional[np.ndarray] = None
            for _ in range(MAIN_STREAM_MAX_READ_ATTEMPTS):
                ok, candidate = cap.read()
                if ok and candidate is not None:
                    frame = candidate
                    break
            if frame is None:
                _LOG.warning(
                    "main_grab_read_failed camera_id=%s attempts=%d",
                    camera_id,
                    MAIN_STREAM_MAX_READ_ATTEMPTS,
                )
                return None
            ok2, buf = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), MAIN_STREAM_JPEG_QUALITY],
            )
            if not ok2:
                _LOG.warning("main_grab_encode_failed camera_id=%s", camera_id)
                return None
            jpeg_bytes = bytes(buf.tobytes() if hasattr(buf, "tobytes") else buf)
            _LOG.info(
                "main_grab_ok camera_id=%s bytes=%d",
                camera_id,
                len(jpeg_bytes),
            )
            return jpeg_bytes
        finally:
            try:
                cap.release()
            except Exception as exc:  # pragma: no cover — defensive
                _LOG.warning(
                    "main_grab_release_failed camera_id=%s err=%r",
                    camera_id,
                    exc,
                )

    # ------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        """No-op for API symmetry with other Phase 1 components.

        The grabber owns no persistent thread — every call is reentrant
        and any blocked acquirer is unblocked by ``stop_event`` from the
        ``IngestSupervisor``'s ``ShutdownSupervisor``.
        """
        return None


__all__ = [
    "MainStreamGrabber",
    "MAIN_STREAM_CACHE_TTL_S",
    "MAIN_STREAM_MAX_READ_ATTEMPTS",
    "MAIN_STREAM_JPEG_QUALITY",
    "CacheEntry",
]
