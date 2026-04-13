# CONTEXT — Phase 0: Environment & Sanity

**Purpose:** Lock implementation decisions for Phase 0 so downstream agents (researcher, planner) can act without re-asking the user.

**Phase goal (from ROADMAP.md):** Verify the real Windows+WSL2 host can reach, read, and decode every DVR stream end-to-end before any pipeline work begins. Answer SUMMARY.md §6 Q1–Q6 on measured ground-truth, not assumptions.

**Requirements in scope:** ENV-01, ENV-02, ENV-03, ENV-04, ENV-05, ING-06

---

## Canonical Refs

These files MUST be read by downstream agents. Full relative paths from project root:

- `specs.md` — original architecture spec (Trigger & Catch, event logic, task backlog)
- `context.md` — deployment topology, engineering rationale, data schema, `.env` structure
- `terminal.txt` — ground-truth `ffplay` command lines per camera (sub + main)
- **`cameras.txt`** — **new as of this session** — per-camera hardware profile, FoV, H.265 anomalies, NAL-unit-0 crash notes, pre-known zone geometry (Perimeter Crossfire, Threshold Head-to-Head)
- `.planning/PROJECT.md` — core value, constraints, key decisions
- `.planning/REQUIREMENTS.md` — v1 REQ-IDs + traceability table
- `.planning/research/SUMMARY.md` — research synthesis; §2 stack pins, §6 empirical blockers, §7 build order
- `.planning/research/STACK.md` — pinned versions, install recipe, WSL2 gotchas
- `.planning/research/PITFALLS.md` — 20 pitfalls, prevention code snippets
- `.planning/research/ARCHITECTURE.md` — process/thread model, queue policies
- `.planning/ROADMAP.md` — phase goals and success criteria

---

## New Facts Learned This Session

These change assumptions made in PROJECT.md / SUMMARY.md. Downstream agents MUST treat these as authoritative:

1. **Codec is H.265 (HEVC) on all four cameras**, not H.264. Phase 0 startup must assert `cv2.getBuildInformation()` shows both `FFMPEG: YES` **and** HEVC decoder support. Without it, streams will open but decode to green or black.

2. **Cameras 3 and 4 have a known hardware anomaly**: strict Video-mode multiplexer leaves an orphaned audio NAL header, which causes FFmpeg to crash with `Invalid NAL unit 0` unless `fflags=+discardcorrupt` + `err_detect=ignore_err` are set. The `discardcorrupt` flag in `terminal.txt` is **not defensive — it is mandatory** for these two cameras. Downstream reconnect code must keep these flags and must tolerate higher decode-error rates on Cam 3 and Cam 4 than on Cam 1 and Cam 2.

3. **Per-camera native frame rate differs:**
   - Cam 1 (exterior red) — 720p @ **25 fps**
   - Cam 2 (interior orange) — 5MP sensor → 1080p output @ **12 fps** (A/V)
   - Cam 3 (exterior blue) — 5MP sensor → 1080p output @ **12 fps** (V-only, NAL crash)
   - Cam 4 (interior green) — 720p @ **25 fps** (V-only, NAL crash)
   - ByteTracker `frame_rate` parameter in Phase 2 must be set per-camera, not globally.

4. **Cameras 2 and 3 are 5MP sensors** downscaled by the XVR to 1080p. Even the sub-stream decode cost is heavier than a native 720p stream. Phase 0 must measure per-camera CPU cost independently, not assume one measurement generalizes.

5. **XVR hardware identification:** Cantonk/HeroSpeed OEM firmware, interfaced via BitVision. Not generic Hikvision. Useful for any DVR-specific debugging later.

6. **Human-readable camera names (from the indicator LED colors):**
   - Cam 1 → `exterior_red` (upper-right front facade, right→left approach)
   - Cam 2 → `interior_orange` (above primary entrance, staircase view)
   - Cam 3 → `exterior_blue` (upper-left front facade, left→right approach)
   - Cam 4 → `interior_green` (above staircase landing, entrance view)

7. **Pre-known zone geometry** (use as Phase 2 zone anchors — NOT Phase 0 scope):
   - **Perimeter Crossfire** — Cam 1 ∩ Cam 3 overlap directly in front of the main entrance
   - **Threshold Head-to-Head** — Cam 2 ∩ Cam 4 overlap the central floor between door and stairs
   - Cam 4 has a known backlight/blowout failure mode when the exterior door opens during day; Cam 2 compensates with a front-lit rear view of the subject. Pair these in any same-person heuristics later.

---

## Decisions (Locked for Downstream Agents)

### Repo & Package Layout

- **Decision:** `src/home_cctv/` package layout with `pyproject.toml` at repo root.
- **Structure** (initial; submodules added per phase):
  ```
  pyproject.toml
  uv.lock
  README.md
  .env                     # user-owned, gitignored
  .env.example             # committed template
  src/home_cctv/
    __init__.py
    __main__.py            # `python -m home_cctv` entry point
    config/
      __init__.py
      env.py               # dotenv loader + pydantic settings
      cameras.py           # cameras.yaml loader (Phase 1)
    ingest/
      __init__.py
      capture.py           # FrameSource abstraction (RTSP + --mp4)
      flags.py             # OPENCV_FFMPEG_CAPTURE_OPTIONS builder (set BEFORE cv2 import)
    phase0/
      __init__.py
      sanity.py            # Phase 0 measurement harness
      report.py            # PHASE0-REPORT.json writer
  tests/
    __init__.py
    test_config_env.py
    test_flags_builder.py
  tools/                   # standalone scripts (e.g. draw_zones.py later)
  data/                    # gitignored — DB + event images
    event_images/
  .planning/
  ```
- **Rationale:** src-layout prevents accidental test imports into prod, plays nicely with `uv sync`, leaves room for subprocess workers and multi-module growth without a later restructure.
- **Entry point:** `python -m home_cctv [--mp4 PATH] [--show] [--phase0]`. `--phase0` runs the measurement harness; default mode runs the full pipeline (not built until Phase 1+).

### Dependency Management

- **Decision:** `uv` + `pyproject.toml` + `uv.lock`.
- **Install recipe:**
  ```
  curl -LsSf https://astral.sh/uv/install.sh | sh
  uv sync                   # creates .venv, installs pinned deps
  uv run python -m home_cctv --phase0
  ```
- **Initial pinned deps in `[project.dependencies]`:** exactly what SUMMARY.md §2 specifies. The full stack is installed in Phase 0 even though most of it isn't used until Phase 2, so each later phase starts offline.
  - `opencv-python-headless==4.13.0.92`
  - `numpy==1.26.4`
  - `python-dotenv==1.2.2`
  - `pydantic>=2.10,<3`
  - `pyyaml>=6,<7`
  - `ultralytics==8.4.37`
  - `openvino==2026.1.0`
  - `onnxruntime==1.24.4`
  - `lap==0.5.13`
  - `scipy>=1.13,<2`
  - `torch==2.5.1+cpu` (custom index)
  - `easyocr==1.7.2`
  - `tensorflow-cpu==2.16.2`
  - `tf-keras==2.16.0`
  - `deepface==0.0.99`
  - `shapely>=2.0,<3` (for Phase 2 zone polygons, pre-installed)
- **Dev deps (`[project.optional-dependencies].dev`):** `pytest`, `pytest-json-report`, `ruff`, `mypy`.
- **`TF_USE_LEGACY_KERAS=1`** exported in `src/home_cctv/__init__.py` (set before any TF import).
- **No conda, no poetry, no plain `requirements.txt`.**

### Phase 0 Measurement Harness Format

- **Decision:** Single JSON report + stdout summary. No pytest for Phase 0 itself (tests in `tests/` stay for unit-test code).
- **Report path:** `.planning/phases/00-environment-sanity/PHASE0-REPORT.json` (committed to show the real measurements from the user's host).
- **Schema (keys downstream agents can read):**
  ```jsonc
  {
    "timestamp_utc": "...",
    "host": {
      "os": "Ubuntu 24.04 (WSL2)",
      "kernel": "...",
      "cpu_model": "...",
      "cpu_cores_physical": 6,
      "cpu_cores_logical": 12,
      "ram_gb": 16,
      "wsl2_networking_mode": "mirrored" | "nat" | "bridged",
      "wsl_version": "..."
    },
    "opencv": {
      "version": "4.13.0.92",
      "ffmpeg_backend": true,
      "hevc_decoder": true,
      "build_info_snippet": "FFMPEG: YES ...",
      "num_threads": 6
    },
    "env_vars_loaded": ["DVR_IP", "DVR_PORT", "DVR_USER", "EVENT_IMAGE_DIR", "DB_PATH"],
    "credentials_masked_in_logs": true,
    "disk_space": {
      "event_image_dir_path": "/home/.../data/event_images",
      "event_image_dir_filesystem": "ext4",
      "free_gb": 42.1,
      "on_drvfs": false
    },
    "dvr": {
      "reachable": true,
      "ip": "192.168.1.10",
      "port": 554,
      "handshake_latency_ms_mean": 187,
      "max_concurrent_sessions_tested": 6,
      "max_concurrent_sessions_last_ok": 5
    },
    "cameras": {
      "1": { "name": "exterior_red",    "sub_path": "/1",  "main_path": "/0",  "codec": "hevc", "advertised_fps": 25, "measured_fps": 24.1, "width": 1280, "height": 720,  "capture_duration_sec": 1800, "frames_decoded": 43380, "frames_corrupted": 0,   "decode_errors": 0,  "hang_events": 0 },
      "2": { "name": "interior_orange", "sub_path": "/11", "main_path": "/10", "codec": "hevc", "advertised_fps": 12, "measured_fps": 11.8, "width": 1920, "height": 1080, "capture_duration_sec": 1800, "frames_decoded": 21240, "frames_corrupted": 0,   "decode_errors": 0,  "hang_events": 0 },
      "3": { "name": "exterior_blue",   "sub_path": "/21", "main_path": "/20", "codec": "hevc", "advertised_fps": 12, "measured_fps": 11.6, "width": 1920, "height": 1080, "capture_duration_sec": 1800, "frames_decoded": 20880, "frames_corrupted": 37,  "decode_errors": 37, "hang_events": 0, "nal_unit_0_workaround_required": true },
      "4": { "name": "interior_green",  "sub_path": "/31", "main_path": "/30", "codec": "hevc", "advertised_fps": 25, "measured_fps": 23.8, "width": 1280, "height": 720,  "capture_duration_sec": 1800, "frames_decoded": 42840, "frames_corrupted": 81,  "decode_errors": 81, "hang_events": 0, "nal_unit_0_workaround_required": true }
    },
    "model_bundle": {
      "yolov8n_pt_cached": true,
      "yolo26n_pt_cached": true,
      "yolo_openvino_export_cached": true,
      "deepface_arcface_cached": true,
      "deepface_retinaface_cached": true,
      "easyocr_english_cached": true,
      "total_weights_size_mb": 1427,
      "cold_start_ms": { "yolo_openvino": 620, "deepface": 8400, "easyocr": 5100 }
    },
    "blockers_resolved": {
      "Q1_host_fps_baseline": true,
      "Q2_wsl2_networking": true,
      "Q3_opencv_ffmpeg_backend": true,
      "Q4_ffplay_flags_translated": true,
      "Q5_reconnect_watchdog_sketch": "designed but not yet implemented",
      "Q6_dvr_connection_cap": true
    },
    "phase0_verdict": "pass" | "fail",
    "notes": "..."
  }
  ```
- **Stdout summary:** A colored, human-readable table (per-camera row) printed at exit. 30 seconds to read, all the key verdicts green/red.

### Display Strategy (`--show` flag)

- **Decision:** Default headless. `--show` flag opens a `cv2.imshow` window. Falls back cleanly with a warning if no display is available (no GUI, no WSLg, no X server).
- **User note captured:** `--show` is the **MVP of a future dashboard live-view button**. When the Vue 3 dashboard milestone ships, the dashboard's "Live View" button will invoke the same code path. The headless JPEG-dump-every-5s path remains available as a low-overhead fallback and for CI/regression testing.
- **Phase 0 scope:** `--show` must work (or gracefully degrade). Actual dashboard UI is deferred; see Deferred Ideas.

### Model Pre-Download

- **Decision:** Phase 0 pre-downloads and caches the full model bundle (YOLO26n + YOLOv8n fallback + OpenVINO IR export of YOLO + DeepFace ArcFace + DeepFace RetinaFace + EasyOCR English).
- **Cache location:** `$HOME/.cache/home_cctv/models/` (inside WSL2 ext4, never `/mnt/c/...`).
- **Verification:** Every startup asserts file existence + size match against a `weights.lock.json` committed to the repo. Corrupt cache → redownload. No first-call network dependency after Phase 0 succeeds.
- **Rationale:** Eliminates Pitfall #3 (blocking cold-start download stalling the trigger thread) once and for all. One slow Phase 0 run instead of four surprising Phase 6/7 stalls.

### Phase 0 Test Surface: ALL 4 CAMERAS

- **Decision:** Phase 0 runs the 30-minute capture test against **all 4 cameras** in sequence, not a single representative stream. Each camera gets its own entry in the JSON report.
- **Sequence:** Cam 1 → Cam 2 → Cam 3 → Cam 4, sequential (not parallel — parallel ingest is Phase 1). This intentionally exercises the NAL-unit-0 workaround on Cam 3 and Cam 4, surfacing their decode-error rate before Phase 1 builds the full pipeline.
- **Per-camera exit criteria:**
  - `measured_fps` within 20% of advertised
  - Zero process hangs
  - Zero green-frame detections (bottom-strip variance sanity check)
  - For Cam 3 + Cam 4: `decode_errors > 0` is allowed (NAL anomaly expected) but `frames_decoded / (frames_decoded + frames_corrupted) ≥ 0.95`
- **`cameras.yaml` initial draft** (committed to repo as the Phase 0 artifact that Phase 1 will consume):
  ```yaml
  dvr:
    host_env: DVR_IP           # loaded from .env
    port_env: DVR_PORT
    user_env: DVR_USER
    pass_env: DVR_PASS
    vendor: "Cantonk/HeroSpeed OEM (BitVision ecosystem)"
  cameras:
    - id: 1
      name: exterior_red
      location: "upper-right corner, front facade"
      coverage: "right-to-left approach along front"
      sub_path: "/1"
      main_path: "/0"
      codec: hevc
      native_fps: 25
      native_width: 1280
      native_height: 720
      audio_multiplex: true
      notes: "Channel 01, AHD 720P25, H.265"
    - id: 2
      name: interior_orange
      location: "above primary entrance door"
      coverage: "staircase + central floor"
      sub_path: "/11"
      main_path: "/10"
      codec: hevc
      native_fps: 12
      native_width: 1920
      native_height: 1080
      sensor_native: "5MP"
      audio_multiplex: true
      notes: "Channel 02, AHD 5MP12 downscaled 1080p, H.265"
    - id: 3
      name: exterior_blue
      location: "upper-left corner, front facade"
      coverage: "left-to-right approach along front"
      sub_path: "/21"
      main_path: "/20"
      codec: hevc
      native_fps: 12
      native_width: 1920
      native_height: 1080
      sensor_native: "5MP"
      audio_multiplex: false
      nal_unit_0_workaround_required: true
      notes: "Channel 03, AHD 5MP12 downscaled 1080p, H.265 V-only. Orphaned audio NAL header — requires +discardcorrupt mandatory."
    - id: 4
      name: interior_green
      location: "above staircase landing"
      coverage: "rear-to-front view of primary entrance"
      sub_path: "/31"
      main_path: "/30"
      codec: hevc
      native_fps: 25
      native_width: 1280
      native_height: 720
      audio_multiplex: false
      nal_unit_0_workaround_required: true
      known_hazard: "exterior-door backlight blowout during day; silhouetting at night"
      notes: "Channel 04, AHD 720P25, H.265 V-only. Same NAL workaround as Cam 3."
  ```
- **Phase 0 does NOT write `zones.yaml` yet.** Zone geometry (Perimeter Crossfire, Threshold Head-to-Head) is noted here for Phase 2's zone-drawing tool to consume.

### `OPENCV_FFMPEG_CAPTURE_OPTIONS` String

- **Decision:** Single canonical env string, identical for all 4 cameras (the NAL workaround flags don't hurt the clean cameras). Built in `ingest/flags.py` and exported into `os.environ` **before any `import cv2`** anywhere in the codebase:
  ```
  rtsp_transport;tcp|fflags;nobuffer+discardcorrupt|flags;low_delay|err_detect;ignore_err|stimeout;5000000|reconnect;1|reconnect_streamed;1|reconnect_delay_max;2|analyzeduration;1000000|probesize;2000000
  ```
- **Enforcement:** `src/home_cctv/__init__.py` sets the env var at module load, then imports cv2. A runtime assert at startup verifies the env var matches (catches accidental reorderings).

### Supervisor / Shutdown Behavior in Phase 0

- **Decision:** Ctrl+C exits within 2 seconds, cleanly:
  - All `cv2.VideoCapture` handles released
  - Log file flushed + closed
  - JSON report written (with `phase0_verdict: "aborted"` if incomplete)
  - No orphan ffmpeg subprocesses
- **Implementation:** A single `threading.Event` shared between the capture loop and the signal handler. No subprocess workers in Phase 0 (those arrive in Phase 3), so no mp.Queue drain logic is needed yet.

### Logging Format

- **Decision:** Python `logging` + `logging.handlers.RotatingFileHandler` (max 10 MB × 5 files under `logs/`). Per-camera prefix in the format. Structured key=value after the message for grep-ability. A custom `Filter` masks any `rtsp://user:pass@host` URL into `rtsp://user:***@host`.
- **Example line:**
  ```
  2026-04-14T01:12:44Z INFO  [cam1:exterior_red] frame_ok size=1280x720 fps=24.1 frame_idx=1032
  2026-04-14T01:12:45Z WARN  [cam3:exterior_blue] decode_error kind=nal_unit_0 count=4
  ```

---

## Deferred Ideas (Captured, Not in Phase 0 Scope)

These came up during discussion but belong to later phases or milestones. Do NOT implement in Phase 0.

- **Vue 3 dashboard with a "Live View" button** that invokes the same `--show` code path. Confirmed by user as the future form of the Phase 0 `--show` flag. Belongs to the deferred Vue dashboard milestone after v1 ships.
- **Zone polygon drawing tool (`tools/draw_zones.py`)** — deferred to Phase 2 (ZON-01).
- **`zones.yaml`** with the Perimeter Crossfire + Threshold Head-to-Head polygons. Known geometry, deferred to Phase 2 when the zone evaluator is built.
- **Cam 2 ↔ Cam 4 same-person heuristic** using Cam 2's front-lit rear view to compensate for Cam 4's backlit silhouette. Belongs to a future "cross-camera entity handoff" phase (v2+).
- **DeepFace distance-threshold calibration** on user's actual known-faces gallery — deferred to Phase 3 (FAC-04). Phase 0 only verifies the model loads.

---

## Out of Scope for Phase 0 (Explicit)

- Multi-stream concurrent ingest (→ Phase 1)
- Watchdog reconnect implementation (→ Phase 1; Phase 0 only sketches the design and measures the need)
- YOLO inference, ByteTrack, zones, events, SQLite schema (→ Phase 2)
- Main-stream grabber, triggers, DeepFace, EasyOCR (→ Phase 3)
- Retention, metrics JSON, worker supervision (→ Phase 4)
- Any Vue dashboard work (→ deferred milestone)

---

## Success Criteria (Restatement from ROADMAP.md)

Phase 0 is done when:
1. `python -m home_cctv --phase0` runs end-to-end on the user's Windows 11 + WSL2 Ubuntu 24.04 host, prints a summary, and writes a green-verdict `PHASE0-REPORT.json`.
2. All 4 cameras captured for 30 minutes each with measured FPS within 20% of advertised, zero hangs, zero green frames (Cam 1/2) or `>= 95%` clean frames (Cam 3/4 with NAL workaround).
3. Missing `.env` key, missing FFmpeg backend, missing HEVC decoder, missing disk space (<1 GB), or DB path on `/mnt/c/...` each produce a clear startup error and refuse to run.
4. Ctrl+C exits cleanly in under 2 seconds with all resources released.
5. `--mp4 path.mp4` runs the same capture loop against a file, proving the `FrameSource` abstraction.
6. `PHASE0-REPORT.json` answers SUMMARY.md §6 blockers Q1, Q2, Q3, Q4, Q6 with measured values; Q5 has a committed design sketch for Phase 1 to implement.

---

## Next Step

Hand this CONTEXT.md to `/gsd-plan-phase 0`. The planner should produce 1–3 plans (coarse granularity) that implement the decisions above. The researcher stage can be skipped for Phase 0 — the research is already consolidated in SUMMARY.md and the hard decisions are locked here.
