"""Integration tests for the FrameSource abstraction against MP4 fixtures."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import home_cctv  # noqa: F401  — pre-cv2 env setup
from home_cctv.ingest.capture import (
    Mp4FrameSource,
    RtspFrameSource,
    open_frame_source,
)

FIXTURES = Path(__file__).parent / "fixtures"
_MAKE = FIXTURES / "make_fixtures.py"


@pytest.fixture(scope="session", autouse=True)
def _materialize_fixtures() -> None:
    """Generate MP4 fixtures on first run if they're missing."""
    needed = [
        FIXTURES / "sample_720p25.mp4",
        FIXTURES / "sample_1080p12.mp4",
        FIXTURES / "sample_with_green.mp4",
    ]
    if all(p.exists() and p.stat().st_size > 0 for p in needed):
        return
    subprocess.run([sys.executable, str(_MAKE)], check=True)


def test_factory_dispatches_by_scheme() -> None:
    fs_rtsp = open_frame_source(
        "rtsp://admin:pw@1.2.3.4:554/1", camera_id="cam1"
    )
    fs_mp4 = open_frame_source(
        str(FIXTURES / "sample_720p25.mp4"), camera_id="cam0:mp4"
    )
    assert isinstance(fs_rtsp, RtspFrameSource)
    assert isinstance(fs_mp4, Mp4FrameSource)
    assert fs_rtsp.source_id == "cam1"
    assert fs_mp4.source_id == "cam0:mp4"


def test_is_file_source_class_attributes() -> None:
    # Accessible as class attributes, not only instance attributes.
    assert Mp4FrameSource.is_file_source is True
    assert RtspFrameSource.is_file_source is False


def test_rtsp_sanitizer_masks_password() -> None:
    fs = open_frame_source(
        "rtsp://admin:SuperSecret@1.2.3.4:554/1", camera_id="cam1"
    )
    sanitized = fs._sanitized_target()
    assert "SuperSecret" not in sanitized
    assert "admin:***@" in sanitized


def test_missing_mp4_raises_on_open(tmp_path: Path) -> None:
    fs = open_frame_source(str(tmp_path / "nope.mp4"), camera_id="cam0:mp4")
    with pytest.raises(FileNotFoundError):
        fs.open()


def test_mp4_read_loop_720p25() -> None:
    fs = open_frame_source(
        str(FIXTURES / "sample_720p25.mp4"), camera_id="cam0:mp4"
    )
    fs.open()
    try:
        count_ok = 0
        while True:
            ok, frame = fs.read()
            if ok:
                count_ok += 1
                assert frame is not None
                assert frame.shape[1] == 1280 and frame.shape[0] == 720
            if not ok and fs.stats.decode_errors > 0:
                break
    finally:
        fs.release()
    # 3 s @ 25 fps = 75 frames, minus the 5 dropped post-open.
    assert count_ok >= 65, f"only got {count_ok} frames"
    assert fs.stats.frames_decoded == count_ok
    assert fs.stats.measured_fps > 15.0, (
        f"mp4 decode too slow: {fs.stats.measured_fps}"
    )


def test_green_frames_are_counted_as_corrupted() -> None:
    fs = open_frame_source(
        str(FIXTURES / "sample_with_green.mp4"), camera_id="cam0:mp4"
    )
    fs.open()
    try:
        while True:
            ok, _ = fs.read()
            if not ok and fs.stats.decode_errors > 0:
                break
    finally:
        fs.release()
    # 5 drop-after-open + 10 green tail = at least 10 corrupted (green tail).
    assert fs.stats.frames_corrupted >= 10


def test_stats_readable_after_release() -> None:
    fs = open_frame_source(
        str(FIXTURES / "sample_720p25.mp4"), camera_id="cam0:mp4"
    )
    fs.open()
    for _ in range(20):
        fs.read()
    fs.release()
    # Should not raise — stats survive release for the harness to print totals.
    _ = fs.stats.frames_decoded
    _ = fs.stats.frames_corrupted
    _ = fs.stats.decode_errors


def test_env_assertion_catches_tampering() -> None:
    saved = os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]
    try:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp"
        with pytest.raises(RuntimeError, match="OPENCV_FFMPEG_CAPTURE_OPTIONS"):
            open_frame_source("rtsp://x/1", camera_id="cam1")
    finally:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = saved
