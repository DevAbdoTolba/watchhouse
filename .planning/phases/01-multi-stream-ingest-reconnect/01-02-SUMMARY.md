---
phase: 01-multi-stream-ingest-reconnect
plan: 02
subsystem: ingest
tags: [threading, watchdog, backoff, reconnect, jitter, cross-thread-release, rtsp]

# Dependency graph
requires:
  - phase: 01-multi-stream-ingest-reconnect
    plan: 01
    provides: "StreamReader (run loop wrapping FrameSource), FrameQueue(maxlen=2) drop-oldest, CameraHeartbeat (4-state structured 30-s emitter), CaptureStats with hang_events field"
  - phase: 00-environment-sanity
    provides: "FrameSource Protocol, _BaseCvCapture (post-open 5-frame drop + green-guard + credential masking), CredentialMaskFilter, ShutdownSupervisor"
provides:
  - "home_cctv.ingest.backoff.JitteredBackoff — pure deterministic full-jitter exponential backoff with sustained-health reset"
  - "home_cctv.ingest.backoff module constants INITIAL_BACKOFF_S=1, MAX_BACKOFF_S=30, HEALTHY_RESET_S=60"
  - "home_cctv.ingest.watchdog.ReadWatchdog — per-camera daemon thread that cross-thread-releases a FrameSource when last_frame_monotonic goes stale > threshold_s"
  - "home_cctv.ingest.watchdog module constants DEFAULT_STALL_THRESHOLD_S=10, DEFAULT_CHECK_INTERVAL_S=1"
  - "Additive FrameSource Protocol extension: is_open: bool property"
  - "_BaseCvCapture.is_open implementation — True while a cv2.VideoCapture is held"
  - "StreamReader.__init__ accepts optional backoff kwarg for deterministic testing"
  - "StreamReader.run() two-level loop — outer reconnect-with-backoff, inner read-until-failure"
  - "Structured log lines: reconnect_attempt, reader_connected, reader_source_released_externally, watchdog_release"
affects: [01-03-supervisor-main-grabber, 02-detection-tracking, 04-metrics-json]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "full-jitter exponential backoff: delay = uniform(0, current_ceiling), ceiling doubles up to a cap"
    - "two-call reset_if_healthy: first primes _healthy_since, second returns True when window elapsed"
    - "cross-thread forced release: watchdog calls frame_source.release() to unblock a stuck cap.read()"
    - "per-episode release idempotency: _last_release_at_frame_ts guard prevents double-tap"
    - "interruptible sleep via stop_event.wait(delay) — never time.sleep in a pipeline-owned thread"
    - "additive Protocol extension: add property, implement on base class, callers use getattr defensively"

key-files:
  created:
    - "src/home_cctv/ingest/backoff.py"
    - "src/home_cctv/ingest/watchdog.py"
    - "tests/test_backoff.py"
    - "tests/test_watchdog.py"
    - "tests/test_stream_reader_reconnect.py"
  modified:
    - "src/home_cctv/ingest/capture.py"
    - "src/home_cctv/ingest/stream_reader.py"

key-decisions:
  - "reset_if_healthy uses a two-call pattern with NO 'within 2 s of now' staleness gate — freshness of last_frame_monotonic is an input contract, not something the method re-validates"
  - "Watchdog mechanism = side-thread release() call (CONTEXT G-01 default) — explicitly NOT ThreadPoolExecutor.submit().result(timeout=...) and NOT subprocess"
  - "FrameSource Protocol extension is additive only — is_open defaults to True via getattr in the reader so legacy stubs keep working"
  - "Reader must never reference _cap — uses is_open Protocol property only, enforced via negative grep"
  - "Post-open 5-frame drop stays inside _BaseCvCapture.open() — reader does nothing special for reconnects, the drop re-fires automatically"
  - "MP4 sources disable reconnect — EOF terminates the reader cleanly"
  - "Backoff sleeps use stop_event.wait(delay), never time.sleep — interruptible by shutdown even during 30-s backoff"
  - "Live-source 1 ms yield on transient decode error uses stop_event.wait(0.001) to stay responsive without spinning CPU"
  - "Structured log templates locked for Plan 03 grep-assertions: reconnect_attempt camera_id=... attempt=N delay_s=X.XX ceiling_s=Y.YY, reader_connected camera_id=... attempt=N, watchdog_release camera_id=... age_s=X.XX threshold_s=Y.YY hang_events=N"

patterns-established:
  - "Pattern: pure backoff helper — JitteredBackoff is testable without threads or wall clock; inject random.Random(seed) for determinism"
  - "Pattern: side-thread lifecycle watchdog — ReadWatchdog observes a CaptureStats field and calls release() cross-thread on staleness; same pattern will serve main-stream grabber's timeout protection in Plan 03"
  - "Pattern: additive Protocol extension — add the property to the Protocol, implement on the base class, callers use getattr with a safe default; no existing code breaks"

requirements-completed:
  - ING-02
  - ING-03

# Metrics
duration: 13min
completed: 2026-04-17
---

# Phase 1 Plan 2: Watchdog + Reconnect Summary

**Per-camera `ReadWatchdog` thread that cross-thread-releases a stuck `cap.read()` within 10 s + pure deterministic `JitteredBackoff` (1 → 2 → 4 → 8 → 16 → 30 s full-jitter with 60-s sustained-health reset) + `StreamReader` extended into a two-level loop (outer reconnect-with-backoff, inner read-until-failure) that survives a 30-s DVR outage without process restart and exits within 2 s of SIGINT even mid-outage.**

## Performance

- **Duration:** 13 min
- **Started:** 2026-04-17T20:02:20Z
- **Completed:** 2026-04-17T20:15:16Z
- **Tasks:** 3 (all TDD, 6 commits total — RED + GREEN per task)
- **Files created:** 5 (2 source + 3 test)
- **Files modified:** 2 (capture.py + stream_reader.py)

## Accomplishments

- **`JitteredBackoff`** — pure Python helper (no threads, no I/O). Full-jitter delay (`random.uniform(0, current_ceiling_s)`) with schedule `1 → 2 → 4 → 8 → 16 → 30` (cap sticky). Two-call `reset_if_healthy(last_frame_monotonic, now)` pattern: first call primes `_healthy_since`, second call returns `True` when `now - _healthy_since >= 60.0 s` and resets the counter. Deterministic under injected `random.Random(seed)` for testability. 9 tests pass.
- **`ReadWatchdog`** — per-camera daemon `threading.Thread`. Every `check_interval_s` (default 1 s) inspects `CaptureStats.last_frame_monotonic`; if `now - last > threshold_s` (default 10 s) calls `frame_source.release()` from the watchdog thread. The reader's blocked `cv2.VideoCapture.read()` then returns `(False, None)` and the reader's outer loop detects the released state via `FrameSource.is_open == False` and reconnects. Idempotent within a single stall episode (`_last_release_at_frame_ts` guard), re-armable across episodes (first healthy read after recovery advances `last_frame_monotonic` to a new value, rebuilding the state to detect the next stall). Increments `CaptureStats.hang_events` for observability. Swallows `release()` exceptions so the watchdog itself never dies even under CONTEXT G-01's "technically undefined but reliable" cross-thread backend call. 7 tests pass.
- **Additive `FrameSource.is_open` Protocol extension** — added `is_open: bool` property to the `FrameSource` Protocol and implemented it on `_BaseCvCapture` as `return self._cap is not None`. `RtspFrameSource` and `Mp4FrameSource` inherit the implementation with zero behavior change. No existing call site was altered. The reader consumes `is_open` via `getattr(fs, "is_open", True)` so legacy stubs that predate the extension still behave like live sources (keep trying on decode errors; never exit except via `stop_event`).
- **`StreamReader.run()` two-level loop**:
  1. **Outer loop (open-or-reconnect):** call `frame_source.open()`. On failure for a live source, emit `reconnect_attempt camera_id=... attempt=N delay_s=X.XX ceiling_s=Y.YY err=...`, sleep via `stop_event.wait(delay)` (interruptible), then retry. MP4 open failure logs `reader_open_failed` and exits cleanly — reconnect is disabled for file sources.
  2. **Inner loop (read-until-failure-or-stop):** successful decode → push `(frame, time.monotonic())` onto the queue AND call `backoff.reset_if_healthy(last_frame_monotonic=stats.last_frame_monotonic, now=time.monotonic())` for the sustained-health reset. Failed decode → decide EOF (file source heuristic) vs external release (`is_open == False` → break inner loop → outer loop reconnects) vs transient decode error (1 ms interruptible yield, keep trying).
- **Structured log lines locked for Plan 03 grep-assertions:**
  - `reconnect_attempt camera_id=<id> attempt=<n> delay_s=<x.xx> ceiling_s=<x.xx> err=<...>` (WARNING)
  - `reader_connected camera_id=<id> attempt=<n>` (INFO)
  - `reader_source_released_externally camera_id=<id>` (WARNING)
  - `watchdog_release camera_id=<id> age_s=<x.xx> threshold_s=<x.xx> hang_events=<n>` (WARNING)
- **Backoff sleep interruptibility** — every backoff sleep in the reader is `stop_event.wait(delay)`. Negative grep `! grep -qE 'time\.sleep\(' src/home_cctv/ingest/stream_reader.py` enforces this. SIGINT now exits the reader within ≤1 s even if it's mid-30-s backoff.
- **All Phase 0 + Phase 1 Plan 01-01 tests remain green** — 130/130 passing (107 pre-existing + 23 new).

## Task Commits

Each task used TDD with separate RED + GREEN commits:

1. **Task 1 RED — failing JitteredBackoff tests** — `018b511` (test)
2. **Task 1 GREEN — JitteredBackoff implementation** — `abc7523` (feat)
3. **Task 2 RED — failing ReadWatchdog tests** — `1ea8d90` (test)
4. **Task 2 GREEN — ReadWatchdog implementation** — `7a2e02d` (feat)
5. **Task 3 RED — failing StreamReader reconnect tests** — `5371ca1` (test)
6. **Task 3 GREEN — StreamReader reconnect + FrameSource.is_open** — `525dfca` (feat)

_No REFACTOR commits needed — each task's first GREEN pass satisfied all behavior tests and acceptance greps after a single narrow fix for the `_cap` negative grep (see Deviations below)._

## Files Created/Modified

- `src/home_cctv/ingest/backoff.py` — `JitteredBackoff` + module constants
- `src/home_cctv/ingest/watchdog.py` — `ReadWatchdog` + module constants
- `src/home_cctv/ingest/capture.py` — **additive** `is_open` Protocol extension + `_BaseCvCapture.is_open` implementation
- `src/home_cctv/ingest/stream_reader.py` — `__init__` gains optional `backoff` kwarg; `run()` rewritten as two-level reconnect-aware loop
- `tests/test_backoff.py` — 9 pure tests (no threads, no wall clock) covering schedule, determinism, two-call reset semantics
- `tests/test_watchdog.py` — 7 tests covering healthy-no-release, stale-release-once-per-episode, hang_events increment, stop termination, pre-first-frame grace, re-arm
- `tests/test_stream_reader_reconnect.py` — 7 tests covering open-failure reconnect, external-release detection via `is_open`, sustained-health reset via monkeypatched monotonic, post-open 5-frame drop preserved across reconnect, stop_event interrupts backoff sleep, structured log format, MP4 EOF disables reconnect

## Decisions Made

- **`reset_if_healthy` has a two-call contract, not a staleness gate.** The caller invokes it after every successful decode so `last_frame_monotonic ≈ now` by construction. The method primes `_healthy_since` on the first call, resets on the second call where the 60-s window has elapsed. A "within 2 s of now" staleness gate was explicitly rejected because it would contradict this invariant and make Test 6 impossible for callers with `last_frame_monotonic == now` to the microsecond.
- **`FrameSource.is_open` extension is additive only.** The Protocol gained a property, `_BaseCvCapture` got a 4-line implementation, no existing call site changed, the reader consumes it via `getattr(..., default=True)` for forward-compatibility with pre-extension stubs. Negative grep `! grep -qE '_cap' src/home_cctv/ingest/stream_reader.py` enforces that the reader never reaches into `_BaseCvCapture` internals.
- **Watchdog mechanism = side-thread `release()` call.** Rejected alternatives: `ThreadPoolExecutor.submit(cap.read).result(timeout=1.0)` (contradicts D-01 thread-only concurrency model; also would require wrapping every read call which breaks Plan 01-01's reader) and `subprocess per camera` (explicit D-01 violation requiring user sign-off). The current implementation matches PITFALLS §1.1's definitive guidance.
- **MP4 sources never reconnect.** `is_file_source == True` at open failure → log + return. At EOF (decode_errors > 0 + frames_decoded > 0) → log `reader_eof` + return. This keeps the `--mp4` regression path fast and deterministic.
- **Backoff sleeps are always `stop_event.wait(delay)`, never `time.sleep(delay)`.** Prevents a SIGINT from blocking for up to 30 s during a long backoff.
- **Inner-loop 1 ms yield uses `stop_event.wait(0.001)`.** Live source decode errors (including the first 5 frames of post-open drop) return `(False, None)` immediately; without a small yield the inner loop would spin a CPU core. With the yield, the reader is still responsive to `stop_event` within 1 ms even on a continuously-failing live stream.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Acceptance-grep glitch] Log event name `reader_cap_released_externally` contained `_cap`**
- **Found during:** Task 3 post-implementation acceptance check.
- **Issue:** The initial log line name `reader_cap_released_externally` contained the substring `_cap`, tripping the acceptance negative grep `! grep -qE '_cap' src/home_cctv/ingest/stream_reader.py`. The grep's intent was "no private `_cap` attribute access in the reader" but matches any literal `_cap`, including event-name slugs in log strings and doc comments.
- **Fix:** Renamed the log event to `reader_source_released_externally` (same semantic meaning — "the frame source was released by something outside this thread"). Paraphrased the adjacent docstring to drop the `_cap` reference as well.
- **Files modified:** `src/home_cctv/ingest/stream_reader.py`
- **Verification:** Negative grep now returns empty; all 7 Task 3 tests still pass; full suite 130/130 green.
- **Committed in:** `525dfca` (same Task 3 GREEN commit).

**2. [Rule 3 — Blocking test infrastructure] `caplog` does not see records when `logger.propagate = False`**
- **Found during:** Task 3 full-suite integration run (the single-file run passed; the full suite failed on Test 1 / Test 6 of the reconnect tests).
- **Issue:** Phase 0 `configure_logging` sets `logger.propagate = False` on the `home_cctv` logger tree. When another test (e.g. `test_heartbeat.py`) runs before the reconnect tests and flips this flag, pytest's `caplog` fixture (which attaches to the root logger) receives nothing — even though the records are clearly visible on stderr. This is the exact issue documented in Plan 01-01's deviation log, surfacing again in a new test file.
- **Fix:** Added a module-scoped autouse fixture `_ensure_logger_propagation` that resets the logger handlers AND explicitly sets `lg.propagate = True` before each test in `test_stream_reader_reconnect.py`. The reconnect tests are then robust to test ordering.
- **Files modified:** `tests/test_stream_reader_reconnect.py`
- **Verification:** Full suite 130/130 green regardless of test order. The same fix pattern could be extracted into a shared conftest fixture in a future plan if more modules hit this.
- **Committed in:** `525dfca` (same Task 3 GREEN commit — the reconnect tests were introduced and fixed in the same RED→GREEN cycle; the propagation issue surfaced only when integrating with the full suite after implementation).

---

**Total deviations:** 2 auto-fixed (1 acceptance-grep collision, 1 blocking test infrastructure)
**Impact on plan:** Zero scope change. Both fixes are surface-level — one renamed a log event to unbreak a negative grep, one is a pytest fixture that normalizes logger state. No behavior change in production code.

## Threat Flags

No new security-relevant surface beyond what the plan's `<threat_model>` already covers (T-02-01 through T-02-07). The watchdog and reconnect paths introduce only internal control flow over existing `FrameSource` methods. Credential masking continues to flow through the Phase 0 `CredentialMaskFilter` on the `home_cctv` logger tree (verified negative grep `! grep -qE 'rtsp://[^:]+:[^*@]+@' src/home_cctv/ingest/backoff.py src/home_cctv/ingest/watchdog.py`).

## Issues Encountered

- Plan 01-01's "caplog doesn't see logs when `propagate=False`" issue resurfaced in Plan 02's test module. Rather than reading the rotating file handler from disk (01-01's workaround), this plan uses an autouse fixture that resets `propagate = True` for the `home_cctv` subtree before each test. Either pattern works; the propagation reset is slightly cleaner for modules that rely heavily on `caplog`.
- The live-source 1 ms yield (`stop_event.wait(0.001)`) was added during Task 3 to prevent the reader from spinning on a stub that returns `(False, None)` immediately. Without it, the reconnect tests were fine but a real live RTSP source with an unusual error cadence could wedge one CPU core. Documented as a Decision above.

## User Setup Required

None — Plan 01-02 delivers pure Python code with no new external services, env vars, or installation steps. The WSL2 mirrored-networking flip documented in STATE.md is still a pending non-blocker for the Plan 01-03 live-DVR validation run; Plan 01-02 ships without needing it because all tests use stubs or the offline MP4 fixture.

## Next Phase Readiness

### Hand-off to Plan 01-03 (`IngestSupervisor` + `MainStreamGrabber`)

Plan 01-03 composes one `StreamReader` + one `CameraHeartbeat` + one `ReadWatchdog` per camera under a single `IngestSupervisor`. Everything is in place:

- **`ReadWatchdog` construction** — supervisor instantiates one per camera: `ReadWatchdog(camera_id=..., frame_source=reader.frame_source, stats=reader.frame_source.stats, stop_event=supervisor.stop_event)`. Default thresholds (10 s stall, 1 s check interval) are correct for the 3-way (watchdog / reader / heartbeat) per-camera arrangement. Plan 01-03 just calls `start()` on the watchdog after the reader; shutdown is already wired via the shared `stop_event`.
- **`JitteredBackoff` lifecycle** — the supervisor does NOT need to touch `JitteredBackoff`; each `StreamReader` owns its own instance internally. Tests pass a seeded backoff; production defaults to `random.Random()`.
- **Log line grep-assertions** — Plan 03 acceptance tests can grep for:
  - `reconnect_attempt camera_id=cam1 attempt=\d+ delay_s=\d+\.\d+ ceiling_s=\d+\.\d+`
  - `reader_connected camera_id=cam1 attempt=\d+`
  - `watchdog_release camera_id=cam1 age_s=\d+\.\d+ threshold_s=\d+\.\d+ hang_events=\d+`
  - `reader_source_released_externally camera_id=cam1`
- **Reconnect cap.release() coupling with heartbeat** — the heartbeat's `state=stalled` classification triggers on `last_read_age_s > 10 s`, which is the same window the watchdog uses to fire a release. The heartbeat will briefly log `state=stalled` during an outage, then `state=starting` during reconnect (new `first_frame_monotonic` hasn't been set), then back to `state=healthy` once the post-open 5-frame drop clears. All four state values remain reachable in the live flow.

### Hand-off to Phase 3 (main-stream grabber 503 retries — if adopted)

`JitteredBackoff` is re-usable for any reconnect caller. If Plan 03's `MainStreamGrabber` wants to retry DVR 503s (rare but documented in PHASE0-REPORT at the 6-session cap), it can `JitteredBackoff(rng=random.Random())` independent of the reader's instance. No shared state.

### Hand-off to Phase 4 (`metrics.json` + OPS-02)

- `CaptureStats.hang_events` is now actively incremented; Phase 4's metrics writer includes it verbatim.
- No new metrics fields added — Phase 4 reshape from Phase 1 Plan 01-01 is still accurate.

## Self-Check: PASSED

All claims verified:

- `src/home_cctv/ingest/backoff.py` exists.
- `src/home_cctv/ingest/watchdog.py` exists.
- `tests/test_backoff.py` exists.
- `tests/test_watchdog.py` exists.
- `tests/test_stream_reader_reconnect.py` exists.
- Commit `018b511` (Task 1 RED) present on `master`.
- Commit `abc7523` (Task 1 GREEN) present on `master`.
- Commit `1ea8d90` (Task 2 RED) present on `master`.
- Commit `7a2e02d` (Task 2 GREEN) present on `master`.
- Commit `5371ca1` (Task 3 RED) present on `master`.
- Commit `525dfca` (Task 3 GREEN) present on `master`.
- `uv run pytest tests/ -q` → 130 passed, 1 deselected.
- All positive acceptance greps pass; all three negative greps return empty (`_cap`, `time.sleep(`, `rtsp://user:pass@host`).
- `FrameSource.is_open` Protocol property present and implemented on `_BaseCvCapture`.
- No `_cap` substring anywhere in `src/home_cctv/ingest/stream_reader.py`.

---
*Phase: 01-multi-stream-ingest-reconnect*
*Completed: 2026-04-17*
