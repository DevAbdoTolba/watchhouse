# Domain Pitfalls: CPU-Only Multi-Camera RTSP AI Pipeline (Windows + WSL2)

**Domain:** Home surveillance, dual-stream RTSP, YOLOv8n + ByteTrack + DeepFace + EasyOCR
**Researched:** 2026-04-13
**Scope:** Pitfalls NOT already mitigated by the project's existing design decisions
**Already solved by design (not re-listed):** CPU throttling via Trigger & Catch, UDP packet drops via forced TCP, motion blur via velocity gate, false-positive pixel motion via AI-in-zone

---

## 0. Executive Pitfall List (Priority Order)

| # | Pitfall | Severity | Phase |
|---|---------|----------|-------|
| 1 | OpenCV VideoCapture cannot recover from RTSP stall on WSL2 (no ffmpeg timeout) | Critical | Stream Ingest |
| 2 | SQLite write contention from 4 worker threads + trigger workers | Critical | Storage |
| 3 | DeepFace model auto-download blocks the trigger thread on first run | Critical | Detection (Faces) |
| 4 | ByteTrack ID switching breaks "peak bbox" face trigger silently | Critical | Tracking |
| 5 | EVENT_IMAGE_DIR fills the disk with no retention policy | Critical | Storage / Ops |
| 6 | Green frames / H.264 decode artifacts pass into models as "real" frames | High | Stream Ingest |
| 7 | Main-stream reconnect storm when triggers spike | High | Triggers |
| 8 | Zone polygon drift after a camera is bumped or re-aimed | High | Zones / Ops |
| 9 | Naive datetime + DST drift in event timestamps | High | Storage |
| 10 | EasyOCR silently allocates CUDA path then falls back, leaking RAM | High | Detection (ALPR) |
| 11 | OCR false positives from text on clothing, signs, license-plate frames | High | Detection (ALPR) |
| 12 | DeepFace false matches on low-resolution / off-angle crops | High | Detection (Faces) |
| 13 | YOLOv8n misclassifies at night under IR mode (greyscale + bloom) | High | Detection (Core) |
| 14 | WSL2 ↔ Windows path crossing for EVENT_IMAGE_DIR breaks on UI read | Medium | Storage / UI |
| 15 | DVR credentials leak via process args, crash dumps, or logged URLs | Medium | Security |
| 16 | WSL2 vmmem RAM creep — no cap, host swaps | Medium | Ops |
| 17 | OpenCV built without FFmpeg / wrong wheel — TCP env vars silently ignored | Medium | Stream Ingest |
| 18 | DVR connection cap exceeded (most BitVision/Hikvision DVRs cap at ~6 concurrent RTSP) | Medium | Stream Ingest |
| 19 | NTP drift across WSL2 boundary corrupts event ordering | Medium | Ops |
| 20 | ByteTrack ID reuse after long occlusion → wrong "Entered Vehicle" attribution | Medium | Tracking |

---

## 1. Runtime Pitfalls

### 1.1 OpenCV `VideoCapture.read()` Hangs Forever on RTSP Stall (WSL2-Specific)

**What goes wrong:** The DVR drops the TCP session (router reboot, DVR firmware glitch, NIC sleep). On Linux/WSL2, `cv2.VideoCapture` over FFmpeg has *no usable read timeout*. The thread blocks inside `read()` indefinitely. Your "graceful reconnect" code never runs because the call never returns.

**Why WSL2 specifically:** Windows host network stack + WSL2 NAT does not surface TCP RST quickly. The half-open socket can sit for tens of minutes. On bare Linux you'd get a quicker fail; on WSL2 you do not.

**Warning sign:**
- A camera worker logs nothing for >30s but the process is "alive"
- `py-spy dump` on the worker thread shows it parked in `cv2.VideoCapture.read`
- DB has no events for one camera but the others keep flowing

**Prevention:**
1. Set FFmpeg-level timeouts via env *before* opening the capture:
   ```
   OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp|stimeout;5000000|reconnect;1|reconnect_streamed;1|reconnect_delay_max;2
   ```
   `stimeout` is microseconds. This is the only knob that actually works.
2. Run each camera's `read()` in its own *thread* and have a *watchdog thread* per camera that:
   - tracks `last_frame_monotonic_ts`
   - if `now - last > N seconds`, calls `cap.release()` from the watchdog and re-opens
   - releasing a stuck capture from another thread is the only way to unblock it
3. Never share a `VideoCapture` between threads. One owner, one watchdog.
4. In offline MP4 mode, none of this fires — so test the watchdog with a *deliberately killed* RTSP feed (block port 554 with iptables for 60s), not just `Ctrl+C`.

**Phase:** Stream Ingest

---

### 1.2 OpenCV Wheel Built Without FFmpeg — TCP Env Vars Silently Ignored

**What goes wrong:** `pip install opencv-python` *usually* ships with FFmpeg, but `opencv-python-headless` on some platforms, or a system `python3-opencv` apt package, can be built against GStreamer or no backend. When that happens, `OPENCV_FFMPEG_CAPTURE_OPTIONS` is read by nothing. Your stream "works" but is silently using UDP and you're back to packet drops you thought you fixed.

**Warning sign:**
- `cv2.getBuildInformation()` does not show `FFMPEG: YES`
- Wireshark on the WSL2 vEthernet shows UDP traffic on high ports despite TCP being "set"
- Random green-frame bursts that don't correlate with anything

**Prevention:**
1. Add a startup self-check that asserts:
   ```python
   assert "FFMPEG:                      YES" in cv2.getBuildInformation()
   ```
2. Pin `opencv-python` (not `-headless` if you ever want to imshow; either way verify FFmpeg)
3. Log the resolved env at boot: `print(os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS"))`
4. On WSL2, prefer the pip wheel over apt's `python3-opencv` — apt's build is older and can lack RTSP/FFmpeg.

**Phase:** Stream Ingest (Phase 0 / setup)

---

### 1.3 Green Frames and Slice Errors From `discardcorrupt` Mode

**What goes wrong:** `discardcorrupt + nobuffer` is the right call, but on a noisy LAN you'll still occasionally get a frame where only the top quarter decoded. It's a valid `np.ndarray` of the right shape, but the bottom is uniform green or grey. YOLO happily runs on it and either misses real objects or detects a "person" in the artifact band.

**Warning sign:**
- Periodic detections at the very edge of the frame
- `Person_Detected` events at exactly the same Y coordinate
- High variance in mean per-channel intensity frame-to-frame
- ffmpeg log lines containing "concealing", "error while decoding MB", "missing reference picture"

**Prevention:**
1. Cheap sanity check before inference:
   ```python
   if frame is None or frame.size == 0: skip
   # green frame heuristic: variance in the bottom strip
   bottom = frame[-40:, :, :]
   if bottom.std() < 3.0: skip  # dead frame
   ```
2. Track a rolling decode-error counter from FFmpeg's stderr (OpenCV swallows it; redirect via `os.dup2` if you want it). When error rate > threshold, force a capture restart.
3. Drop the first 5 frames after every reconnect — they are almost always partial.

**Phase:** Stream Ingest → Detection (gate)

---

### 1.4 DVR Concurrent Connection Cap

**What goes wrong:** Hikvision-OEM DVRs (which BitVision is) typically cap at 6 simultaneous RTSP sessions across all clients. You're already using 4 sub-streams continuously. Every Trigger & Catch main-stream pull is a 5th, 6th, or 7th session. If the BitVision phone app or a browser is also viewing, you exceed the cap and get `RTSP/1.0 503 Service Unavailable` or just a TCP refusal.

**Warning sign:**
- Main-stream pulls succeed in dev, fail intermittently in production
- Trigger workers log "could not open capture" exactly when the phone app is open
- BitVision app shows "channel busy"

**Prevention:**
1. Cache main-stream `VideoCapture` per camera but keep it **closed** between events; reopen on demand with a backoff.
2. Serialize main-stream pulls globally — one main-stream open at a time across the whole pipeline (a single `threading.Semaphore(1)`).
3. Detect 503 / connection-refused as *retry with jitter*, not crash.
4. Document in README: "Close the BitVision phone app for sustained reliability."
5. If possible, query the DVR's max-stream config via its CGI/ISAPI and surface it at startup.

**Phase:** Stream Ingest / Triggers

---

### 1.5 Main-Stream Reconnect Storm When Trigger Rate Spikes

**What goes wrong:** A delivery driver walks across the driveway. ByteTrack assigns IDs `Person_12, 13, 14` (rapid ID switches under partial occlusion). Each "new" ID hits "peak bbox" within 2 seconds. You fire 3 main-stream connections, 3 face crops, 3 DeepFace calls. CPU spikes, sub-streams stall (because they share cores), more reconnects fire, cascading failure.

**Warning sign:**
- Burst of >5 events in <10s for the same physical event
- Sub-stream FPS drops to ~2 during a trigger burst
- Memory growth during bursts (DeepFace doesn't release intermediate tensors fast)

**Prevention:**
1. Per-camera trigger-rate limiter: max 1 main-stream pull per N seconds per camera.
2. Per-entity dedup: once a `Person_*` has fired a face trigger, suppress further face triggers for that ID for a TTL.
3. Use a *queue* with `maxsize` between trackers and the trigger worker. When full, drop newest. Never block the tracker thread.
4. DeepFace and EasyOCR work on a single dedicated worker thread (not per-camera). Trigger thread is the only one that opens main-streams.

**Phase:** Triggers

---

## 2. Model-Specific Pitfalls

### 2.1 ByteTrack ID Switching Breaks "Peak BBox" Face Trigger

**What goes wrong:** ByteTrack is solid for clean tracks but loses IDs on:
- Person walking behind a car (occlusion > Kalman lookahead)
- Two people crossing
- A frame drop ≥ track_buffer
- Rapid scale change (person walking toward camera fast)

Your "peak bbox" trigger fires on the *new* ID's growth curve, which starts small again. Result: you fire the face trigger on the *receding* peak (the back of the head) instead of the approaching peak.

**Warning sign:**
- Saved face crops are the back/side of heads
- DeepFace confidence is consistently low on real people
- `entity_id` for what's clearly the same person changes 3-4 times in one event

**Prevention:**
1. Tune ByteTrack:
   - `track_buffer = 60` (frames; ~4-6s at 10-15 FPS) — survive longer occlusion
   - `match_thresh = 0.7-0.8` — looser on a low-res sub-stream
   - `frame_rate` arg must match actual FPS, not stream-advertised FPS (measure it)
2. Replace "peak bbox" with "monotonic-growth-then-flat" detection:
   - Maintain a sliding window of bbox area per ID
   - Trigger when area growth slope crosses zero *and* current area is in the top quartile of the ID's history
3. Add a face-trigger candidate buffer per ID. When the ID dies, send the *largest sharpness-scored* crop from its lifetime, not the "peak moment" frame. This makes the trigger robust to ID switch by deferring the decision.
4. Compute a sharpness score (variance of Laplacian) on the cropped face region and reject crops below a threshold before calling DeepFace.

**Phase:** Tracking → Triggers

---

### 2.2 ByteTrack ID Reuse → Wrong "Entered Vehicle" Attribution

**What goes wrong:** ByteTrack reuses freed IDs after `track_buffer` expires. Person A walks out of frame as `Person_42`. 30 seconds later Person B walks in and gets `Person_42`. The vehicle-interaction logic sees `Person_42` "appearing inside Car_7 bounds" and logs "Exited Vehicle" for the wrong person, in the wrong context.

**Warning sign:**
- "Exited Vehicle" events with no preceding "Entered Vehicle"
- ID timeline shows `Person_42` with a multi-minute gap

**Prevention:**
1. Wrap ByteTrack with your own monotonic ID layer:
   ```python
   global_id = f"{camera_id}_{session_id}_{bytetrack_id}_{birth_frame}"
   ```
   Birth frame in the suffix means a reused ByteTrack ID becomes a new global ID.
2. Vehicle-interaction state machine must require both `Entered` and `Exited` to share the same global ID, otherwise discard.
3. Persist `entity_id` to DB as the global ID, not raw ByteTrack ID. Future you will thank present you.

**Phase:** Tracking

---

### 2.3 DeepFace Cold-Start Model Download Blocks the Trigger Thread

**What goes wrong:** First call to `DeepFace.represent()` or `.find()` downloads model weights (VGG-Face / Facenet / RetinaFace) from `github.com/serengil/...` to `~/.deepface/weights/`. Sizes range from 35 MB to 540 MB depending on backend. The download is *synchronous, blocking, single-threaded, and inside* the first inference call. If your network is slow or GitHub is rate-limiting, your first real event's trigger thread sits there for 2-15 minutes. Sub-stream watchdogs may meanwhile reset captures, etc.

**Worse:** If the download is interrupted, DeepFace caches a *truncated* weights file and every subsequent call raises an opaque deserialization error. There is no "redownload if corrupt" logic.

**Warning sign:**
- First-event log line `Loading VGG-Face model...` followed by long silence
- `EOFError`, `OSError: SavedModel file does not exist`, `truncated tar archive`
- `~/.deepface/weights/*.h5` smaller than published size

**Prevention:**
1. **Pre-download in the install/setup phase**, not at first inference:
   ```python
   from deepface import DeepFace
   DeepFace.build_model("Facenet")        # or whichever you pick
   DeepFace.build_model("retinaface")     # detector
   ```
   Run this in a setup script gated by `__main__`, not on import.
2. Verify the weights file size on every startup against a known-good size; if mismatch, delete and redownload before serving traffic.
3. Pick *one* face model and stick with it. Default DeepFace will silently swap models if you change a parameter.
4. Pin `deepface` and `tensorflow` versions; weight URLs and detector backends change between minor versions.

**Phase:** Detection (Faces) — addressed in setup phase

---

### 2.4 DeepFace Thread-Safety / TensorFlow Session Issues

**What goes wrong:** TensorFlow 2.x is mostly thread-safe at the call site but DeepFace internally caches models in module globals. Calling `DeepFace.represent` from two threads simultaneously can race the lazy model build. Also, TF will grab all available CPU threads by default, fighting YOLO for cores.

**Warning sign:**
- Random `KeyError` or `AttributeError` deep in DeepFace internals
- CPU pegged at 100% on all cores during a face inference
- YOLO sub-stream FPS halves while DeepFace runs

**Prevention:**
1. **One** dedicated worker thread for DeepFace. Trigger-fed via a queue.
2. Cap TF threads:
   ```python
   import tensorflow as tf
   tf.config.threading.set_intra_op_parallelism_threads(2)
   tf.config.threading.set_inter_op_parallelism_threads(1)
   os.environ["OMP_NUM_THREADS"] = "2"
   ```
   Set BEFORE importing deepface. Leave cores for YOLO.
3. Build the model once at startup in the worker thread, not per-call.

**Phase:** Detection (Faces)

---

### 2.5 DeepFace False Matches on Low-Resolution / Off-Angle Crops

**What goes wrong:** Even on a main-stream pull, the face crop coming out of a sub-stream-derived bbox upscaled to main-stream coords can be 80x80 pixels of off-axis face. DeepFace will return a "match" with high cosine similarity for the wrong person because the embedding model has learned that low-res faces are roughly equivalent.

**Warning sign:**
- Matches on people not in your gallery
- Same gallery person matching multiple unrelated visitors
- High match rate at night

**Prevention:**
1. Reject crops below a minimum size (e.g., 112x112 for Facenet, 160x160 ideal). Log as "face too small" and skip.
2. Run a face detector (RetinaFace) on the main-stream frame independently — do not just use the *YOLO person bbox top third*. Person bbox top is unreliable for face localization.
3. Use a *strict* distance threshold; the DeepFace defaults are tuned for clean photos. For Facenet512+cosine, use 0.30 instead of 0.40.
4. Require N consecutive frame matches for the same gallery identity before logging `Face_Recognized`. One frame is too noisy.
5. Log unknown faces with their crop for periodic review; do not silently accept "best match" as truth.

**Phase:** Detection (Faces)

---

### 2.6 EasyOCR Memory Footprint and Silent CUDA Fallback

**What goes wrong:**
1. `easyocr.Reader(['en'])` allocates ~1.2 GB RAM for detector + recognizer models on CPU. Your project hosts 4 stream workers + YOLO + ByteTrack + DeepFace; RAM gets tight.
2. EasyOCR defaults to `gpu=True`. On a CPU-only WSL2 box without CUDA libs, it logs `CUDA not available - defaulting to CPU` *as a warning, not an error* and proceeds. People miss this and assume it's GPU-accelerated for weeks.
3. EasyOCR also auto-downloads weights on first call (~64 MB craft + ~15 MB recognizer) with the same blocking-and-corruption risk as DeepFace.

**Warning sign:**
- WSL2 vmmem in Task Manager > 6 GB
- Per-OCR latency 3-8 seconds (not the 200-500 ms people expect from GPU benchmarks they read online)
- `CUDA not available` warning at startup

**Prevention:**
1. Instantiate `easyocr.Reader(['en'], gpu=False)` *explicitly*. Do not rely on auto-detect.
2. Pre-download weights in the setup script: instantiate Reader once at install time.
3. Run OCR in the *same* dedicated worker as DeepFace, not its own — they should never run concurrently on a CPU box.
4. Lock the language list to exactly what you need (`['en']` only). Each extra language is another 60+ MB.
5. Add a hard timeout around `reader.readtext()` (signal-based or thread+future). A pathological frame can take 30+ seconds.

**Phase:** Detection (ALPR) — addressed in setup phase

---

### 2.7 EasyOCR False Positives From Text on Clothing, Signs, Plate Frames

**What goes wrong:** EasyOCR is a general-purpose OCR. It will read "AMAZON" off a delivery uniform, "STOP" off a street sign, the dealer's logo above the plate, and the state name *above* the plate number. Without filtering you'll get `Plate_Read` events with `extracted_text="AMAZON PRIME"`.

**Warning sign:**
- Plate reads that are obviously not plates (English words, all caps brand names)
- The same "plate" text repeated across many cars

**Prevention:**
1. Run a *plate-shape detector first*. Options:
   - Use YOLO trained on plates (e.g., `yolov8n-license-plate` from community)
   - Use OpenCV contour heuristic on the car bbox: aspect ratio 2:1 to 5:1, perimeter, rectangularity
2. Validate OCR output against a regex for your locale's plate format. UK: `^[A-Z]{2}[0-9]{2}\s?[A-Z]{3}$`. US varies by state — pick one or accept a permissive class-pattern.
3. Reject all-letter results (probably a brand name) unless they match a known plate pattern.
4. Restrict OCR ROI to the *bottom 40%* of the car bbox where plates live, not the full car.
5. Require a confidence ≥ 0.6 from EasyOCR (`reader.readtext(..., low_text=0.4, text_threshold=0.7)`).

**Phase:** Detection (ALPR)

---

### 2.8 YOLOv8n Accuracy Collapses Under Night / IR Mode

**What goes wrong:** Legacy IP cameras switch to IR mode after dusk. The image becomes monochrome with a strong center bloom from the IR LEDs. YOLOv8n's COCO training data is overwhelmingly daylight color; nighttime mAP for `person` and `car` drops by 20-40%. You get false negatives (missed real intrusions) and false positives (rocks classified as people).

**Warning sign:**
- Detection counts crater between 7 PM and 7 AM in your DB
- Daytime trigger-to-event ratio is sane, nighttime is full of weird classes
- High confidence scores on small objects in the IR bloom region

**Prevention:**
1. Tune confidence threshold *per camera, per time-of-day*. Start with `conf=0.35` daytime, `conf=0.55` IR mode.
2. Detect IR mode automatically: mean saturation of the frame approaches 0 (greyscale). Switch confidence + class filters when detected.
3. Mask the IR bloom hot-spot (a fixed circle in the image center) before inference, or after inference reject any detection whose center lies in it.
4. Consider a YOLOv8n model fine-tuned on night/IR data if false-negatives matter (search HuggingFace for COCO-night fine-tunes).
5. Cap detection classes to `[0, 2, 5, 7]` (person, car, bus, truck). Prevents wildlife / handbag / cat false positives common at night.

**Phase:** Detection (Core)

---

### 2.9 YOLO Letterboxing vs Original Coordinates

**What goes wrong:** Ultralytics handles this for you, but if you ever switch to ONNX/OpenCV-DNN to save RAM, you have to undo letterbox padding manually. People forget and end up with bboxes shifted by 30-100 pixels — silently wrong, no exception thrown. ByteTrack ingests garbage.

**Warning sign:** Bounding boxes consistently offset from objects in the visualization overlay.

**Prevention:** Stick with the Ultralytics wrapper unless you have a *measured* reason to leave. If you do leave, write a unit test that compares Ultralytics output to your custom path on a fixed image.

**Phase:** Detection (Core)

---

## 3. Storage & Ops Pitfalls

### 3.1 SQLite Write Contention From Multiple Worker Threads

**What goes wrong:** SQLite supports many readers but **one writer at a time**. Your 4 sub-stream workers + 1 trigger worker all want to write `EventLog` rows. In default journal mode, you get `database is locked` errors under burst load. Naive retry-loops hide it; the lost writes pile up.

**Warning sign:**
- `sqlite3.OperationalError: database is locked`
- Event count in DB doesn't match log line counts
- Sub-stream workers stalling for 100+ ms during inserts

**Prevention:**
1. Enable WAL mode at startup, *once*:
   ```python
   conn.execute("PRAGMA journal_mode=WAL;")
   conn.execute("PRAGMA synchronous=NORMAL;")
   conn.execute("PRAGMA busy_timeout=5000;")
   ```
2. **Single-writer pattern:** All event inserts go through one dedicated DB writer thread fed by a `queue.Queue`. Workers `put()`, never `execute()`. This is the cleanest fix and removes the contention class entirely.
3. Use a separate connection per thread if you ever revert to multi-writer; never share `sqlite3.Connection` across threads.
4. Don't open a connection per insert — pool one per writer thread.
5. WAL mode files (`-wal`, `-shm`) must be on the same filesystem as the DB. If `DB_PATH` is on a Windows mount (`/mnt/c/...`), WAL is broken because Windows DrvFs locks differently. Keep the DB inside the WSL2 ext4 filesystem.

**Phase:** Storage (Phase early)

---

### 3.2 SQLite on `/mnt/c/...` (WSL2 DrvFs) Is Slow and Unreliable

**What goes wrong:** Engineers naturally put `DB_PATH=/mnt/c/Users/.../cctv_events.db` so they can browse it from Windows. DrvFs has *terrible* fsync semantics for SQLite — writes are 50-200x slower than ext4, and WAL mode locking is unreliable. You'll see corrupted DBs after a power loss.

**Warning sign:**
- Writes take 200+ ms when they should take 2 ms
- `database disk image is malformed` after host reboot
- SQLite browser opens the file from Windows but locks it for the WSL2 writer

**Prevention:**
1. Keep `DB_PATH` inside WSL2 (`/home/user/.../cctv_events.db`)
2. Provide a read-only HTTP/JSON shim for the future Vue dashboard rather than direct file access
3. If you absolutely must browse from Windows, take periodic `.backup` snapshots to `/mnt/c/...` instead of running the live DB there

**Phase:** Storage / Ops

---

### 3.3 EVENT_IMAGE_DIR Fills the Disk Silently

**What goes wrong:** Each main-stream snapshot is 100-500 KB. A busy day with 500 events is ~150 MB. Over a year, that's 50+ GB. WSL2's virtual disk file (`ext4.vhdx`) doesn't shrink when you delete files; once it grows, it stays grown until you manually compact. Eventually the host disk fills, WSL2 errors out, and the pipeline crashes mid-write.

**Warning sign:**
- `df -h` inside WSL2 shows >85% used
- `OSError: [Errno 28] No space left on device` in event saver
- DB has events with `image_path` pointing to non-existent files (writer survived, image-saver did not)

**Prevention:**
1. Implement a retention policy from day one, not as a "later" task:
   - Time-based: delete images older than N days
   - Size-based: keep total `EVENT_IMAGE_DIR` under M GB (delete oldest first)
   - Run the cleaner as a scheduled task / background thread, not on shutdown only
2. Save images as JPEG quality 80, not PNG (5-10x smaller, no perceptible loss for evidence)
3. Optionally store thumbnails (320 px) alongside full images — UI loads thumbnails, full image on click
4. **Keep "important" events forever**: tag events with `Face_Recognized` or `Plate_Read` as protected from cleanup
5. Monitor free disk space at startup and refuse to start if < 1 GB free
6. Document the WSL2 vhdx compact procedure (`Optimize-VHD`) in ops notes

**Phase:** Storage / Ops

---

### 3.4 WSL2 ↔ Windows Path Crossing Breaks UI Image Loading

**What goes wrong:** Pipeline runs in WSL2 and writes `image_path = /home/user/data/event_images/2026-04-13/evt_883.jpg`. The Vue dashboard runs in a Windows browser that cannot resolve a Linux path. Even the WSL2-side path `\\wsl$\Ubuntu\home\user\...` differs from what's in the DB.

**Warning sign:**
- Dashboard shows broken image icons but DB rows look fine
- Image works when UI runs inside WSL2, breaks from Windows browser

**Prevention:**
1. Store **relative paths** in DB: `2026-04-13/evt_883.jpg`. The runtime joins with `EVENT_IMAGE_DIR` from `.env`. The UI joins with its own configured base. Never store absolute paths.
2. Provide images via an HTTP endpoint (FastAPI / aiohttp serving `EVENT_IMAGE_DIR`), not direct filesystem access from the browser. This abstracts the path entirely.
3. Document path expectations in `.env.example`.

**Phase:** Storage / UI

---

### 3.5 Naive Datetimes + DST Drift in Event Timestamps

**What goes wrong:** `datetime.now()` returns a naive local time. Two events at "2:30 AM" on a DST fall-back night are indistinguishable; your sort by timestamp lies. Also, WSL2's clock can drift from Windows host after sleep/resume by seconds-to-minutes, so cross-camera ordering breaks.

**Warning sign:**
- Events on DST transition night appear out of order
- Loitering windows look wrong because `now() - last_seen` crosses a DST boundary
- Cross-camera correlations are off after host wake

**Prevention:**
1. Store timestamps as **UTC ISO 8601 with timezone**:
   ```python
   from datetime import datetime, timezone
   ts = datetime.now(timezone.utc).isoformat()
   ```
   SQLite has no timezone-aware type but it sorts strings correctly if you always use ISO Z.
2. Use **monotonic time** (`time.monotonic()`) for all *durations* (loitering, velocity windows). Never use wall clock for duration math.
3. Display in local time only at the UI layer.
4. Run NTP on the Windows host; verify with `wsl -d Ubuntu date` vs `date /t` after a sleep cycle. If WSL2 drifts, run `sudo hwclock -s` or install systemd-timesyncd inside WSL2.

**Phase:** Storage

---

### 3.6 Zone Polygon Drift After a Camera Is Bumped

**What goes wrong:** You hand-draw zone polygons in pixel coordinates over a reference frame. A wind storm tilts Cam 3 by 5 degrees. The "Driveway" zone is now over the neighbour's lawn. Loitering and entered-vehicle events fire on irrelevant areas. There's no automated way to know — events still flow, they're just wrong.

**Warning sign:**
- Sudden change in zone-event distribution across cameras
- A camera starts producing zero zone events while still tracking objects
- A zone event with no plausible trigger object visible in the saved image

**Prevention:**
1. Save the *reference frame used to define the zones* alongside the polygon JSON. On startup compare current frame to reference via ORB / phase correlation. If shift exceeds a threshold, log a warning and *optionally* refuse to enable that camera until reconfirmed.
2. Define zones as fractions of frame width/height, not absolute pixels, when the camera resolution changes (e.g., between sub and main streams) — but you still need shift detection for physical bumps.
3. Provide a "calibration mode" command that overlays current zones on a fresh frame and asks for re-confirmation.
4. Periodically (weekly?) save a tagged "calibration frame" so you can visually audit drift.

**Phase:** Zones / Ops

---

### 3.7 NTP Drift Across the WSL2 Boundary

**What goes wrong:** WSL2's clock is *not* automatically synced to Windows after host sleep/hibernate. After a laptop wake, WSL2 can be seconds to minutes off. Event timestamps from the pipeline will be wrong relative to wall clock and to any external system (DVR's own logs, your phone notifications).

**Warning sign:** `date` inside WSL2 differs from Windows clock by > 2s after a sleep cycle.

**Prevention:**
1. Add `sudo hwclock -s` to a per-trigger task on host wake, OR enable systemd-timesyncd in WSL2:
   ```
   sudo apt install systemd-timesyncd
   sudo timedatectl set-ntp true
   ```
2. WSL2 since Windows 11 22H2 has automatic time sync on wake; verify your version supports it.
3. Store UTC + monotonic durations to be robust against drift either way.

**Phase:** Ops

---

### 3.8 WSL2 vmmem RAM Creep — No Cap Set

**What goes wrong:** By default, WSL2 will allocate up to 50% of host RAM (or 8 GB, whichever is less, on older builds). DeepFace + EasyOCR + 4 OpenCV workers can climb to 6-8 GB. When the host needs RAM, it pages WSL2's vmmem to disk and the entire pipeline freezes for tens of seconds.

**Warning sign:**
- Periodic 10-30s pipeline freezes that correlate with Windows running other apps
- `vmmem` process in Task Manager > 6 GB
- Sub-stream watchdogs fire during a freeze

**Prevention:**
1. Cap WSL2 RAM in `%UserProfile%\.wslconfig`:
   ```
   [wsl2]
   memory=4GB
   processors=6
   swap=2GB
   ```
2. Restart WSL2 (`wsl --shutdown`) for it to apply.
3. Profile actual RSS at steady state and at burst before settling on the cap.
4. Use `tracemalloc` or `memray` to find leaks early; DeepFace and OpenCV are notorious for not releasing intermediate buffers.

**Phase:** Ops

---

### 3.9 OpenCV `imwrite` Race With UI / Scanner Reading the File

**What goes wrong:** `cv2.imwrite("evt_883.jpg", frame)` writes the file in place. Your future Vue dashboard, or a thumbnail generator, may read the file mid-write and get a truncated JPEG. Also, on WSL2 DrvFs paths, the write is not atomic.

**Prevention:**
1. Write to `evt_883.jpg.tmp` then `os.rename()` to final name (atomic on same filesystem).
2. Insert the DB row *after* the rename succeeds, not before. If the image save fails, no DB row is created — no orphaned `image_path`.

**Phase:** Storage

---

## 4. Security Pitfalls

### 4.1 DVR Credentials Leak via Process Args, Crash Dumps, Logged URLs

**What goes wrong:** Even though credentials live in `.env`, the moment you build the RTSP URL `rtsp://admin:REDACTED@192.168.1.10:554/1` and pass it to `cv2.VideoCapture()`, that string can leak via:
- `ps -ef` (if invoked via subprocess)
- Python tracebacks that include local variables (`rich.traceback`, `pretty_exceptions`)
- Logged exception messages — `cv2` sometimes echoes the URL in error strings
- Crash dumps / sentry reports
- `repr()` on the capture object in some debuggers

**Warning sign:** Grep your logs for `admin:` or your password — if any line contains it, you're leaking.

**Prevention:**
1. Never log the raw URL. Define a logging filter that masks `rtsp://[^@]+@` → `rtsp://***:***@`.
2. Disable rich/pretty tracebacks in production, or configure them with `show_locals=False`.
3. Build the URL in the smallest possible scope, then immediately overwrite the local variable with the masked version.
4. Add `.env` to `.gitignore` (already required) AND `.dockerignore` if you ever containerize.
5. Set `os.umask(0o077)` early so any files the pipeline creates aren't world-readable on multi-user systems.
6. Verify the DVR credentials are not your home Wi-Fi password or reused elsewhere — Hikvision-OEM DVRs are routinely scanned by botnets. Even on LAN, assume eventual exposure.
7. If possible, create a *dedicated read-only RTSP user* on the DVR for this pipeline (Hikvision supports per-user roles).

**Phase:** Security / Ops

---

### 4.2 `.env` File Permissions and Editor Backups

**What goes wrong:** `.env` is in `.gitignore` but VS Code creates `.env.swp`, vim creates `.env~`, and these aren't gitignored by default. Also, `.env` itself often ends up world-readable (`644`) because that's the default umask.

**Prevention:**
1. `.gitignore`: add `.env`, `.env.*`, `*.env`, `.env.swp`, `.env~`
2. `chmod 600 .env`
3. Provide `.env.example` with placeholder values, committed to git
4. Optional: use `python-dotenv` with `dotenv_values()` not `load_dotenv()` so secrets aren't injected into `os.environ` where every subprocess sees them

**Phase:** Security / Ops

---

### 4.3 RTSP Over LAN Is Plaintext

**What goes wrong:** Forced TCP doesn't mean encrypted. RTSP credentials are sent in basic-auth or digest-auth in cleartext on port 554. Anyone on your LAN with `tcpdump` reads them. Most home setups are fine, but a guest device on Wi-Fi changes the threat model.

**Prevention:**
1. Acknowledge in docs that LAN security is the perimeter. Keep the DVR off guest Wi-Fi.
2. Long-term: VLAN the cameras and DVR onto an isolated subnet that only the pipeline host can reach.
3. There is no RTSP-over-TLS support on a legacy DVR. Accept it or replace the DVR (out of scope).

**Phase:** Security / Documented Constraint

---

## 5. Phase Mapping Summary

| Pitfall | Stream Ingest | Detection | Tracking | Triggers | Storage | Security | Ops |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| 1.1 RTSP stall on WSL2 | X |  |  |  |  |  | X |
| 1.2 OpenCV without FFmpeg | X |  |  |  |  |  | X |
| 1.3 Green / corrupted frames | X | X |  |  |  |  |  |
| 1.4 DVR connection cap | X |  |  | X |  |  |  |
| 1.5 Reconnect storm |  |  |  | X |  |  |  |
| 2.1 ByteTrack ID switch + peak bbox |  |  | X | X |  |  |  |
| 2.2 ByteTrack ID reuse |  |  | X |  |  |  |  |
| 2.3 DeepFace cold-start download |  | X |  |  |  |  | X |
| 2.4 DeepFace thread safety |  | X |  |  |  |  |  |
| 2.5 DeepFace false matches |  | X |  |  |  |  |  |
| 2.6 EasyOCR memory + CUDA fallback |  | X |  |  |  |  | X |
| 2.7 EasyOCR text false positives |  | X |  |  |  |  |  |
| 2.8 YOLOv8n night/IR collapse |  | X |  |  |  |  |  |
| 2.9 YOLO letterbox math |  | X |  |  |  |  |  |
| 3.1 SQLite write contention |  |  |  |  | X |  |  |
| 3.2 SQLite on DrvFs |  |  |  |  | X |  | X |
| 3.3 EVENT_IMAGE_DIR fills disk |  |  |  |  | X |  | X |
| 3.4 WSL2/Windows path crossing |  |  |  |  | X |  |  |
| 3.5 DST / naive datetimes |  |  |  |  | X |  |  |
| 3.6 Zone polygon drift |  |  |  |  |  |  | X |
| 3.7 NTP drift across WSL2 |  |  |  |  |  |  | X |
| 3.8 vmmem RAM creep |  |  |  |  |  |  | X |
| 3.9 imwrite race |  |  |  |  | X |  |  |
| 4.1 Credentials leak in logs |  |  |  |  |  | X | X |
| 4.2 `.env` perms / editor backups |  |  |  |  |  | X |  |
| 4.3 Plaintext RTSP on LAN |  |  |  |  |  | X |  |

---

## 6. Phase-Specific Warnings (Quick Reference for Roadmap Author)

### Phase: Stream Ingest
**Likely pitfalls:** 1.1, 1.2, 1.3, 1.4
**Mitigations to bake into the very first deliverable:**
- `stimeout` in the FFmpeg env string (not just `rtsp_transport;tcp`)
- Per-camera capture-owner thread + watchdog thread pattern
- `cv2.getBuildInformation()` startup assertion
- Decoded-frame sanity check (variance / shape)

### Phase: Detection (Core / Faces / ALPR)
**Likely pitfalls:** 2.3, 2.4, 2.5, 2.6, 2.7, 2.8
**Mitigations:**
- Pre-download all model weights in setup, verify file sizes on every boot
- Single dedicated worker thread for both DeepFace and EasyOCR with bounded queue
- TF + OMP thread caps set BEFORE importing deepface/tensorflow
- Locale plate regex + ROI restriction for ALPR
- Time-of-day / IR-aware confidence thresholds for YOLO

### Phase: Tracking
**Likely pitfalls:** 2.1, 2.2
**Mitigations:**
- Wrap raw ByteTrack ID with monotonic global ID
- "Largest sharpness-scored crop over ID lifetime" instead of instantaneous peak
- Tune `track_buffer`, `match_thresh`, `frame_rate` for actual sub-stream FPS

### Phase: Triggers
**Likely pitfalls:** 1.4, 1.5, 2.1
**Mitigations:**
- Global single semaphore on main-stream open
- Per-camera and per-entity trigger rate limits
- Bounded queue between trackers and trigger worker, drop-newest policy

### Phase: Storage
**Likely pitfalls:** 3.1, 3.2, 3.3, 3.4, 3.5, 3.9
**Mitigations:**
- Single-writer DB thread, WAL mode, `busy_timeout`, `synchronous=NORMAL`
- DB lives on ext4 inside WSL2, never on `/mnt/c`
- Relative `image_path` in DB
- UTC ISO timestamps + monotonic durations
- `imwrite` to `.tmp` then atomic rename, then DB insert
- Retention policy implemented in Phase 1, not "later"

### Phase: Security
**Likely pitfalls:** 4.1, 4.2, 4.3
**Mitigations:**
- Logging filter that masks `rtsp://user:pass@` patterns
- Disable rich tracebacks with `show_locals=True` in production
- `chmod 600 .env`, gitignore swap files
- Dedicated read-only DVR user

### Phase: Ops / Lifecycle
**Likely pitfalls:** 1.1, 2.3, 3.2, 3.3, 3.6, 3.7, 3.8
**Mitigations:**
- `.wslconfig` RAM/CPU caps documented
- Ops runbook: vhdx compaction, NTP drift check, zone calibration ritual
- Disk-free check at startup
- Periodic calibration frame snapshot

---

## 7. Sources & Confidence

| Pitfall area | Confidence | Basis |
|---|---|---|
| OpenCV/FFmpeg RTSP stall behavior | HIGH | OpenCV issue tracker, FFmpeg `stimeout` documented behavior, widely reported |
| `OPENCV_FFMPEG_CAPTURE_OPTIONS` requiring FFmpeg-built wheel | HIGH | OpenCV docs + getBuildInformation contract |
| ByteTrack ID switch under occlusion | HIGH | ByteTrack paper + tracker community consensus |
| DeepFace cold-start blocking download | HIGH | DeepFace source code (lazy `download` in `functions.py`) |
| DeepFace thread-safety caveats | MEDIUM | TF threading docs + DeepFace module-global model cache pattern |
| EasyOCR ~1.2 GB CPU footprint | MEDIUM | community benchmarks, varies by language pack |
| EasyOCR silent CUDA fallback | HIGH | Default `gpu=True`, warning-only fallback in source |
| SQLite WAL + busy_timeout pattern | HIGH | SQLite official docs |
| SQLite corruption on DrvFs | HIGH | Microsoft WSL docs + SQLite forum threads |
| WSL2 vmmem behavior + .wslconfig | HIGH | Microsoft docs |
| WSL2 NTP drift on sleep | MEDIUM | Microsoft GitHub issues, fixed in newer Win11 builds |
| Hikvision-OEM RTSP session caps | MEDIUM | Vendor documentation varies; ~6 is typical for consumer DVRs |
| Plaintext RTSP basic/digest auth | HIGH | RFC 2326 |
| YOLOv8n night/IR accuracy drop | MEDIUM | COCO training distribution, community reports |

**LOW confidence items (flag for empirical validation in early phases):**
- Exact RAM footprint of EasyOCR on this specific host (measure)
- Actual DVR concurrent stream cap for this BitVision unit (test by opening 7 streams)
- Whether the user's WSL2 build has automatic NTP sync on wake (check Win11 build number)
- Whether OpenCV pip wheel on user's Python version is FFmpeg-enabled (run `getBuildInformation`)

These four should become *Phase 0 verification tasks* in the roadmap rather than assumptions.
