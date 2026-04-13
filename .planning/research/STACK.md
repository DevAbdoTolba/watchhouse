# Stack Research — Home CCTV AI Pipeline

**Domain:** CPU-only multi-camera RTSP computer-vision surveillance (Python, Windows + WSL2 Ubuntu 24.04, 4 legacy IP cameras via DVR)
**Researched:** 2026-04-13
**Overall confidence:** HIGH for runtime / detection / tracking / OpenVINO; MEDIUM for face & OCR (multiple workable choices, recommendation is opinionated).

---

## TL;DR — Executive Recommendation

Keep ~80% of the user's proposed stack. Two changes materially improve the project and one is optional but strongly advised:

1. **Replace standalone `ByteTrack` (the `ifzhang/ByteTrack` repo) with Ultralytics' built-in tracker (`tracker="bytetrack.yaml"`).** It is the same algorithm, ships with Ultralytics, and removes a dead dependency that has no PyPI package and requires `python setup.py develop`. **(HIGH confidence, mandatory)**

2. **Export YOLO weights to OpenVINO IR and run inference through Ultralytics' OpenVINO backend.** OpenVINO is ~2.5–3× faster than PyTorch CPU inference on Intel CPUs and is the single biggest CPU-budget win available. **(HIGH confidence, mandatory for hitting 4× streams @ 10–15 FPS)**

3. **Strongly consider `YOLO26n` instead of `YOLOv8n`.** Released 2026-01-14 by Ultralytics, NMS-free, ~43% faster on CPU than the prior generation, drop-in API-compatible. Same `model.track(...)` interface. **(HIGH confidence, recommended.)** Caveat: AGPL-3.0 license — already true of YOLOv8 too, so no new legal exposure for a personal home project.

Everything else (OpenCV RTSP-over-TCP, DeepFace, EasyOCR, SQLite, python-dotenv, future Vue 3 dashboard) is fine as specified, with version pins and a few caveats below.

---

## Core Runtime

| Technology | Pinned Version | Purpose | Why |
|---|---|---|---|
| **Python** | `3.11.x` (CPython, inside WSL2 Ubuntu 24.04) | Host language | 3.11 is the sweet spot for the entire pipeline: TensorFlow (DeepFace), PyTorch ≥ 2.11, OpenVINO 2026.1, NumPy 2.x, Ultralytics, EasyOCR all support it; 3.13/3.14 still trail in TF/Paddle wheel availability. |
| **opencv-python (headless)** | `opencv-python-headless==4.13.0.92` | RTSP ingest, frame I/O, polygon math, drawing | Latest stable on PyPI (verified via `pypi.org/pypi/opencv-python/json`). Use the **headless** build inside WSL2 — you do not need GTK/Qt; saves ~80 MB and removes X11 issues. |
| **ffmpeg** (system) | `ffmpeg ≥ 6.x` from Ubuntu 24.04 apt | Backs OpenCV's VideoCapture for RTSP | OpenCV's `cv2.CAP_FFMPEG` shells out to libav* — the system ffmpeg version controls RTSP behavior. Ubuntu 24.04 ships ffmpeg 6.x which honors `nobuffer`, `discardcorr`, and `rtsp_transport;tcp`. |
| **NumPy** | `numpy==1.26.4` (NOT 2.x) | Array math | **CRITICAL pin.** TensorFlow 2.16/2.17 (DeepFace's runtime) historically broke on NumPy 2.x, and several mid-2024 DeepFace bug reports (`serengil/deepface#1376`, `#1135`) trace to TF/NumPy ABI mismatches. Pinning `numpy<2` is the safest path until the entire stack confirms 2.x parity. Ultralytics, OpenCV, ONNX Runtime, EasyOCR all work with 1.26.x. |
| **python-dotenv** | `python-dotenv==1.2.2` | Load `.env` secrets | Released 2026-03-01; supports Python 3.10+; nothing else needed. |

**Confidence:** HIGH. All versions verified against PyPI metadata 2026-04-13.

---

## Detection

| Technology | Pinned Version | Purpose | Why |
|---|---|---|---|
| **Ultralytics** | `ultralytics==8.4.37` | YOLO loader, training, export, **built-in tracker**, OpenVINO export | Latest release 2026-04-10. The library is the canonical loader for YOLOv8/YOLO11/YOLO26 weights. Includes `model.track()`, ONNX export, OpenVINO export, and benchmarking. **Verified via PyPI + GitHub releases.** |
| **YOLO model weights** | **`yolo26n.pt` (recommended)** *or* `yolov8n.pt` (safe fallback) | Person + vehicle detection on sub-streams | YOLO26n was released 2026-01-14 with NMS-free inference, ~38.9 ms ONNX CPU latency, and "up to 43% faster on CPU" over the prior generation per Ultralytics docs. YOLOv8n remains a known-good fallback if YOLO26 weights cause any reproducibility issue early on. |
| **OpenVINO Runtime** | `openvino==2026.1.0` | Optimised CPU inference backend for Intel CPUs | Verified via PyPI metadata (latest 2026.1.0). Ultralytics docs report ~2.8× speed-up on YOLO11n moving PyTorch → OpenVINO on Intel CPUs. For 4 sub-streams @ 10–15 FPS this is the difference between feasible and infeasible. |
| **ONNX Runtime** | `onnxruntime==1.24.4` | Fallback CPU backend (also used by InsightFace if you switch face libs later) | Latest release on PyPI. Use OpenVINO first; keep ORT around as a portability backstop and because some libs (insightface, future ALPR models) depend on it directly. |

### Inference path (production)

```python
from ultralytics import YOLO
# One-time export (run once, commit the resulting folder OR cache locally)
m = YOLO("yolo26n.pt")
m.export(format="openvino", half=False, dynamic=False, imgsz=640)
# Then load the OpenVINO model in the actual pipeline
det = YOLO("yolo26n_openvino_model/")
results = det.track(frame, persist=True, tracker="bytetrack.yaml",
                    classes=[0, 2], imgsz=640, conf=0.4, verbose=False)
```

`classes=[0, 2]` filters to COCO `person` (0) and `car` (2). Add `5` (bus) and `7` (truck) if you want broader vehicle coverage.

**Confidence:** HIGH for Ultralytics + OpenVINO path; HIGH for YOLOv8n; MEDIUM for YOLO26n purely because the model is 3 months old at time of research and field-testing under "sub-stream H.264 + heavy IR compression" conditions is sparser than YOLOv8.

---

## Tracking

| Technology | Pinned Version | Purpose | Why |
|---|---|---|---|
| **Ultralytics built-in ByteTrack** | shipped with `ultralytics==8.4.37` (no separate install) | Persistent IDs, centroid history → velocity | The Ultralytics docs explicitly support `tracker="bytetrack.yaml"` and `tracker="botsort.yaml"`. It is the same Tianheng Cheng / Yifu Zhang ByteTrack algorithm, integrated and maintained alongside the detector. |
| **lap** | `lap==0.5.13` | Linear assignment (Hungarian) for ByteTrack matching | Released 2026-02-23 with NumPy 2.x-compatible wheels for Python 3.8–3.14. Ultralytics tracker pulls this in automatically; pinning it avoids a wheel-build situation on WSL2. |
| **scipy** | `scipy>=1.13,<2` | Required by Ultralytics + lap fallbacks | Standard. |

**Anti-recommendation: do NOT use `ifzhang/ByteTrack` directly.**
- No PyPI package; requires `git clone && python setup.py develop`.
- Last meaningful commits are years old; the active fork is the Ultralytics integration.
- Pulls in YOLOX as a training-time dependency that is irrelevant to your pipeline.
- Forces you to write your own YOLOv8 → ByteTrack adapter when Ultralytics already did it.

If you ever need re-identification across long occlusions, switch the YAML to `botsort.yaml` (also built into Ultralytics, adds ReID + camera motion compensation). Default to `bytetrack.yaml` for CPU.

**Confidence:** HIGH.

---

## Face Recognition (event-triggered)

| Technology | Pinned Version | Purpose | Why |
|---|---|---|---|
| **DeepFace** | `deepface==0.0.99` | Face verification + identification when a tracked person's bbox peaks | Latest on PyPI; 22.5k stars; actively maintained (`serengil/deepface`); supports many backends (VGG-Face, FaceNet, **ArcFace**, SFace, GhostFaceNet) and detectors (RetinaFace, MTCNN, YuNet, OpenCV). Matches the user's spec, works on CPU, and has the friendliest one-call API. |
| **TensorFlow (CPU wheel)** | `tensorflow-cpu==2.16.2` | DeepFace's runtime | Use the **CPU-only** wheel — no CUDA garbage on a GPU-less machine. Pin to 2.16.2 specifically: 2.17 had documented breakage with DeepFace's VGG-Face model loader (issue #1376), 2.16 is the last reliably tested branch, and the `tensorflow-cpu` distribution exists on PyPI with manylinux wheels that work cleanly inside WSL2. |
| **tf-keras** | `tf-keras==2.16.0` | Keras 2 shim for TF 2.16 | TF 2.16 ships Keras 3 by default, which DeepFace does not yet fully support. Installing `tf-keras` and setting `os.environ["TF_USE_LEGACY_KERAS"] = "1"` keeps DeepFace happy. **This is the single most common DeepFace install footgun in 2025/26.** |
| **retina-face** | pulled in transitively | Default DeepFace detector | Keep DeepFace's default detector (`detector_backend="retinaface"`) for the trigger frame — accuracy matters more than speed because it only fires on event, and the upstream face crop already comes from a YOLO person bbox. |

### Recommended DeepFace configuration

```python
from deepface import DeepFace
DeepFace.find(
    img_path=face_crop,                # cropped from main-stream snapshot
    db_path="data/known_faces/",       # one folder per identity
    model_name="ArcFace",              # best accuracy/CPU tradeoff
    detector_backend="retinaface",
    enforce_detection=False,
    distance_metric="cosine",
    silent=True,
)
```

### Alternative considered: InsightFace + buffalo_l

`insightface==0.7.3` (last release April 2023) wraps the ArcFace `buffalo_l` ONNX model and runs through `onnxruntime`. Pros: lighter weight than the TF stack, no Keras-version drama, sub-50 ms CPU latency for a single face, the literature consensus is that it edges DeepFace on raw recognition accuracy.

Cons for **this** project specifically:
- No active releases in ~3 years (still works, but means you carry the maintenance burden).
- Removes TF entirely, which is a positive — but you would also need to write your own enrolment/index/search loop because InsightFace gives you embeddings, not a `find()`.
- DeepFace's `find()` already does the SQLite-of-faces dance (it pickles the embedding DB next to the images) which matches your "minimal ops, single-machine, zero-budget" constraints.

**Verdict:** DeepFace is the right choice **for this project's ergonomics**. Switch to InsightFace later only if (a) DeepFace's TF dependency causes pain, or (b) recognition accuracy on your home dataset is unacceptable.

**Confidence:** MEDIUM-HIGH (multiple workable options; recommendation is ergonomic, not "objectively only correct").

---

## OCR / ALPR (event-triggered)

| Technology | Pinned Version | Purpose | Why |
|---|---|---|---|
| **EasyOCR** | `easyocr==1.7.2` | Read license-plate text on triggered, parked-car frames | Latest on PyPI (2024-09-24). Single-call API, ships its own detector + recogniser, runs on CPU, no Paddle ecosystem to install. The accuracy gap vs. PaddleOCR matters most for moving / blurry plates — your centroid-velocity gate already eliminates that case, so EasyOCR is "good enough" by design. |
| **torch (CPU)** | `torch==2.5.1+cpu` (install from official PyTorch CPU index) | EasyOCR's backend | Pin to a CPU wheel (`pip install torch --index-url https://download.pytorch.org/whl/cpu`). PyTorch 2.11 is the latest, but 2.5.x is the most-tested release line for the EasyOCR + Ultralytics combination and avoids dragging in CUDA libraries on Windows/WSL2. |

### Pre-OCR pipeline (do this — it matters more than which OCR you pick)

1. Run the YOLO car detection on the main-stream frame.
2. Crop a generous bbox around the **front grille / rear bumper** region (heuristic from car bbox aspect ratio).
3. Convert to grayscale, CLAHE-equalise, deskew via Hough lines.
4. *Then* call EasyOCR on the cleaned crop.

Steps 2–3 are the difference between 60% and 90% plate accuracy on legacy 1080p sub-stream cameras at night. EasyOCR is fine; raw frames are not.

### Alternative considered: PaddleOCR (PP-OCRv5)

| | EasyOCR 1.7.2 | PaddleOCR 3.4.0 (PP-OCRv5) |
|---|---|---|
| Accuracy on plates (literature) | Good | Better (~+13% in PP-OCRv5 tests; better on noise / low contrast) |
| Install footprint | Small (PyTorch + a couple of models) | Heavy (PaddlePaddle wheel, ~500 MB, distinct ecosystem) |
| Active maintenance | Slow (last release Sep 2024) | Very active (3.x in 2025/2026, VL release Q1 2026) |
| API ergonomics | One call | Multi-step (detector + classifier + recogniser) |
| WSL2 wheel reliability | Excellent | Mixed (PaddlePaddle CPU wheels occasionally lag) |

**Verdict:** Stick with EasyOCR for the v1 pipeline because (a) the velocity gate makes the accuracy gap small, (b) install/operate burden matters on a zero-ops home machine, and (c) you can swap to PaddleOCR behind a `OcrAdapter` interface in a later phase if accuracy turns out to be the bottleneck. **Design the OCR call site as an interface from day one** so that swap is a one-file change.

**Confidence:** MEDIUM. EasyOCR is a defensible choice, not a clear winner. Architectural escape hatch (the adapter) is the important risk mitigation.

---

## Storage

| Technology | Pinned Version | Purpose | Why |
|---|---|---|---|
| **SQLite** | bundled with Python `sqlite3` (Python 3.11 ships SQLite 3.40+) | `cctv_events.db` event log | Zero-ops, single-file, transactional, perfect for one-machine event logs. No need for `aiosqlite` — your write rate is "a few events per minute". |
| **filesystem** | `EVENT_IMAGE_DIR` (per spec) | Triggered main-stream JPEGs | Date-partitioned (`./data/event_images/YYYY-MM-DD/evt_<uuid>.jpg`). Don't put images in the DB. |

Optional (later):
- **Alembic** for schema migrations once the `EventLog` table starts evolving. Skip until you actually need to alter the schema in production.
- **Litestream** for one-line continuous SQLite replication to disk/cloud — only relevant if you ever want off-machine backups.

**Confidence:** HIGH.

---

## Configuration & Secrets

| Technology | Pinned Version | Purpose | Why |
|---|---|---|---|
| **python-dotenv** | `python-dotenv==1.2.2` | Load `.env` at process start | Per spec. Latest release. |
| **pydantic** *(optional but recommended)* | `pydantic==2.10.x` | Typed settings object wrapping `os.environ` | One `Settings(BaseSettings)` class gives you typed access to `DVR_IP`, `DVR_PORT`, `EVENT_IMAGE_DIR`, etc., plus boot-time validation that the env file is complete. Adds ~3 MB and saves an entire category of "I forgot to set X" bugs. |
| **`.gitignore` enforcement** | n/a | Ensure `.env` never reaches a remote | Per spec — already a Decision. |

**Confidence:** HIGH.

---

## RTSP / Stream Handling Specifics (Windows + WSL2)

These are not "libraries" but they are stack-level decisions that belong here because they materially affect which versions you can use.

| Concern | Decision | Rationale |
|---|---|---|
| Where does the pipeline run? | **Inside WSL2 Ubuntu 24.04**, not on Windows directly | Native ffmpeg, predictable wheel availability for OpenVINO/torch/TF, Linux-style `os.environ`. Confirmed working pattern per OpenCV forum threads on RTSP-from-WSL2. |
| Network reach to DVR @ `192.168.1.10` | WSL2 must use **mirrored networking mode** (Windows 11 22H2+) or a Hyper-V bridged adapter | Default NAT mode in older WSL2 setups makes the DVR LAN unreachable from inside Ubuntu. Add `[wsl2]\nnetworkingMode=mirrored` to `%USERPROFILE%\.wslconfig`. **This is the #1 WSL2-specific gotcha for this project.** |
| Forcing TCP transport | `os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp\|fflags;nobuffer\|flags;low_delay\|reorder_queue_size;0\|max_delay;500000"` (set **before** importing cv2) | Mirrors the working `ffplay` flags in `terminal.txt`. Pipe `\|` separator, **must** be set before `import cv2`. |
| Buffer size | `cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)` | Per spec; prevents the "playing 4 seconds in the past" symptom. |
| Reconnect strategy | Each camera in its own thread/process; `cap.read()` returns False → exponential backoff reconnect (1s → 2s → 4s, capped at 30s) | OpenCV's `VideoCapture` does not auto-reconnect; you must wrap it. |
| GPU passthrough | **Not relevant.** Even if you had a CUDA GPU, this project is CPU-only by constraint, and WSL2 GPU passthrough adds drivers/headaches with no benefit. | — |

**Confidence:** HIGH for the env var and buffer settings; MEDIUM for the mirrored-networking advice (depends on Windows build — confirm with `wsl --version`).

---

## Future UI (deferred milestone)

| Technology | Pinned Version | Purpose | Why |
|---|---|---|---|
| **Vue 3** | `vue@^3.5` | SPA framework for the dashboard | Per spec. Vue 3 + Composition API + `<script setup>`. |
| **Vite** | `vite@^6` | Dev server + bundler | Standard Vue 3 toolchain. |
| **TypeScript** | `typescript@^5.6` | Type safety for the small dashboard | Tiny app, but still worth typing the `EventLog` row interface once and reusing it. |
| **A small JSON-over-HTTP API** | `fastapi==0.115.x` + `uvicorn==0.32.x` | Read-only endpoint surfacing `EventLog` and serving event JPEGs | Vue cannot read SQLite directly. FastAPI is ~30 lines of code for `GET /events`, `GET /events/{id}/image`, integrates trivially with `pydantic`, and runs as one more `python -m uvicorn ...` process. **Do not** use Flask — DeepFace already drags Flask in transitively, but for the dashboard FastAPI's typed schema-from-pydantic is a much better fit. |
| **CSS** | Plain CSS / CSS variables, no Tailwind | Match the spec's `#151a2c` dark theme with ~50 lines of CSS | Tailwind is overkill for a single-screen dashboard. The whole point is "minimalist". |

**Confidence:** HIGH for the framework choice, MEDIUM for FastAPI (Flask works, FastAPI is just nicer).

---

## Installation Recipe (single virtualenv, WSL2 Ubuntu 24.04)

```bash
# inside WSL2
sudo apt update && sudo apt install -y python3.11 python3.11-venv ffmpeg libgl1
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel

# --- Core ---
pip install \
  "numpy==1.26.4" \
  "opencv-python-headless==4.13.0.92" \
  "python-dotenv==1.2.2" \
  "pydantic>=2.10,<3"

# --- Detection + tracking + CPU acceleration ---
pip install "ultralytics==8.4.37" "openvino==2026.1.0" "onnxruntime==1.24.4" "lap==0.5.13"

# --- PyTorch CPU build (for EasyOCR) ---
pip install torch==2.5.1+cpu --index-url https://download.pytorch.org/whl/cpu

# --- OCR ---
pip install "easyocr==1.7.2"

# --- Face recognition (last, because TF is fussy) ---
pip install "tensorflow-cpu==2.16.2" "tf-keras==2.16.0" "deepface==0.0.99"

# --- Future UI (deferred) ---
# pip install "fastapi==0.115.*" "uvicorn[standard]==0.32.*"
```

**One-time YOLO export to OpenVINO IR (run once, cache the result):**

```bash
python -c "from ultralytics import YOLO; YOLO('yolo26n.pt').export(format='openvino', imgsz=640)"
```

---

## Version Compatibility Matrix

| A | B | Status | Notes |
|---|---|---|---|
| `numpy 1.26.4` | `tensorflow-cpu 2.16.2` | OK | TF 2.16 wheels link against NumPy 1.x ABI. NumPy 2.x is the breakage line. |
| `tensorflow-cpu 2.16.2` | `deepface 0.0.99` | OK **with** `tf-keras 2.16.0` + `TF_USE_LEGACY_KERAS=1` | Without the Keras-2 shim DeepFace's VGG-Face / ArcFace loaders fail. |
| `tensorflow-cpu 2.17+` | `deepface 0.0.99` | BROKEN | Issue #1376 in `serengil/deepface`. Do not upgrade TF. |
| `ultralytics 8.4.37` | `torch 2.5.1+cpu` | OK | Ultralytics supports torch ≥ 1.8; 2.5.x is well-trodden. |
| `ultralytics 8.4.37` | `openvino 2026.1.0` | OK | Ultralytics OpenVINO export targets 2024+; 2026.1 verified fine in current docs. |
| `easyocr 1.7.2` | `torch 2.5.1+cpu` | OK | EasyOCR pins `torch>=1.4`. |
| `easyocr 1.7.2` | `numpy 1.26.4` | OK | — |
| `opencv-python-headless 4.13` | `numpy 1.26.4` | OK | — |
| `opencv-python-headless 4.13` | `numpy 2.x` | OK technically | OpenCV 4.13 wheels are NumPy 2 compatible, but irrelevant given the TF pin above. |
| `lap 0.5.13` | `python 3.11` | OK | Wheels available on manylinux. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|---|---|---|
| **`ifzhang/ByteTrack` repo as a dependency** | No PyPI release, requires `setup.py develop`, brittle install, drags in YOLOX, effectively unmaintained for end-user use | Ultralytics built-in `tracker="bytetrack.yaml"` — same algorithm |
| **`opencv-python` (full)** *inside WSL2* | Pulls GTK/Qt deps for a `cv2.imshow()` you will never use; can fail to import in a headless WSL2 setup | `opencv-python-headless` |
| **`tensorflow` (the GPU meta-package)** | Drags in `nvidia-cudnn-cu12`, `nvidia-cublas-cu12`, etc. — hundreds of MB of CUDA libraries on a CPU-only box | `tensorflow-cpu` |
| **`tensorflow >= 2.17`** | Documented incompatibility with current DeepFace release | `tensorflow-cpu==2.16.2` + `tf-keras==2.16.0` |
| **NumPy 2.x** (right now) | Breaks the TF/DeepFace path | `numpy==1.26.4` |
| **DeepSORT** (e.g. `deep_sort_realtime`) | Heavier than ByteTrack (runs a ReID CNN every frame), no accuracy benefit on a home setup with non-overlapping cameras | ByteTrack via Ultralytics |
| **YOLOv5 / YOLOv4 / YOLOv7** | Older, slower, less accurate, and not maintained inside Ultralytics' CPU-optimised pipeline | YOLO26n (or YOLOv8n fallback) |
| **OpenALPR / commercial ALPR SDKs** | Cost money, contradict the "$0 budget" constraint, and most have abandoned their open-source forks | EasyOCR (with the pre-OCR cleanup pipeline above) |
| **Pixel-based motion detection (cv2.BackgroundSubtractorMOG2)** | Already ruled out in spec; included here so it never sneaks back in | YOLO-class-in-polygon-zone |
| **UDP RTSP transport** | LAN drops cause frame corruption; already proven painful via `ffplay` testing | Forced TCP via `OPENCV_FFMPEG_CAPTURE_OPTIONS` |
| **Running everything in one thread** | `cv2.VideoCapture.read()` will block all four cameras | One thread (or multiprocessing.Process) per camera; main thread schedules YOLO + tracker |
| **Tailwind / shadcn / a UI framework** for the dashboard | Wildly disproportionate for a single dark-mode event-list page | Plain CSS variables; Vue 3 SFCs |
| **Postgres / Redis / Mongo** for event storage | Spinning up a service contradicts "single local machine, zero ops" | SQLite |
| **A message broker (Kafka / Redis Streams) for inter-camera events** | Same reason — overkill | In-process `queue.Queue` or `asyncio.Queue` |
| **Storing event JPEGs in the SQLite DB** | Bloats the file, slows queries, breaks easy filesystem inspection | Files on disk + `image_path` column |

---

## Stack Patterns by Variant

**If you stay strictly on Ultralytics' "happy path":**
- Use `model.track(frame, persist=True, tracker="bytetrack.yaml")` and read `result.boxes.id` for ByteTrack IDs.
- Velocity = derivative of `result.boxes.xywh[:, :2]` smoothed over a 5-frame ring buffer per ID.
- This is the lowest-friction path and what the roadmap should assume.

**If you ever switch off Ultralytics (e.g. testing a new detector):**
- Keep a thin `Detector` interface (`detect(frame) -> list[Detection]`).
- Wrap a vendored `yolox.tracker.byte_tracker.BYTETracker` if Ultralytics is unavailable.
- Velocity logic stays unchanged — it operates on `Detection` objects, not on YOLO output directly.
- This indirection is cheap (~50 lines) and pays off the first time you want to A/B test.

**If your CPU turns out to be too weak even with OpenVINO:**
- Drop sub-stream FPS to 5 (the velocity logic still works; loitering still works).
- Skip every other frame (`if frame_idx % 2: continue`) before YOLO.
- Reduce `imgsz` from 640 → 480 → 416 (each step ~2× faster on CPU; mAP loss is small for nano).
- Process only 2 cameras at a time on a round-robin (camera A+B for 5 minutes, then C+D).
- Last resort: replace YOLO26n with `yolov8n.pt` at `imgsz=320` — fastest viable config on weak Intel CPUs.

---

## Confidence Summary

| Decision | Confidence | Why |
|---|---|---|
| Python 3.11 + opencv-python-headless 4.13 + ffmpeg from apt | HIGH | Verified versions; pattern matches working OpenCV WSL2 RTSP threads |
| Ultralytics 8.4.37 with built-in ByteTrack | HIGH | Verified PyPI + GitHub release + Ultralytics docs explicitly support it |
| **Replace standalone ByteTrack repo with Ultralytics built-in tracker** | HIGH | Standalone repo has no PyPI package and is effectively superseded |
| **Add OpenVINO IR export for CPU acceleration** | HIGH | Ultralytics docs cite ~2.8× CPU speedup; this is the project's biggest CPU win |
| YOLO26n over YOLOv8n | MEDIUM-HIGH | Documented 43% CPU improvement; only "medium" because the model is 3 months old at research time. Trivially reversible (`yolov8n.pt` is a 1-line fallback). |
| NumPy 1.26.4 pin (avoid NumPy 2.x) | HIGH | Documented DeepFace/TF issues with NumPy 2.x ABI |
| TensorFlow-CPU 2.16.2 + tf-keras 2.16.0 + `TF_USE_LEGACY_KERAS=1` | HIGH | Most common DeepFace install footgun; explicit issue references in `serengil/deepface` |
| DeepFace 0.0.99 with ArcFace + RetinaFace | MEDIUM-HIGH | Best ergonomics for this project; InsightFace+buffalo_l is a defensible alternative |
| EasyOCR 1.7.2 (with pre-OCR cleanup) | MEDIUM | Defensible but not objectively best; PaddleOCR PP-OCRv5 is more accurate. Architect behind an `OcrAdapter` interface so swap is cheap. |
| SQLite + filesystem for events | HIGH | Standard for single-machine zero-ops setups |
| python-dotenv 1.2.2 + pydantic settings | HIGH | Standard pattern |
| FastAPI + Vue 3 + plain CSS for the future dashboard | MEDIUM | Flask also works; CSS choice is taste |
| WSL2 mirrored networking for DVR reachability | MEDIUM | Depends on Windows 11 build — confirm at install time with `wsl --version` |

---

## Sources

- PyPI metadata (verified 2026-04-13): https://pypi.org/pypi/ultralytics/json, https://pypi.org/pypi/ultralytics/8.4.37/json, https://pypi.org/pypi/opencv-python/json, https://pypi.org/pypi/deepface/json, https://pypi.org/pypi/easyocr/json, https://pypi.org/pypi/onnxruntime/json, https://pypi.org/pypi/openvino/json, https://pypi.org/pypi/numpy/json, https://pypi.org/pypi/python-dotenv/json, https://pypi.org/pypi/lap/json, https://pypi.org/pypi/torch/json, https://pypi.org/pypi/insightface/json, https://pypi.org/pypi/paddleocr/json, https://pypi.org/pypi/shapely/json — all HIGH confidence
- Ultralytics tracker docs: https://docs.ultralytics.com/modes/track/ — HIGH (built-in BoT-SORT and ByteTrack confirmed; YAML examples)
- Ultralytics YOLO26 docs: https://docs.ultralytics.com/models/yolo26/ — HIGH for performance numbers and licensing; MEDIUM-HIGH for "use this in production" given model age
- Ultralytics OpenVINO integration: https://docs.ultralytics.com/integrations/openvino/ — HIGH (export API and benchmark numbers)
- Ultralytics releases: https://github.com/ultralytics/ultralytics/releases — HIGH (8.4.37 confirmed 2026-04-10)
- ifzhang/ByteTrack repo: https://github.com/ifzhang/ByteTrack — MEDIUM (used to confirm "no PyPI package, install via setup.py develop")
- DeepFace TF/NumPy compatibility issues: https://github.com/serengil/deepface/issues/1376, https://github.com/serengil/deepface/issues/1135 — MEDIUM-HIGH (community-reported but reproducible)
- DeepFace vs InsightFace: https://dev.to/wintrover/upgrading-face-recognition-from-deepface-to-insightface-performance-quality-and-integration-5b7f, https://kitemetric.com/blogs/upgrading-face-recognition-from-deepface-to-insightface — MEDIUM (single-author posts, but consistent with InsightFace's published claims)
- PaddleOCR vs EasyOCR for ALPR: https://ieeexplore.ieee.org/document/10725878/, https://www.mdpi.com/2076-3417/15/14/7833, https://tildalice.io/ocr-tesseract-easyocr-paddleocr-benchmark/ — MEDIUM (peer-reviewed comparisons agree PaddleOCR has the accuracy edge)
- OpenCV RTSP/WSL2 threads: https://forum.opencv.org/t/timeout-when-grabbing-frame-from-rtsp-stream-from-inside-wsl2/20558, https://github.com/opencv/opencv/issues/21558, https://lindevs.com/capture-rtsp-stream-from-ip-camera-using-opencv — HIGH (community confirmation of TCP-forcing pattern)

---
*Stack research for: CPU-only multi-camera RTSP CV surveillance pipeline*
*Researched: 2026-04-13*
