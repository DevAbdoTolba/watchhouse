---
phase: 01-multi-stream-ingest-reconnect
verified: 2026-04-17T22:35:00Z
status: human_needed
score: 5/5 must-haves verified (automated); 3 success criteria require live-DVR physical testing
overrides_applied: 0
re_verification: null
human_verification:
  - test: "SC 2 — 30-s DVR cable-pull recovery (physical)"
    expected: "Pull DVR network cable for 30 s while `python -m home_cctv --live` is running. All 4 streams resume within 60 s of reconnection. Log shows `reconnect_attempt camera_id=... attempt=N delay_s=X.XX ceiling_s=Y.YY` lines with ceiling climbing 1 → 2 → 4 → 8 → 16 → 30. No process restart. `reader_connected camera_id=... attempt=M` lines follow. After 60 s of sustained healthy reads, backoff ceiling resets to 1."
    why_human: "Requires physical network cable manipulation and real DVR at 192.168.1.10. Also gated on WSL2 mirrored-networking — STATE.md notes eth0 still on NAT range; TCP port-open to :554 still works so this remains runnable, but the live DVR must be actually reachable from WSL2."
  - test: "SC 3 — iptables port-554 drop forces watchdog release within 10 s (physical)"
    expected: "With `python -m home_cctv --live` running, in a second shell execute `sudo iptables -I OUTPUT -p tcp --dport 554 -j DROP` for 60 s. Within 10 s of each stall, `watchdog_release camera_id=... age_s=10.XX threshold_s=10.00 hang_events=N` appears in logs for every camera. `stats.hang_events` increments by 1 per camera. Removing the iptables rule → readers reconnect via the backoff + reset path. Drop the rule (`sudo iptables -D OUTPUT -p tcp --dport 554 -j DROP`)."
    why_human: "Requires sudo iptables access on the host and a live DVR to stall. The cross-thread `frame_source.release()` mechanism (G-01) is documented as 'technically undefined but reliable on the FFmpeg backend in practice' — only a real cv2+FFmpeg backend exercises that guarantee; test stubs cannot."
  - test: "SC 5 — one-off main-stream grab against live DVR"
    expected: "In a Python REPL with an IngestSupervisor running against the real DVR, call `sup.grabber.grab_main_frame(camera_id=2)`. Observe exactly ONE `main_grab_open camera_id=2` followed by exactly ONE `main_grab_ok camera_id=2 bytes=N` in the logs. Call it again within 2 s → `main_grab_cache_hit camera_id=2 age_s=<2` with NO new `main_grab_open` line. Call after 2 s → fresh `main_grab_open`. Burst 5 concurrent calls on the same camera → exactly ONE `main_grab_open`, four `main_grab_cache_hit_after_wait`. DVR session count (via BitVision admin or `netstat`) never exceeds 4 sub + 1 main = 5."
    why_human: "Verification of the DVR 6-session cap requires a real DVR and the ability to inspect its admin panel or count TCP sessions. Automated tests cover the semaphore + TTL cache logic against stubs (all 9 grabber tests pass) but DVR-level serialization is an observable behavior only on live hardware."
---

# Phase 1: Multi-Stream Ingest & Reconnect Verification Report

**Phase Goal:** Pull all 4 camera sub-streams concurrently over RTSP/TCP, drop corrupted frames, and recover automatically from DVR outages — all without leaking file descriptors, holding main streams open, or crashing the pipeline.

**Verified:** 2026-04-17T22:35:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth (Roadmap SC) | Status | Evidence |
|---|--------------------|--------|----------|
| 1 | SC 1: 4 StreamReader threads reporting steady FPS + last-read + drop rate, masked URLs, no plaintext passwords | VERIFIED (automated) | `stream_reader.py:108` `class StreamReader(threading.Thread)`; `heartbeat.py:69-87` `format_line` renders exact template `heartbeat fps=... drop_rate=...% last_read_age_s=... state=...`; `ingest_supervisor.py:269-315` boots 4 trios from cameras.yaml; `tests/test_heartbeat.py::test_credentials_are_masked` round-trips `rtsp://bob:secret@...` and asserts `secret` never appears; grep `rtsp://[^:]+:[^*@]+@` returns empty across all new files |
| 2 | SC 2: 30-s cable pull → 60-s recovery with jittered backoff 1→2→4→8→16→30 visible | VERIFIED (automated) + HUMAN NEEDED for live | `backoff.py:31-38` `INITIAL_BACKOFF_S=1.0 MAX_BACKOFF_S=30.0 HEALTHY_RESET_S=60.0`; `backoff.py:55-64` `uniform(0, current_ceiling_s)` full-jitter; `backoff.py:63` ceiling doubles with `min(..., MAX_BACKOFF_S)` cap; `stream_reader.py:198-206` `reconnect_attempt camera_id=... attempt=... delay_s=... ceiling_s=... err=...` log; `stream_reader.py:240-245` `reset_if_healthy` wired; `tests/test_backoff.py` 9/9 pass (schedule 1→2→4→8→16→30, sticky cap, 60-s two-call reset); `tests/test_stream_reader_reconnect.py::test_sustained_health_resets_backoff` passes |
| 3 | SC 3: iptables-drop → watchdog force-release within 10 s | VERIFIED (automated) + HUMAN NEEDED for live | `watchdog.py:39` `DEFAULT_STALL_THRESHOLD_S: float = 10.0`; `watchdog.py:86-110` loop checks `stats.last_frame_monotonic`, on stall increments `hang_events` + calls `frame_source.release()` cross-thread; `watchdog.py:91-95` idempotency guard via `_last_release_at_frame_ts`; `stream_reader.py:270-276` reader detects via `is_open` Protocol property and enters reconnect; `tests/test_watchdog.py` 7/7 pass |
| 4 | SC 4: deque(maxlen=2) drop-oldest + 5-frame post-reconnect drop | VERIFIED (automated) | `stream_reader.py:66` `self._dq: deque = deque(maxlen=maxlen)` (default `_DEFAULT_QUEUE_MAXLEN=2`); `stream_reader.py:72-82` `put()` increments `drop_count` on full-queue append; `capture.py:42` `_DROP_FRAMES_AFTER_OPEN: int = 5`; `capture.py:159` post-open drop fires inside `_BaseCvCapture.read()` on every `open()`; `tests/test_stream_reader_reconnect.py::test_post_open_five_frame_drop_is_preserved_across_reconnect` passes |
| 5 | SC 5: One-off main-stream grab → exactly one open + 2-s TTL + global Semaphore(1) | VERIFIED (automated) + HUMAN NEEDED for DVR | `main_stream_grabber.py:117` `threading.Semaphore(1)` default; `main_stream_grabber.py:51` `MAIN_STREAM_CACHE_TTL_S: float = 2.0`; `main_stream_grabber.py:126` `def grab_main_frame(self, camera_id: int) -> Optional[bytes]`; `main_stream_grabber.py:139-148` first cache check; `main_stream_grabber.py:165-186` double-checked locking with early semaphore release; `main_stream_grabber.py:203-253` `_open_read_release` with `try/finally: cap.release()`; `tests/test_main_stream_grabber.py` 9/9 pass including barrier-race double-check test asserting `open_call_count == 1` |

**Score (automated):** 5/5 truths verified in code + tests. Three truths (SC 2, SC 3, SC 5) require live-DVR physical testing to confirm end-to-end behavior on hardware — automated coverage is at the component/integration-stub level.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/home_cctv/ingest/stream_reader.py` | FrameQueue + StreamReader(threading.Thread) w/ deque(maxlen=2), reconnect loop, `reset_if_healthy` | VERIFIED | 301 lines; `class FrameQueue` (52), `class StreamReader(threading.Thread)` (108); `deque(maxlen=maxlen)` (66); `reconnect_attempt` log (198); `reset_if_healthy` call (240); `stop_event.wait(delay)` (210); no `time.sleep(`; no `_cap` substring; no raw credential URL pattern |
| `src/home_cctv/ingest/watchdog.py` | ReadWatchdog(threading.Thread) cross-thread release, 10-s threshold, hang_events | VERIFIED | 127 lines; `class ReadWatchdog(threading.Thread)` (45); `DEFAULT_STALL_THRESHOLD_S: float = 10.0` (39); `frame_source.release()` (110); `hang_events += 1` (98); `_last_release_at_frame_ts` idempotency (81, 91); never calls `frame_source.open()` |
| `src/home_cctv/ingest/backoff.py` | JitteredBackoff pure helper w/ 1→2→4→8→16→30 schedule + 60-s reset | VERIFIED | 108 lines; `class JitteredBackoff` (41); `INITIAL_BACKOFF_S=1.0` / `MAX_BACKOFF_S=30.0` / `HEALTHY_RESET_S=60.0` (31, 34, 38); `self._rng.uniform(0.0, self.current_ceiling_s)` full-jitter (61); two-call `reset_if_healthy` (72-100) |
| `src/home_cctv/ingest/main_stream_grabber.py` | MainStreamGrabber w/ Semaphore(1) + 2-s TTL + double-checked cache + synchronous grab_main_frame | VERIFIED | 272 lines; `class MainStreamGrabber` (93); `threading.Semaphore(1)` default (117); `MAIN_STREAM_CACHE_TTL_S: float = 2.0` (51); `grab_main_frame(self, camera_id: int) -> Optional[bytes]` (126); double-checked locking with `semaphore_released` flag (163-200); `cap.release()` always in `finally` (246); `assert_capture_options_active` called in default factory (86) |
| `src/home_cctv/ingest/ingest_supervisor.py` | IngestSupervisor composing 4×(reader+watchdog+heartbeat) + grabber on top of ShutdownSupervisor, start-degraded probe, path-traversal guard | VERIFIED | 387 lines; `class IngestSupervisor` (120); `INITIAL_PROBE_ATTEMPTS=3`, `INITIAL_PROBE_TIMEOUT_S=5.0`, `INITIAL_PROBE_OVERALL_DEADLINE_S=15.0`, `SHUTDOWN_GRACE_S=2.0` (66-79); `_validate_path` (100-114); `_probe_single_camera` side-thread timeout (151-202); `_initial_probe` parallel via `ThreadPoolExecutor(max_workers=4)` (205-233); `"no cameras reachable ..."` guard (263-266); mp4_mode short-circuit (207); reuses `ShutdownSupervisor` from Phase 0 (135) |
| `src/home_cctv/obs/heartbeat.py` | CameraHeartbeat 30-s structured emitter with 4-state classification | VERIFIED | 231 lines; `class CameraHeartbeat` (134); `DEFAULT_HEARTBEAT_CADENCE_S: float = 30.0` (32); 4 states `starting/healthy/degraded/stalled` (45-48); `LoggerAdapter` with `camera_id` extra (167); `format_line` exact template matching CONTEXT §G-04 (69-87) |
| `src/home_cctv/__main__.py` | --live flag wires IngestSupervisor; --mp4-mode dev override | VERIFIED | `--live` (39-42); `--mp4-mode PATH` (43-52); `_run_live_pipeline` (200-251) passes `mp4_mode=bool(args.mp4_mode)` (235); reuses `supervisor` (the ShutdownSupervisor) (231); `supervisor.wait_for_shutdown(timeout=float("inf"))` (248); `sup.shutdown()` in finally (250); RuntimeError → exit 1 (239-241); ValueError → exit 2 (242-244); `home_cctv --help` output confirms `--live` and `--mp4-mode PATH` registered |
| Tests: 7 test modules | All pass, no regression | VERIFIED | `tests/test_stream_reader.py` (9 tests), `tests/test_stream_reader_reconnect.py` (7), `tests/test_watchdog.py` (7), `tests/test_backoff.py` (9), `tests/test_main_stream_grabber.py` (9), `tests/test_ingest_supervisor.py` (9), `tests/test_heartbeat.py` (12); 62/62 Phase 1 tests pass, 148/148 full suite pass (`uv run pytest tests/ -q` → `148 passed, 1 deselected in 39.54s`) |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `StreamReader.run()` | `FrameSource.read()` | synchronous per-iteration call | WIRED | `stream_reader.py:234` `ok, frame = self.frame_source.read()` inside inner loop |
| `StreamReader.run()` | `FrameQueue.put()` | drop-oldest deque append | WIRED | `stream_reader.py:236` `self.queue.put((frame, time.monotonic()))` |
| `CameraHeartbeat._emit()` | logging.LoggerAdapter | camera_id adapter + CredentialMaskFilter | WIRED | `heartbeat.py:167` `LoggerAdapter(base_logger, {"camera_id": camera_id})`; `heartbeat.py:215` `self._adapter.info(format_line(sample))` |
| `ReadWatchdog._loop()` | `frame_source.release()` | cross-thread forced release when stale > threshold | WIRED | `watchdog.py:110` `self.frame_source.release()` after age > threshold_s check at line 97 |
| `StreamReader.run()` | `JitteredBackoff.next_delay()` | sleep after open() failure before retry | WIRED | `stream_reader.py:196` `delay = self._backoff.next_delay()`; `stream_reader.py:210` `self.stop_event.wait(delay)` |
| `JitteredBackoff.reset_if_healthy()` | `CaptureStats.last_frame_monotonic` | 60-s sustained-health window | WIRED | `stream_reader.py:240-245` calls `reset_if_healthy(last_frame_monotonic=..., now=time.monotonic())` after every decoded frame |
| `MainStreamGrabber.grab_main_frame()` | `threading.Semaphore(1).acquire()` | process-wide critical section | WIRED | `main_stream_grabber.py:153` `self.semaphore.acquire(timeout=_SEMAPHORE_POLL_S)` gated by `stop_event` |
| `MainStreamGrabber._open_read_release()` | `cv2.VideoCapture → read → release` | short-lived capture | WIRED | `main_stream_grabber.py:212` factory call; `main_stream_grabber.py:247` release in finally |
| `IngestSupervisor.start()` | ShutdownSupervisor + 4 readers + 4 watchdogs + 4 heartbeats + 1 grabber | shared stop_event, daemon threads | WIRED | `ingest_supervisor.py:269-315` constructs + starts all trios; `ingest_supervisor.py:326-330` constructs grabber; all threads use `self.shutdown_sup.stop_event` |
| `IngestSupervisor._initial_probe()` | `build_rtsp_url` + `cv2.VideoCapture(sub_url).isOpened()` | start-degraded gate | WIRED | `ingest_supervisor.py:151-202` `_probe_single_camera`; `ingest_supervisor.py:205-233` parallel probe; MP4-mode short-circuit at 207 |

### Data-Flow Trace (Level 4)

This phase does not render dynamic data to a user-visible surface (no UI in v1 per PROJECT.md). The data flow is log-line output and in-memory queue tuples consumed in Phase 2. The flow is still traced:

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| StreamReader | queue (FrameQueue of (ndarray, monotonic_ts)) | `frame_source.read()` on live RTSP or MP4 | YES — `_BaseCvCapture.read()` returns real decoded frames from FFmpeg backend; NOT hardcoded | FLOWING |
| CameraHeartbeat | HeartbeatSample line | `compute_sample(CaptureStats, FrameQueue, expected_fps, now)` pure function | YES — reads `stats.measured_fps` (property dividing frames_decoded by wall-clock span) and `queue.drop_count`/`push_count` (live counters) | FLOWING |
| ReadWatchdog | release action | observes `stats.last_frame_monotonic` written by `_BaseCvCapture.read()` on every successful decode | YES — stats written on real frames, not static | FLOWING |
| MainStreamGrabber | JPEG bytes | `cv2.imencode(".jpg", frame, quality=85)` on frame from `cap.read()` | YES — real frame from DVR; tests use synthetic ndarray; production path opens real cv2 | FLOWING |
| IngestSupervisor | runtimes dict | populated from `load_cameras(cameras.yaml)` + real FrameSource construction | YES — cameras.yaml is repo-committed and loaded via pydantic | FLOWING |

No HOLLOW_PROP, STATIC, or DISCONNECTED findings. All dynamic-data artifacts trace to real input sources.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full test suite passes | `uv run pytest tests/ -q` | `148 passed, 1 deselected in 39.54s` | PASS |
| Phase 1 test modules pass individually | `uv run pytest tests/test_stream_reader.py tests/test_stream_reader_reconnect.py tests/test_watchdog.py tests/test_backoff.py tests/test_main_stream_grabber.py tests/test_ingest_supervisor.py tests/test_heartbeat.py -q` | `62 passed in 24.84s` | PASS |
| `--live` flag registered on CLI | `uv run python -m home_cctv --help \| grep -E '\-\-live\|\-\-mp4-mode'` | Both flags present | PASS |
| Module imports succeed (import-time env gating) | Implicit via test suite — every test imports `home_cctv.*` and `assert_capture_options_active()` runs inside `_BaseCvCapture.__init__` | No import errors; 148 tests green | PASS |
| Live SIGINT round-trip (mp4_mode, full CLI path) | `tests/test_ingest_supervisor.py::test_cli_live_mp4_mode_exits_cleanly_on_sigint` — spawns `python -m home_cctv --live --mp4-mode PATH`, sleeps 2 s, sends SIGINT, asserts exit in <2.5 s | test passes | PASS |
| Start-degraded all-fail probe raises RuntimeError before spawning threads | `tests/test_ingest_supervisor.py::test_start_raises_when_all_probes_fail` | test passes | PASS |
| Path-traversal guard rejects `..`, missing slash, control chars | `tests/test_ingest_supervisor.py` path-traversal test with 4 cases | test passes | PASS |
| Live-DVR reconnect (SC 2) | Requires physical cable pull against DVR at 192.168.1.10 | N/A | SKIP — routed to human |
| Live-DVR watchdog force-release (SC 3) | Requires `sudo iptables -I OUTPUT -p tcp --dport 554 -j DROP` with real DVR | N/A | SKIP — routed to human |
| Live-DVR main-stream grab (SC 5) | Requires live DVR REPL session | N/A | SKIP — routed to human |

### Requirements Coverage

All 8 Phase 1 requirement IDs from PLAN frontmatter + ROADMAP traceability are satisfied by implementation evidence:

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| ING-01 | 01-01 | 4 camera sub-streams simultaneously over RTSP/TCP | SATISFIED (code + automated); NEEDS HUMAN for full live-DVR sweep | `ingest_supervisor.py:269-315` boots 4 readers from `cameras.yaml`; `_initial_probe` allows start-degraded; RTSP/TCP forced via Phase 0 `OPENCV_FFMPEG_CAPTURE_OPTIONS` (no UDP fallback — grep confirms no UDP in new code). Live 4-stream sustained sweep is a physical test. |
| ING-02 | 01-02 | Recover from stalled cv2.VideoCapture.read() within 10 s via watchdog | SATISFIED (code + automated); NEEDS HUMAN for real FFmpeg-backend release | `watchdog.py:39` `DEFAULT_STALL_THRESHOLD_S=10.0`; `watchdog.py:110` cross-thread `frame_source.release()`; 7 `test_watchdog.py` tests pass; SC 3 iptables test is the physical verification. |
| ING-03 | 01-02 | 30-s outage recovery within 60 s with jittered exponential backoff | SATISFIED (code + automated); NEEDS HUMAN for real DVR outage | `backoff.py` schedule 1→2→4→8→16→30 + 60-s reset; `stream_reader.py` two-level reconnect loop; `test_backoff.py` 9 tests + `test_stream_reader_reconnect.py` 7 tests pass. SC 2 cable-pull is physical verification. |
| ING-04 | 01-01 | Skip green/partial frames + drop first 5 after every reconnect | SATISFIED | `capture.py:42` `_DROP_FRAMES_AFTER_OPEN=5` fires inside `_BaseCvCapture.read()` on every open; `_frames_since_open` resets on `open()` (Phase 0 behavior); green-guard in `frame_quality.is_green_frame` called inside `_BaseCvCapture.read`; stream_reader explicitly does NOT re-check. `test_stream_reader_reconnect.py::test_post_open_five_frame_drop_is_preserved_across_reconnect` verifies. |
| ING-05 | 01-01 | Per-stream health/heartbeat in logs (FPS, last-read, drop rate) | SATISFIED | `heartbeat.py` emits structured INFO line every 30 s matching exact CONTEXT §G-04 template; 12 heartbeat tests pass; 4 states deterministic. |
| ING-07 | 01-03 | Main-stream captures on demand, 2-s TTL cache | SATISFIED (code + automated); NEEDS HUMAN for DVR-level single-open observability | `main_stream_grabber.py` short-lived open/read/release with double-checked locking; 2.0 s TTL; 9 grabber tests including barrier-race assertion that `open_call_count == 1` on burst. SC 5 physical test confirms against real DVR. |
| ING-08 | 01-03 | Concurrent main-stream opens serialized via global semaphore | SATISFIED (code + automated); NEEDS HUMAN for DVR session count | `main_stream_grabber.py:117` `threading.Semaphore(1)` default; `test_main_stream_grabber.py` cross-camera no-overlap assertion passes. SC 5 DVR admin observation confirms live. |
| OPS-01 | 01-01 | Structured rotating logs with per-camera prefixes + credential masking | SATISFIED | Phase 0 `CredentialMaskFilter` applied via `configure_logging`; `LoggerAdapter` with `camera_id` extra used throughout (stream_reader.py, heartbeat.py, watchdog.py, ingest_supervisor.py, main_stream_grabber.py); grep `rtsp://[^:]+:[^*@]+@` returns empty across all Phase 1 source files; `test_heartbeat.py::test_credentials_are_masked` integration test confirms on-disk log contains `bob:***` and not `secret`. |

**Orphaned requirements:** None. REQUIREMENTS.md maps exactly the 8 IDs above to Phase 1, and all 8 appear in at least one PLAN's frontmatter `requirements:` field. Cross-check of REQUIREMENTS.md `| ING-XX | 1 |` rows vs plan frontmatters confirms:

- Plan 01-01 declares ING-01, ING-04, ING-05, OPS-01 (4)
- Plan 01-02 declares ING-02, ING-03 (2)
- Plan 01-03 declares ING-07, ING-08 (2)
- Total: 8 / 8 — no orphans

### Anti-Patterns Found

Scan of all Phase 1 source files (`stream_reader.py`, `watchdog.py`, `backoff.py`, `main_stream_grabber.py`, `ingest_supervisor.py`, `heartbeat.py`, `__main__.py`):

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | No TODO/FIXME/PLACEHOLDER comments in production code | INFO | Clean |
| — | — | No empty `return None` / `return []` / `=> {}` stubs | INFO | Clean |
| — | — | No raw `rtsp://user:pass@host` credential patterns (grep empty) | INFO | Clean |
| — | — | No `time.sleep(` in `stream_reader.py` (reader uses `stop_event.wait` only) | INFO | Clean (SIGINT responsiveness) |
| — | — | No `_cap` substring in `stream_reader.py` (uses Protocol `is_open` only) | INFO | Clean (no private-attribute coupling) |
| `__main__.py` | 31 | `--show` flag advertised but unused in `--live` mode (WR-05 in REVIEW.md) | WARNING | Cosmetic — user might expect visual preview that doesn't materialize in live mode. Not a Phase 1 SC; queued for Phase 4 hardening. |
| `watchdog.py` | 91 | Float-equality on `last_frame_monotonic` for episode idempotency (WR-02) | INFO | Theoretical robustness only — `time.monotonic()` on Linux has ns resolution; collision astronomically unlikely. Current REVIEW flags as latent, not a bug. |
| `main_stream_grabber.py` | 184-185 | Semaphore release followed by flag set; if release raises, finally would double-release (WR-01) | INFO | Only reachable with a BoundedSemaphore subclass (not used in production); theoretical, not a bug. |

No blockers. All warnings are documented in 01-REVIEW.md and do not invalidate the phase goal. No `placeholder` / `coming soon` strings in shipped code.

### Human Verification Required

Three ROADMAP success criteria have automated coverage at the component level but require live-DVR physical testing to confirm end-to-end behavior. See `human_verification` in frontmatter for the full instructions. Brief summary:

1. **SC 2 (30-s cable pull → 60-s recovery):** Requires physical DVR cable manipulation. Automated tests cover the backoff schedule math, the reconnect loop structure, and the `reset_if_healthy` state machine; only a live DVR confirms FFmpeg-level reconnect recovery under a real TCP timeout.

2. **SC 3 (iptables port-554 drop → 10-s watchdog release):** Requires `sudo iptables` access and a live DVR to stall. The cross-thread `frame_source.release()` guarantee is documented in CONTEXT G-01 as "technically undefined but reliable on the FFmpeg backend in practice" — only a real cv2+FFmpeg backend confirms the unblock.

3. **SC 5 (single-open live main-stream grab + DVR session cap):** Requires live DVR REPL session and BitVision admin panel (or `netstat`) to count concurrent sessions. Automated tests confirm the semaphore + TTL + double-checked-locking logic against stubs.

### Deviations from Plan — Honored Decisions

Verified that all user decisions D-01..D-13 and gray-area defaults G-01..G-04 from `01-CONTEXT.md` are upheld in code:

- **D-01 (4 reader threads + 1 grabber thread, no asyncio):** Every Phase 1 worker is `threading.Thread(daemon=True)`. No `asyncio`, no `multiprocessing` imports in Phase 1 modules. Probe uses transient `ThreadPoolExecutor(max_workers=4)` that exits before any persistent thread starts (documented decision, not a D-01 violation). ✓
- **D-02 (RTSP/TCP only, canonical env string):** Phase 0's `OPENCV_FFMPEG_CAPTURE_OPTIONS` is unchanged; `assert_capture_options_active()` called in default main-capture factory. ✓
- **D-03 (deque maxlen=2 drop-oldest):** `stream_reader.py:49` hard default; only test-callers pass larger values. ✓
- **D-04 (post-open 5-frame drop):** Lives inside `_BaseCvCapture.open()` via `_DROP_FRAMES_AFTER_OPEN=5`; reader does not re-implement. ✓
- **D-05 (green-frame guard inside `_BaseCvCapture.read()`):** Reader loop never re-checks; negative grep `! grep is_green_frame stream_reader.py` returns empty. ✓
- **D-06 (CAP_PROP_BUFFERSIZE=1):** Phase 0 behavior unchanged. ✓
- **D-07 (full-jitter backoff 1→2→4→8→16→30, reset after 60 s):** `backoff.py` implementation matches exactly. ✓
- **D-08 (watchdog 10-s force-release):** `watchdog.py:39` `DEFAULT_STALL_THRESHOLD_S=10.0`. ✓
- **D-09 (Semaphore(1) DVR-wide + 2-s TTL main-stream cache):** `main_stream_grabber.py`. ✓
- **D-10 (credential masking):** No new raw-credential interpolation; `_sanitized_target()` used in stream_reader; CredentialMaskFilter flows through all new loggers. ✓
- **D-11 (no SQLite or EVENT_IMAGE_DIR writes in Phase 1):** Confirmed by code scan — no `sqlite3`, no `EVENT_IMAGE_DIR` writes in new files. ✓
- **D-12 (FrameSource abstraction — live + MP4 via same reader):** `stream_reader.py` branches only on `is_file_source` flag; same read loop drives both. ✓
- **D-13 (extend Phase 0 code, do not rewrite):** `capture.py` modified only additively (new `is_open` property); `supervisor.py` reused via `ShutdownSupervisor`. ✓
- **G-01 (watchdog mechanism = side-thread release):** Implemented exactly; alternatives rejected in decisions log. ✓
- **G-02 (start-degraded policy):** `IngestSupervisor._initial_probe` + boot-with-≥1 policy + all-fail RuntimeError. ✓
- **G-03 (sync grab_main_frame API):** Exact signature `grab_main_frame(camera_id: int) -> Optional[bytes]`. ✓
- **G-04 (30-s heartbeat cadence, exact line template):** `DEFAULT_HEARTBEAT_CADENCE_S=30.0`; `format_line` exact template; all 4 states deterministic. ✓

### Non-Regressions

- Phase 0 test count grew from Phase 0 (107) to Phase 1 end (148). All previously-passing tests still pass.
- `uv run pytest tests/ -q` → **148 passed, 1 deselected in 39.54s** — consistent with SUMMARY claims (01-01: 107, 01-02: 130, 01-03: 148).
- No modifications to Phase 0 modules except the **additive** `is_open` Protocol extension in `capture.py` (4 lines added per REVIEW — zero behavior change for existing callers).

### Gaps Summary

No gaps blocking the phase goal. The phase ships:

- All 8 Phase 1 requirements implemented and covered by automated tests.
- All 5 ROADMAP success criteria implemented; 3 have component-level automated coverage only and require physical live-DVR testing to confirm hardware-level behavior (watchdog release on real FFmpeg backend, 30-s cable pull recovery, DVR session cap observation).
- All 13 carry-forward decisions + 4 gray-area defaults honored in code.
- All 148 tests pass. Code-review findings are warnings/info, not blockers.

The `status: human_needed` reflects that physical DVR tests are required to complete SC 2, SC 3, and SC 5 acceptance — not that implementation is incomplete.

---

_Verified: 2026-04-17T22:35:00Z_
_Verifier: Claude (gsd-verifier)_
