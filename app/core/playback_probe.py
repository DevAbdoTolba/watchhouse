"""DVR playback protocol probe.

Different DVR firmwares expose recorded video through very different APIs.
Before committing to a playback UI we need to know which method this DVR
actually answers. This worker tries every common avenue and dumps the
results to the log bus so the console panel shows the full table.

Tries, in order:
  1. ONVIF endpoint reachability (HTTP probe; no SOAP yet, just "is there
     something there"). Hits the well-known /onvif/device_service path on
     ports 80, 8080, 8000.
  2. A series of RTSP "playback URL with time parameters" templates that
     cover Hikvision, Dahua, Hisilicon, BitVision and generic Cantonk
     conventions. Asks cv2.VideoCapture to open each and reports
     whether it opened and whether one frame could be read.

This is a strictly read-only probe; nothing is written or changed on
the DVR.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import cv2
from PySide6.QtCore import QThread, Signal

from app.core.config import Settings
from app.core.log import bus, mask_url


@dataclass(frozen=True)
class ProbeAttempt:
    label: str
    url_or_endpoint: str   # already masked
    ok: bool
    detail: str


def _iso_compact(t: datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%SZ")


def _underscore(t: datetime) -> str:
    return t.strftime("%Y_%m_%d_%H_%M_%S")


def _dash(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _url_dash(t: datetime) -> str:
    # URL-encoded variant of the dash format (spaces -> %20, colons -> %3A)
    return quote(_dash(t), safe="")


def _try_http_onvif(host: str, port: int) -> ProbeAttempt:
    label = f"ONVIF http://{host}:{port}/onvif/device_service"
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=1.5) as sock:
            req = (
                f"POST /onvif/device_service HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Content-Type: application/soap+xml; charset=utf-8\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            ).encode("ascii")
            sock.sendall(req)
            sock.settimeout(1.5)
            data = sock.recv(2048)
            elapsed = (time.monotonic() - t0) * 1000
            first_line = data.split(b"\r\n", 1)[0].decode("ascii", errors="replace") if data else "(no data)"
            if not data:
                return ProbeAttempt(label, label, False, f"no response in {elapsed:.0f} ms")
            # Any HTTP-looking response from /onvif/device_service indicates
            # there's at least a device there talking ONVIF (even a 400/401
            # is good news, it means the path exists).
            ok = b"HTTP/" in data
            return ProbeAttempt(label, label, ok, f"{first_line} ({elapsed:.0f} ms)")
    except OSError as e:
        return ProbeAttempt(label, label, False, f"connect failed: {e!s}")


def _try_rtsp_open(label: str, url: str, timeout_s: float = 5.0) -> ProbeAttempt:
    """Try to open a single RTSP URL. Reports whether it opened and
    whether at least one frame could be read inside `timeout_s`."""
    masked = mask_url(url)
    t0 = time.monotonic()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    if not cap.isOpened():
        cap.release()
        elapsed = (time.monotonic() - t0) * 1000
        return ProbeAttempt(label, masked, False, f"VideoCapture did not open ({elapsed:.0f} ms)")

    frame_got = False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ok, frame = cap.read()
        if ok and frame is not None:
            frame_got = True
            break
    cap.release()
    elapsed = (time.monotonic() - t0) * 1000
    if frame_got:
        return ProbeAttempt(label, masked, True, f"opened AND first frame received ({elapsed:.0f} ms)")
    return ProbeAttempt(label, masked, False, f"opened but no frame in {timeout_s:.0f}s ({elapsed:.0f} ms)")


class PlaybackProbeWorker(QThread):
    """Runs the full battery against camera 1 only (the rest will use the
    same scheme once one works)."""

    finished_with = Signal(list)  # list[ProbeAttempt]

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings

    def run(self) -> None:
        s = self._settings
        bus.info("PBPROBE", "starting DVR playback protocol probe (cam 1, last 5 min)")

        results: list[ProbeAttempt] = []

        # --- ONVIF reachability ---
        for port in (80, 8080, 8000):
            r = _try_http_onvif(s.dvr_ip, port)
            bus.info("PBPROBE", f"{r.label}: {'OK' if r.ok else 'no'} - {r.detail}")
            results.append(r)

        # --- RTSP playback URL templates (camera 1) ---
        # Probe window: 5 minutes ago, lasting 60s. Recent enough to be
        # present, old enough to not collide with the live tail.
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(minutes=5)
        end = start + timedelta(minutes=1)

        user_q = quote(s.dvr_user, safe="")
        pass_q = quote(s.dvr_pass, safe="")
        host = s.dvr_ip
        port = s.dvr_port

        # Common channel-encoded values for the four indexed sub-streams
        # used by this DVR's live URL scheme (/0, /10, /20, /30 main; /1
        # /11 /21 /31 sub). Try a few "what does ch=1 mean here" variants.
        templates: list[tuple[str, str]] = [
            (
                "Hikvision ISAPI tracks",
                f"rtsp://{user_q}:{pass_q}@{host}:{port}/Streaming/tracks/101"
                f"?starttime={_iso_compact(start)}&endtime={_iso_compact(end)}",
            ),
            (
                "Hikvision ISAPI tracks (no leading 1)",
                f"rtsp://{user_q}:{pass_q}@{host}:{port}/Streaming/tracks/01"
                f"?starttime={_iso_compact(start)}&endtime={_iso_compact(end)}",
            ),
            (
                "Dahua realmonitor (channel=1)",
                f"rtsp://{user_q}:{pass_q}@{host}:{port}/cam/realmonitor"
                f"?channel=1&subtype=0&starttime={_underscore(start)}&endtime={_underscore(end)}",
            ),
            (
                "Dahua playback (channel=1)",
                f"rtsp://{user_q}:{pass_q}@{host}:{port}/cam/playback"
                f"?channel=1&subtype=0&starttime={_url_dash(start)}&endtime={_url_dash(end)}",
            ),
            (
                "Hisilicon h264 ch1 main starttime",
                f"rtsp://{user_q}:{pass_q}@{host}:{port}/h264/ch1/main/av_stream"
                f"?starttime={_iso_compact(start)}",
            ),
            (
                "BitVision-style indexed /0 with starttime",
                f"rtsp://{user_q}:{pass_q}@{host}:{port}/0"
                f"?starttime={_iso_compact(start)}&endtime={_iso_compact(end)}",
            ),
            (
                "BitVision-style indexed /playback/0",
                f"rtsp://{user_q}:{pass_q}@{host}:{port}/playback/0"
                f"?starttime={_underscore(start)}&endtime={_underscore(end)}",
            ),
            (
                "Generic /playback?channel=1",
                f"rtsp://{user_q}:{pass_q}@{host}:{port}/playback"
                f"?channel=1&starttime={_iso_compact(start)}&endtime={_iso_compact(end)}",
            ),
        ]

        for label, url in templates:
            bus.info("PBPROBE", f"trying: {label}  {mask_url(url)}")
            r = _try_rtsp_open(label, url, timeout_s=4.0)
            bus.info("PBPROBE", f"  -> {'OK' if r.ok else 'no'}: {r.detail}")
            results.append(r)

        winners = [r for r in results if r.ok]
        if winners:
            bus.info("PBPROBE", f"=== {len(winners)} method(s) responded ===")
            for w in winners:
                bus.info("PBPROBE", f"  WIN  {w.label}: {w.detail}")
        else:
            bus.error(
                "PBPROBE",
                "no playback method answered. The DVR likely uses a proprietary "
                "binary protocol; will need a vendor-SDK or web-UI approach for v0.2.0.",
            )

        bus.info("PBPROBE", "probe complete")
        self.finished_with.emit(results)
