---
phase: 01-multi-stream-ingest-reconnect
reviewed: 2026-04-17T00:00:00Z
depth: standard
files_reviewed: 16
files_reviewed_list:
  - src/home_cctv/__main__.py
  - src/home_cctv/ingest/backoff.py
  - src/home_cctv/ingest/capture.py
  - src/home_cctv/ingest/ingest_supervisor.py
  - src/home_cctv/ingest/main_stream_grabber.py
  - src/home_cctv/ingest/stream_reader.py
  - src/home_cctv/ingest/watchdog.py
  - src/home_cctv/obs/__init__.py
  - src/home_cctv/obs/heartbeat.py
  - tests/test_backoff.py
  - tests/test_heartbeat.py
  - tests/test_ingest_supervisor.py
  - tests/test_main_stream_grabber.py
  - tests/test_stream_reader.py
  - tests/test_stream_reader_reconnect.py
  - tests/test_watchdog.py
findings:
  critical: 0
  warning: 6
  info: 8
  total: 14
status: issues_found
---

# Phase 1: Code Review Report

**Reviewed:** 2026-04-17T00:00:00Z
**Depth:** standard
**Files Reviewed:** 16
**Status:** issues_found

## Summary

Phase 1 shipped a solid 4-camera concurrent RTSP ingest. The locked decisions (D-01..D-13, G-01..G-04) are honored in code: the watchdog does cross-thread `cap.release()` (G-01), full-jitter backoff is implemented correctly (D-07), the main-stream grabber does double-checked locking under a `Semaphore(1)` (D-09), credential masking lives in a logging filter (D-10), and Phase 1 writes nothing to SQLite or disk (D-11).

No critical bugs were found. File-descriptor safety is good — every `cv2.VideoCapture.open()` has a `try/finally` release path. The path-traversal guard rejects `..`, missing-slash, and control chars (including `\n` and `\0` via `ord(c) < 32`). The semaphore double-check path does not double-release on the normal code paths.

However, several **correctness edge cases and test gaps** exist that deserve fixes before Phase 2 builds on this foundation:

1. The **reader-connected log line** fires on every reconnect — but the accompanying `reader_started` only fires when `attempt == 0`, which is only the very first open. After a watchdog-triggered reconnect (attempt >= 1), callers get `reader_connected` without `reader_started`, which is fine — but the current `reader_connected` line always uses `self._backoff.attempt` which is **never zero after a failed open**, because `open() success` does NOT reset `attempt` back to 0. Only `reset_if_healthy` does that, and it fires only after 60 s of sustained reads. This means attempts-counter logs creep forever across reconnects without a visible reset event other than the 60-s sustained-health milestone. Not a bug, but log hygiene.

2. A subtle race exists between the watchdog's `release()` and the reader's `is_open` check. The reader reads `is_open` **after** a failed read returns `(False, None)` — but the watchdog may have set `_cap = None` in between `cap.read()` returning and `is_open` being evaluated. Reader then correctly enters reconnect. If `open()` runs immediately and a NEW `cap` is in place before watchdog's *next* tick, the watchdog sees a fresh `last_frame_monotonic` and doesn't fire again. This is correct behavior but **entirely dependent on idempotency via `_last_release_at_frame_ts`** — which currently uses `==` equality on a float timestamp. If the reader happens to emit a frame with exactly the same `last_frame_monotonic` value as the previous episode (astronomically unlikely but possible on a low-resolution clock), the watchdog would skip. See WR-02.

3. The `_validate_path` guard covers the common injection vectors listed in the plan but **misses one documented attack**: Unicode normalization / percent-encoded `..`. The guard rejects literal `..` but not `%2e%2e` or Unicode homoglyphs. Not a documented Phase 1 requirement, but noting for Phase 3.

4. **Test 3 of `test_stream_reader_reconnect.py`** monkey-patches `time.monotonic` globally via `sr.time.monotonic` — this is the `time` module itself, which means the patch leaks to **every** codepath in the test process, including the outer `time.sleep(0.02)` and any lock timeouts. The test passes but is fragile; see WR-04.

5. **Log-hygiene check: `grab_main_frame` logs `camera_id=%s`, never the URL.** ✓ `_open_read_release` logs `camera_id=%s`, never the URL. ✓ `reader_started` / `reader_connected` use `_sanitized_target()` which strips the password. ✓ The belt-and-braces `CredentialMaskFilter` catches anything else. The only borderline call site is `_probe_single_camera`, which builds `url = build_rtsp_url(...)` and passes it to `cv2.VideoCapture(url, ...)` but never logs `url` itself — the log line `probe_ok camera_id=%d attempt=%d` is clean.

6. **`--live` path has no `sink` or `DisplaySink` integration**, and SIGINT during the initial probe is not gracefully aborted. See WR-05 and WR-06.

Test quality is generally strong — tests assert the correct invariants (no-overlap, exactly-1-release-per-episode, JPEG magic bytes, drop_count semantics). A few tests are weaker than they could be; see WR-06 and IN-02.

## Warnings

### WR-01: `grab_main_frame` can double-release the semaphore on early-path exception

**File:** `src/home_cctv/ingest/main_stream_grabber.py:184-200`
**Issue:** In the double-checked cache-hit branch, `self.semaphore.release()` is called on line 184, then `semaphore_released = True` is assigned on line 185. If `release()` itself raises (extremely rare — `threading.Semaphore.release()` on an unbounded semaphore shouldn't raise — but theoretically possible on a `BoundedSemaphore` subclass, or if a buggy mock is injected), `semaphore_released` stays `False` and the `finally` block will release again. Separately, if a caller passes a `BoundedSemaphore` (tests don't, but nothing in the constructor signature prevents it), the `finally` path after a successful `return entry.jpeg_bytes` on the cache-hit-after-wait branch would NOT double-release because `semaphore_released=True` guards it — so this specific branch is safe. The risk is purely in the release-raises-exception window.
**Fix:** Flip the guard before the release so a mid-release exception still marks the semaphore as handled:
```python
# Release the semaphore early so the next caller can proceed...
semaphore_released = True
self.semaphore.release()
return entry.jpeg_bytes
```
This is a 2-line reorder with no behavior change on the happy path, and it closes the theoretical double-release window.

### WR-02: Watchdog episode-idempotency uses float-equality on `last_frame_monotonic`

**File:** `src/home_cctv/ingest/watchdog.py:91`
**Issue:** `if last == self._last_release_at_frame_ts:` relies on exact float equality to decide whether two ticks represent the "same stall episode." On a high-resolution clock this is safe — `time.monotonic()` on Linux gives nanosecond precision, so the odds of two distinct frames getting identical timestamps are negligible. But on WSL2 the clock resolution can degrade under load; if two successful reads happen to land on the same microsecond (and `last_frame_monotonic` is assigned from `time.monotonic()` inside `_BaseCvCapture.read`), the watchdog would treat a fresh stall as a repeat of the old episode and silently NOT fire — the stream stays stuck for the full threshold again before recovery.
**Fix:** Use a sequence counter instead of a timestamp for episode identity. Add an integer `stall_episode_id` to `CaptureStats` that the watchdog increments on release and the reader resets on every successful frame. Or, simpler: compare against a tolerance:
```python
if self._last_release_at_frame_ts is not None and \
   abs(last - self._last_release_at_frame_ts) < 1e-6:
    continue
```
Or strictest: record `self._last_release_at_frame_ts = last` AND `self._released_episode_active = True`, reset the flag to False as soon as `stats.frames_decoded` advances past a captured value. The current code is correct in practice (the monotonic clock on WSL2 Linux has ns resolution), so this is a latent robustness issue, not a production bug.

### WR-03: `_run_live_pipeline` swallows `IngestSupervisor.start()` exceptions without shutting down the outer `ShutdownSupervisor`

**File:** `src/home_cctv/__main__.py:238-244`
**Issue:** If `sup.start()` raises `RuntimeError` (all-probes-fail) or `ValueError` (malformed path), the except blocks log the error and return an exit code — but the outer `supervisor` (the `ShutdownSupervisor` installed earlier) was never torn down. Because signal handlers are still bound to the singleton and nothing calls `supervisor.shutdown()`, any `FrameSource` the supervisor might have registered during the partial `sup.start()` (before the exception was raised) will not be released. In practice this is mitigated because `IngestSupervisor.start()` does path-validation BEFORE any `register()` call, and does the initial probe BEFORE any `register()` call, so the RuntimeError/ValueError branches never leave dangling registrations today. But the code is one refactor away from leaking — any future addition that calls `self.shutdown_sup.register(fs)` *before* a fallible operation would leak on the error path.
**Fix:** Wrap `sup.start()` in a `try/except` that always calls `supervisor.shutdown()` on error:
```python
try:
    sup.start()
except RuntimeError as exc:
    logger.error("ingest_start_failed err=%r", exc)
    supervisor.shutdown()
    return 1
except ValueError as exc:
    logger.error("ingest_config_invalid err=%r", exc)
    supervisor.shutdown()
    return 2
```

### WR-04: `test_sustained_health_resets_backoff` monkey-patches the `time` module globally

**File:** `tests/test_stream_reader_reconnect.py:243-244`
**Issue:** `monkeypatch.setattr(sr.time, "monotonic", fake_mono)` — `sr.time` is a reference to the `time` stdlib module, so this patch is **process-wide**. The test's own `time.sleep(0.02)` on line 257 and `time.monotonic()` on line 255 execute against the fake clock, not the real one. The test only works because `deadline = time.monotonic() + 5.0` is evaluated with the real clock BEFORE the patch takes effect (line 255 reads `time.monotonic` at the time of the comparison, not at deadline computation — wait, actually line 255 IS evaluated after the patch, so `time.monotonic()` in the `while time.monotonic() < deadline` loop is the fake clock). In practice this test passes because `fake_mono` advances by 1 s per call, and `deadline` was set with the real clock, so the loop condition behaves unpredictably. Pytest's `monkeypatch` fixture auto-restores the patch at teardown, so test isolation survives — but the test is much more fragile than it looks.
**Fix:** Patch `sr.time.monotonic` against a closure that's only invoked by `stream_reader.py`, not by the test helper. The cleanest fix is to expose a `clock` kwarg on `StreamReader` and on `JitteredBackoff.reset_if_healthy` so the test can inject the fake clock narrowly:
```python
reader = StreamReader(camera_id=..., clock=fake_mono)
```
Or use `time.monotonic_ns` inside `stream_reader.py` and patch only that. Without either, the test's "simulate 65 s of frames" assertion is correlated with real wall-clock wait loops in the test harness.

### WR-05: `--live` entry point lacks display/sink, `--show` flag is silently ignored in live mode

**File:** `src/home_cctv/__main__.py:89-90, 200-251`
**Issue:** The `build_parser` advertises `--show` as a general-purpose flag ("Open cv2.imshow preview window") but `_run_live_pipeline` never reads `args.show`. A user running `python -m home_cctv --live --show` will see NO window — the flag silently does nothing. The docstring for `_run_live_pipeline` also does not mention that `--show` is ignored.
**Fix:** Either (a) wire a per-camera `DisplaySink` into each reader runtime and honor `--show`, or (b) log a warning at startup if `args.show` is true in live mode:
```python
if args.show:
    logger.warning("--show ignored in --live mode (no per-camera display yet)")
```
Option (b) is a 2-line fix and preserves the v1 scope.

### WR-06: `test_cli_live_mp4_mode_exits_cleanly_on_sigint` does not validate clean pipeline teardown

**File:** `tests/test_ingest_supervisor.py:363-425`
**Issue:** The test asserts `elapsed < 6.0` and `returncode in (0, -signal.SIGINT, 130)` but does NOT assert anything about the log output. A process could exit in time with returncode 0 even if a reader thread deadlocked on the join (daemon threads don't block process exit). A silent degradation where the reader leaks but the main process exits cleanly would pass this test. The test should also verify:
* `"ingest_started"` appears in stderr/stdout (the supervisor actually booted 4 cameras)
* `"ingest_stopped"` appears (the shutdown path ran, not just process kill)
* No `"ERROR"` lines in the output
**Fix:** Add assertions on `stdout + stderr` decoded content:
```python
output = (stdout + stderr).decode(errors='replace')
assert "ingest_started" in output, "supervisor did not reach ingest_started"
assert "ingest_stopped" in output, "supervisor did not complete clean shutdown"
assert "ERROR" not in output or "no cameras reachable" in output
```

## Info

### IN-01: `JitteredBackoff.next_delay` has a boundary behavior worth documenting

**File:** `src/home_cctv/ingest/backoff.py:55-64`
**Issue:** `next_delay()` uses `random.uniform(0.0, current_ceiling_s)` which can return exactly `0.0` on its closed-closed range. A zero delay means the reconnect loop spins to `open()` immediately. For the first attempt (ceiling=1), this is fine. For attempt 6+ (ceiling=30), a 0.0 delay is also fine — but it happens, on average, once per ~2^32 calls. The docstring ("Full-jitter delay in ``[0, current_ceiling_s]``") is correct but doesn't note that zero is a legitimate result that triggers an immediate retry.
**Fix:** Add a note in the docstring:
```python
"""... Can return 0.0; callers should tolerate immediate retries."""
```

### IN-02: `test_reconnect_after_three_open_failures` uses a busy-wait deadline

**File:** `tests/test_stream_reader_reconnect.py:156-158`
**Issue:** `while time.monotonic() < deadline and fs.open_calls < 4: time.sleep(0.05)` is a polling loop. If the reader's backoff happens to roll a delay near 7 s (ceiling is 1+2+4=7 worst case with seed 42), the test's 10-s deadline has thin margin. On a loaded CI the test could flake.
**Fix:** Use a tighter seeded RNG that produces near-zero delays, OR increase the deadline with a comment explaining the worst-case:
```python
# Worst case with seed 42: 1+2+4 = 7 s of backoff. Deadline is 15 s for margin.
deadline = time.monotonic() + 15.0
```

### IN-03: `_probe_single_camera` can waste up to 5 s of the 15-s overall deadline on a probe timeout whose worker thread is already stuck

**File:** `src/home_cctv/ingest/ingest_supervisor.py:162-183`
**Issue:** On probe timeout (worker still alive after 5 s), the code calls `cap_holder[0].release()` but the worker thread might not have assigned `cap_holder[0]` yet — it's daemon=True so it won't block process exit. But the worker continues running the already-in-progress `cv2.VideoCapture(url, ...)` call, which can hold file descriptors for a long time. With 4 parallel probes each potentially leaking a descriptor on timeout, worst case is 4 stuck captures across the process lifetime. In practice the probe is called once at startup and the descriptors eventually get cleaned up by the FFmpeg backend.
**Fix:** Document this in the docstring; no code change needed for Phase 1. Consider bounding via the existing `INITIAL_PROBE_OVERALL_DEADLINE_S=15.0` and accepting the leak.

### IN-04: `CaptureStats.measured_fps` divides by zero on identical first/last timestamps

**File:** `src/home_cctv/ingest/capture.py:64-73`
**Issue:** If `first_frame_monotonic == last_frame_monotonic` (single frame or clock identity), the guard `if dt > 0 else 0.0` handles it. But the guard `frames_decoded < 2` already returns 0.0 on single-frame states, so the second guard is redundant in theory. Defensively correct.
**Fix:** None. Leave as belt-and-braces.

### IN-05: `MainStreamGrabber.shutdown()` is a no-op but the docstring suggests otherwise

**File:** `src/home_cctv/ingest/main_stream_grabber.py:256-263`
**Issue:** The docstring says "No-op for API symmetry" which is accurate, but `IngestSupervisor.shutdown()` on line 374-375 calls `self.grabber.shutdown()`. If a blocked caller is in the middle of `grab_main_frame`, there's no way to wake them via `grabber.shutdown()` — they depend on `stop_event`. This is intentional (per the docstring) but the coupling from `IngestSupervisor.shutdown()` is misleading because it appears to participate in the shutdown sequence.
**Fix:** Either remove the `self.grabber.shutdown()` call from `IngestSupervisor.shutdown()` since it's a no-op, or reinforce in a comment: `# Grabber unblocks via stop_event; this call is a no-op for API symmetry.`

### IN-06: `reader_started` log line ONLY fires when `attempt == 0`, which is after `next_delay()` would have incremented it

**File:** `src/home_cctv/ingest/stream_reader.py:223`
**Issue:** `if self._backoff.attempt == 0:` — this check succeeds only on the very first boot (before any failed open). If the reader's FIRST `open()` fails, `next_delay()` advances `attempt` to 1 before the next `open()` attempt succeeds. The `reader_started` line then never fires for this camera's lifetime unless a 60-s sustained-health `reset()` reverts `attempt` to 0. This means some cameras with a flaky first connect will never log `reader_started` — the signal a downstream log consumer might grep for to confirm startup.
**Fix:** Move `reader_started` emission to be unconditional on first connect, using a local bool flag:
```python
reader_started_logged = False
# ... inside the outer loop after open() succeeds ...
if not reader_started_logged:
    _LOG.info("reader_started camera_id=%s ...", ...)
    reader_started_logged = True
```

### IN-07: `_SEMAPHORE_POLL_S = 0.5` means worst-case SIGINT latency on a held semaphore is ~0.5 s

**File:** `src/home_cctv/ingest/main_stream_grabber.py:62`
**Issue:** The docstring says "keeps SIGINT latency well under 2 s (T-03-05)." That's correct — 0.5 s is well under 2 s. The module constant could be a class attribute so callers/tests can override it without monkey-patching, but this is nice-to-have.
**Fix:** None in this phase.

### IN-08: `_CameraIdDefault` filter is applied to handlers but NOT to the adapter's own records

**File:** `src/home_cctv/obs/logging_setup.py:40-48`
**Issue:** The `_CameraIdDefault` filter injects `camera_id="main"` on any record that lacks it. It's applied to the file handler and the stream handler, but NOT to the logger itself. This means if the root logger gets a record from an unrelated library (e.g., `pydantic`'s internal logger) it would need the formatter to tolerate the missing key. The formatter uses `%(camera_id)s` which would KeyError — but the filter runs BEFORE formatting, so this is fine in practice.
**Fix:** None. Just noting for maintainers.

---

_Reviewed: 2026-04-17T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
