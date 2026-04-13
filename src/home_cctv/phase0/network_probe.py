"""DVR reachability + handshake latency + concurrent-session stepping.

Answers SUMMARY.md §6 Q6 (DVR connection cap). These probes run live against
the real DVR; unit tests mock the socket / capture layer so they remain
hermetic.
"""
from __future__ import annotations

import logging
import socket
import time
from typing import List, Optional

import cv2

from home_cctv.ingest.flags import assert_capture_options_active

_LOG = logging.getLogger("home_cctv.phase0.network_probe")


def probe_dvr_reachable(host: str, port: int, *, timeout: float = 3.0) -> bool:
    """Return True iff a TCP socket to ``host:port`` opens within ``timeout``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        _LOG.debug("probe_dvr_reachable(%s, %s) failed: %r", host, port, exc)
        return False


def measure_handshake_latency(
    rtsp_url: str, *, samples: int = 3
) -> Optional[float]:
    """Mean wall-clock milliseconds to open + read one frame, over N samples.

    Returns ``None`` if the capture never opened successfully (caller decides
    whether that is fatal).
    """
    assert_capture_options_active()
    deltas: List[float] = []
    for i in range(max(1, samples)):
        t0 = time.monotonic()
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        try:
            if not cap.isOpened():
                _LOG.debug("handshake sample %d: not opened", i)
                continue
            ok, _ = cap.read()
            if not ok:
                _LOG.debug("handshake sample %d: read failed", i)
                continue
            deltas.append((time.monotonic() - t0) * 1000.0)
        finally:
            try:
                cap.release()
            except Exception:
                pass
        time.sleep(0.2)
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def step_concurrent_sessions(
    rtsp_url: str, *, max_n: int = 6
) -> tuple[int, int]:
    """Open up to ``max_n`` simultaneous captures; return ``(last_ok, tested)``.

    ``last_ok`` is the largest N for which every capture up to and including N
    successfully opened AND read a frame. ``tested`` is ``max_n`` (how far we
    were asked to probe). All captures are released in the finally block.
    """
    assert_capture_options_active()
    caps: List[cv2.VideoCapture] = []
    last_ok = 0
    try:
        for n in range(1, max_n + 1):
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            caps.append(cap)
            if not cap.isOpened():
                _LOG.info("concurrent session %d: open failed", n)
                break
            ok, _ = cap.read()
            if not ok:
                _LOG.info("concurrent session %d: read failed", n)
                break
            last_ok = n
            time.sleep(0.1)
    finally:
        for cap in caps:
            try:
                cap.release()
            except Exception:
                pass
    return last_ok, max_n


__all__ = [
    "probe_dvr_reachable",
    "measure_handshake_latency",
    "step_concurrent_sessions",
]
