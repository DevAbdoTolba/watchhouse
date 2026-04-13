"""Display sinks.

``--show`` opens a ``cv2.imshow`` window when a display is available; otherwise
(and by default) writes periodic JPEGs + thumbnails to disk. The ``--show``
path is the MVP of the future dashboard Live-View button
(CONTEXT.md §"Display Strategy").

Two sinks:

* :class:`HeadlessJpegSink` — writes a full-resolution JPEG every 5 s plus a
  320-pixel-wide thumbnail every 60 s, partitioned by ``source_id`` under
  ``$EVENT_IMAGE_DIR/phase0_probe/<source>/``.
* :class:`ImshowSink` — thin wrapper around ``cv2.imshow`` with a namedWindow
  handle so ``cv2.destroyWindow`` can clean up on exit.

The :class:`DisplaySink` factory picks between them at runtime and falls back
to headless whenever ``DISPLAY``/``WAYLAND_DISPLAY`` is unset or the imshow
constructor raises. ``force_headless=True`` bypasses the probe entirely for
tests.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

_LOG = logging.getLogger("home_cctv.display")

_JPEG_EVERY_SEC: float = 5.0
_THUMB_EVERY_SEC: float = 60.0
_THUMB_WIDTH: int = 320


class Sink(Protocol):
    def write(self, frame: np.ndarray) -> None: ...

    def close(self) -> None: ...


class HeadlessJpegSink:
    """Write a JPEG every 5 s + thumbnail every 60 s per source."""

    def __init__(self, out_dir: Path, source_id: str) -> None:
        safe = source_id.replace(":", "_").replace("/", "_")
        self._dir = Path(out_dir) / "phase0_probe" / safe
        self._dir.mkdir(parents=True, exist_ok=True)
        self._last_jpeg: float = 0.0
        self._last_thumb: float = 0.0
        self._source_id = source_id
        self._jpeg_count = 0
        self._thumb_count = 0

    def write(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        if now - self._last_jpeg >= _JPEG_EVERY_SEC:
            path = self._dir / f"{int(time.time())}.jpg"
            cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self._last_jpeg = now
            self._jpeg_count += 1
            _LOG.debug(
                "jpeg_written source=%s path=%s", self._source_id, path
            )
        if now - self._last_thumb >= _THUMB_EVERY_SEC:
            h, w = frame.shape[:2]
            thumb_h = max(1, int(h * (_THUMB_WIDTH / w)))
            thumb = cv2.resize(frame, (_THUMB_WIDTH, thumb_h))
            path = self._dir / f"thumb_{int(time.time())}.jpg"
            cv2.imwrite(str(path), thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
            self._last_thumb = now
            self._thumb_count += 1

    def close(self) -> None:  # pragma: no cover — nothing to do
        pass


class ImshowSink:  # pragma: no cover — exercised only when a display exists
    def __init__(self, source_id: str) -> None:
        self._title = f"home_cctv: {source_id}"
        cv2.namedWindow(self._title, cv2.WINDOW_NORMAL)

    def write(self, frame: np.ndarray) -> None:
        cv2.imshow(self._title, frame)
        cv2.waitKey(1)

    def close(self) -> None:
        try:
            cv2.destroyWindow(self._title)
        except Exception:
            pass


class DisplaySink:
    """Factory — returns an :class:`ImshowSink` or :class:`HeadlessJpegSink`."""

    @staticmethod
    def open(
        *,
        source_id: str,
        out_dir: Path,
        show: bool,
        force_headless: bool = False,
    ) -> Sink:
        if force_headless or not show:
            return HeadlessJpegSink(out_dir, source_id)
        has_display = bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )
        if not has_display:
            _LOG.warning(
                "--show requested but no DISPLAY/WAYLAND_DISPLAY — "
                "falling back to HeadlessJpegSink"
            )
            return HeadlessJpegSink(out_dir, source_id)
        try:  # pragma: no cover — only when a real display is attached
            return ImshowSink(source_id)
        except Exception as exc:
            _LOG.warning(
                "ImshowSink failed err=%r — falling back to headless", exc
            )
            return HeadlessJpegSink(out_dir, source_id)


__all__ = ["DisplaySink", "HeadlessJpegSink", "ImshowSink", "Sink"]
