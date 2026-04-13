"""Tests for phase0.model_bundle verify + persist round-trip."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from home_cctv.phase0 import model_bundle
from home_cctv.phase0.model_bundle import (
    WEIGHTS_LOCK_PATH,
    persist_weights_lock,
    verify_model_bundle,
)


def test_verify_empty_cache_no_raise(tmp_path: Path) -> None:
    v = verify_model_bundle(tmp_path)
    assert v["yolo26n_pt_cached"] is False
    assert v["yolov8n_pt_cached"] is False
    assert v["easyocr_english_cached"] is False
    # B1: cold_start_ms always present with the three canonical keys.
    assert set(v["cold_start_ms"].keys()) == {
        "yolo_openvino",
        "deepface",
        "easyocr",
    }


def test_verify_after_touching_yolo(tmp_path: Path) -> None:
    # Pre-populate an unverified lock so the "accept any non-zero size"
    # path is exercised.
    target = tmp_path / "yolo26n.pt"
    target.write_bytes(b"\x00" * 100_000)
    v = verify_model_bundle(tmp_path)
    assert v["yolo26n_pt_cached"] is True
    assert v["total_weights_size_mb"] >= 0.09


def test_persist_weights_lock_round_trip(tmp_path: Path, monkeypatch) -> None:
    fake_lock = tmp_path / "weights.lock.json"
    monkeypatch.setattr(model_bundle, "WEIGHTS_LOCK_PATH", fake_lock)
    bundle = {
        "weights": {
            "yolo26n.pt": {"size_bytes": 6_144_000},
            "yolov8n.pt": {"size_bytes": 6_200_000},
            "yolo26n_openvino_model": {"size_bytes": 12_000_000},
            "deepface_arcface": {"size_bytes": 130_000_000},
            "deepface_retinaface": {"size_bytes": 60_000_000},
            "easyocr_english": {"size_bytes": 100_000_000},
        },
        "cold_start_ms": {
            "yolo_openvino": 600,
            "deepface": 8400,
            "easyocr": 5100,
        },
    }
    persist_weights_lock(bundle)
    import json

    data = json.loads(fake_lock.read_text())
    assert data["weights"]["yolo26n.pt"]["verified"] is True
    assert data["weights"]["yolo26n.pt"]["size_bytes"] == 6_144_000
    assert data["weights"]["yolo26n_openvino_model"]["is_directory"] is True
    assert data["cold_start_ms"] == {
        "yolo_openvino": 600,
        "deepface": 8400,
        "easyocr": 5100,
    }


def test_verify_respects_size_mismatch(tmp_path: Path, monkeypatch) -> None:
    fake_lock = tmp_path / "weights.lock.json"
    monkeypatch.setattr(model_bundle, "WEIGHTS_LOCK_PATH", fake_lock)
    # Write a lock claiming yolo26n.pt should be 10 MB.
    persist_weights_lock(
        {
            "weights": {"yolo26n.pt": {"size_bytes": 10_000_000}},
            "cold_start_ms": {"yolo_openvino": 1, "deepface": 1, "easyocr": 1},
        }
    )
    # Place a much smaller file — must report not cached.
    (tmp_path / "yolo26n.pt").write_bytes(b"\x00" * 100)
    v = verify_model_bundle(tmp_path)
    assert v["yolo26n_pt_cached"] is False


def test_weights_lock_path_is_absolute_to_repo_root() -> None:
    # W3: parents[3] from src/home_cctv/phase0/model_bundle.py reaches repo
    # root regardless of cwd.
    assert WEIGHTS_LOCK_PATH.is_absolute()
    assert WEIGHTS_LOCK_PATH.name == "weights.lock.json"
    assert WEIGHTS_LOCK_PATH.parent.name == "home_cameras" or (
        WEIGHTS_LOCK_PATH.parent / "pyproject.toml"
    ).exists()


@pytest.mark.slow
def test_download_bundle_integration(tmp_path: Path, monkeypatch) -> None:
    """Exercises the real download path. Excluded from default run."""
    fake_lock = tmp_path / "weights.lock.json"
    monkeypatch.setattr(model_bundle, "WEIGHTS_LOCK_PATH", fake_lock)
    bundle = model_bundle.download_model_bundle(tmp_path)
    assert "cold_start_ms" in bundle
    v = verify_model_bundle(tmp_path)
    assert v["cold_start_ms"]["yolo_openvino"] > 0
