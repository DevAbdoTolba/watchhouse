---
phase: 01-multi-stream-ingest-reconnect
plan: 03
subsystem: ingest
tags: [threading, semaphore, ttl-cache, rtsp, supervisor, lifecycle, path-traversal-guard, start-degraded, mp4-mode]

# Dependency graph
requires:
  - phase: 01-multi-stream-ingest-reconnect
    plan: 01
    provides: "StreamReader, FrameQueue(maxlen=2), CameraHeartbeat (4-state structured 30-s emitter), CaptureStats"
  - phase: 01-multi-stream-ingest-reconnect
    plan: 02
    provides: "JitteredBackoff, ReadWatchdog (cross-thread release), StreamReader reconnect loop, FrameSource.is_open Protocol extension"
  - phase: 00-environment-sanity
    provides: "ShutdownSupervisor (SIGINT drain), CredentialMaskFilter, assert_capture_options_active, Settings + cameras.yaml loader, open_frame_source factory"
provides:
  - "home_cctv.ingest.main_stream_grabber.MainStreamGrabber — DVR-wide Semaphore(1) + 2-s TTL cache + synchronous grab_main_frame(camera_id: int) -> bytes | None with double-checked cache locking"
  - "home_cctv.ingest.main_stream_grabber module constants MAIN_STREAM_CACHE_TTL_S=2.0, MAIN_STREAM_MAX_READ_ATTEMPTS=3, MAIN_STREAM_JPEG_QUALITY=85"
  - "home_cctv.ingest.main_stream_grabber.CacheEntry dataclass"
  - "home_cctv.ingest.ingest_supervisor.IngestSupervisor — composes 4 readers + 4 watchdogs + 4 heartbeats + 1 grabber under one ShutdownSupervisor"
  - "home_cctv.ingest.ingest_supervisor.CameraRuntime dataclass"
  - "home_cctv.ingest.ingest_supervisor constants INITIAL_PROBE_ATTEMPTS=3, INITIAL_PROBE_TIMEOUT_S=5.0, INITIAL_PROBE_OVERALL_DEADLINE_S=15.0, SHUTDOWN_GRACE_S=2.0"
  - "home_cctv.__main__ --live flag wires cameras.yaml through IngestSupervisor"
  - "home_cctv.__main__ --mp4-mode PATH dev override — every virtual camera reads from the MP4, RTSP probe short-circuited"
affects: [02-detection-tracking, 03-trigger-catch, 04-hardening-operations]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "double-checked cache locking: fast-path check → acquire semaphore → re-check under the cache lock → release semaphore early on hit"
    - "DVR-wide global Semaphore(1): a single shared serializer across all callers + all cameras"
    - "interruptible semaphore acquire: while not stop_event.is_set(): semaphore.acquire(timeout=0.5)"
    - "side-thread open timeout: wrap cv2.VideoCapture in threading.Thread, join with timeout, cross-thread release on hang (G-01 pattern)"
    - "parallel one-shot probe via ThreadPoolExecutor(max_workers=4) with overall deadline — transient threads, not persistent workers"
    - "path-traversal guard as pre-condition: validate cameras.yaml paths BEFORE any thread is spawned, with zero side effects on rejection"
    - "mp4-mode short-circuit: regression/dev flag that skips network I/O while exercising the full composition"

key-files:
  created:
    - "src/home_cctv/ingest/main_stream_grabber.py"
    - "src/home_cctv/ingest/ingest_supervisor.py"
    - "tests/test_main_stream_grabber.py"
    - "tests/test_ingest_supervisor.py"
  modified:
    - "src/home_cctv/__main__.py"

key-decisions:
  - "MainStreamGrabber is reentrant, not thread-owning — every caller enters grab_main_frame() on its own thread and serialization comes from the process-wide Semaphore(1); shutdown() is a no-op for API symmetry."
  - "Double-checked cache locking is mandatory (ING-07 invariant): the second cache check must happen AFTER the semaphore acquire and BEFORE any _open_read_release call so that two racing callers on the same camera_id produce exactly ONE VideoCapture.open."
  - "On second-check hit the semaphore is released EARLY (not in finally) so the next caller is not forced to wait on a no-op; a local `semaphore_released` flag guards against double-release in finally."
  - "Stop-event gate on acquire uses a 0.5-s poll loop rather than a blocking acquire — keeps SIGINT latency well under 2 s (T-03-05) while still allowing callers to block for the full DVR handshake latency (~3.4 s per PHASE0-REPORT)."
  - "IngestSupervisor reuses the existing Phase 0 ShutdownSupervisor — does NOT create its own. This keeps install_signal_handlers in __main__.py as the single source of SIGINT truth."
  - "Initial probe is parallel (ThreadPoolExecutor max_workers=4) not serial. Worst-case boot latency with all cameras down: ~15 s instead of ~60 s (4 × 3 × 5 s). Probe threads are transient — they exit before any reader/watchdog/heartbeat starts, so D-01's '4 reader threads + 1 grabber' invariant is preserved."
  - "Per-probe side-thread timeout (G-01): cv2.VideoCapture has no native timeout kwarg (stimeout=5 s in OPENCV_FFMPEG_CAPTURE_OPTIONS is FFmpeg-level, not Python-level). Every open is wrapped in a daemon threading.Thread joined with INITIAL_PROBE_TIMEOUT_S; on timeout the cap is cross-thread released."
  - "Start-degraded means the probe status is informational — all 4 reader threads start regardless of probe result. Failing cameras enter their JitteredBackoff reconnect loop at t=0. Only ALL-fail (`RuntimeError('no cameras reachable')`) blocks boot."
  - "Path-traversal guard is a pre-condition, not a replacement for build_rtsp_url. It runs BEFORE any thread is spawned so a malformed cameras.yaml produces a ValueError with zero side effects (no partial boot, no leaked threads)."
  - "mp4_mode fans to ALL 4 cameras: __main__.py builds `frame_source_factory = lambda target, camera_id: open_frame_source(mp4_path, camera_id=camera_id)` — every reader thread reads the same MP4 file. This is the regression-test path used by Test 7 (CLI subprocess with SIGINT)."

patterns-established:
  - "Pattern: double-checked cache locking for serialized resources — first check (fast path, no lock contention on fresh cache) → acquire shared resource → re-check (post-barrier, source of truth) → early-release on hit so contention doesn't compound."
  - "Pattern: one-shot lifecycle ThreadPoolExecutor for startup work — transient worker pool that exits before any persistent pipeline thread starts, so it does not inflate the D-01 thread budget."
  - "Pattern: pre-spawn validation — run all input-validation checks BEFORE spawning any daemon thread, so a rejection leaves the process in a clean state."
  - "Pattern: reusable ShutdownSupervisor — every new subsystem takes a `shutdown: Optional[ShutdownSupervisor] = None` kwarg; production wires __main__.py's instance; tests construct their own."
  - "Pattern: mp4_mode short-circuit — boolean flag on the supervisor that threads through to `_initial_probe` so regression tests and the dev CLI can exercise the full composition without real RTSP."

requirements-completed:
  - ING-07
  - ING-08

# Metrics
duration: 11min
completed: 2026-04-17
---

# Phase 1 Plan 3: IngestSupervisor + MainStreamGrabber Summary

**DVR-wide `MainStreamGrabber` with `threading.Semaphore(1)` + 2-s TTL cache + double-checked cache locking (one `VideoCapture.open` per burst under concurrent callers on the same camera_id), plus an `IngestSupervisor` that composes 4 readers / 4 watchdogs / 4 heartbeats / 1 grabber under the existing Phase 0 `ShutdownSupervisor` — with a parallel start-degraded probe, a path-traversal guard on `sub_path` + `main_path`, and a `--live` / `--mp4-mode PATH` CLI that boots the whole pipeline end-to-end.**

## Performance

- **Duration:** 11 min
- **Started:** 2026-04-17T20:25:16Z
- **Completed:** 2026-04-17T20:35:57Z
- **Tasks:** 2 (both TDD, 4 commits total — RED + GREEN per task)
- **Files created:** 4 (2 source + 2 test)
- **Files modified:** 1 (`__main__.py`)

## Accomplishments

- **`MainStreamGrabber`** — reentrant class owning a DVR-wide `threading.Semaphore(1)` and a `dict[int, CacheEntry]` protected by `_cache_lock`. `grab_main_frame(camera_id: int) -> bytes | None` is synchronous per CONTEXT §G-03. Internal flow: first cache check (fast path, under `_cache_lock`) → acquire semaphore (polling with 0.5-s timeout gated by `stop_event`) → second cache check under `_cache_lock` (ING-07 double-checked locking invariant) — if a fresh entry appeared, release semaphore early and log `main_grab_cache_hit_after_wait` → on miss, call `_open_read_release` which does a short-lived `cv2.VideoCapture(url, CAP_FFMPEG) → isOpened? → up-to-3 reads → cv2.imencode(".jpg", quality=85) → release` (release always runs in `finally`, T-03-02). 9 tests pass covering single-grab, TTL hit/expiry, cross-camera serialization (zero open-interval overlap under concurrency), read failure with no cache poisoning, 20-calls-under-3-s latency, stop-event unblock, and the **Test 8 barrier race** that asserts `open_call_count == 1` when two threads race on cold cache.

- **`IngestSupervisor`** — composes the full Phase 1 stack into one `start()` / `shutdown()` lifecycle on top of the existing Phase 0 `ShutdownSupervisor` (reused, not replaced). `start()` runs in this order: (1) path-traversal validation of every `sub_path` + `main_path` in `cameras_file.cameras` — a single malformed entry raises `ValueError` with zero side effects; (2) `_initial_probe()` — parallel `ThreadPoolExecutor(max_workers=4)` across all cameras bounded by `INITIAL_PROBE_OVERALL_DEADLINE_S=15 s`, with each `_probe_single_camera` wrapping its `cv2.VideoCapture` in a side-thread join with `INITIAL_PROBE_TIMEOUT_S=5 s` (G-01 pattern — OpenCV has no native timeout kwarg); (3) all-fail check — raise `RuntimeError("no cameras reachable …")` before spawning any thread; (4) per-camera construction — `FrameSource` via the injectable factory (`open_frame_source` in production, `Mp4FrameSource` for tests), `FrameQueue(maxlen=2)`, `StreamReader`, `ReadWatchdog`, `CameraHeartbeat` (expected_fps = `cam.advertised_sub_fps`), all registered on the `ShutdownSupervisor` and sharing `stop_event`; (5) `MainStreamGrabber` construction with a `url_resolver` closure that builds the main URL per `camera_id`. Shutdown: `ShutdownSupervisor.request_stop()` → `.shutdown()` (force-releases every `FrameSource` cross-thread per Plan 02) → `rt.reader.join(2 s)` / watchdog / heartbeat per runtime → `grabber.shutdown()` (no-op) → log `ingest_stopped runtime_s=…`. 9 tests pass covering lifecycle (all 4 readers alive + shutdown < 2.5 s), all daemon threads, idempotent shutdown, start-degraded with 1 reachable (all 4 readers still running), all-fail probe RuntimeError (zero leaked threads), grabber integration via supervisor, `--live --mp4-mode` subprocess SIGINT round-trip, and the **Test 8 path-traversal guard** with all 4 required cases (`..`, missing `/`, control char, main_path traversal).

- **`--live` + `--mp4-mode PATH` CLI flags** on `python -m home_cctv`:
  - `--live` wires `cameras.yaml` → `IngestSupervisor` → `supervisor.wait_for_shutdown(∞)` so SIGINT drains everything in <2 s.
  - `--mp4-mode PATH` threads `mp4_mode=bool(args.mp4_mode)` into the supervisor AND swaps `frame_source_factory` to a lambda returning `open_frame_source(mp4_path, camera_id=...)` so every virtual camera reads from the same MP4 — a true regression path for the full composition.
  - `_run_live_pipeline` catches `RuntimeError` (no cameras reachable) → returncode 1 and `ValueError` (malformed path) → returncode 2 so shell scripts can distinguish failure modes.

- **All Phase 0 + Phase 1 Plan 01-01 + Plan 01-02 tests remain green** — 148/148 passing (130 pre-existing + 9 grabber + 9 supervisor). No regression.

## Task Commits

Each task used TDD with separate RED + GREEN commits:

1. **Task 1 RED — failing MainStreamGrabber tests** — `6d9641a` (test)
2. **Task 1 GREEN — MainStreamGrabber implementation** — `a147670` (feat)
3. **Task 2 RED — failing IngestSupervisor tests** — `5af6bc6` (test)
4. **Task 2 GREEN — IngestSupervisor + `__main__.py` --live wiring** — `3a999da` (feat)

_No REFACTOR commits needed — both implementations passed all behavior tests + all acceptance greps on first GREEN. No deviations, no fixes required._

## Files Created/Modified

- **Created** `src/home_cctv/ingest/main_stream_grabber.py` — `MainStreamGrabber` class + constants + `CacheEntry` dataclass + `_default_capture_factory`.
- **Created** `src/home_cctv/ingest/ingest_supervisor.py` — `IngestSupervisor` class + `CameraRuntime` dataclass + `_validate_path` helper + lifecycle / probe constants.
- **Created** `tests/test_main_stream_grabber.py` — 9 tests: constants, single-grab, TTL cache hit, TTL expiry, cross-camera serialization (no-overlap assertion), read failure, 20-call latency, stop-event unblock, double-checked-locking barrier race.
- **Created** `tests/test_ingest_supervisor.py` — 9 tests: constants, lifecycle (<2.5 s shutdown), all-daemon threads, idempotent shutdown, start-degraded, all-fail probe, grabber via supervisor, --live --mp4-mode SIGINT subprocess, path-traversal guard (4 cases).
- **Modified** `src/home_cctv/__main__.py` — added `--live` and `--mp4-mode PATH` flags, branched to new `_run_live_pipeline` helper BEFORE the `--phase0` branch, kept `--phase0` and `--mp4` paths untouched.

## Decisions Made

- **`MainStreamGrabber.shutdown()` is a no-op.** The grabber owns no persistent thread — every call is reentrant on the caller's thread. Shutdown is driven entirely by the shared `stop_event`; any blocked acquirer returns `None` within 0.5 s of SIGINT. Keeping `.shutdown()` in the public surface is purely for API symmetry so `IngestSupervisor.shutdown()` can call it uniformly.
- **Double-checked cache locking uses a `semaphore_released` flag, not a try/except.** The second cache check's hit path releases the semaphore early (so the next caller isn't blocked on a no-op). The outer `finally` must NOT double-release. A simple boolean flag is cleaner than nested try/except — one code path, zero ambiguity.
- **`IngestSupervisor` reuses the Phase 0 `ShutdownSupervisor`.** The `shutdown` kwarg defaults to a fresh instance for standalone use, but `__main__.py`'s `_run_live_pipeline` always passes the already-installed one so `install_signal_handlers` remains the single source of SIGINT truth.
- **Parallel probe is NOT a persistent worker pool.** The `ThreadPoolExecutor(max_workers=4)` is a `with` block — it spins up, runs the 4 probes, and exits before a single reader thread starts. This keeps D-01's "4 reader threads + 1 main-stream grabber thread" invariant intact; probe threads are transient.
- **Per-probe side-thread timeout over `signal.alarm` or `Future.result(timeout=...)`.** `cv2.VideoCapture` construction blocks synchronously on the network. `signal.alarm` is not thread-safe (it's a process-wide signal and we're inside a `ThreadPoolExecutor` worker). `Future.result(timeout=...)` on the executor would only time out the outer probe — the blocked cv2 call underneath would still hold its system resources. A daemon side-thread joined with timeout + cross-thread `release()` is the only reliable pattern on the FFmpeg backend (CONTEXT §G-01).
- **Path-traversal guard runs BEFORE probes.** A tampered `cameras.yaml` must not even reach the network probe. Running the guard first also means a `ValueError` raise leaves the process in a cleaner state than a `RuntimeError` from the probe.
- **`mp4_mode` skips ONLY the probe.** The reader threads still open their (MP4) FrameSource and the watchdog + heartbeat still run — so an `--mp4-mode` run exercises the full composition, which is exactly what Test 7's subprocess SIGINT round-trip validates.
- **CLI `_run_live_pipeline` returns 1 for RuntimeError, 2 for ValueError.** Shell callers can distinguish "no cameras reachable" (transient, retry later) from "malformed cameras.yaml" (fix config then retry).

## Deviations from Plan

**None.** Plan executed exactly as written. No auto-fixes, no blocking issues, no acceptance-grep nits. All 18 acceptance-grep checks (9 per task) pass on first GREEN. No CLAUDE.md rules were violated at any commit. Each task used TDD with one RED commit + one GREEN commit — no REFACTOR phase was needed.

## Threat Flags

No new security-relevant surface beyond what the plan's `<threat_model>` already covers (T-03-01 through T-03-08). The grabber + supervisor introduce only internal control flow + one new validation pre-condition (`_validate_path`), all of which flow through the existing Phase 0 `CredentialMaskFilter` on the `home_cctv` logger tree. Verified negative greps:

- `! grep -qE 'rtsp://[^:]+:[^*@]+@' src/home_cctv/ingest/main_stream_grabber.py` → empty.
- `! grep -qE 'rtsp://[^:]+:[^*@]+@' src/home_cctv/ingest/ingest_supervisor.py` → empty.

Both supervisor and grabber log lines use `camera_id=%s` / `source_id=%s` / `self.settings.masked_rtsp_base()` only — the resolved credentialed URL is never interpolated into any log message.

## Issues Encountered

None. Every acceptance check passed on first GREEN. The test-ordering logger-propagation issue from Plan 01-02 did NOT resurface because both new test modules include the autouse `_ensure_logger_propagation` fixture from the outset.

## User Setup Required

None — Plan 01-03 ships pure Python with no new external services, env vars, or installation steps. `cameras.yaml` is already committed and consumed by `load_cameras`. The pending WSL2 mirrored-networking flip (documented in STATE.md) is still not strictly needed — Plan 01-03 tests use MP4 fixtures and stub capture factories; the live DVR validation is deferred until the user chooses to run `python -m home_cctv --live` against the real DVR.

## Next Phase Readiness

### Hand-off to Phase 2 (`InferenceWorker` — YOLO + ByteTrack)

Phase 2 consumes `IngestSupervisor.runtimes[cam_id].reader.queue` — the `FrameQueue` that yields the freshest `(frame_ndarray, monotonic_ts)` tuple via `get_latest()`. No new pipe / queue / broker is needed; the same drop-oldest `deque(maxlen=2)` that Plan 01-01 introduced is already wired end-to-end. Phase 2's ByteTracker `frame_rate` kwarg MUST key off `cam.advertised_sub_fps` — the same value Plan 01-03 already passes as `expected_fps` into each `CameraHeartbeat`. No refactor of the supervisor is needed to add inference; just iterate `sup.runtimes.values()` and spawn one `InferenceWorker` per runtime.

### Hand-off to Phase 3 (`TriggerEvaluator` — DeepFace + EasyOCR on main-stream triggers)

Phase 3's `TriggerEvaluator` calls `IngestSupervisor.grabber.grab_main_frame(camera_id: int) -> bytes | None` DIRECTLY. The signature is locked and matches CONTEXT §G-03. Phase 3 needs to:

1. Detect a trigger condition (face-peak, parked car, zone enter) from the Phase 2 tracker state.
2. Call `grab_main_frame(camera_id)` — blocks for ~3 s on cold cache, ~0 s on cache hit (second trigger on the same camera within 2 s).
3. Persist the returned JPEG bytes to `EVENT_IMAGE_DIR` using the Phase 3 dated path convention.
4. Feed the decoded frame to DeepFace / EasyOCR as appropriate.

Phase 3 does NOT need to touch the semaphore, cache, or any grabber internal — the public API hides them completely. The grabber's 2-s TTL is specifically calibrated for burst triggers (face-peak fires within ~1 s of a first detection, so the second trigger hits the cache and does not re-open).

### Hand-off to Phase 4 (`metrics.json` + OPS-02)

- `IngestSupervisor.runtimes` is the canonical reflection point for Phase 4's metrics writer — iterate the dict, pull `stats = rt.reader.frame_source.stats`, pull `queue = rt.reader.queue`, call `compute_sample(stats, queue, expected_fps=…, now=time.monotonic())` per camera, serialize each `HeartbeatSample` to a JSON row. No new observation surface is needed.
- The `MainStreamGrabber`'s cache hit vs. miss count is not currently exposed as a counter — Phase 4 can add a trivial `grab_count` / `cache_hit_count` pair to the grabber for its own metrics without touching the public API (belt-and-braces additive extension, same pattern as Plan 01-02's `is_open` Protocol addition).

## ROADMAP Success-Criteria Traceability

| SC | Description | Covered by |
|----|-------------|------------|
| SC 1 | 4 StreamReader threads with steady FPS + last-read + drop rate, all creds masked | Plans 01-01 + 01-03 (supervisor end-to-end) |
| SC 2 | 30-s cable pull → resume within 60 s with jittered backoff | Plan 02 behavior, integrated here via IngestSupervisor composition |
| SC 3 | iptables-drop → watchdog force-release within 10 s | Plan 02 behavior, integrated here via ReadWatchdog in each CameraRuntime |
| SC 4 | `deque(maxlen=2)` drop-oldest + 5-frame post-reconnect drop | Plans 01-01 + 01-02 (inherited) |
| SC 5 | One-off main-stream grab, single `VideoCapture` open, 2-s TTL, global semaphore — `IngestSupervisor.grabber.grab_main_frame(camera_id)` | **Task 1 of this plan** — Tests 1/2/3/4/8 |

## Phase 1 Closure Status

| Requirement | Plan | Status |
|-------------|------|--------|
| ING-01 (4 per-camera readers) | 01-01 | ✅ |
| ING-02 (stuck-read watchdog, 10-s cap) | 01-02 | ✅ |
| ING-03 (jittered exponential reconnect) | 01-02 | ✅ |
| ING-04 (deque(maxlen=2) drop-oldest) | 01-01 | ✅ |
| ING-05 (30-s structured heartbeat) | 01-01 | ✅ |
| ING-07 (main-stream on demand, 2-s TTL) | **01-03** | ✅ |
| ING-08 (DVR-wide Semaphore(1)) | **01-03** | ✅ |
| OPS-01 (credential masking in logs) | 01-01 | ✅ (belt-and-braces in 01-02 + 01-03) |

All 8 Phase 1 requirements are complete.

## Self-Check: PASSED

All claims verified:

- `src/home_cctv/ingest/main_stream_grabber.py` exists.
- `src/home_cctv/ingest/ingest_supervisor.py` exists.
- `tests/test_main_stream_grabber.py` exists.
- `tests/test_ingest_supervisor.py` exists.
- `src/home_cctv/__main__.py` modified (contains `--live`, `mp4_mode=bool(args.mp4_mode)`).
- Commit `6d9641a` (Task 1 RED) present on `master`.
- Commit `a147670` (Task 1 GREEN) present on `master`.
- Commit `5af6bc6` (Task 2 RED) present on `master`.
- Commit `3a999da` (Task 2 GREEN) present on `master`.
- `uv run pytest tests/ -q` → 148 passed, 1 deselected.
- All positive acceptance greps pass (18 total across both tasks).
- Both negative acceptance greps return empty (no `rtsp://user:pass@host` in either new file).
- `uv run python -m home_cctv --help` shows `--live` and `--mp4-mode PATH`.

---
*Phase: 01-multi-stream-ingest-reconnect*
*Completed: 2026-04-17*
