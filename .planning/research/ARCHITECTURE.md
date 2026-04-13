# Architecture Research — Home CCTV AI Pipeline

**Project:** Home CCTV AI Pipeline
**Mode:** Architecture (single dimension)
**Confidence:** HIGH on process model & GIL analysis, HIGH on reconnect strategy, MEDIUM on exact CPU budget percentages (depend on actual host CPU — needs Phase 0 measurement)

---

## Process Model

**Recommendation: Hybrid — 1 main process + 4 stream-reader threads + 1 inference thread + 2 worker processes + 1 writer thread.**

The reasoning hinges on which operations release the GIL and which do not:

| Operation | Releases GIL? | Implication |
|---|---|---|
| `cv2.VideoCapture.read()` (FFmpeg backend) | YES — blocks in C | Threads are fine, no need for processes |
| YOLOv8n inference (Ultralytics → PyTorch CPU) | YES — releases in ATen ops | Threads work, but only ONE inference at a time benefits |
| ByteTrack (pure Python / numpy) | Partial — numpy releases on big ops, update is Python-heavy | Keep on the inference thread |
| DeepFace (TensorFlow CPU) | YES on TF ops, NO on Python preprocessing | Heavy + slow → isolate in subprocess |
| EasyOCR (PyTorch CPU) | YES on torch ops | Heavy + slow → isolate in subprocess |
| SQLite write | YES (sqlite3 releases GIL) | Single writer thread is fine |

**Concrete topology:**

```
Main Process (orchestrator)
├── Thread: StreamReader[cam1]   ── cv2.VideoCapture sub /1
├── Thread: StreamReader[cam2]   ── cv2.VideoCapture sub /11
├── Thread: StreamReader[cam3]   ── cv2.VideoCapture sub /21
├── Thread: StreamReader[cam4]   ── cv2.VideoCapture sub /31
├── Thread: InferenceWorker      ── YOLOv8n + ByteTrack + Zone eval + Trigger eval
│                                   (round-robins frames from 4 cam queues)
├── Thread: MainStreamGrabber    ── cv2.VideoCapture per request, on-demand,
│                                   short-lived (open → grab 1 → close)
├── Thread: EventWriter          ── SQLite WAL writes + image saves
│                                   (consumes from event queue)
│
├── Subprocess: FaceWorker       ── DeepFace, fed via multiprocessing.Queue
│                                   (cropped face images in, name+confidence out)
└── Subprocess: PlateWorker      ── EasyOCR, fed via multiprocessing.Queue
                                    (cropped plate images in, text+confidence out)
```

**Why this split:**

1. **Stream readers MUST be threads, not the main inference loop.** `cap.read()` blocks for ~30–100 ms per call on a 10 FPS stream and stalls the whole pipeline if you try to round-robin synchronously. Each reader sits in a tight `while: cap.read() → push to queue` loop. Because `cap.read()` releases the GIL inside FFmpeg, all 4 readers run truly in parallel.

2. **Inference is a single thread, not four.** YOLOv8n on CPU saturates available cores via PyTorch's intra-op thread pool (set with `torch.set_num_threads()` and `cv2.setNumThreads()`). Running four parallel YOLO inferences fights for the same cores and trashes cache. One inference thread that round-robins across cameras is faster and more predictable. Set `torch.set_num_threads(N_PHYSICAL_CORES - 2)` to leave headroom for readers and workers.

3. **DeepFace and EasyOCR MUST be subprocesses, not threads.** Two reasons:
   - **Cold start cost.** They take 5–15 s to import + load weights. You don't want that blocking the inference thread on first trigger. Pre-warm them in the subprocess at startup.
   - **Memory bloat & TF/Torch global state.** DeepFace's TensorFlow backend installs signal handlers and allocates huge contiguous buffers. Mixing that into the same process as Ultralytics' PyTorch leads to subtle conflicts (especially around OpenMP thread pools — `KMP_DUPLICATE_LIB_OK` hell). Process isolation removes this entire class of problem.
   - Communication via `multiprocessing.Queue` of pickled (np.ndarray crop, metadata dict). Crops are small (100–300 KB), well below pickle/queue throughput limits.

4. **Main-stream grabber is a thread, not a persistent capture.** Do NOT keep all 4 main-stream `VideoCapture`s open continuously — that defeats the entire "Trigger & Catch" CPU savings. Open the main stream on demand: `cv2.VideoCapture(main_url) → cap.grab() → cap.retrieve() → cap.release()`. Cost is one RTSP TCP handshake per trigger (~200–500 ms). For burst triggers on the same camera within 2 seconds, cache the open capture briefly with a TTL.

5. **EventWriter is a single thread, not a process.** SQLite is single-writer by design. Wrap it in WAL mode (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`) and feed it via a `queue.Queue`. Image file writes (cv2.imwrite of JPEG, ~50–200 KB) also live here so DB row + image hit disk in the same critical section.

**Asyncio: do NOT use it.** OpenCV's VideoCapture is not asyncio-aware — you'd end up doing `loop.run_in_executor` for every read, which is just threads with extra ceremony. There's no I/O multiplexing benefit because each RTSP socket is wrapped inside FFmpeg, opaque to Python.

---

## Component Boundaries

| Component | Type | Inputs | Outputs | Owns |
|---|---|---|---|---|
| `StreamReader` (×4) | Thread | RTSP URL, reconnect policy | `Frame(camera_id, ts, ndarray)` → cam_queue | `cv2.VideoCapture` lifetime, reconnect backoff |
| `FrameQueue` (×4) | `collections.deque(maxlen=2)` + Lock | Frames | Frames | Drop-oldest policy |
| `InferenceWorker` | Thread | 4× cam_queue | `TrackedFrame(camera_id, frame, tracks)` → internal | YOLO model, ByteTracker per camera |
| `ZoneEvaluator` | Pure func, called inside InferenceWorker | TrackedFrame, zones config | `ZoneHits[]` | Polygon shapely tests |
| `TriggerEvaluator` | Stateful, called inside InferenceWorker | ZoneHits + track history | `TriggerRequest(type, camera_id, track_id, bbox, ts)` → trigger_queue | Per-track state: peak bbox area, velocity, dwell time, intersection state |
| `MainStreamGrabber` | Thread | trigger_queue | `(TriggerRequest, mainframe_ndarray)` → dispatch to Face/Plate workers | Short-lived VideoCapture, optional 2 s TTL cache |
| `FaceWorker` | Subprocess | (crop, meta) via mp.Queue | `EventRow` via result mp.Queue | DeepFace model, embedding cache, known-faces DB |
| `PlateWorker` | Subprocess | (crop, meta) via mp.Queue | `EventRow` via result mp.Queue | EasyOCR reader, plate regex validator |
| `EventWriter` | Thread | event_queue (from inference + worker results) | sqlite rows + jpeg files on disk | `sqlite3.Connection`, dated dir tree |
| `ConfigLoader` | Module | `.env`, `zones.yaml`, `triggers.yaml` | Frozen dataclasses | File watch optional (post-MVP) |
| `Supervisor` | Main thread | stdin / signals | shutdown events | Lifecycle, health checks, restart of dead threads |

The hard rule: **TriggerEvaluator never blocks on the network or on workers.** It pushes a request and forgets. If the trigger queue is full (e.g. plate worker is 3 s behind), drop the request and increment a `triggers_dropped_total` counter — better to lose one event than to back-pressure the inference loop and start dropping live frames.

---

## Data Flow

```
RTSP sub /1  ──┐
RTSP sub /11 ──┤  StreamReader×4 ──► cam_queue×4 (deque maxlen=2, drop-oldest)
RTSP sub /21 ──┤
RTSP sub /31 ──┘
                                              │
                                              ▼
                              ┌───────────────────────────────┐
                              │  InferenceWorker (1 thread)   │
                              │  while True:                  │
                              │    for cam in round_robin:    │
                              │      f = cam_queue[cam].pop() │
                              │      dets = yolo(f)           │
                              │      tracks = bytetrack(dets) │
                              │      hits = zones(tracks)     │
                              │      trigs = trigger_eval()   │
                              │      ────► trigger_queue       │
                              │      ────► event_queue (Person_│
                              │              Detected, Loiter) │
                              └───────────────────────────────┘
                                              │
                  ┌───────────────────────────┴──────────────┐
                  ▼                                          ▼
        trigger_queue (Queue, maxsize=16,                event_queue (Queue,
        drop-newest on full)                              maxsize=256)
                  │                                          │
                  ▼                                          │
    ┌─────────────────────────┐                              │
    │ MainStreamGrabber       │                              │
    │ open cap → grab 1 →     │                              │
    │ release (or TTL cache)  │                              │
    └─────────────────────────┘                              │
                  │                                          │
        crop face / crop plate                               │
                  │                                          │
       ┌──────────┴───────────┐                              │
       ▼                      ▼                              │
  FaceWorker.in_q       PlateWorker.in_q                     │
   (mp.Queue)            (mp.Queue)                          │
       │                      │                              │
       ▼                      ▼                              │
  FaceWorker.out_q      PlateWorker.out_q                    │
       │                      │                              │
       └──────────┬───────────┘                              │
                  ▼                                          │
              event_queue ◄──────────────────────────────────┘
                  │
                  ▼
         ┌──────────────────┐
         │  EventWriter     │
         │  sqlite WAL +    │
         │  jpeg to disk    │
         └──────────────────┘
```

**Queue sizing & drop policy — explicit:**

| Queue | Type | Size | On Full | Rationale |
|---|---|---|---|---|
| `cam_queue[i]` | `collections.deque(maxlen=2)` | 2 | drop-oldest (automatic) | Live video — old frames are worthless; never let inference work on stale data |
| `trigger_queue` | `queue.Queue` | 16 | drop-newest, log + counter | Triggers are bursty; if grabber falls behind, prefer to keep the in-flight one and drop new ones |
| `face_in_q` (mp) | `mp.Queue` | 4 | drop-newest, log | DeepFace ~1–3 s per inference on CPU; backlog kills latency |
| `plate_in_q` (mp) | `mp.Queue` | 4 | drop-newest, log | EasyOCR ~0.5–2 s per crop |
| `event_queue` | `queue.Queue` | 256 | block briefly (100 ms), then drop-newest | DB writes are fast (1–5 ms in WAL); only fills if disk is sick |

**Critical principle:** On live video, **drop-oldest** at the ingress (frames) and **drop-newest** at egress (triggers, events). Frames lose value with age; triggers don't, but a burst of identical triggers from the same track is also low-value, so dropping recent ones is acceptable.

**Use `deque(maxlen=2)` instead of `Queue(maxsize=2)` for camera frames.** `Queue.put()` blocks; `deque.append()` silently evicts. The reader thread should never block on a slow inference loop.

---

## Reconnect Strategy

This is where most homegrown CCTV pipelines die. Four failure modes to handle explicitly.

### 1. Stream death (RTSP socket drops, DVR reboots, network blip)

**Detection:** `cap.read()` returns `(False, None)`, OR returns `(True, frame)` but with the same frame for >2 s (frozen pipeline), OR no frame for >5 s.

**Response (in StreamReader thread):**
```
on read_failure:
    consecutive_failures += 1
    if consecutive_failures >= 3:
        cap.release()
        sleep(backoff)  # 1s, 2s, 4s, 8s, capped at 30s
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        re-apply CAP_PROP_BUFFERSIZE=1, OPEN_TIMEOUT etc.
        consecutive_failures = 0
```

**Set `OPENCV_FFMPEG_CAPTURE_OPTIONS` BEFORE importing cv2** — it's read at module import, not per-VideoCapture. Mirror the working ffplay flags from `terminal.txt`:
```
rtsp_transport;tcp|fflags;nobuffer+discardcorrupt|flags;low_delay|err_detect;ignore_err|analyzeduration;1000000|probesize;2000000
```
Plus `cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)` after open.

**Watchdog timer:** Each StreamReader writes `last_frame_ts[cam_id] = time.monotonic()` on every successful read. Supervisor checks every 2 s; if `now - last_frame_ts > 10`, kill and respawn the reader thread.

### 2. DVR full restart (all 4 streams die simultaneously)

The DVR going down for 30–60 s is the common case after a power blip. Per-stream backoff handles it but synchronously — they'll all hammer the DVR with reconnect attempts at the same instant.

**Mitigation:** Add jitter to backoff (`backoff * random.uniform(0.8, 1.5)`) so the 4 readers spread their reconnects across a 1–2 s window. Also use a shared `dvr_health` flag — if all 4 streams are failing, slow EVERYONE down to a 30 s probe interval and emit a `DVR_Unreachable` event row.

### 3. YOLO inference falls behind (slow path)

**Symptom:** `cam_queue[i]` stays full, frames are being dropped at ingress. This is fine for short bursts but indicates capacity exhaustion if sustained.

**Detection:** Track `frames_dropped_per_minute[cam_id]`. If >50% drop rate sustained for >30 s, you're under-provisioned.

**Response:** Three levers, in order:
1. **Adaptive frame skipping.** InferenceWorker maintains a `target_interval[cam_id]`. If drops sustained → bump from 100 ms to 150 ms to 200 ms. Reduces from 10 → 6 → 5 FPS per camera.
2. **YOLO imgsz reduction.** Drop from `imgsz=640` to `imgsz=480` — roughly 1.7× speedup, modest accuracy loss. Acceptable for sub-stream "is there a person somewhere".
3. **Skip every Nth camera.** Round-robin with weighting — a "dead" alley camera can run at 5 FPS while a busy driveway runs at 12 FPS.

These should be configurable via `triggers.yaml` per camera, not hardcoded.

### 4. SQLite write blocks / disk full

**Symptom:** EventWriter's `event_queue` grows.

**Response:**
- WAL mode + `PRAGMA synchronous=NORMAL` makes writes effectively non-blocking under normal load.
- On `sqlite3.OperationalError: database is locked`: retry 3× with 50 ms backoff (this catches Vue-dashboard read contention later).
- On `OSError: No space left on device`: switch EventWriter into "emergency mode" — keep writing DB rows but skip image files. Emit one `Storage_Full` event per minute.
- Add a startup-time disk-space check; refuse to start if `EVENT_IMAGE_DIR` filesystem has <5 GB free.

### 5. Worker subprocess death

If FaceWorker or PlateWorker subprocess dies (segfault in TF/Torch, OOM kill), the Supervisor must:
- Detect via `process.is_alive()` polled every 5 s
- Drain its result queue
- Respawn it
- Resume — triggers that were in flight are lost; that's acceptable

Wrap each worker's main loop in `try/except Exception: log + continue`. The subprocess should NEVER die from a bad crop — only from external signals.

---

## Config Layer

Three config sources, loaded once at startup, frozen into dataclasses:

**1. `.env` — secrets and paths only**
```
DVR_IP, DVR_PORT, DVR_USER, DVR_PASS
EVENT_IMAGE_DIR, DB_PATH
```
Loaded via `python-dotenv` at startup. Never reloaded. Validated: missing key = hard fail with clear error.

**2. `cameras.yaml` — per-camera stream config**
```yaml
cameras:
  - id: 1
    name: Driveway
    sub_path: "/1"
    main_path: "/0"
    target_fps: 12
    yolo_imgsz: 640
  - id: 2
    ...
```
URLs are *constructed* from `.env` + paths. Never store credentials in YAML.

**3. `zones.yaml` — polygons + trigger thresholds per camera**
```yaml
cam_1:
  zones:
    - name: Driveway
      polygon: [[120, 400], [800, 400], [800, 700], [120, 700]]
      classes: [person, car]
    - name: Front_Door
      polygon: [[400, 200], [600, 200], [600, 350], [400, 350]]
      classes: [person]
  triggers:
    loiter_seconds: 15
    face_peak_min_area: 8000
    plate_velocity_threshold: 2.0   # px/frame
    plate_velocity_window: 10       # frames
```

**Loading order (Phase 1 of build order):**
1. `dotenv` first
2. Validate required env vars exist
3. Load YAML files via `pyyaml` → parse into `@dataclass(frozen=True)` objects
4. Pre-compute Shapely `Polygon` objects from raw point lists (do this once, not per frame)
5. Inject the frozen config into all components at construction

**Polygon drawing tool:** Build a tiny standalone `tools/draw_zones.py` that displays a captured frame and lets the user click-to-define polygons, dumping YAML. This is essential for the human in the loop — nobody hand-types pixel coordinates accurately.

**Hot reload:** Out of scope for MVP. Restart the pipeline to pick up zone changes. Add `watchdog` file watcher post-MVP.

---

## Build Order

This determines the roadmap phases. Each step must be runnable end-to-end before moving on — no scaffolding for Phase 5 in Phase 1.

**Phase 0 — Single-stream sanity (1 camera, no AI)**
- `.env` loader + cameras.yaml schema
- StreamReader for ONE camera, drops to disk as JPEG every 5 s
- Goal: prove OpenCV+FFmpeg flags work in WSL2 against the real DVR. This validates `terminal.txt` translates to OpenCV.
- Exit criterion: 30 minutes of continuous capture without a crash.

**Phase 1 — All 4 streams as raw frame pumps**
- Add 3 more StreamReaders, each in its own thread, each with deque(maxlen=2)
- Add reconnect with jittered backoff + watchdog
- Add Supervisor + structured logging
- Exit criterion: pull the DVR's network cable for 30 s, plug back in — all 4 streams resume within 60 s without process restart.

**Phase 2 — Single-stream YOLO + ByteTrack inference path**
- One InferenceWorker thread reading from one cam_queue
- Ultralytics YOLOv8n + supervision/ByteTrack (or ultralytics built-in tracker)
- Draw bboxes + track IDs on screen for visual debugging
- Exit criterion: stable 10+ FPS on one stream, IDs persist across short occlusions.

**Phase 3 — Multi-stream inference (still no triggers)**
- One InferenceWorker round-robins all 4 cam_queues
- Per-camera ByteTracker instances (do NOT share trackers across cameras)
- Tune `torch.set_num_threads()` and adaptive frame skip
- Exit criterion: 4 streams × 8+ FPS sustained for 1 hour, CPU under 80%.

**Phase 4 — Zones, polygons, basic events**
- Build `tools/draw_zones.py` polygon picker
- ZoneEvaluator (Shapely point-in-polygon on bbox center)
- EventWriter (SQLite WAL, schema from PROJECT.md)
- First event types: `Person_Detected`, `Car_Detected` with zone_name
- Exit criterion: walking through "Driveway" zone produces one event row + one snapshot.

**Phase 5 — TriggerEvaluator + MainStreamGrabber**
- Per-track state: peak bbox area, centroid velocity (rolling 10-frame window), dwell time, intersection map
- Trigger types: `face_peak`, `car_stopped`, `loiter`, `vehicle_intersection`
- MainStreamGrabber thread, on-demand cv2.VideoCapture with 2 s TTL cache
- Trigger queue with drop-newest
- Exit criterion: standing in front of camera produces ONE face_peak trigger (not 50), parking a car produces ONE plate trigger.

**Phase 6 — FaceWorker subprocess + DeepFace integration**
- mp.Process with pre-warmed DeepFace model
- Known-faces directory + embedding cache on disk
- Result back via mp.Queue → event_queue
- Exit criterion: standing in front of camera produces a `Face_Recognized` row within 5 s of trigger.

**Phase 7 — PlateWorker subprocess + EasyOCR**
- mp.Process with pre-warmed EasyOCR reader
- Plate regex validator (rejects garbage strings)
- Exit criterion: parked car at night produces a `Plate_Read` row.

**Phase 8 — Vehicle interaction & loitering events**
- Bbox intersection state machine (`Entered_Vehicle`, `Exited_Vehicle`)
- Loitering by dwell-time accumulator
- Exit criterion: walking up to a parked car and "getting in" (occlusion) produces one Entered_Vehicle event.

**Phase 9 — Hardening**
- Disk-space guard
- Metrics counters (frames_dropped, triggers_dropped, db_writes, etc.) → JSON file every 30 s
- `--mp4` offline mode for regression tests
- Crash recovery integration test (kill -9 the worker, verify supervisor respawns)

**Phase 10 — Vue dashboard** (deferred, separate milestone)

The critical invariant across all phases: **at every step, `python pipeline.py` runs end-to-end and writes events to SQLite.** No phase is "done" if it requires a follow-up phase to be testable.

---

## CPU Budget (Estimated)

Assuming a typical 6-core / 12-thread modern desktop CPU (Ryzen 5 5600 class). These are estimates from training-data benchmarks for similar workloads — **measure in Phase 0 and adjust**.

| Stage | CPU % (sustained) | Notes |
|---|---|---|
| 4× StreamReader (FFmpeg decode H.264 sub @ 640×360 @ 12fps) | 15–25% total | FFmpeg decode is well-optimized; sub streams are tiny |
| InferenceWorker — YOLOv8n @ imgsz=640, 4 cams round-robin ~10 FPS each | 40–60% | The dominant cost. Scales near-linearly with imgsz² |
| ByteTrack + Zone eval + Trigger eval | 2–5% | Pure Python + numpy + shapely; cheap |
| MainStreamGrabber (idle 99% of time, brief 1080p decode on trigger) | 1–3% | Single-frame grabs; cost is in the RTSP handshake not decode |
| FaceWorker subprocess (DeepFace, idle most of time, 1–3 s burst on trigger) | 5–15% during trigger; ~0% idle | Pre-warmed model holds ~500 MB RAM idle |
| PlateWorker subprocess (EasyOCR, idle most of time) | 5–15% during trigger; ~0% idle | Pre-warmed reader holds ~400 MB RAM idle |
| EventWriter (SQLite WAL + JPEG encode) | 1–3% | JPEG encode dominates; ~50 ms per snapshot |
| **Total sustained (no triggers firing)** | **60–95%** | Tight. Headroom is the main risk. |
| **Total during a face+plate burst** | **80–100%+** | Acceptable for 1–3 s spikes |

**Risk:** On a low-end CPU (4-core mobile, older i5), sustained budget exceeds 100% and frames will start dropping. Mitigations are already in the design:
- `imgsz=480` instead of 640 → ~35% faster YOLO
- Adaptive per-camera frame skip
- Longer round-robin period (5 cams worth of work spread over 6 slots)

**Memory budget:** ~2.5 GB total (YOLOv8n ~150 MB, DeepFace ~500 MB, EasyOCR ~400 MB, OpenCV/FFmpeg buffers ~200 MB, Python overhead ~300 MB, headroom ~1 GB).

---

## Key Architectural Decisions Summary

| # | Decision | Rationale |
|---|---|---|
| 1 | Threads for I/O + inference, processes for heavy ML workers | GIL release in cv2/torch makes threading viable for the hot path; subprocess isolation prevents TF/Torch global-state conflicts and cold-start blocking |
| 2 | Single InferenceWorker, not 4 parallel | YOLOv8n already saturates cores via PyTorch intra-op pool; 4 parallel inferences fight for the same cores and cache |
| 3 | `deque(maxlen=2)` for camera frames, not `Queue` | Live video must drop-oldest silently; readers must never block |
| 4 | Drop-newest at trigger queue | Bursts of similar triggers from one track add no information; back-pressuring inference is unacceptable |
| 5 | Main-stream captures opened on demand, not held open | The entire CPU saving of "Trigger & Catch" is forfeit if 4 main streams decode continuously |
| 6 | SQLite WAL mode + single writer thread | WAL allows concurrent readers (future Vue dashboard); single writer matches SQLite's design |
| 7 | No asyncio anywhere | OpenCV is not async-aware; nothing to gain |
| 8 | Pre-warm DeepFace and EasyOCR in subprocesses at startup | 5–15 s import cost must not happen on first trigger |
| 9 | Per-camera ByteTracker instances, never shared | Track IDs must not collide across cameras |
| 10 | Polygon picker as a separate `tools/` script, not in main pipeline | Decouples a one-shot human task from the runtime |

---

## Confidence Assessment

| Area | Level | Reason |
|---|---|---|
| GIL behavior of cv2/torch/sqlite | HIGH | Well-documented; cv2 release in FFmpeg, torch release in ATen, sqlite3 release confirmed in CPython source |
| Threading vs multiprocessing split | HIGH | Standard practice for Python CV pipelines; matches every reference implementation I'm aware of |
| Drop-oldest queue policy | HIGH | Universal pattern in real-time video |
| Reconnect/backoff strategy | HIGH | Standard network resilience patterns; watchdog timer pattern is essential and battle-tested |
| Single InferenceWorker vs N workers | MEDIUM-HIGH | Correct in theory and on modern multi-core CPUs; on a 2-core machine you may want 2 workers — measure |
| CPU budget percentages | MEDIUM | Depends entirely on actual hardware; estimates are reasonable for a mid-tier desktop, must be measured in Phase 0 |
| OpenCV ffplay-flag translation working | MEDIUM | The env-var-before-import requirement is a known footgun; needs Phase 0 validation |
| DeepFace cold-start cost (~5–15 s) | MEDIUM | Varies by backend (TF vs ONNX); confirm in Phase 6 |

---

## Open Questions for Later Phases

1. **YOLOv8n vs YOLOv8s tradeoff.** If the actual CPU has more headroom than estimated, `8s` gives much better small-person detection. Decide in Phase 3 with real measurements.
2. **ByteTrack: ultralytics built-in vs `supervision` library vs standalone.** The Ultralytics integration is simplest but couples you to their tracker abstraction. Decide in Phase 2.
3. **Main-stream grab via cv2 vs short-lived ffmpeg subprocess.** If `cv2.VideoCapture` proves slow to open against this DVR (>1 s handshake), fall back to spawning `ffmpeg -frames:v 1 -f image2pipe`. Decide in Phase 5.
4. **Face embedding storage.** SQLite BLOB vs flat .npy files. Defer to Phase 6.
5. **Per-camera vs global model instance.** Currently designed as single global YOLO model — this is correct, but the ByteTracker MUST be per-camera. Confirm during Phase 3.
