"""FrameSource abstraction.

One interface, two implementations:

* ``RtspFrameSource``  → ``cv2.VideoCapture(url, CAP_FFMPEG)``
* ``Mp4FrameSource``   → ``cv2.VideoCapture(path, CAP_FFMPEG)``

Both paths share the same read loop, the same green-frame guard, the same
``CaptureStats`` object, and the same release semantics. This is what makes
``--mp4`` a true regression test for the live pipeline (ING-06).

Notes relevant to downstream phases:

* ``FrameSource.__init__`` runs ``assert_capture_options_active()`` so any
  accidental reordering or env mutation is surfaced at construction time
  (PITFALLS §1.1).
* The first ``_DROP_FRAMES_AFTER_OPEN`` frames after every ``open()`` are
  always tagged as corrupted and dropped — post-reconnect green bursts are
  common on WSL2 + FFmpeg (PITFALLS §1.3).
* ``is_file_source`` is a **class** attribute (not instance) so downstream
  loops can gate EOF heuristics without actually opening the capture.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

import cv2
import numpy as np

from home_cctv.ingest.flags import assert_capture_options_active
from home_cctv.ingest.frame_quality import is_green_frame

_LOG = logging.getLogger("home_cctv.capture")

# PITFALLS §1.3: drop the first N frames after every open/reconnect to absorb
# the green-burst window. 5 is the published value.
_DROP_FRAMES_AFTER_OPEN: int = 5

_RTSP_CRED_RE = re.compile(r"(://[^:/@\s]+):[^@\s]+@")


@dataclass
class CaptureStats:
    """Per-source decode counters.

    ``frames_decoded`` counts only frames that made it past the green-frame
    gate and the post-open drop window; ``frames_corrupted`` counts anything
    the FrameSource rejected internally (dead frames, green bursts); and
    ``decode_errors`` counts the times ``cap.read()`` returned ``(False, None)``.
    """

    frames_decoded: int = 0
    frames_corrupted: int = 0
    decode_errors: int = 0
    hang_events: int = 0
    first_frame_monotonic: Optional[float] = None
    last_frame_monotonic: Optional[float] = None

    @property
    def measured_fps(self) -> float:
        if (
            self.frames_decoded < 2
            or self.first_frame_monotonic is None
            or self.last_frame_monotonic is None
        ):
            return 0.0
        dt = self.last_frame_monotonic - self.first_frame_monotonic
        return (self.frames_decoded - 1) / dt if dt > 0 else 0.0


class FrameSource(Protocol):
    """Structural type of every frame source. See ``_BaseCvCapture`` below."""

    source_id: str
    stats: CaptureStats
    is_file_source: bool

    def open(self) -> None: ...

    def read(self) -> tuple[bool, Optional[np.ndarray]]: ...

    def release(self) -> None: ...


class _BaseCvCapture:
    # Subclasses override. ``Mp4FrameSource`` → True; ``RtspFrameSource`` → False.
    # Downstream loops use this to decide whether "decode_errors > 0 after some
    # frames" means EOF (file, clean exit) or a transient network hiccup (live
    # RTSP — must NOT break the loop early, especially on Cam 3/4 which produce
    # NAL-unit-0 errors early by design).
    is_file_source: bool = False

    def __init__(self, target: str, *, source_id: str) -> None:
        assert_capture_options_active()
        self._target = target
        self.source_id = source_id
        self._cap: Optional["cv2.VideoCapture"] = None
        self._frames_since_open: int = 0
        self.stats = CaptureStats()

    # ------------------------------------------------------------------ open
    def open(self) -> None:
        cap = cv2.VideoCapture(self._target, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError(
                f"FrameSource[{self.source_id}] failed to open "
                f"{self._sanitized_target()}"
            )
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self._cap = cap
        self._frames_since_open = 0
        _LOG.info(
            "capture_opened source_id=%s target=%s",
            self.source_id,
            self._sanitized_target(),
        )

    # ------------------------------------------------------------------ read
    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        cap = self._cap
        if cap is None:
            return False, None
        try:
            ok, frame = cap.read()
        except Exception as exc:  # pragma: no cover — defensive
            _LOG.warning(
                "cap_read_exception source_id=%s err=%r", self.source_id, exc
            )
            self.stats.decode_errors += 1
            return False, None

        if not ok or frame is None:
            self.stats.decode_errors += 1
            return False, None

        self._frames_since_open += 1
        if self._frames_since_open <= _DROP_FRAMES_AFTER_OPEN:
            self.stats.frames_corrupted += 1
            return False, None

        if is_green_frame(frame):
            self.stats.frames_corrupted += 1
            return False, None

        now = time.monotonic()
        if self.stats.first_frame_monotonic is None:
            self.stats.first_frame_monotonic = now
        self.stats.last_frame_monotonic = now
        self.stats.frames_decoded += 1
        return True, frame

    # --------------------------------------------------------------- release
    def release(self) -> None:
        cap = self._cap
        if cap is not None:
            try:
                cap.release()
            except Exception as exc:  # pragma: no cover — defensive
                _LOG.warning(
                    "cap_release_failed source_id=%s err=%r",
                    self.source_id,
                    exc,
                )
        self._cap = None

    # --------------------------------------------------------------- sanitize
    def _sanitized_target(self) -> str:
        return self._target


class RtspFrameSource(_BaseCvCapture):
    """Live RTSP capture. Never auto-exits on decode errors (Cam 3/4 NAL)."""

    is_file_source: bool = False

    def _sanitized_target(self) -> str:
        return _RTSP_CRED_RE.sub(r"\1:***@", self._target)


class Mp4FrameSource(_BaseCvCapture):
    """Offline MP4 capture. Used by ``--mp4`` and Phase 0 regression tests."""

    is_file_source: bool = True

    def open(self) -> None:
        p = Path(self._target)
        if not p.exists():
            raise FileNotFoundError(f"Mp4FrameSource: {p} does not exist")
        super().open()


def open_frame_source(target: str, *, camera_id: str) -> FrameSource:
    """Factory.

    * ``rtsp://`` / ``rtsps://`` → :class:`RtspFrameSource`
    * everything else → :class:`Mp4FrameSource` (the file existence check runs
      at ``.open()``, not at construction, so tests can exercise the factory
      without touching disk)
    """
    if target.startswith(("rtsp://", "rtsps://")):
        return RtspFrameSource(target, source_id=camera_id)
    return Mp4FrameSource(target, source_id=camera_id)


__all__ = [
    "CaptureStats",
    "FrameSource",
    "RtspFrameSource",
    "Mp4FrameSource",
    "open_frame_source",
]
