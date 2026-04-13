"""Tests for phase0.report schema + dry-run offline harness."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from home_cctv.config.cameras import CameraConfig
from home_cctv.config.env import load_settings
from home_cctv.phase0.report import (
    REPORT_PATH,
    CameraResult,
    Phase0Report,
    new_report,
    print_stdout_summary,
    write_report,
)
from home_cctv.phase0.sanity import per_camera_exit_ok, run_phase0

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    d = tmp_path / "img"
    d.mkdir()
    db = tmp_path / "events.db"
    mc = tmp_path / "models"
    mc.mkdir()
    lg = tmp_path / "logs"
    lg.mkdir()
    monkeypatch.setenv("DVR_IP", "192.168.1.10")
    monkeypatch.setenv("DVR_PORT", "554")
    monkeypatch.setenv("DVR_USER", "admin")
    monkeypatch.setenv("DVR_PASS", "testpw")
    monkeypatch.setenv("EVENT_IMAGE_DIR", str(d))
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("LOG_DIR", str(lg))
    monkeypatch.setenv("MODEL_CACHE_DIR", str(mc))
    monkeypatch.chdir(REPO)
    return load_settings()


def test_new_report_has_context_md_keys() -> None:
    r = new_report()
    d = r.to_dict()
    required = {
        "timestamp_utc",
        "host",
        "opencv",
        "env_vars_loaded",
        "credentials_masked_in_logs",
        "disk_space",
        "dvr",
        "cameras",
        "model_bundle",
        "blockers_resolved",
        "phase0_verdict",
        "notes",
    }
    assert required.issubset(d.keys())


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    r = new_report()
    r.host = {"os": "Linux", "ram_gb": 16.0}
    r.phase0_verdict = "pass"
    path = tmp_path / "r.json"
    write_report(r, path)
    loaded = Phase0Report.from_json(path)
    assert loaded.to_dict() == r.to_dict()


def _cam(id_: int, name: str, fps: int, nal: bool = False) -> CameraConfig:
    return CameraConfig(
        id=id_,
        name=name,
        location="x",
        coverage="x",
        sub_path="/1",
        main_path="/0",
        codec="hevc",
        native_fps=fps,
        native_width=1280,
        native_height=720,
        nal_unit_0_workaround_required=nal,
    )


def _result(**kwargs) -> CameraResult:
    base = dict(
        name="x",
        sub_path="/1",
        main_path="/0",
        codec="hevc",
        advertised_fps=25,
        measured_fps=24.0,
        width=1280,
        height=720,
        capture_duration_sec=1800,
        frames_decoded=40000,
        frames_corrupted=0,
        decode_errors=0,
        hang_events=0,
    )
    base.update(kwargs)
    return CameraResult(**base)


def test_per_camera_exit_ok_cam1_ok() -> None:
    cam = _cam(1, "exterior_red", 25)
    r = _result(measured_fps=24.0, frames_corrupted=0)
    assert per_camera_exit_ok(cam, r) is True


def test_per_camera_exit_ok_cam1_fps_too_low() -> None:
    cam = _cam(1, "exterior_red", 25)
    r = _result(measured_fps=10.0)
    assert per_camera_exit_ok(cam, r) is False


def test_per_camera_exit_ok_cam3_ratio_ok() -> None:
    cam = _cam(3, "exterior_blue", 12, nal=True)
    r = _result(
        advertised_fps=12,
        measured_fps=11.6,
        frames_decoded=950,
        frames_corrupted=30,
    )
    assert per_camera_exit_ok(cam, r) is True


def test_per_camera_exit_ok_cam3_ratio_fail() -> None:
    cam = _cam(3, "exterior_blue", 12, nal=True)
    r = _result(
        advertised_fps=12,
        measured_fps=11.6,
        frames_decoded=900,
        frames_corrupted=100,
    )
    assert per_camera_exit_ok(cam, r) is False


def test_per_camera_exit_ok_cam1_any_corruption_tolerates_drop_window() -> None:
    # 5 frames dropped on open is the baseline — allowed.
    cam = _cam(1, "exterior_red", 25)
    r = _result(measured_fps=24.0, frames_corrupted=5)
    assert per_camera_exit_ok(cam, r) is True


def test_per_camera_exit_ok_cam1_excess_corruption_fails() -> None:
    cam = _cam(1, "exterior_red", 25)
    r = _result(measured_fps=24.0, frames_corrupted=20)
    assert per_camera_exit_ok(cam, r) is False


def test_stdout_summary_contains_camera_names(capsys) -> None:
    r = new_report()
    r.cameras = {
        "1": {
            "name": "exterior_red",
            "sub_path": "/1",
            "main_path": "/0",
            "codec": "hevc",
            "advertised_fps": 25,
            "measured_fps": 24.0,
            "frames_decoded": 40000,
            "frames_corrupted": 0,
            "decode_errors": 0,
            "hang_events": 0,
            "exit_ok": True,
        },
        "2": {
            "name": "interior_orange",
            "sub_path": "/11",
            "main_path": "/10",
            "codec": "hevc",
            "advertised_fps": 12,
            "measured_fps": 11.9,
            "frames_decoded": 20000,
            "frames_corrupted": 0,
            "decode_errors": 0,
            "hang_events": 0,
            "exit_ok": True,
        },
        "3": {
            "name": "exterior_blue",
            "sub_path": "/21",
            "main_path": "/20",
            "codec": "hevc",
            "advertised_fps": 12,
            "measured_fps": 11.6,
            "frames_decoded": 900,
            "frames_corrupted": 30,
            "decode_errors": 30,
            "hang_events": 0,
            "exit_ok": True,
        },
        "4": {
            "name": "interior_green",
            "sub_path": "/31",
            "main_path": "/30",
            "codec": "hevc",
            "advertised_fps": 25,
            "measured_fps": 24.0,
            "frames_decoded": 40000,
            "frames_corrupted": 50,
            "decode_errors": 50,
            "hang_events": 0,
            "exit_ok": True,
        },
    }
    print_stdout_summary(r)
    out = capsys.readouterr().out
    for name in (
        "exterior_red",
        "interior_orange",
        "exterior_blue",
        "interior_green",
    ):
        assert name in out


def test_offline_dry_run_mp4(tmp_path, fake_settings) -> None:
    fixture = FIXTURES / "sample_720p25.mp4"
    # The conftest-autouse fixture generates the MP4 on first run, but the
    # dry-run test can be invoked standalone — regenerate if missing.
    if not fixture.exists():
        from tests.fixtures.make_fixtures import main as make_fixtures_main

        make_fixtures_main(FIXTURES)

    # B3: snapshot the canonical report path if it exists so we can later
    # assert it was NOT touched by the dry run.
    canonical = REPO / REPORT_PATH
    canonical_snapshot = None
    if canonical.exists():
        canonical_snapshot = (canonical.stat().st_mtime, canonical.read_bytes())

    dry_path = tmp_path / "dry.json"
    report = run_phase0(
        fake_settings,
        mp4_override=str(fixture),
        duration_sec=3,
        skip_model_bundle=True,
        skip_network_probe=True,
        report_path=dry_path,
    )

    assert dry_path.exists(), "dry-run report must be written"
    data = json.loads(dry_path.read_text())
    assert len(data["cameras"]) == 4
    # cold_start_ms always present with the three canonical keys.
    assert set(data["model_bundle"]["cold_start_ms"].keys()) == {
        "yolo_openvino",
        "deepface",
        "easyocr",
    }
    # B3 assertion: canonical report file unchanged.
    if canonical_snapshot is not None:
        mtime_after = canonical.stat().st_mtime
        bytes_after = canonical.read_bytes()
        assert mtime_after == canonical_snapshot[0]
        assert bytes_after == canonical_snapshot[1]
    else:
        assert not canonical.exists(), (
            "dry-run must not create canonical PHASE0-REPORT.json"
        )
    assert report.phase0_verdict in {"pass", "fail", "aborted"}
