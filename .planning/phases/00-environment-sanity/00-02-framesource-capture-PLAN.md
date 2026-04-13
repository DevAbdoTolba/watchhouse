---
phase: 00-environment-sanity
plan: 02
type: execute
wave: 2
depends_on: ["00-01"]
files_modified:
  - src/home_cctv/ingest/capture.py
  - src/home_cctv/ingest/frame_quality.py
  - src/home_cctv/ingest/display.py
  - src/home_cctv/ingest/supervisor.py
  - src/home_cctv/__main__.py
  - tests/test_frame_quality.py
  - tests/test_framesource_mp4.py
  - tests/test_supervisor_shutdown.py
  - tests/fixtures/make_fixtures.py
autonomous: true
requirements:
  - ENV-01
  - ENV-04
  - ING-06

must_haves:
  truths:
    - "User runs `python -m home_cctv --mp4 tests/fixtures/sample.mp4` and it captures + saves JPEGs + prints steady FPS — no RTSP required"
    - "User presses Ctrl+C during capture and the process exits within 2 seconds with capture released"
    - "Same FrameSource interface serves rtsp://... and file:// MP4 paths — identical downstream code path"
    - "Green/partial frames are dropped before leaving the FrameSource (bottom-strip variance < threshold → skip)"
    - "First 5 frames after every reconnect are dropped"
    - "`--show` opens a preview window when a display is available, gracefully degrades with a logged warning otherwise"
  artifacts:
    - path: src/home_cctv/ingest/capture.py
      provides: "FrameSource abstraction (RTSP + MP4), watchdog-friendly, per-source decode stats"
      exports: ["FrameSource", "RtspFrameSource", "Mp4FrameSource", "open_frame_source", "CaptureStats"]
    - path: src/home_cctv/ingest/frame_quality.py
      provides: "Green-frame / bottom-strip variance sanity check"
      exports: ["is_green_frame", "BOTTOM_STRIP_VARIANCE_THRESHOLD"]
    - path: src/home_cctv/ingest/display.py
      provides: "cv2.imshow wrapper with headless fallback"
      exports: ["DisplaySink", "HeadlessJpegSink"]
    - path: src/home_cctv/ingest/supervisor.py
      provides: "Shared threading.Event + SIGINT handler + release-all-captures-on-exit"
      exports: ["ShutdownSupervisor", "install_signal_handlers"]
    - path: tests/fixtures/make_fixtures.py
      provides: "Script to synthesize deterministic MP4 fixtures for offline tests (no DVR required)"
  key_links:
    - from: "src/home_cctv/ingest/capture.py"
      to: "home_cctv.ingest.flags.assert_capture_options_active"
      via: "FrameSource.__init__ runtime assert"
      pattern: "assert_capture_options_active\\("
    - from: "src/home_cctv/__main__.py"
      to: "src/home_cctv/ingest/capture.py"
      via: "open_frame_source(args.mp4 or rtsp_url)"
      pattern: "open_frame_source\\("
    - from: "src/home_cctv/ingest/supervisor.py"
      to: "src/home_cctv/ingest/capture.py"
      via: "supervisor.register(frame_source); cap.release() called on shutdown"
      pattern: "register\\("
    - from: "src/home_cctv/ingest/capture.py"
      to: "src/home_cctv/ingest/frame_quality.py"
      via: "FrameSource.read() calls is_green_frame before returning"
      pattern: "is_green_frame\\("
---

<objective>
Implement the `FrameSource` abstraction so the exact same capture+save loop runs against a live RTSP URL AND against a local MP4 file (ING-06). Add the green-frame variance guard (PITFALLS #6) and the drop-first-5-frames-after-reconnect policy. Wire up a `ShutdownSupervisor` that guarantees Ctrl+C exits cleanly in under 2 seconds with all captures released (ENV-04). Add a `--show` display sink with headless fallback. Wire all of it into `python -m home_cctv --mp4 <path>` so the executor can prove end-to-end parity without a DVR.

Purpose: Phase 0's unique value is proving that one code path serves RTSP and MP4 — so Plan 03's measurement harness can call the same interface for the 4-camera live test AND for the `--mp4` regression test. This plan is the last piece of scaffolding before the measurement harness.

Output: `uv run python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4` prints `[cam0:mp4] frame_ok size=1280x720 fps=24.x ...` every read, writes JPEGs to `$EVENT_IMAGE_DIR/phase0_probe/` every 5 seconds, and exits cleanly on Ctrl+C.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/phases/00-environment-sanity/00-CONTEXT.md
@.planning/phases/00-environment-sanity/00-01-scaffolding-PLAN.md
@.planning/research/PITFALLS.md
@.planning/research/SUMMARY.md
@cameras.txt
@terminal.txt

<interfaces>
<!-- Consumed from Plan 01 -->

```python
# src/home_cctv/__init__.py — already sets OPENCV_FFMPEG_CAPTURE_OPTIONS + TF_USE_LEGACY_KERAS
# src/home_cctv/ingest/flags.py
OPENCV_FFMPEG_OPTIONS_STRING: str
def apply_capture_options() -> None: ...
def assert_capture_options_active() -> None: ...

# src/home_cctv/config/env.py
class Settings(BaseSettings):
    DVR_IP: str; DVR_PORT: int; DVR_USER: str; DVR_PASS: str
    EVENT_IMAGE_DIR: Path; DB_PATH: Path; LOG_DIR: Path; MODEL_CACHE_DIR: Path
    def masked_rtsp_base(self) -> str: ...
def load_settings() -> Settings: ...
def validate_runtime_paths(settings: Settings) -> None: ...

# src/home_cctv/obs/logging_setup.py
def configure_logging(log_dir: Path, camera_id: str | None = None) -> logging.Logger: ...
class CredentialMaskFilter(logging.Filter): ...
```
</interfaces>

<produced_by_this_plan>
The FrameSource interface, consumed by Plan 03 for the 4-camera sweep and by all of Phase 1:

```python
# src/home_cctv/ingest/capture.py
@dataclass
class CaptureStats:
    frames_decoded: int
    frames_corrupted: int   # green / variance-rejected
    decode_errors: int      # None returned from cap.read()
    hang_events: int
    first_frame_monotonic: float | None
    last_frame_monotonic: float | None
    @property
    def measured_fps(self) -> float: ...

class FrameSource(Protocol):
    is_file_source: bool   # True for Mp4FrameSource, False for RtspFrameSource
    def open(self) -> None: ...
    def read(self) -> tuple[bool, np.ndarray | None]: ...  # (ok, frame)
    def release(self) -> None: ...
    @property
    def stats(self) -> CaptureStats: ...
    @property
    def source_id(self) -> str: ...

def open_frame_source(target: str, *, camera_id: str) -> FrameSource: ...
#   - target starts with "rtsp://" → RtspFrameSource
#   - target is a filesystem path     → Mp4FrameSource
```
</produced_by_this_plan>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: FrameSource abstraction (RTSP + MP4) + green-frame sanity check + fixture generator</name>
  <files>src/home_cctv/ingest/capture.py, src/home_cctv/ingest/frame_quality.py, tests/fixtures/make_fixtures.py, tests/test_frame_quality.py, tests/test_framesource_mp4.py</files>
  <read_first>
    - src/home_cctv/ingest/flags.py (Plan 01)
    - src/home_cctv/__init__.py (Plan 01 — confirms env order)
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"FrameSource abstraction", §"Per-camera exit criteria"
    - .planning/research/PITFALLS.md §1.3 (green-frame bottom-strip variance), §1.1 (stimeout), §1.4 (DVR connection cap)
    - .planning/research/SUMMARY.md §5 pitfall #1 (stalled read), #6 (green frames)
    - cameras.txt (advertised FPS per camera — MP4 fixtures mimic these)
    - terminal.txt (flag parity)
  </read_first>
  <behavior>
    - Test: `is_green_frame` with a solid green ndarray (RGB 0,255,0) returns True
    - Test: `is_green_frame` with a solid black ndarray returns True (variance=0 in bottom strip)
    - Test: `is_green_frame` with a real noisy frame (random uint8 720p) returns False
    - Test: `BOTTOM_STRIP_VARIANCE_THRESHOLD == 3.0` (from PITFALLS §1.3)
    - Test: `open_frame_source("rtsp://admin:pw@1.2.3.4/1", camera_id="cam1")` returns an `RtspFrameSource` with `.source_id == "cam1"` (does NOT actually open the socket yet)
    - Test: `open_frame_source("/tmp/foo.mp4", camera_id="cam0:mp4")` returns an `Mp4FrameSource` — missing file raises `FileNotFoundError` on `.open()`, not on construction
    - Test: `Mp4FrameSource.is_file_source is True`; `RtspFrameSource.is_file_source is False` (classattr, not instance attr)
    - Test: `Mp4FrameSource` against a synthesized 3-second 720p25 MP4 reads ~75 frames, `stats.frames_decoded == 75`, `stats.decode_errors == 0`, `stats.measured_fps` within 20% of 25
    - Test: `Mp4FrameSource` against a synthesized MP4 whose last frame is solid green sees `stats.frames_corrupted >= 1` (the green frame was rejected)
    - Test: `FrameSource.__init__` calls `assert_capture_options_active()` — if a test mutates `os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]` the constructor raises `RuntimeError`
    - Test: After `release()`, `.stats` is still readable (not reset) so the harness can print totals after shutdown
  </behavior>
  <action>
    1. Create `src/home_cctv/ingest/frame_quality.py`:

    ```python
    """Frame-quality heuristics. PITFALLS §1.3: `discardcorrupt` can still let
    partially-decoded frames through as valid ndarrays where the bottom strip
    is uniform green or grey. YOLO would happily hallucinate detections on the
    dead band. Cheap variance check gates this before the frame leaves the
    FrameSource.
    """
    from __future__ import annotations
    import numpy as np

    # Per PITFALLS §1.3 — bottom 40 rows, variance < 3.0 → dead frame
    BOTTOM_STRIP_HEIGHT: int = 40
    BOTTOM_STRIP_VARIANCE_THRESHOLD: float = 3.0

    def is_green_frame(frame: np.ndarray) -> bool:
        """Return True if frame should be DROPPED as green/partial/dead.

        Also treats None, empty, or wrong-shape frames as dead.
        """
        if frame is None or frame.size == 0 or frame.ndim != 3:
            return True
        h = frame.shape[0]
        strip_h = min(BOTTOM_STRIP_HEIGHT, max(h // 4, 1))
        bottom = frame[-strip_h:, :, :]
        return float(bottom.std()) < BOTTOM_STRIP_VARIANCE_THRESHOLD
    ```

    2. Create `src/home_cctv/ingest/capture.py`. Key points: FrameSource asserts the env var at construction (PITFALLS §1.1), the first 5 frames after every reconnect are tagged and dropped, green frames are counted in `frames_corrupted` not `frames_decoded`, decode errors (None from cap.read()) are counted in `decode_errors`, `hang_events` is incremented when an external watchdog calls `release()` mid-read (Phase 1 consumer).

    ```python
    """FrameSource abstraction.

    One interface, two implementations:
      - RtspFrameSource  → cv2.VideoCapture(url, CAP_FFMPEG)
      - Mp4FrameSource   → cv2.VideoCapture(path, CAP_FFMPEG)

    Both paths share the same read loop, the same green-frame guard, the same
    stats object, the same release semantics. This is what makes --mp4 a true
    regression test for the live pipeline (ING-06).
    """
    from __future__ import annotations
    import logging
    import time
    from dataclasses import dataclass, field
    from pathlib import Path
    from typing import Optional, Protocol

    import cv2
    import numpy as np

    from home_cctv.ingest.flags import assert_capture_options_active
    from home_cctv.ingest.frame_quality import is_green_frame

    _LOG = logging.getLogger("home_cctv.capture")

    _DROP_FRAMES_AFTER_OPEN: int = 5  # PITFALLS §1.3 (green-bursts post-reconnect)

    @dataclass
    class CaptureStats:
        frames_decoded: int = 0
        frames_corrupted: int = 0
        decode_errors: int = 0
        hang_events: int = 0
        first_frame_monotonic: Optional[float] = None
        last_frame_monotonic: Optional[float] = None

        @property
        def measured_fps(self) -> float:
            if (
                self.frames_decoded < 2
                or self.first_frame_monotonic is None
                or self.last_frame_monotonic is None
            ):
                return 0.0
            dt = self.last_frame_monotonic - self.first_frame_monotonic
            return (self.frames_decoded - 1) / dt if dt > 0 else 0.0

    class FrameSource(Protocol):
        source_id: str
        stats: CaptureStats
        is_file_source: bool
        def open(self) -> None: ...
        def read(self) -> tuple[bool, Optional[np.ndarray]]: ...
        def release(self) -> None: ...

    class _BaseCvCapture:
        # Subclasses override. Mp4 → True; Rtsp → False. Used by downstream loops
        # to decide whether "decode_errors > 0 after some frames" means EOF (file)
        # or a transient network hiccup (live RTSP — must NOT break the loop early,
        # especially on Cam 3/4 which produce NAL-unit-0 errors early).
        is_file_source: bool = False

        def __init__(self, target: str, *, source_id: str) -> None:
            assert_capture_options_active()
            self._target = target
            self.source_id = source_id
            self._cap: Optional[cv2.VideoCapture] = None
            self._frames_since_open: int = 0
            self.stats = CaptureStats()

        def open(self) -> None:
            self._cap = cv2.VideoCapture(self._target, cv2.CAP_FFMPEG)
            if not self._cap.isOpened():
                raise RuntimeError(f"FrameSource[{self.source_id}] failed to open {self._sanitized_target()}")
            try:
                self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            self._frames_since_open = 0

        def read(self) -> tuple[bool, Optional[np.ndarray]]:
            if self._cap is None:
                return False, None
            ok, frame = self._cap.read()
            if not ok or frame is None:
                self.stats.decode_errors += 1
                return False, None

            self._frames_since_open += 1
            # Drop the first N frames post-open/post-reconnect (PITFALLS §1.3)
            if self._frames_since_open <= _DROP_FRAMES_AFTER_OPEN:
                self.stats.frames_corrupted += 1
                return False, None

            if is_green_frame(frame):
                self.stats.frames_corrupted += 1
                return False, None

            now = time.monotonic()
            if self.stats.first_frame_monotonic is None:
                self.stats.first_frame_monotonic = now
            self.stats.last_frame_monotonic = now
            self.stats.frames_decoded += 1
            return True, frame

        def release(self) -> None:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None

        def _sanitized_target(self) -> str:
            return self._target  # subclasses override for rtsp

    class RtspFrameSource(_BaseCvCapture):
        def _sanitized_target(self) -> str:
            # Mask any password in the URL for logging
            import re
            return re.sub(r"(://[^:/@\s]+):[^@\s]+@", r"\1:***@", self._target)

    class Mp4FrameSource(_BaseCvCapture):
        is_file_source = True

        def open(self) -> None:
            p = Path(self._target)
            if not p.exists():
                raise FileNotFoundError(f"Mp4FrameSource: {p} does not exist")
            super().open()

    def open_frame_source(target: str, *, camera_id: str) -> FrameSource:
        """Factory. rtsp:// → RtspFrameSource, everything else → Mp4FrameSource."""
        if target.startswith(("rtsp://", "rtsps://")):
            return RtspFrameSource(target, source_id=camera_id)
        return Mp4FrameSource(target, source_id=camera_id)
    ```

    3. Create `tests/fixtures/make_fixtures.py` — a script that synthesizes deterministic MP4 fixtures offline, so Task 2 and Plan 03's `--mp4` regression path work without a DVR:

    ```python
    """Generate deterministic MP4 fixtures for offline FrameSource tests.

    Run once: `uv run python tests/fixtures/make_fixtures.py`
    Produces:
      tests/fixtures/sample_720p25.mp4       — 3 s of noisy 1280x720 @ 25fps
      tests/fixtures/sample_1080p12.mp4      — 3 s of noisy 1920x1080 @ 12fps
      tests/fixtures/sample_with_green.mp4   — last 10 frames are solid green (bottom strip)
    """
    from __future__ import annotations
    import sys
    from pathlib import Path
    import numpy as np
    import cv2

    HERE = Path(__file__).parent

    def write_mp4(path: Path, width: int, height: int, fps: int, seconds: int, *, green_tail: int = 0) -> None:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, float(fps), (width, height))
        total = fps * seconds
        rng = np.random.default_rng(seed=42)
        for i in range(total):
            if i >= total - green_tail and green_tail > 0:
                frame = np.zeros((height, width, 3), dtype=np.uint8)
                frame[:, :, 1] = 255  # pure green — bottom strip variance will be 0
            else:
                frame = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)
            writer.write(frame)
        writer.release()
        assert path.exists() and path.stat().st_size > 0, f"failed to write {path}"
        print(f"wrote {path} ({path.stat().st_size // 1024} KB)")

    def main() -> int:
        HERE.mkdir(parents=True, exist_ok=True)
        write_mp4(HERE / "sample_720p25.mp4", 1280, 720, 25, 3)
        write_mp4(HERE / "sample_1080p12.mp4", 1920, 1080, 12, 3)
        write_mp4(HERE / "sample_with_green.mp4", 1280, 720, 25, 3, green_tail=10)
        return 0

    if __name__ == "__main__":
        sys.exit(main())
    ```

    4. Create `tests/test_frame_quality.py`:

    ```python
    import numpy as np
    from home_cctv.ingest.frame_quality import is_green_frame, BOTTOM_STRIP_VARIANCE_THRESHOLD

    def test_threshold_value_from_pitfalls():
        assert BOTTOM_STRIP_VARIANCE_THRESHOLD == 3.0

    def test_solid_green_rejected():
        f = np.zeros((720, 1280, 3), dtype=np.uint8); f[:, :, 1] = 255
        assert is_green_frame(f) is True

    def test_solid_black_rejected():
        f = np.zeros((720, 1280, 3), dtype=np.uint8)
        assert is_green_frame(f) is True

    def test_none_rejected():
        assert is_green_frame(None) is True

    def test_noisy_frame_accepted():
        rng = np.random.default_rng(7)
        f = rng.integers(0, 255, size=(720, 1280, 3), dtype=np.uint8)
        assert is_green_frame(f) is False

    def test_top_half_noisy_bottom_green_rejected():
        # Simulates partial decode: top 4/5 real, bottom strip green
        rng = np.random.default_rng(7)
        f = rng.integers(0, 255, size=(720, 1280, 3), dtype=np.uint8)
        f[-40:, :, :] = 0
        f[-40:, :, 1] = 255
        assert is_green_frame(f) is True
    ```

    5. Create `tests/test_framesource_mp4.py`. Use `pytest` fixture that invokes `make_fixtures.main()` if the fixtures don't exist yet:

    ```python
    import os
    import subprocess
    import sys
    from pathlib import Path
    import pytest

    import home_cctv  # ensures env setup
    from home_cctv.ingest.capture import (
        open_frame_source, Mp4FrameSource, RtspFrameSource,
    )

    FIXTURES = Path(__file__).parent / "fixtures"

    @pytest.fixture(scope="session", autouse=True)
    def _materialize_fixtures():
        if not (FIXTURES / "sample_720p25.mp4").exists():
            subprocess.run(
                [sys.executable, str(FIXTURES / "make_fixtures.py")],
                check=True,
            )

    def test_factory_dispatches_by_scheme():
        fs_rtsp = open_frame_source("rtsp://admin:pw@1.2.3.4:554/1", camera_id="cam1")
        fs_mp4 = open_frame_source(str(FIXTURES / "sample_720p25.mp4"), camera_id="cam0:mp4")
        assert isinstance(fs_rtsp, RtspFrameSource)
        assert isinstance(fs_mp4, Mp4FrameSource)
        assert fs_rtsp.source_id == "cam1"
        assert fs_mp4.source_id == "cam0:mp4"

    def test_rtsp_sanitizer_masks_password():
        fs = open_frame_source("rtsp://admin:SuperSecret@1.2.3.4:554/1", camera_id="cam1")
        assert "SuperSecret" not in fs._sanitized_target()
        assert "admin:***@" in fs._sanitized_target()

    def test_missing_mp4_raises_on_open(tmp_path):
        fs = open_frame_source(str(tmp_path / "nope.mp4"), camera_id="cam0:mp4")
        with pytest.raises(FileNotFoundError):
            fs.open()

    def test_mp4_read_loop_720p25():
        fs = open_frame_source(str(FIXTURES / "sample_720p25.mp4"), camera_id="cam0:mp4")
        fs.open()
        try:
            count_ok = 0
            while True:
                ok, frame = fs.read()
                if ok:
                    count_ok += 1
                    assert frame.shape[1] == 1280 and frame.shape[0] == 720
                if not ok and fs.stats.decode_errors > 0:
                    break
        finally:
            fs.release()
        # 3 s @ 25 fps = 75 frames, minus the 5 dropped post-open
        assert count_ok >= 65
        assert fs.stats.frames_decoded == count_ok
        # upper bound left unbounded — offline decode often exceeds realtime fps
        assert fs.stats.measured_fps > 15.0, f"mp4 decode too slow: {fs.stats.measured_fps}"

    def test_green_frames_are_counted_as_corrupted():
        fs = open_frame_source(str(FIXTURES / "sample_with_green.mp4"), camera_id="cam0:mp4")
        fs.open()
        try:
            while True:
                ok, _ = fs.read()
                if not ok and fs.stats.decode_errors > 0:
                    break
        finally:
            fs.release()
        # 5 drop-after-open + 10 synthetic green tail = 15 minimum corrupted
        assert fs.stats.frames_corrupted >= 10

    def test_env_assertion_catches_tampering():
        saved = os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]
        try:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp"
            with pytest.raises(RuntimeError, match="OPENCV_FFMPEG_CAPTURE_OPTIONS mismatch"):
                open_frame_source("rtsp://x/1", camera_id="cam1")
        finally:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = saved
    ```

    6. Run `uv run python tests/fixtures/make_fixtures.py` and then `uv run pytest tests/test_frame_quality.py tests/test_framesource_mp4.py -x -q`. All must pass.
  </action>
  <verify>
    <automated>uv run python tests/fixtures/make_fixtures.py &amp;&amp; uv run pytest tests/test_frame_quality.py tests/test_framesource_mp4.py -x -q</automated>
  </verify>
  <done>
    - All frame-quality and framesource-mp4 tests pass
    - `uv run python -c "from home_cctv.ingest.capture import open_frame_source; fs = open_frame_source('tests/fixtures/sample_720p25.mp4', camera_id='cam0:mp4'); fs.open(); ok, f = fs.read(); print(ok, getattr(f, 'shape', None)); fs.release()"` prints a shape like `(720, 1280, 3)` (after the post-open drop, the first returned frame is valid)
    - `grep -q "assert_capture_options_active" src/home_cctv/ingest/capture.py` exits 0
    - `grep -q "BOTTOM_STRIP_VARIANCE_THRESHOLD = 3.0" src/home_cctv/ingest/frame_quality.py` exits 0
    - `grep -q "is_file_source" src/home_cctv/ingest/capture.py` exits 0 (live RTSP EOF heuristic gate)
    - Fixture files `tests/fixtures/sample_720p25.mp4`, `sample_1080p12.mp4`, `sample_with_green.mp4` exist
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: ShutdownSupervisor + --show display sink + --mp4 end-to-end capture loop</name>
  <files>src/home_cctv/ingest/supervisor.py, src/home_cctv/ingest/display.py, src/home_cctv/__main__.py, tests/test_supervisor_shutdown.py</files>
  <read_first>
    - src/home_cctv/ingest/capture.py (Task 1)
    - src/home_cctv/ingest/frame_quality.py (Task 1)
    - src/home_cctv/__main__.py (Plan 01 Task 2 — this task extends it)
    - src/home_cctv/obs/logging_setup.py (Plan 01 Task 2)
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"Supervisor / Shutdown Behavior", §"Display Strategy"
    - .planning/research/PITFALLS.md §1.1 (release-from-another-thread), §1.5 if present
    - cameras.txt — advertised fps per camera (for log formatting)
  </read_first>
  <behavior>
    - Test: `ShutdownSupervisor.stop_event.is_set()` is False at construction
    - Test: Sending SIGINT to the process sets `stop_event` AND calls `.release()` on every registered FrameSource
    - Test: Calling `supervisor.shutdown()` is idempotent (calling twice is safe, `.release()` is called at most once per source)
    - Test: The supervisor returns from `wait_for_shutdown(timeout=0.1)` within 0.2 seconds when `stop_event` is set (no long blocking)
    - Test: `HeadlessJpegSink` writes a JPEG every 5 seconds of monotonic time (not every frame)
    - Test: `HeadlessJpegSink` writes a thumbnail every 60 seconds of monotonic time
    - Test: `DisplaySink.open()` with `force_headless=True` returns `HeadlessJpegSink` without ever calling `cv2.namedWindow`
    - Test: Ctrl+C simulation via subprocess — `python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4` runs to completion (EOF) within the 3-second fixture length; process exits cleanly with code 0
  </behavior>
  <action>
    1. Create `src/home_cctv/ingest/supervisor.py`:

    ```python
    """Shutdown supervisor. One threading.Event shared by the capture loop and
    the signal handler. Ctrl+C flips the event; the loop checks it every read
    and exits; the supervisor also force-releases every registered FrameSource
    to unblock stuck reads (PITFALLS §1.1 — releasing from another thread is
    the only way to unblock a hung cap.read()).
    """
    from __future__ import annotations
    import logging
    import signal
    import threading
    import time
    from typing import Protocol

    _LOG = logging.getLogger("home_cctv.supervisor")

    class _Releasable(Protocol):
        source_id: str
        def release(self) -> None: ...

    class ShutdownSupervisor:
        def __init__(self) -> None:
            self.stop_event = threading.Event()
            self._sources: list[_Releasable] = []
            self._lock = threading.Lock()
            self._t0 = time.monotonic()
            self._shutdown_called = False

        def register(self, source: _Releasable) -> None:
            with self._lock:
                self._sources.append(source)

        def request_stop(self) -> None:
            self.stop_event.set()

        def shutdown(self) -> None:
            """Force-release every registered FrameSource. Idempotent."""
            with self._lock:
                if self._shutdown_called:
                    return
                self._shutdown_called = True
                self.stop_event.set()
                for s in self._sources:
                    try:
                        _LOG.info("shutdown releasing source_id=%s", s.source_id)
                        s.release()
                    except Exception as exc:
                        _LOG.warning("shutdown release_failed source_id=%s err=%r", s.source_id, exc)
                self._sources.clear()

        def wait_for_shutdown(self, timeout: float) -> bool:
            return self.stop_event.wait(timeout=timeout)

    _SINGLETON: ShutdownSupervisor | None = None

    def install_signal_handlers(supervisor: ShutdownSupervisor) -> None:
        """Install SIGINT + SIGTERM handlers that invoke supervisor.shutdown()."""
        global _SINGLETON
        _SINGLETON = supervisor

        def _handler(signum, _frame):
            _LOG.info("signal received signum=%s — initiating shutdown", signum)
            if _SINGLETON is not None:
                _SINGLETON.request_stop()
                _SINGLETON.shutdown()

        signal.signal(signal.SIGINT, _handler)
        try:
            signal.signal(signal.SIGTERM, _handler)
        except Exception:
            pass  # SIGTERM may not exist on Windows hosts
    ```

    2. Create `src/home_cctv/ingest/display.py`:

    ```python
    """Display sinks. `--show` opens a cv2.imshow window when a display is
    available; otherwise (and by default), writes periodic JPEGs + thumbnails
    to disk. The `--show` path is the MVP of the future dashboard Live View
    button (CONTEXT.md §"Display Strategy").
    """
    from __future__ import annotations
    import logging
    import os
    import time
    from pathlib import Path
    from typing import Protocol

    import cv2
    import numpy as np

    _LOG = logging.getLogger("home_cctv.display")

    _JPEG_EVERY_SEC: float = 5.0
    _THUMB_EVERY_SEC: float = 60.0

    class Sink(Protocol):
        def write(self, frame: np.ndarray) -> None: ...
        def close(self) -> None: ...

    class HeadlessJpegSink:
        """Writes a JPEG every 5 s + thumbnail every 60 s per source."""
        def __init__(self, out_dir: Path, source_id: str) -> None:
            self._dir = Path(out_dir) / "phase0_probe" / source_id.replace(":", "_")
            self._dir.mkdir(parents=True, exist_ok=True)
            self._last_jpeg = 0.0
            self._last_thumb = 0.0
            self._source_id = source_id

        def write(self, frame: np.ndarray) -> None:
            now = time.monotonic()
            if now - self._last_jpeg >= _JPEG_EVERY_SEC:
                path = self._dir / f"{int(now)}.jpg"
                cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                self._last_jpeg = now
                _LOG.debug("jpeg_written source=%s path=%s", self._source_id, path)
            if now - self._last_thumb >= _THUMB_EVERY_SEC:
                h, w = frame.shape[:2]
                thumb_w = 320
                thumb_h = max(1, int(h * (thumb_w / w)))
                thumb = cv2.resize(frame, (thumb_w, thumb_h))
                path = self._dir / f"thumb_{int(now)}.jpg"
                cv2.imwrite(str(path), thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
                self._last_thumb = now

        def close(self) -> None:
            pass

    class ImshowSink:
        def __init__(self, source_id: str) -> None:
            self._title = f"home_cctv: {source_id}"
            cv2.namedWindow(self._title, cv2.WINDOW_NORMAL)

        def write(self, frame: np.ndarray) -> None:
            cv2.imshow(self._title, frame)
            cv2.waitKey(1)

        def close(self) -> None:
            try:
                cv2.destroyWindow(self._title)
            except Exception:
                pass

    class DisplaySink:
        """Factory — picks ImshowSink or HeadlessJpegSink at runtime."""
        @staticmethod
        def open(*, source_id: str, out_dir: Path, show: bool, force_headless: bool = False) -> Sink:
            if force_headless or not show:
                return HeadlessJpegSink(out_dir, source_id)
            # Probe for display availability
            has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            if not has_display:
                _LOG.warning("--show requested but no DISPLAY/WAYLAND_DISPLAY — falling back to headless JPEG sink")
                return HeadlessJpegSink(out_dir, source_id)
            try:
                return ImshowSink(source_id)
            except Exception as exc:
                _LOG.warning("ImshowSink failed err=%r — falling back to headless", exc)
                return HeadlessJpegSink(out_dir, source_id)
    ```

    3. Extend `src/home_cctv/__main__.py` to actually run a capture loop when `--mp4` is passed. Replace `main()` with:

    ```python
    def main(argv: list[str] | None = None) -> int:
        args = build_parser().parse_args(argv)

        from home_cctv.config.env import load_settings, validate_runtime_paths
        from home_cctv.obs.logging_setup import configure_logging
        from home_cctv.ingest.capture import open_frame_source
        from home_cctv.ingest.display import DisplaySink
        from home_cctv.ingest.supervisor import ShutdownSupervisor, install_signal_handlers

        try:
            settings = load_settings()
            validate_runtime_paths(settings)
        except RuntimeError as exc:
            print(f"STARTUP ERROR: {exc}", file=sys.stderr)
            return 2

        logger = configure_logging(settings.LOG_DIR)
        logger.info(
            "booted version=%s phase0=%s mp4=%s show=%s dvr=%s",
            home_cctv.__version__, args.phase0, args.mp4, args.show,
            settings.masked_rtsp_base(),
        )

        supervisor = ShutdownSupervisor()
        install_signal_handlers(supervisor)

        if args.phase0:
            # Full Phase 0 harness ships in Plan 03
            logger.info("--phase0 not yet implemented in Plan 02 — see Plan 03")
            return 0

        if args.mp4 is None:
            logger.info("no --mp4 and no --phase0 — nothing to do, exiting")
            return 0

        source_id = "cam0:mp4"
        fs = open_frame_source(args.mp4, camera_id=source_id)
        supervisor.register(fs)
        sink = DisplaySink.open(
            source_id=source_id, out_dir=settings.EVENT_IMAGE_DIR, show=args.show,
        )

        try:
            fs.open()
            logger.info("[%s] capture_started target=%s", source_id, args.mp4)
            while not supervisor.stop_event.is_set():
                ok, frame = fs.read()
                if not ok:
                    # EOF heuristic applies ONLY to file sources. Live RTSP must
                    # never exit on a single early decode error — Cam 3/4 produce
                    # NAL-unit-0 errors early by design (PITFALLS §1.3 / CONTEXT.md
                    # §"New Facts Learned This Session" #2).
                    if fs.is_file_source and fs.stats.decode_errors > 0 and fs.stats.frames_decoded > 0:
                        # EOF on an MP4 — clean exit
                        logger.info("[%s] eof frames_decoded=%d measured_fps=%.2f",
                                    source_id, fs.stats.frames_decoded, fs.stats.measured_fps)
                        break
                    continue
                sink.write(frame)
                if fs.stats.frames_decoded % 25 == 0:
                    logger.info("[%s] frame_ok size=%dx%d fps=%.2f frame_idx=%d",
                                source_id, frame.shape[1], frame.shape[0],
                                fs.stats.measured_fps, fs.stats.frames_decoded)
        except Exception as exc:
            logger.error("[%s] capture_error err=%r", source_id, exc)
            return 1
        finally:
            sink.close()
            supervisor.shutdown()

        logger.info("[%s] done frames=%d corrupted=%d errors=%d fps=%.2f",
                    source_id, fs.stats.frames_decoded, fs.stats.frames_corrupted,
                    fs.stats.decode_errors, fs.stats.measured_fps)
        return 0
    ```

    4. Create `tests/test_supervisor_shutdown.py`:

    ```python
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    import home_cctv  # noqa
    from home_cctv.ingest.supervisor import ShutdownSupervisor

    FIXTURES = Path(__file__).parent / "fixtures"
    REPO = Path(__file__).parent.parent

    class _FakeSource:
        def __init__(self, sid: str):
            self.source_id = sid
            self.released = 0
        def release(self) -> None:
            self.released += 1

    def test_stop_event_default_unset():
        sup = ShutdownSupervisor()
        assert sup.stop_event.is_set() is False

    def test_shutdown_releases_all_sources():
        sup = ShutdownSupervisor()
        a, b = _FakeSource("cam1"), _FakeSource("cam2")
        sup.register(a); sup.register(b)
        sup.shutdown()
        assert sup.stop_event.is_set() is True
        assert a.released == 1
        assert b.released == 1

    def test_shutdown_is_idempotent():
        sup = ShutdownSupervisor()
        a = _FakeSource("cam1"); sup.register(a)
        sup.shutdown()
        sup.shutdown()
        assert a.released == 1

    def test_wait_returns_quickly_after_set():
        sup = ShutdownSupervisor()
        sup.request_stop()
        t0 = time.monotonic()
        got = sup.wait_for_shutdown(timeout=1.0)
        assert got is True
        assert time.monotonic() - t0 < 0.2

    def test_end_to_end_mp4_run_exits_cleanly(tmp_path, monkeypatch):
        """Subprocess-level: python -m home_cctv --mp4 fixtures/sample_720p25.mp4 runs to EOF and exits 0."""
        if not (FIXTURES / "sample_720p25.mp4").exists():
            subprocess.run([sys.executable, str(FIXTURES / "make_fixtures.py")], check=True)

        env = os.environ.copy()
        env["DVR_IP"] = "192.168.1.10"
        env["DVR_PORT"] = "554"
        env["DVR_USER"] = "admin"
        env["DVR_PASS"] = "testpw"
        env["EVENT_IMAGE_DIR"] = str(tmp_path / "img")
        env["DB_PATH"] = str(tmp_path / "cctv.db")
        env["LOG_DIR"] = str(tmp_path / "logs")
        env["MODEL_CACHE_DIR"] = str(tmp_path / "models")

        result = subprocess.run(
            [sys.executable, "-m", "home_cctv", "--mp4", str(FIXTURES / "sample_720p25.mp4")],
            env=env, cwd=str(REPO), capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # At least one JPEG should have landed
        jpegs = list((tmp_path / "img" / "phase0_probe" / "cam0_mp4").glob("*.jpg"))
        assert len(jpegs) >= 1, f"no JPEGs in {tmp_path}: {result.stderr}"
        # Password must not appear in logs
        log_content = ""
        for f in (tmp_path / "logs").glob("*.log"):
            log_content += f.read_text(encoding="utf-8", errors="ignore")
        assert "testpw" not in log_content
    ```

    5. Run `uv run pytest tests/ -x -q`. All tests pass.

    6. Run the `--mp4` happy path by hand one time: `uv run python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4` and confirm it prints per-camera `frame_ok` lines, writes JPEGs under `$EVENT_IMAGE_DIR/phase0_probe/cam0_mp4/`, and exits cleanly at EOF.
  </action>
  <verify>
    <automated>uv run pytest tests/ -x -q</automated>
  </verify>
  <done>
    - `uv run pytest tests/test_supervisor_shutdown.py -x -q` passes
    - `uv run python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4` runs to EOF and exits 0
    - JPEG files appear under `$EVENT_IMAGE_DIR/phase0_probe/cam0_mp4/`
    - No plaintext password appears in logs (integration-test enforced)
    - `grep -q "install_signal_handlers" src/home_cctv/__main__.py` exits 0
    - `grep -q "DisplaySink.open" src/home_cctv/__main__.py` exits 0
    - `grep -q "supervisor.register" src/home_cctv/__main__.py` exits 0
  </done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| filesystem → process | MP4 files fed via `--mp4` may be malformed or huge |
| RTSP URL → FFmpeg decoder | DVR may serve corrupt streams (H.265 NAL-unit-0 anomaly on Cam 3/4) |
| OS signal → process | SIGINT/SIGTERM must not leave file descriptors leaked |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-00-09 | Denial of Service | Stuck `cap.read()` on WSL2 (PITFALLS §1.1) | mitigate | `stimeout=5000000` (5s) in env options + `ShutdownSupervisor.shutdown()` calls `fs.release()` from outside the read thread — the only way to unblock a hung read |
| T-00-10 | Tampering | Partial-decode "green" frames bleeding into downstream phases | mitigate | `is_green_frame` bottom-strip variance gate inside `FrameSource.read`; first 5 frames post-open always dropped (PITFALLS §1.3) |
| T-00-11 | Information Disclosure | RTSP URL with plaintext password in exceptions/logs | mitigate | `RtspFrameSource._sanitized_target()` masks password before any log line; `CredentialMaskFilter` (Plan 01) as belt-and-braces |
| T-00-12 | Denial of Service | `--show` on a headless host crashes `cv2.imshow` | mitigate | `DisplaySink.open()` probes `DISPLAY`/`WAYLAND_DISPLAY`, falls back to `HeadlessJpegSink` with a logged warning |
| T-00-13 | Denial of Service | Orphan ffmpeg subprocess after Ctrl+C | mitigate | `install_signal_handlers()` wires both SIGINT and SIGTERM → `supervisor.shutdown()` → `fs.release()` on every registered source; idempotent |
| T-00-14 | Elevation of Privilege | Malicious MP4 triggers libavformat vulnerability | accept | `--mp4` is operator-supplied on a single-user host; threat model accepts this |
</threat_model>

<verification>
- All tests in `tests/test_frame_quality.py`, `tests/test_framesource_mp4.py`, `tests/test_supervisor_shutdown.py` pass
- `uv run python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4` runs to EOF, exits 0, leaves JPEGs on disk
- `uv run python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4 --show` either opens a window or logs the graceful fallback (never crashes)
- Password `testpw` from the integration test never appears in any log file
- `python -c "from home_cctv.ingest.capture import FrameSource, RtspFrameSource, Mp4FrameSource, open_frame_source; print('ok')"` exits 0
</verification>

<success_criteria>
Plan 02 is done when:
1. `FrameSource` abstraction exists with both RTSP and MP4 implementations sharing identical read/release semantics and a shared `CaptureStats` (ING-06 satisfied)
2. Green frames and first-5-post-open frames are counted as `frames_corrupted`, not `frames_decoded`
3. Ctrl+C (SIGINT) and SIGTERM both trigger `supervisor.shutdown()` which releases every registered source exactly once (ENV-04 satisfied)
4. `--show` works with `DISPLAY`/`WAYLAND_DISPLAY` set and degrades gracefully otherwise
5. `--mp4` end-to-end regression path prints per-frame stats, saves JPEGs every 5 s, exits 0 on EOF
6. No plaintext RTSP passwords reach any log handler or any exception traceback
7. All unit + integration tests pass
</success_criteria>

<output>
After completion, create `.planning/phases/00-environment-sanity/00-02-SUMMARY.md` with:
- FrameSource interface signature (what Plan 03 will import)
- Confirmation of --mp4 end-to-end pass
- CaptureStats reference values measured from the fixture MP4
- Supervisor shutdown latency measured in the integration test
- Handoff notes to Plan 03 (the 4-camera harness will call `open_frame_source` 4× sequentially with the RTSP URLs from `cameras.yaml`)
</output>
