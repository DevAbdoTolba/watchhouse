"""Host / OpenCV / WSL2 / FFmpeg / HEVC detection.

Answers SUMMARY.md §6 Q2 (WSL2 networking mode) + Q3 (FFmpeg backend present).
Startup asserts flip from silent stalls to loud ``RuntimeError``s that name
the missing piece, matching ENV-05.
"""
from __future__ import annotations

import logging
import platform
import re
import subprocess
from pathlib import Path
from typing import Literal

import cv2
import psutil

_LOG = logging.getLogger("home_cctv.phase0.host_probe")

_FFMPEG_YES_RE = re.compile(r"FFMPEG:\s+YES")
# OpenCV's getBuildInformation does not print per-codec entries, so we detect
# HEVC support by asserting the bundled ``avcodec`` library is ≥ 58 (the
# release line that shipped native HEVC decoding). As a belt-and-braces check,
# we also match the literal substring ``hevc`` / ``h265`` when it is present.
_HEVC_RE = re.compile(r"hevc|h265|h\.265", re.IGNORECASE)
_AVCODEC_RE = re.compile(r"avcodec:\s+YES\s+\((\d+)\.(\d+)\.(\d+)\)", re.IGNORECASE)

NetworkingMode = Literal["mirrored", "nat", "bridged", "unknown"]


def assert_ffmpeg_backend() -> str:
    """Raise if OpenCV was built without FFmpeg.

    On success, returns the FFMPEG-related lines from
    ``cv2.getBuildInformation()`` for inclusion in the Phase 0 report.
    """
    info = cv2.getBuildInformation()
    if not _FFMPEG_YES_RE.search(info):
        raise RuntimeError(
            "OpenCV wheel built without FFmpeg — TCP / stimeout env vars are "
            "silently ignored. Install opencv-python-headless==4.10.0.84 "
            "(see PITFALLS §1.2)."
        )
    # Extract a short snippet: 6 lines around the FFMPEG line.
    lines = info.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if "FFMPEG" in ln:
            out = lines[max(0, i - 1) : i + 6]
            break
    return "\n".join(out).strip() or "FFMPEG: YES"


def assert_hevc_decoder() -> bool:
    """Raise if the OpenCV FFmpeg build lacks HEVC / H.265 decoder support.

    All four cameras are H.265 per ``cameras.txt``; without HEVC the capture
    will open but decode to green/black. ``cv2.getBuildInformation()`` does
    not enumerate individual codecs — instead it lists the bundled FFmpeg
    library versions. ``libavcodec ≥ 58`` ships native HEVC decoding, and
    every opencv-python-headless wheel since 4.6 links against avcodec 58+
    (4.10 links 59.37). We accept either an explicit ``hevc`` / ``h265``
    mention in the build info OR an avcodec version ≥ 58 as proof.
    """
    info = cv2.getBuildInformation()
    # Primary check: FFmpeg must be present at all.
    if not _FFMPEG_YES_RE.search(info):
        raise RuntimeError(
            "OpenCV wheel has no FFmpeg backend; HEVC decode impossible."
        )
    # Fast path: build info explicitly mentions hevc / h265.
    if _HEVC_RE.search(info):
        return True
    # Fallback: accept avcodec ≥ 58 as proof of bundled HEVC.
    m = _AVCODEC_RE.search(info)
    if m:
        major = int(m.group(1))
        if major >= 58:
            return True
    raise RuntimeError(
        "OpenCV wheel lacks HEVC decoder — pip install "
        "opencv-python-headless==4.10.0.84 rebuild required. All 4 "
        "cameras are H.265 per cameras.txt; without HEVC they will open "
        "but decode to green/black."
    )


def _parse_default_gateway() -> str | None:
    """Return the IPv4 default-gateway address by parsing ``/proc/net/route``.

    Returns None on any parse error. Hex octets in ``/proc/net/route`` are
    little-endian so we reverse them.
    """
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as f:
            next(f)  # header
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                iface, dest_hex, gw_hex = parts[0], parts[1], parts[2]
                if dest_hex == "00000000":
                    # little-endian hex; reverse byte pairs
                    octets = [
                        int(gw_hex[i : i + 2], 16) for i in (6, 4, 2, 0)
                    ]
                    return ".".join(str(o) for o in octets)
    except Exception as exc:  # pragma: no cover — defensive
        _LOG.debug("gateway parse failed: %r", exc)
    return None


def detect_wsl2_networking_mode() -> NetworkingMode:
    """Classify WSL2 networking mode from the default-gateway address.

    * ``172.*`` / ``10.255.*`` → NAT (default older WSL2)
    * ``192.168.*`` → mirrored (Windows 11 22H2+)
    * anything else → bridged
    * parse failure → unknown
    """
    gw = _parse_default_gateway()
    if gw is None:
        return "unknown"
    try:
        if gw.startswith("172.") or gw.startswith("10.255."):
            return "nat"
        if gw.startswith("192.168."):
            return "mirrored"
        return "bridged"
    except Exception:
        return "unknown"


def _probe_wsl_version() -> str:
    """Best-effort WSL version string. Falls back to ``/proc/version``."""
    try:
        out = subprocess.run(
            ["wsl.exe", "--version"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            first = out.stdout.strip().splitlines()[0]
            return first
    except Exception:
        pass
    try:
        return Path("/proc/version").read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return "unknown"


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def probe_host() -> dict:
    """Return a dict matching CONTEXT.md §Phase 0 schema ``host`` block."""
    uname = platform.uname()
    vm = psutil.virtual_memory()
    return {
        "os": f"{uname.system} {uname.release}",
        "kernel": uname.version,
        "cpu_model": _cpu_model(),
        "cpu_cores_physical": psutil.cpu_count(logical=False) or 0,
        "cpu_cores_logical": psutil.cpu_count(logical=True) or 0,
        "ram_gb": round(vm.total / (1024**3), 2),
        "wsl2_networking_mode": detect_wsl2_networking_mode(),
        "wsl_version": _probe_wsl_version(),
    }


__all__ = [
    "assert_ffmpeg_backend",
    "assert_hevc_decoder",
    "detect_wsl2_networking_mode",
    "probe_host",
]
