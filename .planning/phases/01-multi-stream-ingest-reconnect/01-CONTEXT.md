# Phase 1: Multi-Stream Ingest & Reconnect — Context

**Gathered:** 2026-04-17
**Status:** Ready for planning
**Mode:** User opted to skip interactive discussion — planner has discretion on all gray areas listed below, grounded by carry-forward decisions from Phase 0 and PROJECT/SUMMARY.

<domain>
## Phase Boundary

Pull all 4 camera sub-streams concurrently over RTSP/TCP, drop corrupted frames, recover automatically from DVR outages, and expose on-demand main-stream grabs — all without leaking file descriptors, holding main streams open, or crashing the pipeline.

**In scope:** ING-01, ING-02, ING-03, ING-04, ING-05, ING-07, ING-08, OPS-01
**Not in scope:** YOLO inference, ByteTrack, zones, any event/DB writes (Phase 2), triggers and DeepFace/EasyOCR (Phase 3), retention/metrics.json/supervisor respawn (Phase 4).

</domain>

<decisions>
## Implementation Decisions

### Carry-forward (locked by prior phases, PROJECT.md, SUMMARY.md)

- **D-01 — Concurrency model:** 4 per-camera reader threads + 1 main-stream grabber thread. No asyncio, no multiprocessing for the reader path. (SUMMARY §2, §8; Decisions Log 2026-04-13)
- **D-02 — Transport:** RTSP/TCP only. Canonical `OPENCV_FFMPEG_CAPTURE_OPTIONS` is already shipped and asserted at import via `home_cctv/ingest/flags.py::assert_capture_options_active()`. Phase 1 reuses it unchanged:
  ```
  rtsp_transport;tcp|fflags;nobuffer+discardcorrupt|flags;low_delay|err_detect;ignore_err|stimeout;5000000|reconnect;1|reconnect_streamed;1|reconnect_delay_max;2|analyzeduration;1000000|probesize;2000000
  ```
- **D-03 — Per-camera queue:** `collections.deque(maxlen=2)` with drop-oldest semantics — the slow inference path (Phase 2) never back-pressures a reader.
- **D-04 — Post-open/reconnect drop:** First 5 frames after every `open()` are dropped (already implemented in `RtspFrameSource` via `_DROP_FRAMES_AFTER_OPEN=5`). Reconnect must reuse the same guard.
- **D-05 — Green-frame guard:** `frame_quality.is_green_frame` is already applied inside `_BaseCvCapture.read()`. Reader loop does not re-check.
- **D-06 — Buffer size:** `cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)` already applied on every open.
- **D-07 — Backoff policy (ING-03):** Jittered exponential backoff per camera — 1 s → 2 s → 4 s → 8 s → 16 s → 30 s (cap). Full-jitter formula (`random.uniform(0, current)`) preferred over equal-jitter; reset to 1 s after N seconds of sustained healthy reads (planner picks N — suggest 60 s).
- **D-08 — Watchdog requirement (ING-02):** Every stuck `cap.read()` must be force-released within 10 s. `stimeout=5000000` (5 s socket timeout) is the first line; a per-camera watchdog is the second line. **Planner's discretion on mechanism** — see Gray Areas below.
- **D-09 — Main-stream on demand (ING-07/08):** Every main-stream frame is a short-lived `VideoCapture.open → read → release`. Global semaphore with `value=1` across all cameras (a single concurrent main-stream open, DVR-wide). 2-second TTL cache keyed by `camera_id` suppresses duplicate opens during burst triggers.
- **D-10 — Credential masking (OPS-01):** Logging filter already converts `rtsp://user:pass@host` → `rtsp://user:***@host`. Phase 1 adds nothing new; new log statements must only use the sanitized target (`RtspFrameSource._sanitized_target()`).
- **D-11 — Filesystem:** DB and `EVENT_IMAGE_DIR` live on WSL2 ext4. Phase 1 does not write to SQLite or the event image dir (that's Phase 2); no DrvFs hazards to guard here.
- **D-12 — Frame source abstraction:** Phase 1 reader consumes a `FrameSource` (existing Protocol in `home_cctv/ingest/capture.py`). `RtspFrameSource` for live; `Mp4FrameSource` for `--mp4` regression. The same reader loop must work against both.
- **D-13 — Reader skeleton already present:** `RtspFrameSource`, `Mp4FrameSource`, `open_frame_source`, `CaptureStats`, and `supervisor.py` scaffold exist from Phase 0. Phase 1 builds the per-camera run loop, watchdog, reconnect-with-backoff, main-stream grabber, and health heartbeat **on top of** this, not from scratch.

### Planner's discretion (gray areas — user skipped discussion)

- **G-01 — Watchdog mechanism.** How the per-camera watchdog force-releases a stuck `cap.read()` in 10 s. Recommended default: side-thread close (watchdog monitors `CaptureStats.last_frame_monotonic`; if `now - last_frame_monotonic > 10 s`, call `cap.release()` from the watchdog thread — the blocked `read()` in the reader thread then returns `(False, None)` and enters reconnect). Reasons: pure-thread model (D-01), zero IPC, reuses existing `RtspFrameSource`. Known risk: cross-thread `cap.release()` while `read()` is active is technically undefined but reliable on the FFmpeg backend in practice. Alternatives the planner may adopt if deemed safer: wrap `cap.read()` in a `ThreadPoolExecutor.submit(...).result(timeout=1.0)` with capture abandoned on timeout; or switch to process-per-camera (contradicts D-01 and needs sign-off from user before adoption).
- **G-02 — Startup failure policy.** If any camera is unreachable at launch, what happens? Recommended default: start-degraded. Pipeline boots with all healthy cameras running; any unreachable camera enters its normal reconnect loop from t=0. A startup banner lists each camera's initial status. Pipeline only refuses to start if **all 4** cameras are unreachable after a brief initial probe (e.g. 3 attempts with short backoff). Reason: mid-run outages must already be survivable (ING-03); enforcing all-up at startup would contradict that design.
- **G-03 — Main-stream grab API shape.** How Phase 3 will call into the main-stream grabber. Recommended default: a synchronous function `grab_main_frame(camera_id: int) -> bytes | None` on the MainStreamGrabber that internally serializes through the D-09 semaphore and checks the TTL cache. Caller blocks for the duration (usually <3 s). Avoid a queue/Future-based API unless the planner identifies a specific Phase 3 requirement for non-blocking requests. A single dedicated MainStreamGrabber thread/worker owns the semaphore and cache state; all callers enter it via the sync function.
- **G-04 — Health heartbeat surface + cadence (ING-05).** Recommended default: every 30 s, each camera logs one structured heartbeat line to the rotating log:
  ```
  INFO [cam1:exterior_red] heartbeat fps=14.9 frames_decoded=N decode_errors=M drop_rate=X.Y% last_read_age_s=0.07 state=healthy
  ```
  Phase 1 does **not** write `metrics.json` (that's OPS-02, Phase 4). Do stub a `home_cctv/obs/heartbeat.py` (or equivalent) so Phase 4 can lift the per-camera snapshot into the metrics JSON without refactoring the reader.

### Claude's Discretion

Everything in the "Planner's discretion" block above is explicitly Claude's call at plan time, with the user reviewing the plan before execution.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents (researcher, planner) MUST read these before producing PLAN.md.**

### Source truth for requirements and decisions
- `.planning/PROJECT.md` — core value, constraints (GPU pivot 2026-04-16; dashboard deferred)
- `.planning/REQUIREMENTS.md` §Stream Ingest (ING-01..08) and §Operations (OPS-01) — exactly the 8 reqs scoped to Phase 1
- `.planning/ROADMAP.md` Phase 1 block — success criteria 1–5 are the acceptance test surface
- `.planning/STATE.md` — carries forward Phase 0 completion notes

### Research synthesis
- `.planning/research/SUMMARY.md` §2 (stack pins), §4 (ingest/reconnect strategy), §6 Q5 (reconnect watchdog design), §8 (process/thread model)
- `.planning/research/STACK.md` — pinned versions (unchanged from Phase 0)
- `.planning/research/PITFALLS.md` — §1.1 OPENCV_FFMPEG_CAPTURE_OPTIONS must be set pre-`import cv2`; §1.3 post-open green burst; §1.5 stuck `cap.read()` on WSL2; §2.x reconnect pitfalls
- `.planning/research/ARCHITECTURE.md` — thread model, queue policies, main-stream grabber design

### Phase 0 artifacts (ground truth for Phase 1 reader behavior)
- `.planning/phases/00-environment-sanity/00-CONTEXT.md` — locked decisions (packaging, env-string format, `FrameSource` abstraction)
- `.planning/phases/00-environment-sanity/PHASE0-REPORT.json` — real measured sub-stream FPS (15/6/6/15), DVR session cap (≥6), RTSP handshake latency (3415 ms mean on current NAT), decode-error baseline per camera
- `.planning/phases/00-environment-sanity/00-99-phase0-patch-SUMMARY.md` — patched exit criteria, env-var reporting fix, wsl_version decode fix

### Hardware ground truth
- `cameras.txt` — per-camera hardware profile, H.265 anomalies, NAL-unit-0 notes
- `cameras.yaml` — authoritative per-camera config consumed by `home_cctv/config/cameras.py`; Phase 1 reads `sub_path` + `sub_stream_fps` per camera (NOT `main_stream_fps` for continuous reading)
- `terminal.txt` — ground-truth `ffplay` flag strings, basis for `OPENCV_FFMPEG_CAPTURE_OPTIONS`

### Code already in tree (Phase 0 deliverables, Phase 1 extends)
- `src/home_cctv/ingest/flags.py` — canonical env-string builder + `assert_capture_options_active()`
- `src/home_cctv/ingest/capture.py` — `FrameSource` Protocol, `RtspFrameSource`, `Mp4FrameSource`, `open_frame_source()`, `CaptureStats` (Phase 1 extends this, **does not** rewrite)
- `src/home_cctv/ingest/frame_quality.py` — `is_green_frame()` (called inside `_BaseCvCapture.read()`)
- `src/home_cctv/ingest/supervisor.py` — Phase 0 supervisor scaffold (Phase 1 extends into multi-camera + main-stream-grabber supervisor)
- `src/home_cctv/ingest/display.py` — `--show` window, unchanged in Phase 1
- `src/home_cctv/config/env.py` — `.env` loader + pydantic settings (credentials already masked)
- `src/home_cctv/config/cameras.py` — `cameras.yaml` loader (Phase 1 reads all 4 cameras from it)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets

- **`RtspFrameSource` + `Mp4FrameSource`** (`ingest/capture.py`): both implement the same `FrameSource` Protocol; `cap.set(CAP_PROP_BUFFERSIZE, 1)`, post-open frame drop, green-frame skip, and credential masking are **already inside** `_BaseCvCapture`. Phase 1 per-camera loop wraps this — it does not reimplement `read()`.
- **`CaptureStats`** (same file): carries `frames_decoded`, `frames_corrupted`, `decode_errors`, `hang_events`, `first_frame_monotonic`, `last_frame_monotonic`, `measured_fps`. The watchdog reads `last_frame_monotonic` to detect stuck reads; the heartbeat reads all of it for the log line.
- **`assert_capture_options_active()`** (`ingest/flags.py`): gate invoked from every `_BaseCvCapture.__init__`. Phase 1 adds nothing; accidental env-var reorders already crash at source construction.
- **`is_green_frame`** (`ingest/frame_quality.py`): applied inside `_BaseCvCapture.read()`. Reader loop sees only clean frames or `(False, None)`.
- **`supervisor.py`** (Phase 0 scaffold, 3.7 KB): extends into a Phase 1 `IngestSupervisor` that owns the 4 reader threads, the main-stream grabber, and the watchdog thread(s).
- **`config/cameras.py`**: already loads `cameras.yaml` — Phase 1 reads sub_path + sub_stream_fps per camera.

### Established Patterns

- **Import-time env gating:** `OPENCV_FFMPEG_CAPTURE_OPTIONS` is set in `home_cctv/__init__.py` before any `import cv2`. All Phase 1 code stays inside the package so that invariant holds.
- **Structured logging w/ per-camera prefix:** `logging` + `RotatingFileHandler` (`max 10 MB × 5` under `logs/`) + credential-masking filter. Phase 1 heartbeat reuses the same logger namespace (`home_cctv.capture` or a new `home_cctv.ingest`).
- **`monotonic` for durations, `utcnow` for timestamps:** already the convention in `CaptureStats`.

### Integration Points

- **Reader threads produce:** per-camera `deque(maxlen=2)` of `(frame_ndarray, monotonic_ts)` — Phase 2's InferenceWorker consumes these.
- **Main-stream grabber exposes:** `grab_main_frame(camera_id)` — called by Phase 3's TriggerEvaluator. Phase 1 must lock the API signature now so Phase 3 doesn't refactor the supervisor.
- **Supervisor lifecycle:** `start() → run() → shutdown()` driven by `home_cctv/__main__.py` and a `threading.Event`. SIGINT handling already exists for Phase 0; extend to drain all 4 cameras + the grabber + the watchdog thread within 2 s (OPS-01 / OPS-04 hardens this further in Phase 4, but clean SIGINT is a Phase 1 nicety).

</code_context>

<specifics>
## Specific Ideas

- **Heartbeat every 30 s** is a soft default; if the operator complains about log volume the cadence is trivially tunable. Do not make it configurable via YAML in Phase 1 — constant in code.
- **Do not** open a main-stream during startup probe. Only sub-streams are touched outside of `grab_main_frame`.
- **WSL2 mirrored networking is still unresolved** (user attempted `.wslconfig` switch but WSL is ignoring it — eth0 stays on NAT range). This is non-blocking: TCP port-open test to `192.168.1.10:554` is 8 ms on NAT; the 3415 ms mean is RTSP-session setup, not TCP. Phase 1 proceeds on NAT; retry mirrored later.
- **Sub-stream FPS ground truth (2026-04-16 2-hour sweep):** Cam 1 = 14.97, Cam 2 = 5.99, Cam 3 = 6.00, Cam 4 = 14.98. Any Phase 1 test that asserts "measured FPS within 20%" keys against these sub-stream numbers, not the main-stream advertised rates.

</specifics>

<deferred>
## Deferred Ideas

- **`metrics.json` writer** — Phase 4 (OPS-02). Phase 1 heartbeat log line carries the same per-camera payload so the metrics writer is a reshape, not a remeasure.
- **Worker supervision / respawn** — Phase 4 (OPS-03). Phase 1 supervisor may log worker death but does not respawn.
- **Per-camera ByteTracker frame_rate wiring** — Phase 2 (reads `sub_stream_fps` from `cameras.yaml`).
- **Retention policy, emergency mode** — Phase 4 (STO-08, STO-09).
- **Revisit WSL2 mirrored-mode** — non-blocking; track in STATE.md carry-forward notes.

</deferred>

---

*Phase: 01-multi-stream-ingest-reconnect*
*Context gathered: 2026-04-17*
