"""Tests for host / opencv / wsl2 / dvr reachability probes."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from home_cctv.phase0.host_probe import (
    assert_ffmpeg_backend,
    assert_hevc_decoder,
    detect_wsl2_networking_mode,
    probe_host,
)
from home_cctv.phase0.network_probe import probe_dvr_reachable


def test_assert_ffmpeg_backend_on_real_cv2() -> None:
    # Real OpenCV 4.10 headless wheel ships with FFmpeg YES.
    snippet = assert_ffmpeg_backend()
    assert "FFMPEG" in snippet


def test_assert_hevc_decoder_on_real_cv2() -> None:
    assert assert_hevc_decoder() is True


def test_assert_ffmpeg_backend_raises_when_missing() -> None:
    fake_build = "Video I/O:\n    FFMPEG:                      NO\n"
    with patch(
        "home_cctv.phase0.host_probe.cv2.getBuildInformation",
        return_value=fake_build,
    ):
        with pytest.raises(RuntimeError, match="FFmpeg"):
            assert_ffmpeg_backend()


def test_assert_hevc_decoder_raises_when_missing() -> None:
    # FFmpeg present, but avcodec is pre-HEVC (57.x) and no hevc substring.
    fake_build = (
        "Video I/O:\n"
        "    FFMPEG:                      YES\n"
        "      avcodec:                   YES (57.107.100)\n"
        "      avformat:                  YES (57.83.100)\n"
    )
    with patch(
        "home_cctv.phase0.host_probe.cv2.getBuildInformation",
        return_value=fake_build,
    ):
        with pytest.raises(RuntimeError, match="HEVC"):
            assert_hevc_decoder()


def test_assert_hevc_decoder_accepts_avcodec_59() -> None:
    fake_build = (
        "Video I/O:\n"
        "    FFMPEG:                      YES\n"
        "      avcodec:                   YES (59.37.100)\n"
    )
    with patch(
        "home_cctv.phase0.host_probe.cv2.getBuildInformation",
        return_value=fake_build,
    ):
        assert assert_hevc_decoder() is True


def test_probe_host_returns_required_keys() -> None:
    h = probe_host()
    required = {
        "os",
        "kernel",
        "cpu_model",
        "cpu_cores_physical",
        "cpu_cores_logical",
        "ram_gb",
        "wsl2_networking_mode",
        "wsl_version",
    }
    assert required.issubset(h.keys())
    assert isinstance(h["cpu_cores_physical"], int)
    assert isinstance(h["cpu_cores_logical"], int)
    assert isinstance(h["ram_gb"], (int, float))


def test_detect_wsl2_networking_mode_returns_known_value() -> None:
    mode = detect_wsl2_networking_mode()
    assert mode in {"mirrored", "nat", "bridged", "unknown"}


def test_dvr_reachable_negative_case() -> None:
    # Port 1 is "tcpmux" historically; nothing listens in WSL2.
    assert probe_dvr_reachable("127.0.0.1", 1, timeout=0.2) is False
