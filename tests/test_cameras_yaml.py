"""Tests for cameras.yaml loader + build_rtsp_url."""
from __future__ import annotations

from pathlib import Path

import pytest

from home_cctv.config.cameras import (
    CameraConfig,
    CamerasFile,
    build_rtsp_url,
    load_cameras,
)
from home_cctv.config.env import load_settings

REPO = Path(__file__).resolve().parent.parent
CAMERAS_YAML = REPO / "cameras.yaml"


@pytest.fixture
def settings(tmp_path, monkeypatch):
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
    monkeypatch.chdir(tmp_path)
    return load_settings()


def test_cameras_file_loads_four() -> None:
    cams = load_cameras(CAMERAS_YAML)
    assert isinstance(cams, CamerasFile)
    assert len(cams.cameras) == 4
    assert [c.id for c in cams.cameras] == [1, 2, 3, 4]


def test_cam1_exterior_red_fields() -> None:
    cams = load_cameras(CAMERAS_YAML)
    c = cams.cameras[0]
    assert c.name == "exterior_red"
    assert c.sub_path == "/1"
    assert c.main_path == "/0"
    assert c.codec == "hevc"
    assert c.native_fps == 25
    assert c.native_width == 1280
    assert c.native_height == 720
    assert c.nal_unit_0_workaround_required is False


def test_cam2_interior_orange_fields() -> None:
    cams = load_cameras(CAMERAS_YAML)
    c = cams.cameras[1]
    assert c.name == "interior_orange"
    assert c.sub_path == "/11"
    assert c.main_path == "/10"
    assert c.native_fps == 12
    assert c.native_width == 1920
    assert c.native_height == 1080
    assert c.sensor_native == "5MP"


def test_cam3_has_nal_flag() -> None:
    cams = load_cameras(CAMERAS_YAML)
    c = cams.cameras[2]
    assert c.name == "exterior_blue"
    assert c.sub_path == "/21"
    assert c.nal_unit_0_workaround_required is True


def test_cam4_has_nal_flag_and_25fps() -> None:
    cams = load_cameras(CAMERAS_YAML)
    c = cams.cameras[3]
    assert c.name == "interior_green"
    assert c.sub_path == "/31"
    assert c.nal_unit_0_workaround_required is True
    assert c.native_fps == 25


def test_build_rtsp_url_sub(settings) -> None:
    cams = load_cameras(CAMERAS_YAML)
    url = build_rtsp_url(settings, cams.cameras[0], stream="sub")
    assert url == "rtsp://admin:testpw@192.168.1.10:554/1"


def test_build_rtsp_url_main(settings) -> None:
    cams = load_cameras(CAMERAS_YAML)
    url = build_rtsp_url(settings, cams.cameras[0], stream="main")
    assert url == "rtsp://admin:testpw@192.168.1.10:554/0"


def test_build_rtsp_url_cam3_sub(settings) -> None:
    cams = load_cameras(CAMERAS_YAML)
    url = build_rtsp_url(settings, cams.cameras[2], stream="sub")
    assert url == "rtsp://admin:testpw@192.168.1.10:554/21"
