# Roadmap — Home CCTV AI Pipeline (Milestone v1)

**Milestone goal:** A working local-only AI event pipeline that watches 4 RTSP streams, runs YOLOv8n+ByteTrack continuously, fires DeepFace and EasyOCR on trigger, logs events to SQLite, and survives overnight.

**Granularity:** coarse (5 phases including Phase 0)
**Coverage:** 63/63 v1 requirements mapped
**Source inputs:** `.planning/PROJECT.md`, `.planning/REQUIREMENTS.md`, `.planning/research/SUMMARY.md`

**Deferred to a later milestone:** The React dark-mode dashboard (`#151a2c`), the FastAPI read-only shim, and all UI work. The entire v1 pipeline is operator-usable via SQLite + file browser; UI is not on the critical path. Do NOT plan dashboard work in this milestone.

**Parallelization:** enabled (per config). Phase 0 is strictly sequential — it answers empirical blockers. Phases 1-4 allow parallel plan execution where plan dependencies permit.

---

## Phases

- [ ] **Phase 0: Environment & Sanity** — Verify the real host can actually reach, read, and decode one DVR stream end-to-end before any pipeline work begins
- [ ] **Phase 1: Multi-Stream Ingest & Reconnect** — Pull all 4 sub-streams in parallel over RTSP/TCP and survive DVR outages without a restart
- [ ] **Phase 2: Detection, Tracking & Zoned Events** — Run YOLO+ByteTrack continuously on all 4 streams, evaluate polygon zones, and persist events to SQLite
- [ ] **Phase 3: Trigger & Catch Event Models** — Fire DeepFace, EasyOCR, vehicle-interaction, and loitering events from tracker state using on-demand main-stream grabs
- [ ] **Phase 4: Hardening & Operations** — Retention, emergency-mode, metrics, worker supervision, offline MP4 mode, clean shutdown

---

## Phase Details

### Phase 0: Environment & Sanity

**Goal**: Answer every empirical blocker (SUMMARY.md §6 Q1-Q6) by getting a single real DVR stream end-to-end on the actual host, so the remaining phases can be built on measured ground-truth instead of assumptions.

**Depends on**: Nothing (first phase)

**Requirements**: ENV-01, ENV-02, ENV-03, ENV-04, ENV-05, ING-06

**Success Criteria** (what must be TRUE):
  1. User runs `python -m home_cctv` on Windows 11 + WSL2 Ubuntu 24.04 with Python 3.11 and the pipeline starts with a clear summary of which camera, which config file, and which env vars it loaded — all creds masked in logs and no credentials in source.
  2. User captures a single DVR sub-stream from inside WSL2 for 30 continuous minutes via the translated `OPENCV_FFMPEG_CAPTURE_OPTIONS` flags, writes a JPEG to disk every 5 seconds, and finishes with zero hangs, zero green frames, and a measured steady-state FPS printed at exit.
  3. User deletes `.env`, starts the pipeline, and sees a clear error naming the missing variable (and a second clear error when `EVENT_IMAGE_DIR` has <1 GB free or when OpenCV lacks the FFmpeg backend) — the process refuses to start instead of half-running.
  4. User presses Ctrl+C mid-capture and the process exits within 2 seconds with the capture released, the log file flushed, and no orphan ffmpeg subprocess.
  5. User points the same binary at a local MP4 via `--mp4 path/to/file.mp4` and the same capture-and-save loop runs against the file — proving the RTSP and file paths share the same frame-source abstraction from day one.

**Empirical questions answered in this phase (gating):**
- Q1 real-host YOLO/OpenVINO baseline FPS (measurement harness committed)
- Q2 WSL2 mirrored networking confirmed reachable to `192.168.1.10:554`
- Q3 `cv2.getBuildInformation()` shows `FFMPEG: YES` (startup assert)
- Q4 `terminal.txt` ffplay flags translated to OpenCV env string, verified against a 30-min capture
- Q5 forced `iptables`-drop reconnect test design sketched (implemented fully in Phase 1)
- Q6 DVR concurrent connection cap measured by stepping 1→2→…→N sessions

**Plans**: TBD
**UI hint**: no

---

### Phase 1: Multi-Stream Ingest & Reconnect

**Goal**: Pull all 4 camera sub-streams concurrently over RTSP/TCP, drop corrupted frames, and recover automatically from DVR outages — all without leaking file descriptors, holding main streams open, or crashing the pipeline.

**Depends on**: Phase 0 (FFmpeg env string and host baseline must be known)

**Requirements**: ING-01, ING-02, ING-03, ING-04, ING-05, ING-07, ING-08, OPS-01

**Success Criteria** (what must be TRUE):
  1. User starts the pipeline and sees 4 `StreamReader` threads each reporting steady FPS, last-read timestamp, and drop rate — masked RTSP URLs only, no plaintext passwords in any log line.
  2. User pulls the DVR network cable for 30 seconds and all 4 streams resume within 60 seconds of reconnection, with jittered exponential backoff visible in the log (1s → 2s → 4s → 8s → …), and with no process restart required.
  3. User blocks port 554 with `iptables` for 60 seconds and every stuck `cap.read()` is force-released by its watchdog within 10 seconds — not hanging forever the way bare `cv2.VideoCapture` does on WSL2.
  4. User inspects the live `cam_queue[i]` state and sees drop-oldest semantics on `deque(maxlen=2)` — the slow inference path never back-pressures a stream reader, and the first 5 frames after every reconnect are skipped to drop green/partial slices.
  5. User issues a one-off "grab main-stream frame for camera 2" request and sees exactly one short-lived `VideoCapture` open on the main stream, one JPEG saved, one release, with a 2-second TTL cache preventing double-opens — and a global semaphore proving only one main-stream open at a time across all cameras, so the DVR connection cap is never tripped.

**Plans**: 3 plans
- [ ] 01-01-PLAN.md — StreamReader threads + FrameQueue(maxlen=2) drop-oldest + CameraHeartbeat 30s structured emitter (ING-01, ING-04, ING-05, OPS-01)
- [ ] 01-02-PLAN.md — JitteredBackoff + ReadWatchdog (cross-thread release) + StreamReader reconnect loop (ING-02, ING-03)
- [ ] 01-03-PLAN.md — MainStreamGrabber (Semaphore(1) + 2s TTL) + IngestSupervisor composing 4 readers/watchdogs/heartbeats + grabber + --live CLI (ING-07, ING-08)
**UI hint**: no

---

### Phase 2: Detection, Tracking & Zoned Events

**Goal**: Run a single OpenVINO-accelerated YOLO inference worker over all 4 streams with per-camera ByteTrack state, evaluate polygon zones every frame, and persist `Person_Detected` / `Car_Detected` / `Zone_Enter` events to a properly-tuned SQLite `EventLog` with atomic image writes — the first end-to-end "interesting events land in the DB" phase.

**Depends on**: Phase 1 (needs 4 live sub-streams + on-demand main-stream grabs + bounded cam queues)

**Requirements**: TRK-01, TRK-02, TRK-03, TRK-04, TRK-05, TRK-06, TRK-07, TRK-08, TRK-09, ZON-01, ZON-02, ZON-03, ZON-04, STO-01, STO-02, STO-03, STO-04, STO-05, STO-06, STO-07, STO-10, ING-09, ING-10, ING-11

**Success Criteria** (what must be TRUE):
  1. User watches a live debug overlay on any one camera and sees persistent bounding boxes and ByteTrack IDs on every `person` and every `car/bus/truck` — with short flickers suppressed (min-track-age gating) and confidence floors + minimum bbox area rejecting noise before anything reaches the event pipeline.
  2. User runs the pipeline against all 4 sub-streams for 1 continuous hour and sees a single InferenceWorker round-robining across the 4 cam queues at ≥8 FPS per camera sustained with CPU <80% — and monotonic global IDs (`{camera}_{session}_{byteid}_{birthframe}`) that never collide across cameras and never get reused after retirement.
  3. User runs `tools/draw_zones.py` against a live frame from each camera, clicks to define polygons, saves `zones.yaml`, and restarts the pipeline — the new zones load automatically and the tool requires no hand-typed pixel coordinates.
  4. User walks through the "Driveway" polygon once and sees exactly one `Zone_Enter` row in `$DB_PATH` and one JPEG on disk for that event — no duplicate rows from a single track (per-`(entity_id, event_type)` cooldown), no orphan image (atomic `.tmp`+rename before DB insert), and the `image_path` column stores a **relative** path that joins with `EVENT_IMAGE_DIR` at runtime.
  5. User opens the SQLite file (which lives on WSL2 ext4, not `/mnt/c/...`) and sees WAL mode enabled, `synchronous=NORMAL`, the `EventLog` schema from PROJECT.md with UTC ISO-8601 timestamps, indexes on `(timestamp, camera_id, event_type, entity_id, zone_name)`, and zero `database is locked` errors under a 20-events/sec burst — because a single EventWriter thread is the only writer.
  6. User runs `python -m home_cctv --ingest-file yesterday_morning.mp4` against a recorded video whose camera-clock overlay reads `2026-04-13 07:14:22`, and the resulting `Zone_Enter` rows land in `EventLog` at timestamps `2026-04-13T07:14:22Z+N` — NOT at `now()` — via a `FileTimeSource` that reads the overlay via small-ROI OCR (fallback: file mtime, then filename parse, then hard error). Re-ingesting the same file produces zero duplicate rows because the dedup key `(camera_id, event_type, entity_id, timestamp ± 5s)` matches the existing rows.
  7. User runs `python -m home_cctv --analyze-file experiment.mp4 --isolated` and the resulting events are tagged with a unique `session_id` (or written to a sandbox table, per STO-10 implementation), so the main event log stays untouched. User can query sandbox events separately, drop them on demand, and the production `EventLog` is unaffected.

**Plans**: TBD
**UI hint**: no

---

### Phase 3: Trigger & Catch Event Models

**Goal**: Fire DeepFace (on person bbox peak), EasyOCR (on parked-car velocity ≈ 0), vehicle-interaction (Entered/Exited), and loitering events using tracker state — all via a TriggerEvaluator that pulls exactly one main-stream frame per event, routed to isolated pre-warmed face and plate subprocesses, and writes `Face_Recognized` / `Face_Unknown` / `Plate_Read` / `Entered_Vehicle` / `Exited_Vehicle` / `Loitering` rows.

**Depends on**: Phase 2 (needs stable tracker IDs, velocity+area history, zone state, and a working EventWriter)

**Requirements**: TRG-01, TRG-02, TRG-03, TRG-04, TRG-05, TRG-06, TRG-07, FAC-01, FAC-02, FAC-03, FAC-04, FAC-05, FAC-06, FAC-07, ALP-01, ALP-02, ALP-03, ALP-04, ALP-05

**Success Criteria** (what must be TRUE):
  1. User stands in front of any camera and within 5 seconds sees exactly one `Face_Recognized` (or, if below threshold, exactly one `Face_Unknown` with a logged reason) row with a plausible name, a confidence score, and a main-stream JPEG showing the user's face — not the back of their head (peak is chosen from the full track lifetime, sharpest crop, not the first peak sample).
  2. User parks a car in view at night, the car comes to rest, and within a few seconds exactly one `Plate_Read` row appears with plate text that matches the regex validator (garbage strings and clothing/signs rejected by the bottom-40%-ROI + plate-shape pre-filter) — and the ALPR backend sits behind a clean `OcrAdapter` interface so PaddleOCR could be dropped in without touching call sites.
  3. User walks up to a parked car and is occluded by it: exactly one `Entered_Vehicle` row lands in the DB, and when they later re-emerge, exactly one `Exited_Vehicle` row lands — matched by the monotonic global ID wrapper so vehicle interactions don't mis-attribute after ByteTrack ID reuse.
  4. User lingers inside a zone past that zone's `loiter_seconds` threshold and exactly one `Loitering` event fires per zone-dwell — and repeated rapid-fire triggers for the same entity are de-duplicated by per-entity cooldown TTL instead of spamming the event log.
  5. User induces a trigger burst (e.g. walks in and out of frame 10 times in 5 seconds) and sees trigger_queue drop-newest behavior with a visible drop counter in the logs, the main-stream semaphore preventing CPU spikes, and no cascading stream stalls — DeepFace and EasyOCR both run in pre-warmed subprocesses that never block the main inference thread, with weights verified at startup and zero first-call downloads at runtime.

**Plans**: TBD
**UI hint**: no

---

### Phase 4: Hardening & Operations

**Goal**: Make the pipeline survive multi-day unattended operation — disk-full emergency mode, retention policy that protects valuable events, worker supervision with respawn, offline MP4 regression mode, metrics JSON, clean SIGINT/SIGTERM shutdown.

**Depends on**: Phase 3 (every production behavior is now in place; this phase hardens them)

**Requirements**: STO-08, STO-09, OPS-02, OPS-03, OPS-04, OPS-05

**Success Criteria** (what must be TRUE):
  1. User runs the pipeline for 24 continuous hours and sees the retention policy prune the oldest non-protected snapshots to stay under the configured age + size cap — with every `Face_Recognized` and `Plate_Read` event and its snapshot still present, untouched, after the cleanup pass.
  2. User fills `EVENT_IMAGE_DIR` past its free-space threshold and the pipeline enters emergency mode: DB rows keep landing, image writes are skipped, one `Storage_Full` event is emitted per minute (not per frame), and the pipeline does not crash or stall inference.
  3. User `kill -9`s the FaceWorker subprocess mid-run and the Supervisor detects the death within 5 seconds, respawns a pre-warmed replacement, drops in-flight work cleanly, and logs the respawn — and the same behavior holds for PlateWorker.
  4. User opens `metrics.json` and sees it refreshed every 30 seconds with `frames_dropped`, `triggers_fired`, `triggers_dropped`, `events_written`, per-camera FPS, and CPU percent — enough for the solo operator to diagnose a sick stream without attaching a debugger.
  5. User sends SIGINT (Ctrl+C) or SIGTERM and within 2 seconds every thread is joined, every capture released, every DB row flushed, and no zombie subprocess remains — and a final run against a recorded MP4 via `--mp4 path/to/file.mp4` produces the same event rows the live pipeline would, proving regression-test parity.

**Plans**: TBD
**UI hint**: no

---

## Deferred to Later Milestone

The following are explicitly NOT in v1 scope and must not be planned during this milestone:

- **Vue 3 dashboard** (`#151a2c` dark mode) with filter UI, click-through, per-entity drill-down
- **FastAPI read-only shim** over SQLite + `EVENT_IMAGE_DIR`
- **Cross-camera entity hand-off**
- **CLIP-based natural-language event search**
- **Daily / weekly summary digest**
- **Pre-event ring buffer** (5 s before trigger)
- **Snapshot perceptual-hash deduplication**
- **Per-zone schedules** (time-of-day loiter thresholds)
- **Audio event tagging** (YAMNet)
- **Plate → owner mapping table**
- **Hot config reload** via `watchdog`

The v1 pipeline is usable via SQLite + file browser; none of these block operator value.

---

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 0. Environment & Sanity | 0/0 | Not started | - |
| 1. Multi-Stream Ingest & Reconnect | 0/0 | Not started | - |
| 2. Detection, Tracking & Zoned Events | 0/0 | Not started | - |
| 3. Trigger & Catch Event Models | 0/0 | Not started | - |
| 4. Hardening & Operations | 0/0 | Not started | - |

---
*Last updated: 2026-04-13 by gsd-roadmapper*
