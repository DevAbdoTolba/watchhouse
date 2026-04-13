"""Unit + integration tests for the ShutdownSupervisor and --mp4 E2E path."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import home_cctv  # noqa: F401  — pre-cv2 env setup
from home_cctv.ingest.supervisor import ShutdownSupervisor

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).parent.parent


class _FakeSource:
    def __init__(self, sid: str) -> None:
        self.source_id = sid
        self.released = 0

    def release(self) -> None:
        self.released += 1


# -------------------------------------------------------------- unit tests ---


def test_stop_event_default_unset() -> None:
    sup = ShutdownSupervisor()
    assert sup.stop_event.is_set() is False


def test_shutdown_releases_all_sources() -> None:
    sup = ShutdownSupervisor()
    a, b = _FakeSource("cam1"), _FakeSource("cam2")
    sup.register(a)
    sup.register(b)
    sup.shutdown()
    assert sup.stop_event.is_set() is True
    assert a.released == 1
    assert b.released == 1


def test_shutdown_is_idempotent() -> None:
    sup = ShutdownSupervisor()
    a = _FakeSource("cam1")
    sup.register(a)
    sup.shutdown()
    sup.shutdown()
    assert a.released == 1


def test_request_stop_does_not_release() -> None:
    sup = ShutdownSupervisor()
    a = _FakeSource("cam1")
    sup.register(a)
    sup.request_stop()
    assert sup.stop_event.is_set() is True
    assert a.released == 0  # release only happens via shutdown()


def test_wait_returns_quickly_after_set() -> None:
    sup = ShutdownSupervisor()
    sup.request_stop()
    t0 = time.monotonic()
    got = sup.wait_for_shutdown(timeout=1.0)
    assert got is True
    assert time.monotonic() - t0 < 0.2


def test_wait_times_out_when_not_set() -> None:
    sup = ShutdownSupervisor()
    t0 = time.monotonic()
    got = sup.wait_for_shutdown(timeout=0.1)
    assert got is False
    assert 0.08 <= time.monotonic() - t0 < 0.5


# -------------------------------------------------------- display sink tests


def test_headless_sink_rate_limits_jpegs(tmp_path: Path) -> None:
    from home_cctv.ingest.display import HeadlessJpegSink
    import numpy as np

    sink = HeadlessJpegSink(tmp_path, "cam0:mp4")
    frame = np.full((720, 1280, 3), 127, dtype=np.uint8)
    # First write always emits (last_jpeg starts at 0.0 → now - 0 >= 5).
    sink.write(frame)
    sink.write(frame)
    sink.write(frame)
    jpegs = list(
        (tmp_path / "phase0_probe" / "cam0_mp4").glob("[0-9]*.jpg")
    )
    assert len(jpegs) == 1, jpegs  # rate-limited to once per 5 s of monotonic


def test_display_sink_force_headless_bypass_probe(tmp_path: Path) -> None:
    from home_cctv.ingest.display import DisplaySink, HeadlessJpegSink

    sink = DisplaySink.open(
        source_id="cam0:mp4",
        out_dir=tmp_path,
        show=True,
        force_headless=True,
    )
    assert isinstance(sink, HeadlessJpegSink)


def test_display_sink_no_display_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from home_cctv.ingest.display import DisplaySink, HeadlessJpegSink

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    sink = DisplaySink.open(
        source_id="cam0:mp4", out_dir=tmp_path, show=True
    )
    assert isinstance(sink, HeadlessJpegSink)


# ----------------------------------------------------- end-to-end integration


def test_end_to_end_mp4_run_exits_cleanly(tmp_path: Path) -> None:
    """`python -m home_cctv --mp4 sample_720p25.mp4` runs to EOF and exits 0."""
    fixture = FIXTURES / "sample_720p25.mp4"
    if not fixture.exists():
        subprocess.run(
            [sys.executable, str(FIXTURES / "make_fixtures.py")], check=True
        )

    env = os.environ.copy()
    # Override the real .env with a synthetic one so the test is hermetic.
    env["DVR_IP"] = "192.168.1.10"
    env["DVR_PORT"] = "554"
    env["DVR_USER"] = "admin"
    env["DVR_PASS"] = "testpw"
    env["EVENT_IMAGE_DIR"] = str(tmp_path / "img")
    env["DB_PATH"] = str(tmp_path / "cctv.db")
    env["LOG_DIR"] = str(tmp_path / "logs")
    env["MODEL_CACHE_DIR"] = str(tmp_path / "models")
    # Pydantic-settings auto-loads .env from CWD which on this repo has legacy
    # keys; point it at a non-existent file so env-vars above are the only
    # source of truth.
    env["HOME_CCTV_ENV_FILE"] = str(tmp_path / "does_not_exist.env")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "home_cctv",
            "--mp4",
            str(fixture),
        ],
        env=env,
        cwd=str(tmp_path),  # avoid loading the repo's real .env
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    jpegs = list(
        (tmp_path / "img" / "phase0_probe" / "cam0_mp4").glob("*.jpg")
    )
    assert jpegs, f"no JPEGs in {tmp_path}; stderr: {result.stderr}"
    # Password must never appear in logs
    log_content = ""
    for f in (tmp_path / "logs").glob("*.log"):
        log_content += f.read_text(encoding="utf-8", errors="ignore")
    assert "testpw" not in log_content, (
        "plaintext password leaked into log file"
    )
    assert "testpw" not in result.stderr
