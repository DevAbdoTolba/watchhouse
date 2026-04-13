# Requirements — Home CCTV AI Pipeline (v1)

*Derived from `.planning/PROJECT.md`, `.planning/research/SUMMARY.md`, and the user's `specs.md` + `context.md` source documents. All v1 requirements are hypotheses until shipped and validated.*

**Scope:** Milestone v1 = "A working local-only AI event pipeline that watches 4 RTSP streams, runs YOLOv8n+ByteTrack continuously, fires DeepFace and EasyOCR on trigger, logs events to SQLite, and survives overnight."

**Out of scope for v1:** Vue 3 dashboard, cross-camera entity handoff, CLIP search, push notifications, multi-user auth, HTTPS, live video streaming, 24/7 recording, any cloud integration. See PROJECT.md → Out of Scope.

---

## v1 Requirements

### Environment (ENV)

- [ ] **ENV-01**: User runs the pipeline end-to-end on Windows 11 + WSL2 Ubuntu 24.04 with Python 3.11, CPU-only, no GPU dependency
- [ ] **ENV-02**: User configures WSL2 mirrored networking (or verified alternative) so the pipeline inside WSL2 can reach the DVR at `192.168.1.10:554`
- [ ] **ENV-03**: User loads all DVR credentials (`DVR_IP`, `DVR_PORT`, `DVR_USER`, `DVR_PASS`) and paths (`EVENT_IMAGE_DIR`, `DB_PATH`) exclusively from `.env` — no credentials in source, YAML, or logs
- [ ] **ENV-04**: User starts the entire pipeline with a single command (`python -m home_cctv` or equivalent) and stops it cleanly with Ctrl+C
- [ ] **ENV-05**: User sees a clear startup error if any required env var is missing, OpenCV lacks the FFmpeg backend, or `EVENT_IMAGE_DIR` has less than 1 GB free

### Stream Ingest (ING)

- [ ] **ING-01**: User ingests all 4 camera sub-streams simultaneously over RTSP/TCP (`rtsp_transport=tcp`, `nobuffer`, `discardcorrupt`, `low_delay`) with no UDP fallback
- [ ] **ING-02**: User's pipeline recovers automatically from a stalled `cv2.VideoCapture.read()` within 10 seconds via a per-camera watchdog that force-releases and reconnects the capture
- [ ] **ING-03**: User's pipeline recovers from a 30-second DVR network outage: all 4 streams resume within 60 seconds without a process restart, with jittered exponential backoff (1s→2s→4s→8s→…→30s)
- [ ] **ING-04**: User's pipeline skips green / partial / corrupted frames (bottom-row variance test) and drops the first 5 frames after every reconnect
- [ ] **ING-05**: User sees per-stream health/heartbeat in the logs (last successful read timestamp, FPS, drop rate)
- [ ] **ING-06**: User can run the same pipeline against an offline MP4 file instead of RTSP for regression testing (`--mp4 path/to/file.mp4`)
- [ ] **ING-07**: User's main-stream captures are opened on demand for a single frame on trigger, then released — never held open continuously — with a short (~2 s) TTL cache for burst triggers on the same camera
- [ ] **ING-08**: User's concurrent main-stream opens are serialized via a single global semaphore so DVR connection caps are not exceeded

### Detection & Tracking (TRK)

- [ ] **TRK-01**: User's pipeline runs YOLOv8n (or YOLO26n) class-filtered to `person` and vehicle classes (`car/bus/truck`), exported to OpenVINO IR for CPU acceleration
- [ ] **TRK-02**: User's pipeline applies per-class confidence floors and a minimum bounding-box-area filter before emitting any detection downstream
- [ ] **TRK-03**: User's pipeline uses Ultralytics' built-in ByteTrack tracker (`tracker="bytetrack.yaml"`) to assign persistent track IDs per camera
- [ ] **TRK-04**: User's pipeline maintains one independent ByteTracker instance per camera — IDs never collide across cameras
- [ ] **TRK-05**: User's pipeline wraps raw ByteTrack IDs in a monotonic global ID (`{camera}_{session}_{byte_id}_{birth_frame}`) that never reuses a retired ID
- [ ] **TRK-06**: User's pipeline computes centroid velocity per track over a rolling 10-frame window and stores bbox-area history for peak detection
- [ ] **TRK-07**: User's pipeline enforces a minimum track age (configurable) before any track can produce a logged event — short flickers are suppressed
- [ ] **TRK-08**: User's pipeline runs YOLO+ByteTrack in a single inference thread that round-robins across the 4 camera queues (one shared model, per-camera tracker state)
- [ ] **TRK-09**: User's pipeline holds each camera frame in a `deque(maxlen=2)` with drop-oldest semantics so stream readers never block on slow inference

### Zones (ZON)

- [ ] **ZON-01**: User defines polygon zones per camera via a standalone tool (`tools/draw_zones.py`) that captures a live frame and lets the user click-to-define polygons, emitting `zones.yaml`
- [ ] **ZON-02**: User's pipeline evaluates every tracked object against the camera's zones every frame via Shapely point-in-polygon on the bbox centroid
- [ ] **ZON-03**: User's pipeline logs a `Zone_Enter` event exactly once when a tracked object first enters a zone (respecting min-track-age)
- [ ] **ZON-04**: User's pipeline enforces a per-`(entity_id, event_type)` cooldown so a single track does not spam the event log

### Event Triggers (TRG)

- [ ] **TRG-01**: User's pipeline fires a `face_peak` trigger when a person-track's bbox area reaches its historical peak and stays there for N frames, selecting the sharpest crop from the track's lifetime rather than the first peak
- [ ] **TRG-02**: User's pipeline fires a `plate_read` trigger when a car-track's centroid velocity drops below a configurable threshold for N consecutive frames (configurable window)
- [ ] **TRG-03**: User's pipeline fires a `loiter` event when a track's dwell time inside a zone exceeds the per-zone `loiter_seconds` threshold
- [ ] **TRG-04**: User's pipeline detects `Entered_Vehicle` when a `Person_*` track and a `Car_*` track have overlapping bboxes and the person track disappears while still overlapping the car
- [ ] **TRG-05**: User's pipeline detects `Exited_Vehicle` when a new person track appears inside an existing car-track bbox
- [ ] **TRG-06**: User's pipeline drops trigger requests (drop-newest + counter) when the trigger queue is full rather than back-pressuring inference
- [ ] **TRG-07**: User's pipeline de-duplicates rapid-fire triggers for the same entity via a per-entity cooldown TTL

### Face Recognition (FAC)

- [ ] **FAC-01**: User's pipeline runs DeepFace (ArcFace + RetinaFace) in an isolated subprocess with pre-warmed model at startup, never in the main inference thread
- [ ] **FAC-02**: User pre-downloads and verifies DeepFace model weights in a setup script — first-call blocking downloads are eliminated
- [ ] **FAC-03**: User's pipeline rejects face crops smaller than 112×112 pixels before running recognition
- [ ] **FAC-04**: User's pipeline uses cosine distance with a tight threshold (starting 0.30) and requires N consecutive frame matches before logging `Face_Recognized`
- [ ] **FAC-05**: User maintains a known-faces gallery (directory of labeled image folders) and a persisted embedding cache
- [ ] **FAC-06**: User's pipeline writes a `Face_Recognized` event row (name + confidence + image path) to SQLite within 5 seconds of the `face_peak` trigger
- [ ] **FAC-07**: User's pipeline falls back to `Face_Unknown` with a logged reason when recognition confidence is below threshold — never silently drops the trigger

### License Plate Reading (ALP)

- [ ] **ALP-01**: User's pipeline runs EasyOCR (`gpu=False` explicit) in an isolated subprocess with pre-warmed reader at startup
- [ ] **ALP-02**: User pre-downloads and verifies EasyOCR model weights in a setup script
- [ ] **ALP-03**: User's pipeline restricts OCR ROI to the bottom ~40% of the car bbox (plate location heuristic) and applies a plate-shape / regex validator to reject garbage strings
- [ ] **ALP-04**: User's pipeline is architected so the OCR backend can be swapped for PaddleOCR without touching call sites (`OcrAdapter` interface)
- [ ] **ALP-05**: User's pipeline writes a `Plate_Read` event row (plate text + confidence + image path) to SQLite

### Storage (STO)

- [ ] **STO-01**: User's pipeline persists events to SQLite at `$DB_PATH` using the `EventLog` schema from PROJECT.md (`event_id`, `timestamp`, `camera_id`, `event_type`, `entity_id`, `zone_name`, `confidence`, `extracted_text`, `image_path`)
- [ ] **STO-02**: User's SQLite connection uses `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, and is served by a single EventWriter thread fed by a bounded queue
- [ ] **STO-03**: User's DB file and `EVENT_IMAGE_DIR` live on the WSL2 ext4 filesystem, never on `/mnt/c/...` (DrvFs)
- [ ] **STO-04**: User's timestamps are stored as UTC ISO-8601; all duration math uses `time.monotonic()`
- [ ] **STO-05**: User's pipeline creates indexes on `(timestamp, camera_id, event_type, entity_id, zone_name)` for dashboard-readiness
- [ ] **STO-06**: User's pipeline stores `image_path` as a **relative** path (joined with `EVENT_IMAGE_DIR` at runtime)
- [ ] **STO-07**: User's pipeline writes event images atomically (write to `.tmp`, rename, then insert DB row) so the DB is never out of sync with the filesystem
- [ ] **STO-08**: User's pipeline enforces a snapshot retention policy (age + size cap) that prunes the oldest non-protected events first — `Face_Recognized` and `Plate_Read` events are protected from cleanup
- [ ] **STO-09**: User's pipeline enters "emergency mode" on disk-full (keep writing DB rows, skip images, emit one `Storage_Full` event per minute)

### Operations (OPS)

- [ ] **OPS-01**: User sees structured rotating logs with per-camera prefixes, event summaries, and credential masking (`rtsp://user:***@host`)
- [ ] **OPS-02**: User's pipeline writes a metrics JSON file (`frames_dropped`, `triggers_fired`, `triggers_dropped`, `events_written`, per-camera FPS, CPU %) every 30 seconds
- [ ] **OPS-03**: User's subprocess workers (FaceWorker, PlateWorker) are supervised: death is detected within 5 seconds and the worker is respawned; in-flight work is lost cleanly
- [ ] **OPS-04**: User's pipeline exits cleanly on SIGINT/SIGTERM — all threads joined, all captures released, all DB rows flushed, no zombie subprocesses
- [ ] **OPS-05**: User can pass `--mp4 path/to/file.mp4` to run the entire pipeline against a recorded file for regression testing (reuses ING-06)

---

## v2 / Deferred

*These are valuable but explicitly NOT in v1 scope. Do not block v1 on them.*

- **Vue 3 dashboard** (`#151a2c` dark mode) reading SQLite via a read-only FastAPI shim — separate milestone
- **Cross-camera entity hand-off** (same person across adjacent cameras)
- **CLIP-based natural-language event search**
- **Daily / weekly summary digest**
- **Pre-event ring buffer** (save 5 s of sub-stream before each trigger)
- **Snapshot perceptual-hash deduplication**
- **Per-zone schedules** (e.g. loiter threshold 5 s at night, 60 s during day)
- **Audio event tagging** (YAMNet on a DVR audio track, if exposed)
- **Plate → owner mapping table**
- **Hot config reload** via `watchdog` filesystem watcher

---

## Out of Scope (Explicit Exclusions)

Kept here to prevent re-adding. See PROJECT.md → "Out of Scope" for full rationale.

- Pixel-based motion detection — false positives on shadows/rain/trees
- Cloud / BitVision P2P for the AI path — latency + cloud dependency
- GPU / Coral / dedicated accelerator — $0 budget
- UDP RTSP transport — packet drops on this LAN
- Running DeepFace / EasyOCR / any heavy model continuously — would instantly peg the CPU
- Push notifications, multi-user auth, HTTPS, live video in the dashboard, 24/7 recording, two-way audio, i18n, mobile-responsive UI
- Postgres / Redis / Kafka / Docker / microservices
- Replacing BitVision for general camera viewing — BitVision stays for cloud/phone viewing
- Multi-machine distributed deployment — single local WSL2 host only

---

## Traceability

*Filled in by the roadmapper. Each REQ-ID maps to exactly one phase.*

| REQ-ID | Phase |
|---|---|
| *(populated by gsd-roadmapper)* | — |
