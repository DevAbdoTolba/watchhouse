"""Unit tests for home_cctv.ingest.frame_quality."""
from __future__ import annotations

import numpy as np

import home_cctv  # noqa: F401  — pre-cv2 env setup
from home_cctv.ingest.frame_quality import (
    BOTTOM_STRIP_VARIANCE_THRESHOLD,
    is_green_frame,
)


def test_threshold_value_from_pitfalls() -> None:
    assert BOTTOM_STRIP_VARIANCE_THRESHOLD == 3.0


def test_solid_green_rejected() -> None:
    f = np.zeros((720, 1280, 3), dtype=np.uint8)
    f[:, :, 1] = 255
    assert is_green_frame(f) is True


def test_solid_black_rejected() -> None:
    f = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert is_green_frame(f) is True


def test_none_rejected() -> None:
    assert is_green_frame(None) is True


def test_empty_ndarray_rejected() -> None:
    f = np.zeros((0,), dtype=np.uint8)
    assert is_green_frame(f) is True


def test_wrong_shape_rejected() -> None:
    f = np.zeros((720, 1280), dtype=np.uint8)  # 2-D, not 3-D
    assert is_green_frame(f) is True


def test_noisy_frame_accepted() -> None:
    rng = np.random.default_rng(7)
    f = rng.integers(0, 255, size=(720, 1280, 3), dtype=np.uint8)
    assert is_green_frame(f) is False


def test_top_noisy_bottom_green_rejected() -> None:
    """Partial decode: top 4/5 real, bottom strip solid green."""
    rng = np.random.default_rng(7)
    f = rng.integers(0, 255, size=(720, 1280, 3), dtype=np.uint8)
    f[-40:, :, :] = 0
    f[-40:, :, 1] = 255
    assert is_green_frame(f) is True
