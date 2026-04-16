# Home CCTV AI Pipeline

## What This Is

A zero-budget smart surveillance system that adds AI event detection (people, faces, vehicles, license plates, zone intrusions, vehicle entries/exits, loitering) on top of four legacy RTSP IP cameras attached to a BitVision DVR at `192.168.1.10`. Runs entirely on a local Windows/WSL2 laptop with an RTX 3060 GPU (CUDA 12 via WSL2 passthrough), using open-source models, and logs events to a local SQLite database and an on-disk image store for later review.

## Core Value

Turn dumb legacy cameras into a smart event log — without buying any new hardware, without cloud dependencies, and without the CPU gymnastics of a GPU-less setup.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

(None yet — ship to validate)

### Active

<!-- Current scope. Building toward these. Hypotheses until shipped. -->

- [ ] Ingest all 4 camera sub-streams over RTSP/TCP with `nobuffer + discardcorrupt` tuning (no UDP, no stalls)
- [ ] Support offline MP4 playback mode for development and regression testing
- [ ] Run continuous YOLOv8n person/car detection on sub-streams at ~10-15 FPS per camera on CPU
- [ ] Maintain persistent object IDs and centroid velocity per track via ByteTrack
- [ ] Define virtual polygon zones per camera for spatial event logic
- [ ] "Trigger & Catch": pull a single main-stream frame only when a tracker condition fires (peak bbox, zero velocity, zone entry)
- [ ] Face recognition via DeepFace triggered when a tracked person's bbox reaches peak size
- [ ] ALPR via EasyOCR triggered when a tracked car's centroid velocity reaches ~0
- [ ] Vehicle interaction logic: detect "Entered Vehicle" and "Exited Vehicle" from person↔car bbox intersection events
- [ ] Loitering detection: ByteTrack ID lingering in a zone polygon beyond a configured time threshold
- [ ] Persist events to SQLite (`cctv_events.db` → `EventLog` table) with the agreed schema
- [ ] Save triggered main-stream snapshots to a dated directory tree under `EVENT_IMAGE_DIR`
- [ ] Load all secrets (DVR creds, paths) from `.env` — never hardcoded
- [ ] Graceful handling of stream drops / DVR reconnects without crashing the pipeline
- [ ] Minimal React dark-mode dashboard (`#151a2c`) reading SQLite/JSON for event review — *deferred to later milestone*

### Out of Scope

- **Pixel-based motion detection** — removed deliberately; produces too many false positives on shadows, rain, trees
- **Cloud / BitVision P2P for AI path** — bypassed locally to eliminate latency and avoid cloud dependency
- **GPU inference / dedicated accelerator** — hardware budget is $0
- **Running heavy models (DeepFace, EasyOCR) continuously** — only event-driven, one frame at a time, to avoid CPU throttling
- **UDP RTSP transport** — mandatory TCP due to packet drops on the local network
- **Rewriting / replacing BitVision for general camera viewing** — BitVision stays for cloud viewing; this project only handles the local AI pipeline
- **Multi-machine / distributed deployment** — single local Windows+WSL2 host only

## Context

**Deployment environment**
- Local Windows laptop running WSL2 (Ubuntu 24.04). Ryzen 7 6800H CPU, 32 GB RAM, NVIDIA RTX 3060 Laptop GPU (6 GB VRAM) exposed to WSL2 via CUDA 12 passthrough (Windows driver 561.09, nvidia-smi inside WSL2 shows CUDA runtime 12.6).
- 4 legacy IP cameras wired to a BitVision DVR at `192.168.1.10:554` (2 internal, 2 external).
- BitVision app handles cloud P2P viewing; this project bypasses it locally for AI.

**Stream routing (DVR local IP: `192.168.1.10`)**

| Camera | Sub Stream (Low-Res, Continuous AI) | Main Stream (High-Res, Triggered) |
|---|---|---|
| Cam 1 | `rtsp://admin:***@192.168.1.10:554/1`  | `rtsp://admin:***@192.168.1.10:554/0`  |
| Cam 2 | `rtsp://admin:***@192.168.1.10:554/11` | `rtsp://admin:***@192.168.1.10:554/10` |
| Cam 3 | `rtsp://admin:***@192.168.1.10:554/21` | `rtsp://admin:***@192.168.1.10:554/20` |
| Cam 4 | `rtsp://admin:***@192.168.1.10:554/31` | `rtsp://admin:***@192.168.1.10:554/30` |

**Core software stack (as pivoted 2026-04-16 after GPU availability confirmed)**
- Python 3.11 + OpenCV (RTSP/TCP, nobuffer, discardcorrupt, low-delay — mirroring ffplay flags)
- YOLOv8n / YOLO11 (Ultralytics) running on CUDA via `torch==2.5.1+cu121` — sub-stream object detection at 15-25 FPS per camera
- ByteTrack for ID persistence + centroid velocity (Ultralytics built-in tracker)
- DeepFace on GPU (TensorFlow 2.16 + CUDA 12) for conditional face recognition
- EasyOCR on GPU (`gpu=True`) for conditional license plate reading
- SQLite (`cctv_events.db`) + local image directory for event storage
- `.env` for all secrets (`DVR_IP`, `DVR_PORT`, `DVR_USER`, `DVR_PASS`, `EVENT_IMAGE_DIR`, `DB_PATH`)
- Future UI: React minimal dark-mode dashboard (`#151a2c`) reading from SQLite/JSON

**Known engineering problems and solutions**
1. *CPU throttling from running all models on all streams* → "Trigger & Catch" architecture. Continuous loop runs only YOLOv8n + ByteTrack on sub-streams. DeepFace / EasyOCR sleep until a tracker rule fires, then process exactly one main-stream frame.
2. *UDP packet drop / corrupted buffers* → Forced TCP transport via `OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp`, `buffersize=1`, low-delay flags mirroring `ffplay -fflags nobuffer+discardcorrupt`.
3. *Motion blur on moving cars, night-time headlight bloom* → Centroid velocity gating. OCR only runs when a car's centroid velocity is ~0 (parked, lights off).
4. *False-positive motion alerts from shadows/rain/trees* → Pixel motion detection removed entirely. Events require an AI-classified object (person/car) entering a defined polygon zone.

**Data model — `cctv_events.db` / `EventLog`**

| Column | Type | Description |
|---|---|---|
| `event_id` | UUID | Primary key |
| `timestamp` | DATETIME | Exact trigger time |
| `camera_id` | INTEGER | 1–4 |
| `event_type` | STRING | `Person_Detected`, `Face_Recognized`, `Plate_Read`, `Entered_Vehicle`, `Exited_Vehicle`, `Loitering`, ... |
| `entity_id` | STRING | ByteTrack ID (e.g. `Person_405`, `Car_12`) |
| `zone_name` | STRING | Which polygon (`Driveway`, `Front_Door`, ...) |
| `confidence` | FLOAT | AI confidence score |
| `extracted_text` | STRING | Recognized name or plate number (if any) |
| `image_path` | STRING | Path to the saved main-stream snapshot |

**Reference material in this repo**
- `specs.md` — architecture specification and task backlog
- `context.md` — deployment topology, engineering rationale, data schema, `.env` structure
- `terminal.txt` — working `ffplay` command lines per camera (sub + main) — ground truth for stream params

## Constraints

- **Budget**: $0 — open-source models only, no paid APIs, no new hardware
- **Compute**: GPU-primary — NVIDIA RTX 3060 Laptop (6 GB VRAM) via WSL2 CUDA 12 passthrough (Windows driver 561.09+, CUDA runtime 12.6). CPU fallback path retained for portability (the pipeline must still run without the GPU by unsetting `CUDA_VISIBLE_DEVICES` or falling back to `torch` CPU wheels)
- **Network protocol**: RTSP over TCP mandatory (UDP drops on this LAN)
- **Cameras**: 4 legacy IP cameras via BitVision DVR at `192.168.1.10` — cannot be upgraded or replaced
- **Security**: DVR credentials must come from `.env`; never committed to git (enforced via `.gitignore`)
- **Operation modes**: Must work with both live RTSP streams *and* offline MP4 files (for dev/test)
- **Stream budget**: Sub-stream continuous AI runs on GPU (target 4× 15-25 FPS sustained). The "Trigger & Catch" main-stream policy is retained semantically (face at peak bbox, plate at zero velocity, etc. are *quality* gates not just CPU budget gates), but the hard CPU-budget anxiety is lifted
- **Dependencies**: Python, OpenCV, Ultralytics YOLOv8, ByteTrack, DeepFace, EasyOCR (all open-source)

## Key Decisions

| Decision | Rationale | Outcome |
|---|---|---|
| "Trigger & Catch" dual-stream architecture instead of running all models on main streams | Original CPU-budget driver is lifted on GPU, but the pattern is retained semantically: face at peak bbox and ALPR at zero velocity are *quality* gates (clearer crop, less motion blur), not just cost gates | — Pending |
| GPU-primary stack (RTX 3060 + CUDA 12 via WSL2 passthrough), CPU fallback retained | User has an RTX 3060 Laptop — using it is a 5-10× speedup over CPU inference, lets us run full-quality models (YOLO11s/m instead of YOLOv8n) and run DeepFace without subprocess-isolation pain | — Pending |
| Force RTSP over TCP with nobuffer/discardcorrupt flags | UDP packets drop on this LAN; proven working via `ffplay` in `terminal.txt` | — Pending |
| Remove pixel-based motion detection entirely | Shadows/rain/trees caused false positives; AI-object-in-polygon is the real signal | — Pending |
| ByteTrack as the foundation for all event logic (IDs + centroid velocity) | Needed for face-peak gating, parked-car gating, vehicle interactions, loitering | — Pending |
| Centroid velocity gate for ALPR (OCR only when velocity ≈ 0) | Motion blur + headlight bloom make moving-car OCR unreliable | — Pending |
| SQLite + local image directory for event storage | Zero-budget, zero-ops, matches local-only deployment | — Pending |
| All secrets via `.env` | Credentials for DVR must never touch source control | — Pending |
| Future dashboard: React dark-mode (`#151a2c`), reads SQLite/JSON | Keeps runtime pipeline decoupled from UI; minimal dependency footprint | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-16 after GPU-stack pivot (RTX 3060 Laptop confirmed available via WSL2 CUDA passthrough)*
