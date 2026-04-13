# Research Synthesis — Home CCTV AI Pipeline

**Synthesized:** 2026-04-13
**Sources:** STACK.md, FEATURES.md, ARCHITECTURE.md, PITFALLS.md
**Audience:** requirements definer + roadmapper (this should be enough; do not re-read the 4 source files unless drilling down)

---

## 1. TL;DR

- **The architecture is correct as specified.** "Trigger & Catch" (continuous YOLO on sub-streams, heavy models on one main-stream frame on event) is the only viable CPU-only design for 4 streams; all 4 research files agree.
- **Two mandatory stack changes on top of PROJECT.md:** (a) drop the standalone `ifzhang/ByteTrack` repo in favor of Ultralytics' built-in `tracker="bytetrack.yaml"` (same algorithm, no setup.py hell); (b) export YOLO weights to OpenVINO IR for ~2.5–3× CPU speedup — this is the single biggest budget win and probably the difference between "works" and "frames dropping".
- **Three mandatory version pins:** `numpy==1.26.4` (NOT 2.x), `tensorflow-cpu==2.16.2` + `tf-keras==2.16.0` + `TF_USE_LEGACY_KERAS=1` (DeepFace breaks on TF ≥ 2.17 and on Keras 3). Getting these wrong costs a day.
- **The project's real risks are not the AI models — they are the boundaries.** RTSP stalls on WSL2 that `cv2` can't time out, SQLite write contention, DeepFace/EasyOCR cold-start weight downloads blocking the trigger thread, ByteTrack ID-switches silently corrupting face triggers, disk filling from snapshots. All have concrete mitigations in §5.
- **Concurrency model: hybrid threads + processes.** 4 stream threads (GIL releases in FFmpeg), 1 YOLO/ByteTrack inference thread (not 4 — parallel YOLOs fight for the same cores), 1 main-stream grabber thread, 1 SQLite writer thread, and DeepFace + EasyOCR each in their own **subprocess** (TF/Torch global-state isolation + cold-start prewarm).
- **Build order is sequential and unambiguous.** ARCHITECTURE and FEATURES agree: stream plumbing → tracking → zones/events → trigger logic → face → plate → vehicle interactions → dashboard. Each phase is runnable end-to-end before the next begins.
- **Phase 0 must do empirical measurement.** CPU % estimates in all files are hardware-dependent ("Ryzen 5 5600 class" assumption). Measuring baseline YOLO FPS + DeepFace/EasyOCR cold-start on the *actual* host is non-negotiable before promising 4 streams × 10–15 FPS.
- **Defer the Vue dashboard to a separate milestone.** The entire runtime pipeline is useful before the UI exists (SQL + file browser is enough for the solo operator-developer). Do not gate earlier phases on UI work.

---

## 2. Recommended Stack (Pinned)

### Runtime
- Python 3.11.x (CPython) inside WSL2 Ubuntu 24.04
- `opencv-python-headless==4.13.0.92`
- ffmpeg ≥ 6.x from Ubuntu 24.04 apt (OpenCV VideoCapture backend)
- `numpy==1.26.4` **(hard pin — do NOT upgrade to 2.x)**
- `python-dotenv==1.2.2`
- `pydantic>=2.10,<3` (typed settings wrapper for `.env`)

### Detection + Tracking
- `ultralytics==8.4.37`
- `openvino==2026.1.0` **(mandatory — YOLO weights exported to OpenVINO IR)**
- `onnxruntime==1.24.4` (fallback backend)
- `lap==0.5.13`; `scipy>=1.13,<2`
- **Model:** `yolo26n.pt` (recommended; ~43% faster CPU), with `yolov8n.pt` as a 1-line fallback
- **Tracker:** Ultralytics built-in `tracker="bytetrack.yaml"` (NOT the `ifzhang/ByteTrack` repo)

### Face (event-triggered)
- `tensorflow-cpu==2.16.2` **(do NOT upgrade to 2.17+)**
- `tf-keras==2.16.0` with `os.environ["TF_USE_LEGACY_KERAS"]="1"`
- `deepface==0.0.99`
- Config: `model_name="ArcFace"`, `detector_backend="retinaface"`, `distance_metric="cosine"`

### OCR / ALPR (event-triggered)
- `torch==2.5.1+cpu` (install from `https://download.pytorch.org/whl/cpu`)
- `easyocr==1.7.2` with `gpu=False` **explicit** (its auto-detect silently falls back and confuses)
- Architect behind an `OcrAdapter` interface — PaddleOCR PP-OCRv5 is the upgrade path if accuracy fails

### Storage
- Stdlib `sqlite3` (WAL mode + `synchronous=NORMAL` + `busy_timeout=5000`)
- Filesystem under `EVENT_IMAGE_DIR`, date-partitioned `YYYY-MM-DD/`, JPEG q80
- **DB and image dir live inside WSL2 ext4, NOT on `/mnt/c/...`** (DrvFs kills SQLite WAL)

### Future UI (deferred milestone)
- Vue 3 `^3.5` + Vite 6 + TS 5.6, plain CSS (no Tailwind)
- `fastapi==0.115.*` + `uvicorn[standard]==0.32.*` as the read-only HTTP shim (Vue cannot read SQLite directly)

### WSL2 environment requirements
- Windows 11 22H2+ with **mirrored networking mode** (`networkingMode=mirrored` in `.wslconfig`) — default NAT makes the DVR at `192.168.1.10` unreachable
- RAM cap in `.wslconfig` (`memory=4GB` or similar) to prevent vmmem creep causing 10–30s host freezes
- RTSP options set **before** `import cv2`:
  ```
  OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp|fflags;nobuffer+discardcorrupt|flags;low_delay|stimeout;5000000|reconnect;1|reconnect_streamed;1|reconnect_delay_max;2
  ```
  `stimeout` (microseconds) is the **only** knob that actually unblocks a stalled `cap.read()` on WSL2.

---

## 3. Feature Set

### v1 — Must Build (Table Stakes + Spec Differentiators)

**Detection & tracking floor**
- YOLO class-gated events (no pixel motion); per-camera class allowlist; per-class confidence floor; min-bbox-area filter; min-track-age before logging; per-(entity, event_type) cooldown.

**Spatial & temporal logic (all in spec)**
- Polygon zones per camera; zone entry/exit events; persistent ByteTrack IDs + centroid velocity history; bbox-area history.

**Trigger & Catch event pipeline (all in spec)**
- Face recognition gated on peak bbox area via DeepFace (one main-stream frame).
- ALPR gated on centroid velocity ≈ 0 via EasyOCR (one main-stream frame).
- Vehicle interaction (`Entered_Vehicle` / `Exited_Vehicle`) via person∩car bbox intersection + ID disappearance.
- Loitering (ByteTrack ID in zone > per-zone threshold).

**Reliability & ops**
- Auto-reconnect with jittered exponential backoff + watchdog thread per camera.
- Per-stream heartbeat/health status; structured rotating logs; graceful shutdown; single-command start; disk-space guard; snapshot retention policy (age + size cap).
- Offline MP4 playback mode for dev/regression.

**Storage**
- SQLite `EventLog` schema from PROJECT.md + indexes on `timestamp, camera_id, event_type, entity_id, zone_name`.
- **Relative** `image_path` values (joined with `EVENT_IMAGE_DIR` at runtime).
- UTC ISO-8601 timestamps; monotonic clock for all duration math.
- Atomic image writes (`.tmp` + rename) before inserting the DB row.

### v1.5 — Strongly Recommended (Low CPU, High Value)
- Known-face library with sub-labels ("Person_405 → 'Alex'").
- Plate → owner mapping table.
- Per-zone, per-class loitering thresholds.
- Event "score" column (confidence × bbox area × dwell) for sorting.
- Polygon-drawing tool (`tools/draw_zones.py`) — essential for the human; nobody hand-types pixel coords.

### Deferred (v2+)
- Vue 3 dashboard (read-only, separate milestone).
- Cross-camera entity hand-off heuristic.
- Track replay (centroid trail drawn on snapshot).
- Daily summary digest.
- Pre-event ring buffer (5s before trigger).
- Snapshot perceptual-hash dedup.
- CLIP-based natural-language event search.

### Anti-features (DO NOT BUILD)
- Pixel-based motion detection (already removed from spec; keep it removed).
- Cloud upload / VSaaS.
- Continuous DeepFace / EasyOCR / CLIP / VLM / audio model on every frame.
- GPU/Coral accelerator path.
- Push notifications, multi-user auth, HTTPS, live video streaming in the dashboard, 24/7 recording, two-way audio, hot config reload, i18n, mobile-responsive UI.
- Postgres/Redis/Kafka, Docker orchestration, microservices.
- Replacing BitVision for general viewing.

---

## 4. Architecture at a Glance

### Topology

```
Main Process (Supervisor)
├── Thread: StreamReader[cam1..4]   ── cv2.VideoCapture sub-stream, own watchdog
├── Thread: InferenceWorker         ── YOLO(OpenVINO) + ByteTrack + Zone + TriggerEval
│                                      round-robins across 4 cam_queues
├── Thread: MainStreamGrabber       ── short-lived cv2.VideoCapture on trigger (2s TTL cache)
├── Thread: EventWriter             ── SQLite WAL writes + JPEG imwrite (single writer)
│
├── Subprocess: FaceWorker          ── DeepFace (pre-warmed), mp.Queue in/out
└── Subprocess: PlateWorker         ── EasyOCR  (pre-warmed), mp.Queue in/out
```

**Invariants:**
- One InferenceWorker, not 4 (YOLO already saturates cores via PyTorch intra-op pool).
- Per-camera ByteTracker instances; **never share trackers across cameras**.
- Main-stream captures opened **on demand**, closed after grab. Holding 4 main streams open forfeits the entire CPU savings.
- DeepFace + EasyOCR in **subprocesses** to isolate TF/Torch global state, KMP/OpenMP conflicts, and 5–15s cold-start.
- `torch.set_num_threads(N_CORES - 2)` + `cv2.setNumThreads()` — leave headroom for readers + workers.
- No asyncio anywhere (OpenCV is not async-aware).

### Queues and Drop Policy

| Queue | Type | Size | On full | Rationale |
|---|---|---|---|---|
| `cam_queue[i]` | `collections.deque(maxlen=2)` | 2 | drop-oldest (auto) | Live video — old frames worthless; readers must never block |
| `trigger_queue` | `queue.Queue` | 16 | drop-newest + counter | Trigger bursts are redundant; keep in-flight, drop new |
| `face_in_q` | `mp.Queue` | 4 | drop-newest + log | DeepFace 1–3s/call; backlog kills latency |
| `plate_in_q` | `mp.Queue` | 4 | drop-newest + log | EasyOCR 0.5–2s/call |
| `event_queue` | `queue.Queue` | 256 | block 100ms then drop-newest | DB writes are fast (1–5ms WAL); only fills on sick disk |

**Rule:** drop-oldest at ingress (frames), drop-newest at egress (triggers, events). Use `deque.append()` not `Queue.put()` for frames — Queue blocks, deque silently evicts.

---

## 5. Top 10 Pitfalls (Ranked)

| # | Pitfall | Severity | Phase(s) | Mitigation (short) |
|---|---------|----------|----------|---------------------|
| 1 | `cv2.VideoCapture.read()` hangs forever on WSL2 when RTSP stalls — no usable read timeout | **Critical** | Phase 0/1 Stream Ingest | `stimeout=5000000` in `OPENCV_FFMPEG_CAPTURE_OPTIONS` before `import cv2`, + per-camera watchdog thread that calls `cap.release()` from outside the stuck thread. Test by `iptables`-dropping port 554 for 60s. |
| 2 | SQLite write contention (`database is locked`) from multiple writer threads; corrupted DB if placed on `/mnt/c/...` DrvFs | **Critical** | Phase 4 Storage | WAL mode + `synchronous=NORMAL` + `busy_timeout=5000` at open; **single EventWriter thread** fed by `queue.Queue`; DB file lives inside WSL2 ext4. |
| 3 | DeepFace / EasyOCR first-call blocking weight download (2–15 min, can corrupt cache on interrupt) stalls the trigger thread | **Critical** | Phase 6/7 (setup) | Pre-download weights in a setup script via `DeepFace.build_model(...)` / `easyocr.Reader(...)`; verify file sizes at every startup; pin weight URLs by pinning lib versions. |
| 4 | ByteTrack ID switching breaks "peak bbox" face trigger → crops are back-of-head; ID reuse across long gaps mis-attributes vehicle interactions | **Critical** | Phase 2/5/8 Tracking | Tune `track_buffer=60`, `match_thresh=0.7–0.8`; defer "peak" decision to end-of-track (largest sharpness-scored crop in ID lifetime); wrap ByteTrack IDs in a monotonic global_id `camera_session_byteid_birthframe`; require same global_id for Entered/Exited pairs. |
| 5 | `EVENT_IMAGE_DIR` fills the disk silently; WSL2 `ext4.vhdx` never shrinks after delete; DB ends up with rows pointing at missing images | **Critical** | Phase 4+ Storage/Ops | Retention policy from day one (age + size cap); JPEG q80 not PNG; "protect" Face_Recognized/Plate_Read events from cleanup; startup refuses <1GB free; atomic write (tmp+rename) before DB insert so no orphans. |
| 6 | Green / partial-slice frames from `discardcorrupt` pass into YOLO as valid `ndarray`s → phantom detections at frame edge | **High** | Phase 0/1 Stream Ingest → Detection | Bottom-strip variance sanity check (`frame[-40:].std() < 3.0 → skip`); drop first 5 frames after every reconnect; track FFmpeg decode-error rate and force restart on threshold. |
| 7 | Main-stream reconnect storm during trigger bursts (ID switches → 3 "peaks" → 3 main-stream opens → CPU spike → sub-streams stall → cascading failure) | **High** | Phase 5 Triggers | Per-camera trigger rate limiter (1 main-stream pull per N seconds); per-entity dedup TTL; single global `Semaphore(1)` across main-stream opens; DeepFace + EasyOCR share one dedicated consumer. |
| 8 | DVR connection cap (Hikvision-OEM DVRs cap at ~6 concurrent RTSP sessions; 4 subs + 1–2 main + phone app = 503) | **High** | Phase 0/5 Stream Ingest | Serialize main-stream opens via global semaphore; treat 503 as retry-with-jitter; document "close BitVision phone app" in README; long-term, dedicated RTSP user on DVR. |
| 9 | DeepFace false matches on small/off-angle crops + EasyOCR false positives on clothing/signs/state names | **High** | Phase 6/7 Detection | Reject face crops < 112×112; tight distance threshold (cosine 0.30 not 0.40); require N consecutive frame matches; for OCR run plate-shape detector first (YOLO plate model or contour heuristic) + regex validate + restrict ROI to bottom 40% of car bbox. |
| 10 | YOLOv8n/YOLO26n accuracy collapses at night under IR mode (greyscale + center bloom) → missed intrusions and rocks classified as people | **High** | Phase 3/4 Detection | Auto-detect IR mode by frame saturation ≈ 0; per-time-of-day confidence thresholds (`0.35` day, `0.55` IR); mask IR bloom hot-spot; cap classes to `[0,2,5,7]` (person/car/bus/truck). |

**Runner-ups worth knowing:**
- OpenCV wheel built without FFmpeg → TCP env vars silently ignored. Startup assert on `cv2.getBuildInformation()` containing `FFMPEG: YES`.
- Naive local-time timestamps + DST drift. Store UTC ISO-8601; use `time.monotonic()` for all durations.
- Zone polygon drift after camera is bumped. Save reference frame; phase-correlation check on startup.
- WSL2 vmmem RAM creep → periodic 10–30s pipeline freezes. Cap in `.wslconfig`.
- DVR credentials leaking via logged URLs / tracebacks. Logging filter that masks `rtsp://[^@]+@`; `show_locals=False` in production tracebacks.

---

## 6. Open Questions Requiring Phase 0 Empirical Work

De-duplicated across STACK / ARCHITECTURE / PITFALLS:

1. **Actual host CPU baseline.** Does the real machine hit 4× streams × 10–15 FPS with YOLO-OpenVINO + ByteTrack? Must measure YOLO FPS per imgsz (640/480/416) and steady-state multi-stream CPU before committing.
2. **WSL2 mirrored networking availability.** Confirm Windows build supports `networkingMode=mirrored` via `wsl --version`. Fallback: Hyper-V bridged adapter. Without one of these, the DVR is unreachable.
3. **OpenCV FFmpeg backend verified.** `cv2.getBuildInformation()` must show `FFMPEG: YES`; Wireshark should confirm TCP on port 554, not UDP.
4. **`terminal.txt` ffplay flags → `OPENCV_FFMPEG_CAPTURE_OPTIONS` translation verified.** 30-minute single-stream capture without hang, green-frame, or drop. This is the entire Phase 0 exit criterion.
5. **Forced `iptables`-drop reconnect test.** Does the watchdog actually recover a stalled `cap.read()` within the configured window? Ctrl+C is NOT a valid test.
6. **DVR concurrent connection cap.** How many RTSP sessions does *this* BitVision/Hikvision unit tolerate before 503? Query CGI/ISAPI if possible; otherwise step 4 → 8.
7. **DeepFace + EasyOCR cold-start wall time and idle RAM** on this host. Needed to size WSL2 memory cap.
8. **YOLO26n vs YOLOv8n empirical check** on *this project's* sub-stream footage (IR mode, heavy H.264 compression, legacy optics). Keep the 1-line fallback ready.
9. **`cv2.VideoCapture` main-stream open latency** against this DVR. If >1s handshake, fall back to short-lived `ffmpeg -frames:v 1 -f image2pipe` subprocess.
10. **SQLite WAL on chosen DB path.** Confirm path is ext4 (not DrvFs); measure insert latency; verify no `database is locked` under burst of 20 events/sec.
11. **ByteTrack `frame_rate` parameter** must match actual measured FPS, not stream-advertised FPS. Measure.
12. **DeepFace distance-threshold calibration** on user's actual known-faces gallery at night/IR. Default 0.40 too loose; 0.30 is the starting recommendation, tune per dataset.

**Phase 0 blockers (pipeline cannot begin without these answered): Q1–Q6.**
Phase-scoped (acceptable to answer during the relevant phase): Q7–Q12.

---

## 7. Recommended Phase Build Order

Merged from ARCHITECTURE.md §"Build Order" and FEATURES.md §"MVP Recommendation" — they agree almost perfectly. Each phase is runnable end-to-end and writes *some* events to SQLite before the next begins.

| # | Phase | Delivers | Exit Criterion | Pitfalls in Scope | Needs Phase Research? |
|---|---|---|---|---|---|
| **0** | **Sanity & environment** | `.env` loader, cameras.yaml schema, single StreamReader, JPEG-to-disk every 5s, `cv2.getBuildInformation()` assert, WSL2 mirrored-networking verified, OpenVINO YOLO export cached | 30 min continuous single-stream capture, zero hangs, ffmpeg flags verified | #1, #6, FFmpeg-backend runner-up | **YES** — answers Q1–Q6 |
| **1** | **4-stream ingest + reconnect hardening** | 4 StreamReaders each in own thread + deque(maxlen=2) + jittered backoff + watchdog + Supervisor + structured logging | Pull DVR cable for 30s → all streams resume within 60s without restart | #1, #6, #8 | No |
| **2** | **Single-stream YOLO + ByteTrack** | One InferenceWorker, Ultralytics built-in ByteTrack, OpenVINO backend, visual bbox/ID overlay | ≥10 FPS sustained, IDs persist across short occlusions | #4 initial tuning | Light |
| **3** | **Multi-stream inference** | Round-robin 4 cam_queues, per-camera ByteTracker, `torch.set_num_threads` tuned, adaptive frame skip | 4 streams × ≥8 FPS for 1 hour, CPU <80% | #10 IR-mode tuning | No |
| **4** | **Zones, events, SQLite schema** | `tools/draw_zones.py`, ZoneEvaluator (Shapely), EventWriter thread (WAL), `Person_Detected`/`Car_Detected` events, indexes, retention pruner, min-track-age, cooldown | Walking through Driveway zone produces exactly one event row + one snapshot | #2, #5, DST, zone drift | No |
| **5** | **TriggerEvaluator + MainStreamGrabber** | Per-track peak-bbox, velocity (10-frame window), dwell, intersection map; MainStreamGrabber with 2s TTL cache; trigger_queue drop-newest; global semaphore on main-stream opens | Standing in front of camera → ONE face_peak trigger; parking car → ONE plate trigger | #4, #7, #8 | **YES** — Q9 main-stream open latency |
| **6** | **FaceWorker subprocess + DeepFace** | mp.Process with pre-warmed DeepFace (ArcFace + RetinaFace), embedding DB, sharpness + min-size reject, N-frame consecutive match, weight pre-download in setup | Standing in front of camera → Face_Recognized row within 5s | #3, #9 face, TF threads | **YES** — Q7, Q12 |
| **7** | **PlateWorker subprocess + EasyOCR** | mp.Process with pre-warmed EasyOCR `gpu=False`, plate-shape pre-filter, regex validator, bottom-40% ROI | Parked car at night → Plate_Read row with plausible text | #3, #9 plate | Light |
| **8** | **Vehicle interaction + loitering** | Bbox intersection state machine, monotonic global_id wrapper, dwell-time accumulator, per-zone loiter thresholds | Walking up to parked car + occlusion → one Entered_Vehicle event | #4 ID reuse | No |
| **9** | **Hardening** | Disk-space guard, metrics counters → JSON every 30s, `--mp4` offline mode, crash-recovery integration test, credential masking in logs, `.env` chmod, vmmem cap documented | `kill -9 FaceWorker` → Supervisor respawns within 5s | #5, vmmem, credentials, DST | No |
| **10** | **Vue 3 dashboard** (deferred milestone) | FastAPI read-only shim over SQLite + `EVENT_IMAGE_DIR`; Vue 3 dark-mode filter UI; click-through; per-entity drill-down; zone painter UI (stretch) | Review yesterday's events end-to-end with filters | Path crossing runner-up | Light |

---

## 8. Conflicts / Disagreements Between Research Outputs

One real ordering conflict (resolved) and one stack-choice point where the files present options without hard conflict:

1. **Loitering + vehicle interaction phase placement.** FEATURES.md puts them earlier as "cheap pure-tracker wins"; ARCHITECTURE.md puts them after face/plate to amortize tracker debugging. **Resolved in favor of ARCHITECTURE.md** — both depend on tracker stability, which is easier to validate after the expensive-model trigger pattern has shaken out ID-switch issues.

2. **Tracker library choice.** STACK.md is strongly opinionated (Ultralytics built-in, mandatory). ARCHITECTURE.md lists "ultralytics built-in vs `supervision` vs standalone" as an open question for Phase 2. **Resolved in favor of STACK.md** — the standalone repo has no PyPI package and is effectively unmaintained; `supervision` adds a dependency without buying anything over the built-in. Only reopen at Phase 2 if Ultralytics' abstraction proves painful.

3. **Minor CPU-percentage inconsistency.** FEATURES.md estimates YOLO at 15–25% of one core per stream (PyTorch CPU baseline); ARCHITECTURE.md estimates 40–60% total for 4 cams round-robin (OpenVINO-accelerated). These are compatible (different assumptions about OpenVINO speedup folding in). Not a conflict, but **Phase 0 must measure this on real hardware regardless.**

4. **DeepFace vs InsightFace.** STACK.md recommends DeepFace "for ergonomics" but flags InsightFace as defensible. PITFALLS.md documents DeepFace-specific footguns (cold-start, TF thread fighting, Keras 3) at length. Footguns are real but resolved by the `tensorflow-cpu==2.16.2` + `tf-keras==2.16.0` + `TF_USE_LEGACY_KERAS=1` recipe. **Stick with DeepFace for v1**; switch only if Phase 6 reveals accuracy problems on the real dataset.

Everything else — process model, drop policies, reconnect strategy, feature categorization, anti-features, storage schema, security posture, WSL2 requirements — is consistent across all four files.
