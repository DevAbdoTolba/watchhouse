---
phase: 00-environment-sanity
plan: 02-framesource-capture
subsystem: ingest
tags: [framesource, capture, green-frame, supervisor, display, mp4, signals]
requirements:
  - ENV-01
  - ENV-04
  - ING-06
dependency_graph:
  requires:
    - "00-01-scaffolding (home_cctv package, flags.assert_capture_options_active, Settings, masked logger)"
  provides:
    - "home_cctv.ingest.capture.FrameSource protocol + CaptureStats dataclass"
    - "home_cctv.ingest.capture.RtspFrameSource / Mp4FrameSource / open_frame_source factory"
    - "home_cctv.ingest.frame_quality.is_green_frame + BOTTOM_STRIP_VARIANCE_THRESHOLD"
    - "home_cctv.ingest.supervisor.ShutdownSupervisor + install_signal_handlers"
    - "home_cctv.ingest.display.DisplaySink / HeadlessJpegSink / ImshowSink"
    - "python -m home_cctv --mp4 PATH end-to-end capture loop with JPEG/thumbnail sink"
    - "tests/fixtures/make_fixtures.py deterministic MP4 generator"
  affects:
    - "Plan 00-03 measurement harness (will call open_frame_source 4× sequentially for live RTSP sweep)"
    - "Phase 1 multi-stream ingest (same FrameSource interface, one thread/process per camera)"
tech_stack:
  added:
    - "no new dependencies — uses existing opencv-python-headless==4.10.0.84 + numpy==1.26.4"
  patterns:
    - "Protocol-based FrameSource with shared _BaseCvCapture + per-source_id stats"
    - "class-level is_file_source flag for EOF-vs-transient-error gating"
    - "pre-cv2 env assertion at FrameSource.__init__ (catches reordering bugs)"
    - "idempotent ShutdownSupervisor with threading.Event + signal-handler singleton"
    - "DisplaySink factory with DISPLAY/WAYLAND_DISPLAY probe + headless fallback"
    - "HeadlessJpegSink rate-limited writes on monotonic clock (5s JPEGs, 60s thumbs)"
key_files:
  created:
    - src/home_cctv/ingest/capture.py
    - src/home_cctv/ingest/frame_quality.py
    - src/home_cctv/ingest/supervisor.py
    - src/home_cctv/ingest/display.py
    - tests/fixtures/make_fixtures.py
    - tests/test_frame_quality.py
    - tests/test_framesource_mp4.py
    - tests/test_supervisor_shutdown.py
  modified:
    - src/home_cctv/__main__.py
    - .gitignore
decisions:
  - "is_green_frame uses max per-channel std on the bottom strip (not overall std) so solid green BGR (0,255,0) correctly fails the variance gate"
  - "Dropped the first 5 frames after every open() unconditionally, independent of green-frame gate (PITFALLS §1.3 reconnect burst window)"
  - "ShutdownSupervisor.shutdown() drains the registered-sources list under a lock then releases outside the lock (avoids re-entry deadlocks if a release() callback ever tries to re-register)"
  - "Subprocess-based E2E test runs from tmp_path cwd so pydantic-settings cannot auto-load the repo's real .env; env vars become the only source of truth"
  - "Generated MP4 fixtures are .gitignored — regenerable via make_fixtures.py"
metrics:
  duration_minutes: 35
  tests_total: 45
  tests_passing: 45
  completed: "2026-04-13"
---

# Phase 0 Plan 02: FrameSource Capture Summary

One-liner: Shipped the `FrameSource` abstraction (RTSP + MP4) with a shared `CaptureStats`, a bottom-strip-variance green-frame guard, the first-5-frames-after-reconnect drop policy, an idempotent `ShutdownSupervisor` that releases every registered capture from outside the read thread in ~100 ms, a `DisplaySink` factory with a graceful headless fallback, and the `python -m home_cctv --mp4 <path>` end-to-end regression path — the last piece of scaffolding before Plan 00-03's four-camera live sweep.

## What Shipped

### FrameSource Interface (what Plan 03 imports)

```python
from home_cctv.ingest.capture import (
    CaptureStats,
    FrameSource,               # Protocol
    RtspFrameSource,           # is_file_source = False
    Mp4FrameSource,            # is_file_source = True
    open_frame_source,         # factory: rtsp:// → Rtsp, else → Mp4
)

@dataclass
class CaptureStats:
    frames_decoded: int = 0
    frames_corrupted: int = 0   # green-frame + post-open drop window
    decode_errors: int = 0      # (False, None) from cap.read()
    hang_events: int = 0        # reserved for Phase 1 watchdog
    first_frame_monotonic: float | None
    last_frame_monotonic: float | None
    @property
    def measured_fps(self) -> float: ...

class FrameSource(Protocol):
    source_id: str
    stats: CaptureStats
    is_file_source: bool
    def open(self) -> None: ...
    def read(self) -> tuple[bool, np.ndarray | None]: ...
    def release(self) -> None: ...
```

Every concrete source:
- Asserts `OPENCV_FFMPEG_CAPTURE_OPTIONS` is still the canonical string at `__init__` time (`assert_capture_options_active()`), so any later env-var tampering is caught before a single frame is decoded.
- Drops the first **5** frames after every `open()` as `frames_corrupted` (PITFALLS §1.3 reconnect burst).
- Runs each subsequent frame through `is_green_frame()` before returning — dead frames increment `frames_corrupted` and return `(False, None)`.
- Sets `cv2.CAP_PROP_BUFFERSIZE=1` at open to prevent the "4 seconds in the past" symptom.
- Retains `.stats` after `release()` so the harness can print totals on shutdown.

`RtspFrameSource._sanitized_target()` strips the password from any `rtsp://user:pass@host/...` URL before it ever reaches a log line. `Mp4FrameSource.open()` raises `FileNotFoundError` on a missing path at open time (not at construction), which matches the factory-can-be-exercised-in-tests-without-disk design.

### Green-Frame Variance Guard

`src/home_cctv/ingest/frame_quality.py`:

```python
BOTTOM_STRIP_HEIGHT = 40
BOTTOM_STRIP_VARIANCE_THRESHOLD = 3.0

def is_green_frame(frame) -> bool:
    # None / empty / wrong shape → True (dead)
    bottom = frame[-strip_h:, :, :]
    per_channel_std = bottom.reshape(-1, C).std(axis=0)
    return float(per_channel_std.max()) < 3.0
```

**Deviation — per-channel std.** The plan's sample code used `float(bottom.std()) < 3.0` (overall std of the bottom strip). That passes for all-black but **fails** for solid green `(0, 255, 0)` because the overall std across mixed-channel values is high (~120). The plan's own unit test `test_solid_green_rejected` would therefore have failed. I switched to `max(per_channel_std)` — a solid colour (any colour) has zero variance within each channel, which correctly flags it as dead; noisy content has high per-channel variance. See Deviations §1 below.

### ShutdownSupervisor (ENV-04)

`src/home_cctv/ingest/supervisor.py`:

- `ShutdownSupervisor` owns a single `threading.Event` plus a lock-protected list of registered `_Releasable` sources.
- `register(source)` → adds to list under lock.
- `request_stop()` → sets the event without touching sources (used by the main loop's polling check).
- `shutdown()` → sets the event, drains the sources list under the lock, then calls `release()` on each **outside** the lock. Idempotent: a second call is a no-op. Errors in one `release()` don't block the others (warned + swallowed).
- `install_signal_handlers(supervisor)` → binds the supervisor to a module singleton and wires SIGINT + SIGTERM to a handler that calls `request_stop()` then `shutdown()`.

**Measured shutdown latency: ~100 ms** from the moment SIGINT is delivered (see "Verification Evidence" below), far under the 2-second budget. Integration test `test_wait_returns_quickly_after_set` also confirms `wait_for_shutdown(timeout=1.0)` returns in <200 ms once the event is set.

### DisplaySink

`src/home_cctv/ingest/display.py`:

- `HeadlessJpegSink(out_dir, source_id)` writes one full-res JPEG every 5 s (monotonic) + one 320-px-wide thumbnail every 60 s under `$EVENT_IMAGE_DIR/phase0_probe/<source_id-with-colon-replaced>/`.
- `ImshowSink(source_id)` opens a `cv2.namedWindow` and pumps frames via `cv2.imshow` / `cv2.waitKey(1)`. Only used when a display is actually attached.
- `DisplaySink.open(...)` probes `DISPLAY` / `WAYLAND_DISPLAY`. Missing → logs `"--show requested but no DISPLAY/WAYLAND_DISPLAY — falling back to HeadlessJpegSink"` and returns the headless sink. `force_headless=True` bypasses the probe entirely for tests.

### `python -m home_cctv --mp4` End-to-End Capture Loop

`src/home_cctv/__main__.py` now does real work when `--mp4` is passed:

```
booted version=0.1.0 phase0=False mp4=<path> show=False dvr=rtsp://admin:***@... event_dir=<dir>
capture_opened source_id=cam0:mp4 target=<path>
[cam0:mp4] capture_started target=<path> is_file_source=True
[cam0:mp4] frame_ok size=1280x720 fps=95.99 frame_idx=25
[cam0:mp4] frame_ok size=1280x720 fps=104.55 frame_idx=50
[cam0:mp4] eof frames_decoded=70 measured_fps=113.03
shutdown releasing source_id=cam0:mp4
[cam0:mp4] done frames=70 corrupted=5 errors=1 fps=113.03
```

**Critical detail:** the EOF heuristic is gated on `fs.is_file_source`. On `Mp4FrameSource`, `(decode_errors > 0 AND frames_decoded > 0)` means "hit EOF, exit cleanly." On `RtspFrameSource` this branch is **never taken** — Cam 3 / Cam 4 produce NAL-unit-0 decode errors on startup by design, and killing the loop on them would be exactly the Phase 0 regression the NAL workaround is supposed to prevent. Plan 00-03's live sweep inherits this invariant for free.

## Verification Evidence

| Plan requirement | Verification | Result |
| --- | --- | --- |
| ENV-01 single-command start on WSL2 with `--mp4` | manual `python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4` exits 0, prints per-25-frame stats + EOF summary | PASS |
| ENV-04 Ctrl+C < 2 s clean shutdown, all captures released | `test_wait_returns_quickly_after_set` + manual SIGINT with slow fake sources → 100.7 ms observed | PASS |
| ENV-04 idempotent shutdown | `test_shutdown_is_idempotent` — second call does not double-release | PASS |
| ING-06 identical interface for RTSP + MP4 | `test_factory_dispatches_by_scheme` + the factory source asserting both subclasses share `_BaseCvCapture` | PASS |
| ING-06 `--mp4` is a true regression path for the live pipeline | `test_end_to_end_mp4_run_exits_cleanly` subprocess test asserts exit 0, JPEGs on disk, no password leak | PASS |
| PITFALLS §1.3 green-frame guard | `test_solid_green_rejected`, `test_solid_black_rejected`, `test_top_noisy_bottom_green_rejected`, `test_noisy_frame_accepted` | PASS |
| PITFALLS §1.3 drop-first-5-post-open | `test_green_frames_are_counted_as_corrupted` — 75-frame fixture + 10-green-tail → corrupted≥10 but decoded frames start after index 5 | PASS |
| PITFALLS §1.1 env-tampering caught at FrameSource init | `test_env_assertion_catches_tampering` — mutate env var → RuntimeError | PASS |
| T-00-11 password never logged | subprocess E2E test grep's `testpw` from rotating log files + stderr | PASS |
| T-00-12 `--show` on headless host degrades gracefully | manual `--show --mp4 ...` logs fallback warning and continues; `test_display_sink_no_display_falls_back` | PASS |
| `is_file_source` as class attribute, grep-verifiable | `grep -q "is_file_source" src/home_cctv/ingest/capture.py` | PASS |
| `assert_capture_options_active` wired | `grep -q "assert_capture_options_active" src/home_cctv/ingest/capture.py` | PASS |
| `BOTTOM_STRIP_VARIANCE_THRESHOLD = 3.0` grep | `grep -q "BOTTOM_STRIP_VARIANCE_THRESHOLD" src/home_cctv/ingest/frame_quality.py` | PASS |
| `install_signal_handlers` wired into main | `grep -q "install_signal_handlers" src/home_cctv/__main__.py` | PASS |
| `DisplaySink.open` wired into main | `grep -q "DisplaySink.open" src/home_cctv/__main__.py` | PASS |
| `supervisor.register` wired into main | `grep -q "supervisor.register" src/home_cctv/__main__.py` | PASS |

**Test counts:**

```
$ uv run pytest tests/ -x -q
.............................................                           [100%]
45 passed in 14.80s
```

- `tests/test_flags_builder.py` — 6 (Wave 1)
- `tests/test_config_env.py` — 6 (Wave 1)
- `tests/test_logging_mask.py` — 7 (Wave 1)
- `tests/test_frame_quality.py` — 8 (new)
- `tests/test_framesource_mp4.py` — 8 (new; env-assertion + factory + fixture integration)
- `tests/test_supervisor_shutdown.py` — 10 (new; unit supervisor + display-sink + E2E subprocess)

**CaptureStats reference values measured from the `sample_720p25.mp4` fixture** (3 s × 25 fps = 75 frames total):

```
frames_decoded:   70   # 75 - 5 post-open drop
frames_corrupted:  5   # exactly the 5 dropped
decode_errors:     1   # EOF sentinel
measured_fps:    ~115  # offline cv2 decode, no realtime cap
```

**Measured shutdown latency** (ad-hoc Python harness that installs signal handlers, registers two slow fake sources with 50 ms release cost, sends SIGINT from a daemon thread, polls until both `released` flags are True):

```
shutdown latency: 100.7 ms (target <2000)
stop_event set: True
a.released: True, b.released: True
```

## Deviations from Plan

### 1. [Rule 1 — Bug] `is_green_frame` bottom-strip variance formula

**Found during:** First run of `tests/test_frame_quality.py::test_solid_green_rejected`.

**Issue:** The plan sample code computed `float(bottom.std()) < 3.0`. For a solid green BGR frame `[0, 255, 0]`, the overall std across the mixed-channel bottom strip is ~120, not <3 — so solid green would pass as a *good* frame, which is the exact opposite of what the plan's own unit test (`assert is_green_frame(solid_green) is True`) expects.

**Fix:** Compute per-channel std (`bottom.reshape(-1, C).std(axis=0)`) and compare the **max** to the threshold. A solid colour of any hue has zero variance within every channel, so max is 0 < 3 → True (dead). Random noise has per-channel std around 73, so max ≫ 3 → False (alive).

**Files modified:** `src/home_cctv/ingest/frame_quality.py`

**Commit:** `20f4c7e`

### 2. [Rule 2 — Missing isolation] Subprocess E2E test must not load the repo's real `.env`

**Found during:** Writing `tests/test_supervisor_shutdown.py::test_end_to_end_mp4_run_exits_cleanly`.

**Issue:** The project's real `.env` (not committed; lives at repo root) uses legacy key names (`DVR_USERNAME` / `DVR_PASSWORD` / `DVR_LOCAL_IP` / `DVR_LOCAL_RTSP_PORT`) while the Settings schema requires `DVR_IP` / `DVR_USER` / `DVR_PASS` / `DVR_PORT`. If the subprocess test runs from the repo root, pydantic-settings auto-discovers that `.env`, which satisfies none of the Settings fields and makes the test fail with a validation error that has nothing to do with the capture loop.

**Fix:** Run the subprocess with `cwd=tmp_path` so pydantic-settings cannot find a `.env` to load; every required setting is provided via the `env=` dict. This is hermetic and forward-compatible regardless of what format the real `.env` evolves into. The real-`.env` mismatch is still Plan 00-03's concern, not ours.

**Files modified:** `tests/test_supervisor_shutdown.py`

**Commit:** `1fd6225`

### 3. [Documented — fixture bloat]

**Found during:** First `make_fixtures.py` run.

**Issue:** The `mp4v` FourCC produces poorly compressed video for random-noise content — the three fixtures together are ~60 MB. That would bloat every git clone for no real-world value since the fixtures are trivially regenerable.

**Fix:** Added `tests/fixtures/*.mp4` to `.gitignore`. The `make_fixtures.py` script is committed; both `test_framesource_mp4.py` and `test_supervisor_shutdown.py` auto-invoke it on first run via a session-scoped autouse fixture, so nothing is broken for a fresh checkout.

**Files modified:** `.gitignore`

**Commit:** `20f4c7e`

### 4. [Rule 3 — Missing `HOME_CCTV_ENV_FILE`]

**Found during:** Designing the subprocess test env.

**Issue/resolution:** Passed `HOME_CCTV_ENV_FILE=<nonexistent>` alongside `cwd=tmp_path` as a belt-and-braces guard. `pydantic-settings`'s `BaseSettings` may or may not honour this specific key depending on the `model_config` in the Settings class, but combined with the cwd change the env isolation is airtight.

## Auth / Human Gates

None. Plan 02 has no network, no credentials, no external services. The subprocess test uses a synthetic `testpw` password precisely so a log-leak regression would be catastrophic and automatable.

## Known Stubs

None introduced by this plan. The only "deferred" path is `--phase0`, which still logs `phase0_harness_not_yet_implemented deferred_to=plan-00-03` and returns 0 — that is the contract Plan 03 will fill, not a stub that pretends to do work.

## Threat Flags

None new. All mitigation-disposed threats from the plan's `<threat_model>` are implemented and test-covered:

| Threat ID | Mitigation | Evidence |
| --- | --- | --- |
| T-00-09 Stuck `cap.read()` on WSL2 | `stimeout=5000000` in env + `ShutdownSupervisor.shutdown()` releases from outside the read thread | SIGINT latency measurement 100 ms |
| T-00-10 Partial-decode "green" frames bleed downstream | `is_green_frame` per-channel variance + first-5-after-open drop | `test_green_frames_are_counted_as_corrupted`, `test_top_noisy_bottom_green_rejected` |
| T-00-11 Plaintext RTSP password leak | `RtspFrameSource._sanitized_target()` + Plan 01 `CredentialMaskFilter` on every handler | `test_rtsp_sanitizer_masks_password`, E2E subprocess grep |
| T-00-12 `cv2.imshow` crash on headless host | `DisplaySink.open` probes DISPLAY/WAYLAND_DISPLAY, falls back to headless | `test_display_sink_no_display_falls_back`, manual `--show` run |
| T-00-13 Orphan ffmpeg subprocess after Ctrl+C | `install_signal_handlers` wires SIGINT + SIGTERM → `supervisor.shutdown()` idempotent | `test_shutdown_is_idempotent`, manual SIGINT run |
| T-00-14 Malicious MP4 | **accepted** per plan — single-user host, operator-supplied path | N/A |

## Handoff Notes to Plan 00-03

Plan 03 should do exactly this for the four-camera live sweep:

```python
import home_cctv  # noqa: F401  — pre-cv2 env setup
from home_cctv.config.env import load_settings, validate_runtime_paths
from home_cctv.obs.logging_setup import configure_logging
from home_cctv.ingest.capture import open_frame_source
from home_cctv.ingest.display import DisplaySink
from home_cctv.ingest.supervisor import ShutdownSupervisor, install_signal_handlers

settings = load_settings()
validate_runtime_paths(settings)
supervisor = ShutdownSupervisor()
install_signal_handlers(supervisor)

for cam_id, rtsp_url, camera_name in cameras_from_yaml:
    logger = configure_logging(settings.LOG_DIR, camera_id=f"cam{cam_id}:{camera_name}")
    fs = open_frame_source(rtsp_url, camera_id=f"cam{cam_id}:{camera_name}")
    supervisor.register(fs)
    sink = DisplaySink.open(source_id=fs.source_id, out_dir=settings.EVENT_IMAGE_DIR, show=False)
    fs.open()
    # ... 30-minute capture loop ...
    # measure: fs.stats.frames_decoded, frames_corrupted, decode_errors, measured_fps
    fs.release()
    sink.close()
```

Key invariants Plan 03 should **not** violate:

1. **Never gate the live-RTSP loop on `decode_errors > 0`.** Cam 3 / Cam 4 produce NAL-unit-0 decode errors on startup by design (CONTEXT.md §"New Facts Learned This Session" #2). Use `is_file_source` to decide when "decode errors mean EOF" — it's `False` for RTSP. Phase 0 per-camera exit criteria already tolerate `frames_corrupted > 0` on Cam 3/4 (plan CONTEXT.md §"Per-camera exit criteria").
2. **Do not re-open the capture inside a tight retry loop.** Each `open()` re-enters the 5-frame drop window. Phase 1's watchdog reconnect logic will own the reopen policy; Phase 0 just measures.
3. **Always `supervisor.register(fs)` before `fs.open()`.** The cost of registering before opening is zero, and it guarantees a stuck `open()` is also interruptible via SIGINT.
4. **The repo-level `.env`** is still in the legacy `DVR_USERNAME/...` schema. Plan 03 must either rename it (handoff item from Plan 01 SUMMARY) or add a compat shim to Settings. Plan 02 does not care because it uses synthetic fixtures.

### Environment notes the Plan 03 planner should read

- `OPENCV_FFMPEG_CAPTURE_OPTIONS` is canonical and guarded. `assert_capture_options_active()` at every FrameSource init will catch drift.
- `opencv-python-headless` is pinned at **4.10.0.84** (not 4.13 — see Wave 1 SUMMARY for the numpy 1.26 reason). FFmpeg + HEVC coverage is identical for this project's usage.
- `tests/fixtures/*.mp4` are gitignored — running the full test suite on a fresh checkout automatically regenerates them on first run.

## Commits

- `20f4c7e` — Task 1: FrameSource abstraction (capture.py + frame_quality.py) + MP4 fixture generator + 16 tests
- `1fd6225` — Task 2: ShutdownSupervisor + DisplaySink + --mp4 capture loop in __main__ + 10 tests

## Self-Check: PASSED

- FOUND: `src/home_cctv/ingest/capture.py`
- FOUND: `src/home_cctv/ingest/frame_quality.py`
- FOUND: `src/home_cctv/ingest/supervisor.py`
- FOUND: `src/home_cctv/ingest/display.py`
- FOUND: `src/home_cctv/__main__.py` (modified)
- FOUND: `tests/fixtures/make_fixtures.py`
- FOUND: `tests/test_frame_quality.py`
- FOUND: `tests/test_framesource_mp4.py`
- FOUND: `tests/test_supervisor_shutdown.py`
- FOUND: `.gitignore` (modified)
- FOUND: commit `20f4c7e` (Task 1)
- FOUND: commit `1fd6225` (Task 2)
- All 45 tests pass (`uv run pytest tests/ -x -q` → `45 passed in 14.80s`)
- `grep -q "assert_capture_options_active" src/home_cctv/ingest/capture.py` → OK
- `grep -q "BOTTOM_STRIP_VARIANCE_THRESHOLD" src/home_cctv/ingest/frame_quality.py` → OK
- `grep -q "is_file_source" src/home_cctv/ingest/capture.py` → OK
- `grep -q "install_signal_handlers" src/home_cctv/__main__.py` → OK
- `grep -q "DisplaySink.open" src/home_cctv/__main__.py` → OK
- `grep -q "supervisor.register" src/home_cctv/__main__.py` → OK
- Manual `python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4` runs to EOF, exits 0, writes JPEGs to `$EVENT_IMAGE_DIR/phase0_probe/cam0_mp4/`, no password in logs
- Manual `--show` on headless WSL2 logs fallback warning, does not crash
- Measured SIGINT → full shutdown latency: **100.7 ms** (budget: 2000 ms)
