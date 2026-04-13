from pathlib import Path

import pytest

from home_cctv.config.env import (
    assert_disk_space_ok,
    assert_not_drvfs,
    load_settings,
)


@pytest.fixture
def good_env(tmp_path, monkeypatch):
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
    monkeypatch.setenv("DVR_PASS", "secret")
    monkeypatch.setenv("EVENT_IMAGE_DIR", str(d))
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("LOG_DIR", str(lg))
    monkeypatch.setenv("MODEL_CACHE_DIR", str(mc))
    # Isolate from the repo-level .env file
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_load_settings_happy(good_env):
    s = load_settings()
    assert s.DVR_IP == "192.168.1.10"
    assert s.DVR_PORT == 554
    assert s.DVR_USER == "admin"
    assert s.DVR_PASS == "secret"
    assert s.masked_rtsp_base() == "rtsp://admin:***@192.168.1.10:554"
    assert "secret" not in s.masked_rtsp_base()


def test_missing_dvr_ip_raises_named_error(good_env, monkeypatch):
    monkeypatch.delenv("DVR_IP", raising=False)
    # Also block .env fallback by pointing cwd somewhere empty.
    monkeypatch.chdir(good_env)
    with pytest.raises(RuntimeError) as ei:
        load_settings()
    assert "DVR_IP" in str(ei.value)


def test_disk_space_too_small_raises(tmp_path):
    with pytest.raises(RuntimeError, match=r"free.*GB|GB.*free"):
        assert_disk_space_ok(tmp_path, min_gb=9_999_999.0)


def test_drvfs_prefix_rejected():
    with pytest.raises(RuntimeError, match="DrvFs"):
        assert_not_drvfs(Path("/mnt/c/Users/anyone/data"))


def test_ext4_home_path_accepted():
    # Use /tmp (ext4 on WSL2 Ubuntu). pytest's tmp_path fixture may
    # land under /mnt/d/... when the repo itself is on DrvFs, which
    # would trip the guard under test.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        assert_not_drvfs(Path(d))


def test_dvr_pass_not_in_masked_base(good_env):
    s = load_settings()
    assert "secret" not in s.masked_rtsp_base()
    assert "***" in s.masked_rtsp_base()
