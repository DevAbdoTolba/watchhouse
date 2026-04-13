"""Generate deterministic MP4 fixtures for offline FrameSource tests.

Run once: ``uv run python tests/fixtures/make_fixtures.py``

Produces:

* ``tests/fixtures/sample_720p25.mp4``       — ~3 s of noisy 1280x720 @ 25 fps
* ``tests/fixtures/sample_1080p12.mp4``      — ~3 s of noisy 1920x1080 @ 12 fps
* ``tests/fixtures/sample_with_green.mp4``   — last 10 frames are solid green

These are the only fixtures the FrameSource + supervisor test suite requires.
Regenerating them is deterministic (seeded RNG) but not reproducible across
OpenCV versions because the container / codec picks a different default bit-
rate. Tests therefore measure *shapes and counts*, never byte hashes.

Codec selection:
    We try ``mp4v`` first (always ships with OpenCV + FFmpeg) and fall back to
    ``avc1`` if the writer refuses to open. If neither works we shell out to
    the system ``ffmpeg`` binary — documented WSL2 + opencv-python-headless
    failure mode when the build lacks an encoder.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

# Importing home_cctv guarantees OPENCV_FFMPEG_CAPTURE_OPTIONS is set before
# cv2 is touched. Safe to import here because this file is a tool, not a test.
import home_cctv  # noqa: F401
import cv2  # noqa: E402

HERE = Path(__file__).parent

_FOURCCS = ("mp4v", "avc1")


def _open_writer(
    path: Path, width: int, height: int, fps: int
) -> cv2.VideoWriter:
    for tag in _FOURCCS:
        fourcc = cv2.VideoWriter_fourcc(*tag)
        writer = cv2.VideoWriter(
            str(path), fourcc, float(fps), (width, height)
        )
        if writer.isOpened():
            return writer
        writer.release()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    raise RuntimeError(
        f"cv2.VideoWriter could not open {path} with any of {_FOURCCS}"
    )


def _write_with_opencv(
    path: Path,
    width: int,
    height: int,
    fps: int,
    seconds: int,
    *,
    green_tail: int,
) -> None:
    writer = _open_writer(path, width, height, fps)
    total = fps * seconds
    rng = np.random.default_rng(seed=42)
    try:
        for i in range(total):
            if green_tail > 0 and i >= total - green_tail:
                frame = np.zeros((height, width, 3), dtype=np.uint8)
                # BGR: pure green is (0, 255, 0) which is also index 1.
                frame[:, :, 1] = 255
            else:
                frame = rng.integers(
                    0, 255, size=(height, width, 3), dtype=np.uint8
                )
            writer.write(frame)
    finally:
        writer.release()
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"cv2.VideoWriter produced an empty file at {path}")


def _write_with_ffmpeg(
    path: Path,
    width: int,
    height: int,
    fps: int,
    seconds: int,
    *,
    green_tail: int,
) -> None:
    """Fallback: pipe raw BGR24 frames to ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "cv2.VideoWriter failed and ffmpeg is not on PATH — "
            "install ffmpeg or fix the OpenCV build"
        )
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        total = fps * seconds
        rng = np.random.default_rng(seed=42)
        with tmp_path.open("wb") as fh:
            for i in range(total):
                if green_tail > 0 and i >= total - green_tail:
                    frame = np.zeros((height, width, 3), dtype=np.uint8)
                    frame[:, :, 1] = 255
                else:
                    frame = rng.integers(
                        0, 255, size=(height, width, 3), dtype=np.uint8
                    )
                fh.write(frame.tobytes())
        cmd = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            str(tmp_path),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
        subprocess.run(cmd, check=True)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced an empty file at {path}")


def write_mp4(
    path: Path,
    width: int,
    height: int,
    fps: int,
    seconds: int,
    *,
    green_tail: int = 0,
) -> None:
    try:
        _write_with_opencv(
            path, width, height, fps, seconds, green_tail=green_tail
        )
    except Exception as exc:
        print(f"cv2 writer failed ({exc!r}); falling back to ffmpeg", flush=True)
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass
        _write_with_ffmpeg(
            path, width, height, fps, seconds, green_tail=green_tail
        )
    size_kb = path.stat().st_size // 1024
    print(f"wrote {path} ({size_kb} KB)")


def main() -> int:
    HERE.mkdir(parents=True, exist_ok=True)
    write_mp4(HERE / "sample_720p25.mp4", 1280, 720, 25, 3)
    write_mp4(HERE / "sample_1080p12.mp4", 1920, 1080, 12, 3)
    write_mp4(
        HERE / "sample_with_green.mp4", 1280, 720, 25, 3, green_tail=10
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
