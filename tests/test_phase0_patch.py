"""Regression tests for the 2026-04-17 Phase 0 patch (00-99).

Covers four bug fixes and one calibration:

- Bug 1/2 — DeepFace ArcFace cache detection reads the canonical
  ``~/.deepface/weights/arcface_weights.h5`` path with a generous byte-size
  floor.
- Bug 3 — ``env_vars_loaded`` reports whichever alias the user's .env
  actually set (legacy DVR_USERNAME / DVR_LOCAL_IP / ... OR canonical
  DVR_USER / DVR_IP / ...).
- Bug 4 — ``wsl.exe --version``'s BOM-less UTF-16-LE stdout decodes cleanly
  (no embedded NUL bytes in the result).
- Calibration — ``per_camera_exit_ok`` keys on ``advertised_sub_fps``
  (not ``native_fps``) so the real 14.97 fps sub-stream pass against a 15
  fps target.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from home_cctv.config.cameras import CameraConfig
from home_cctv.config.env import load_settings
from home_cctv.phase0 import host_probe, model_bundle
from home_cctv.phase0.host_probe import _decode_wsl_version_bytes
from home_cctv.phase0.model_bundle import verify_model_bundle
from home_cctv.phase0.report import CameraResult
from home_cctv.phase0.sanity import _collect_env_vars_loaded, per_camera_exit_ok


# ---------------------------------------------------------------------------
# Bug 4 — wsl.exe --version UTF-16-LE decode
# ---------------------------------------------------------------------------


def test_wsl_version_parses_utf16() -> None:
    """UTF-16-LE stdout from wsl.exe decodes without embedded NUL bytes."""
    raw = (
        "WSL version: 2.6.3.0\r\n"
        "Kernel version: 6.6.87.2-1\r\n"
        "WSLg version: 1.0.71\r\n"
    ).encode("utf-16-le")
    text = _decode_wsl_version_bytes(raw)
    assert "\x00" not in text, f"decoded text still contains NULs: {text!r}"
    assert text.startswith("WSL version: 2.6.3.0")


def test_wsl_version_handles_bom_prefix() -> None:
    """A BOM-prefixed UTF-16-LE stream also decodes cleanly."""
    raw = b"\xff\xfe" + "WSL version: 2.6.3.0\r\n".encode("utf-16-le")
    text = _decode_wsl_version_bytes(raw)
    assert "\x00" not in text
    assert text.startswith("WSL version:")


def test_wsl_version_utf8_fallback() -> None:
    """Plain UTF-8 stdout (no NULs) also decodes correctly."""
    raw = b"WSL version: 2.6.3.0\n"
    text = _decode_wsl_version_bytes(raw)
    assert "\x00" not in text
    assert text.startswith("WSL version:")


def test_wsl_version_empty_bytes() -> None:
    """Empty stdout yields empty string — does not crash."""
    assert _decode_wsl_version_bytes(b"") == ""


# ---------------------------------------------------------------------------
# Bug 2 — DeepFace ArcFace cache detection via canonical path
# ---------------------------------------------------------------------------


def test_deepface_cache_detection(tmp_path: Path, monkeypatch) -> None:
    """verify_model_bundle reports arcface_cached=True when the 137 MB file
    exists at the canonical ``~/.deepface/weights/arcface_weights.h5``.

    Writes a fake 137 MB file at a mocked canonical path; the verifier must
    pick it up without needing a marker copy inside MODEL_CACHE_DIR.
    """
    fake_weights_dir = tmp_path / "deepface_weights"
    fake_weights_dir.mkdir()
    fake_arcface = fake_weights_dir / "arcface_weights.h5"
    # 137 MB round number — above the 100 MB floor in module_bundle.
    fake_arcface.write_bytes(b"\x00" * 137_000_000)
    fake_retina = fake_weights_dir / "retinaface.h5"
    fake_retina.write_bytes(b"\x00" * 107_000_000)

    monkeypatch.setattr(
        model_bundle, "DEEPFACE_ARCFACE_FILE", fake_arcface
    )
    monkeypatch.setattr(
        model_bundle, "DEEPFACE_RETINAFACE_FILE", fake_retina
    )
    # Also isolate WEIGHTS_LOCK_PATH so the real committed lock does not
    # interfere with the empty cache check.
    fake_lock = tmp_path / "weights.lock.json"
    monkeypatch.setattr(model_bundle, "WEIGHTS_LOCK_PATH", fake_lock)

    empty_cache = tmp_path / "cache"
    empty_cache.mkdir()
    v = verify_model_bundle(empty_cache)
    assert v["deepface_arcface_cached"] is True
    assert v["deepface_retinaface_cached"] is True


def test_deepface_cache_detection_rejects_truncated(
    tmp_path: Path, monkeypatch
) -> None:
    """A too-small arcface file (1 KB) must NOT flip the cache flag — rejects
    truncated / placeholder downloads.
    """
    fake_weights_dir = tmp_path / "deepface_weights"
    fake_weights_dir.mkdir()
    fake_arcface = fake_weights_dir / "arcface_weights.h5"
    fake_arcface.write_bytes(b"\x00" * 1024)
    monkeypatch.setattr(model_bundle, "DEEPFACE_ARCFACE_FILE", fake_arcface)
    fake_lock = tmp_path / "weights.lock.json"
    monkeypatch.setattr(model_bundle, "WEIGHTS_LOCK_PATH", fake_lock)

    empty_cache = tmp_path / "cache"
    empty_cache.mkdir()
    v = verify_model_bundle(empty_cache)
    assert v["deepface_arcface_cached"] is False


# ---------------------------------------------------------------------------
# Bug 3 — env_vars_loaded via Settings alias table
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy_env_settings(tmp_path, monkeypatch):
    """Legacy .env vocabulary — only DVR_USERNAME / DVR_PASSWORD / DVR_LOCAL_*
    are set; canonical DVR_USER / DVR_PASS / DVR_IP / DVR_PORT are NOT.
    """
    d = tmp_path / "img"
    d.mkdir()
    db = tmp_path / "events.db"
    mc = tmp_path / "models"
    mc.mkdir()
    lg = tmp_path / "logs"
    lg.mkdir()
    # Clear any canonical names inherited from the environment.
    for k in ("DVR_USER", "DVR_PASS", "DVR_IP", "DVR_PORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DVR_LOCAL_IP", "192.168.1.10")
    monkeypatch.setenv("DVR_LOCAL_RTSP_PORT", "554")
    monkeypatch.setenv("DVR_USERNAME", "admin")
    monkeypatch.setenv("DVR_PASSWORD", "legacy-pw")
    monkeypatch.setenv("EVENT_IMAGE_DIR", str(d))
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("LOG_DIR", str(lg))
    monkeypatch.setenv("MODEL_CACHE_DIR", str(mc))
    monkeypatch.chdir(tmp_path)
    return load_settings()


def test_env_vars_loaded_reports_legacy_aliases(legacy_env_settings) -> None:
    """When only legacy env vars are set, the reporter records the legacy
    alias names. The old code returned [] here because it only looked at
    canonical names.
    """
    names = _collect_env_vars_loaded(legacy_env_settings)
    # Must include at least one alias for each of the 4 DVR fields.
    assert "DVR_LOCAL_IP" in names
    assert "DVR_LOCAL_RTSP_PORT" in names
    assert "DVR_USERNAME" in names
    assert "DVR_PASSWORD" in names
    # All 4 DVR credentials accounted for — the original bug produced [].
    assert len(names) >= 4


@pytest.fixture
def canonical_env_settings(tmp_path, monkeypatch):
    d = tmp_path / "img"
    d.mkdir()
    db = tmp_path / "events.db"
    mc = tmp_path / "models"
    mc.mkdir()
    lg = tmp_path / "logs"
    lg.mkdir()
    for k in (
        "DVR_USERNAME",
        "DVR_PASSWORD",
        "DVR_LOCAL_IP",
        "DVR_LOCAL_RTSP_PORT",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DVR_IP", "192.168.1.10")
    monkeypatch.setenv("DVR_PORT", "554")
    monkeypatch.setenv("DVR_USER", "admin")
    monkeypatch.setenv("DVR_PASS", "canonical-pw")
    monkeypatch.setenv("EVENT_IMAGE_DIR", str(d))
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("LOG_DIR", str(lg))
    monkeypatch.setenv("MODEL_CACHE_DIR", str(mc))
    monkeypatch.chdir(tmp_path)
    return load_settings()


def test_env_vars_loaded_reports_canonical_names(canonical_env_settings) -> None:
    names = _collect_env_vars_loaded(canonical_env_settings)
    assert "DVR_IP" in names
    assert "DVR_PORT" in names
    assert "DVR_USER" in names
    assert "DVR_PASS" in names


# ---------------------------------------------------------------------------
# Calibration — sub-stream fps is the exit-criterion target
# ---------------------------------------------------------------------------


def test_sub_stream_fps_used_for_exit_criterion() -> None:
    """Cam 1 ingests /1 at ~15 fps even though main-stream /0 is 25 fps.

    With the pre-patch native_fps-based threshold the 14.97 measured fps
    would fail (25 × 0.8 = 20). With the post-patch sub_stream_fps
    threshold (15 × 0.8 = 12) it passes.
    """
    cam = CameraConfig(
        id=1,
        name="exterior_red",
        location="x",
        coverage="x",
        sub_path="/1",
        main_path="/0",
        codec="hevc",
        native_fps=25,
        main_stream_fps=25,
        sub_stream_fps=15,
        native_width=1280,
        native_height=720,
    )
    assert cam.advertised_sub_fps == 15

    result = CameraResult(
        name=cam.name,
        sub_path=cam.sub_path,
        main_path=cam.main_path,
        codec=cam.codec,
        advertised_fps=cam.advertised_sub_fps,
        measured_fps=14.97,
        width=cam.native_width,
        height=cam.native_height,
        capture_duration_sec=1800,
        frames_decoded=26929,
        frames_corrupted=5,
        decode_errors=26,
        hang_events=0,
    )
    assert per_camera_exit_ok(cam, result) is True


def test_sub_stream_default_from_native_fps_halved() -> None:
    """When only native_fps is provided, sub_stream_fps defaults to half."""
    cam = CameraConfig(
        id=9,
        name="legacy_only",
        location="x",
        coverage="x",
        sub_path="/1",
        main_path="/0",
        codec="hevc",
        native_fps=12,
        native_width=1280,
        native_height=720,
    )
    assert cam.main_stream_fps == 12
    assert cam.sub_stream_fps == 6
    assert cam.advertised_sub_fps == 6
