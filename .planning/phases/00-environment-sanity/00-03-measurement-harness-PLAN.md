---
phase: 00-environment-sanity
plan: 03
type: execute
wave: 3
depends_on: ["00-01", "00-02"]
files_modified:
  - src/home_cctv/phase0/__init__.py
  - src/home_cctv/phase0/sanity.py
  - src/home_cctv/phase0/report.py
  - src/home_cctv/phase0/host_probe.py
  - src/home_cctv/phase0/network_probe.py
  - src/home_cctv/phase0/model_bundle.py
  - src/home_cctv/config/cameras.py
  - src/home_cctv/__main__.py
  - cameras.yaml
  - weights.lock.json
  - .planning/phases/00-environment-sanity/PHASE0-REPORT.json
  - tests/test_cameras_yaml.py
  - tests/test_report_schema.py
  - tests/test_host_probe.py
  - tests/test_model_bundle.py
autonomous: false  # final task is a checkpoint: human runs the 4×30-min live DVR sweep
requirements:
  - ENV-01
  - ENV-02
  - ENV-05

must_haves:
  truths:
    - "`uv run python -m home_cctv --phase0 --mp4 tests/fixtures/sample_720p25.mp4` produces a valid PHASE0-REPORT.dryrun.json with phase0_verdict=pass (offline dry-run) — the canonical PHASE0-REPORT.json is NEVER touched by dry-runs or tests (B3)"
    - "`uv run python -m home_cctv --phase0` on the real host captures all 4 DVR sub-streams for 30 minutes each, sequentially, and writes PHASE0-REPORT.json with per-camera rows"
    - "Startup fails loudly with a clear message if cv2.getBuildInformation() lacks 'FFMPEG: YES'"
    - "Startup fails loudly with a clear message if the HEVC decoder is missing"
    - "Startup fails loudly with a clear message if WSL2 cannot reach 192.168.1.10:554 (TCP connect test)"
    - "Per-camera exit criteria from CONTEXT.md are enforced: measured_fps within 20% of advertised, zero hangs, zero green frames on Cam 1/2, ≥95% clean frames on Cam 3/4"
    - "Full model bundle (YOLO26n + YOLOv8n + OpenVINO IR + DeepFace ArcFace + DeepFace RetinaFace + EasyOCR English) is downloaded + verified + cached under $HOME/.cache/home_cctv/models/"
    - "download_model_bundle() automatically warms up each of the three inference models once and persists the wall-clock cold-start timings to weights.lock.json + PHASE0-REPORT.json as model_bundle.cold_start_ms.{yolo_openvino, deepface, easyocr} (B1, CONTEXT.md §Phase 0 Measurement Harness Format)"
    - "weights.lock.json is committed, startup re-verifies file sizes against it, corrupt cache triggers redownload. Downstream agents MUST NOT need a manual shell snippet to populate it — `download_model_bundle()` auto-calls `persist_weights_lock()` on first successful run (W6)."
    - "SUMMARY.md §6 blockers Q1, Q2, Q3, Q4, Q6 are answered in PHASE0-REPORT.json with measured values; Q5 has a committed design-sketch note"
    - "cameras.yaml is committed with the exact structure from CONTEXT.md (4 cameras, all paths, NAL workaround flags on Cam 3/4)"
  artifacts:
    - path: cameras.yaml
      provides: "4-camera config (locked structure from CONTEXT.md §cameras.yaml initial draft)"
      contains: "nal_unit_0_workaround_required: true"
    - path: src/home_cctv/config/cameras.py
      provides: "Pydantic loader for cameras.yaml; builds full RTSP URLs from Settings + per-camera paths"
      exports: ["CameraConfig", "CamerasFile", "load_cameras", "build_rtsp_url"]
    - path: src/home_cctv/phase0/sanity.py
      provides: "Main Phase 0 harness — sweeps all 4 cameras for 30 min each, writes PHASE0-REPORT.json"
      exports: ["run_phase0", "PHASE0_DURATION_SEC", "per_camera_exit_ok"]
    - path: src/home_cctv/phase0/report.py
      provides: "Report writer + JSON schema matching CONTEXT.md"
      exports: ["Phase0Report", "CameraResult", "write_report", "print_stdout_summary"]
    - path: src/home_cctv/phase0/host_probe.py
      provides: "Host/OpenCV/WSL2/FFmpeg/HEVC detection"
      exports: ["probe_host", "assert_ffmpeg_backend", "assert_hevc_decoder", "detect_wsl2_networking_mode"]
    - path: src/home_cctv/phase0/network_probe.py
      provides: "DVR reachability + handshake latency + concurrent-session stepping"
      exports: ["probe_dvr_reachable", "measure_handshake_latency", "step_concurrent_sessions"]
    - path: src/home_cctv/phase0/model_bundle.py
      provides: "Downloader + verifier for YOLO + DeepFace + EasyOCR weights"
      exports: ["download_model_bundle", "verify_model_bundle", "WEIGHTS_LOCK_PATH"]
    - path: weights.lock.json
      provides: "Committed manifest of expected weight file names + byte sizes"
      contains: "yolo26n.pt"
    - path: .planning/phases/00-environment-sanity/PHASE0-REPORT.json
      provides: "Committed real-host Phase 0 measurements (updated per run by the user)"
      contains: "phase0_verdict"
  key_links:
    - from: "src/home_cctv/phase0/sanity.py"
      to: "src/home_cctv/ingest/capture.open_frame_source"
      via: "run_phase0() loop calls open_frame_source(rtsp_url, camera_id=) for each of 4 cameras sequentially"
      pattern: "open_frame_source\\("
    - from: "src/home_cctv/phase0/sanity.py"
      to: "src/home_cctv/config/cameras.load_cameras"
      via: "load_cameras('cameras.yaml') at run_phase0() entry"
      pattern: "load_cameras\\("
    - from: "src/home_cctv/__main__.py"
      to: "src/home_cctv/phase0/sanity.run_phase0"
      via: "args.phase0 → run_phase0(settings, mp4_override=args.mp4)"
      pattern: "run_phase0\\("
    - from: "src/home_cctv/phase0/sanity.py"
      to: "src/home_cctv/phase0/model_bundle.verify_model_bundle"
      via: "Pre-flight verification before any camera sweep starts"
      pattern: "verify_model_bundle\\("
    - from: "src/home_cctv/phase0/host_probe.py"
      to: "cv2.getBuildInformation"
      via: "assert_ffmpeg_backend + assert_hevc_decoder at process start"
      pattern: "getBuildInformation"
---

<objective>
Land the Phase 0 measurement harness: `cameras.yaml` + loader, the host/network/DVR probes that answer SUMMARY.md §6 blockers Q1–Q6, the model-bundle pre-downloader + verifier, and the 4-camera sequential sweep that produces `PHASE0-REPORT.json`. When this plan runs successfully on the user's real Windows 11 + WSL2 Ubuntu 24.04 host, Phase 0 is DONE and Phase 1 can begin on measured ground-truth.

Purpose: Transform the scaffolding from Plans 01+02 into the single deliverable Phase 0 promises — an empirical answer to "can this machine actually reach, read, and decode every DVR stream end-to-end?". Every decision from CONTEXT.md flows into this plan: the exact 4-camera config, the exact JSON schema, the exact exit criteria, the exact model bundle list.

Output:
1. `.planning/phases/00-environment-sanity/PHASE0-REPORT.json` committed with `phase0_verdict: "pass"` (or honest failure notes)
2. `cameras.yaml` committed with all 4 cameras (verbatim from CONTEXT.md)
3. `weights.lock.json` committed with verified weight sizes
4. Full model bundle cached under `$HOME/.cache/home_cctv/models/`
5. All three plans' work validated end-to-end on the real host
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/phases/00-environment-sanity/00-CONTEXT.md
@.planning/phases/00-environment-sanity/00-01-scaffolding-PLAN.md
@.planning/phases/00-environment-sanity/00-02-framesource-capture-PLAN.md
@.planning/research/SUMMARY.md
@.planning/research/PITFALLS.md
@.planning/research/STACK.md
@cameras.txt
@terminal.txt

<interfaces>
Consumed from prior plans:

```python
# Plan 01
from home_cctv.config.env import Settings, load_settings, validate_runtime_paths
from home_cctv.obs.logging_setup import configure_logging
from home_cctv.ingest.flags import OPENCV_FFMPEG_OPTIONS_STRING, assert_capture_options_active

# Plan 02
from home_cctv.ingest.capture import (
    FrameSource, RtspFrameSource, Mp4FrameSource, open_frame_source, CaptureStats,
)
from home_cctv.ingest.display import DisplaySink, HeadlessJpegSink
from home_cctv.ingest.supervisor import ShutdownSupervisor, install_signal_handlers
from home_cctv.ingest.frame_quality import is_green_frame
```

Produced by this plan (consumed by Phase 1+):

```python
# src/home_cctv/config/cameras.py
class CameraConfig(BaseModel):
    id: int
    name: str                  # e.g. "exterior_red"
    location: str
    coverage: str
    sub_path: str              # e.g. "/1"
    main_path: str             # e.g. "/0"
    codec: Literal["hevc", "h264"]
    native_fps: int
    native_width: int
    native_height: int
    sensor_native: Optional[str] = None   # "5MP" for cams 2, 3
    audio_multiplex: bool = False
    nal_unit_0_workaround_required: bool = False
    known_hazard: Optional[str] = None
    notes: str

class CamerasFile(BaseModel):
    dvr: dict
    cameras: list[CameraConfig]

def load_cameras(path: Path) -> CamerasFile: ...
def build_rtsp_url(settings: Settings, cam: CameraConfig, *, stream: Literal["sub","main"]) -> str: ...
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: cameras.yaml + cameras loader + host/network probes + weights.lock.json scaffold</name>
  <files>cameras.yaml, src/home_cctv/config/cameras.py, src/home_cctv/phase0/__init__.py, src/home_cctv/phase0/host_probe.py, src/home_cctv/phase0/network_probe.py, weights.lock.json, tests/test_cameras_yaml.py, tests/test_host_probe.py</files>
  <read_first>
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"Phase 0 Test Surface: ALL 4 CAMERAS" (has the exact cameras.yaml draft to commit verbatim)
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"Phase 0 Measurement Harness Format" (exact JSON schema)
    - .planning/research/PITFALLS.md §1.2 (cv2.getBuildInformation FFMPEG assert)
    - .planning/research/SUMMARY.md §6 Q2 (WSL2 mirrored networking), §6 Q3 (FFmpeg backend), §6 Q6 (DVR connection cap)
    - cameras.txt — ground truth per-camera facts
    - terminal.txt — ffplay commands prove the sub_path + main_path values
  </read_first>
  <behavior>
    - Test: `load_cameras(Path("cameras.yaml"))` returns a `CamerasFile` with exactly 4 cameras; ids 1..4 in order
    - Test: Cam 1 (`exterior_red`) has sub_path="/1", main_path="/0", codec="hevc", native_fps=25, 1280x720
    - Test: Cam 2 (`interior_orange`) has sub_path="/11", main_path="/10", native_fps=12, 1920x1080, sensor_native="5MP"
    - Test: Cam 3 (`exterior_blue`) has sub_path="/21", nal_unit_0_workaround_required=True
    - Test: Cam 4 (`interior_green`) has sub_path="/31", nal_unit_0_workaround_required=True, native_fps=25
    - Test: `build_rtsp_url(settings, cams[0], stream="sub")` produces `rtsp://admin:testpw@192.168.1.10:554/1` given a fixture Settings
    - Test: `build_rtsp_url(settings, cams[0], stream="main")` produces `rtsp://.../0`
    - Test: `assert_ffmpeg_backend()` passes on the user's host and raises `RuntimeError` with "FFmpeg" in the message when FFmpeg is absent (mocked)
    - Test: `assert_hevc_decoder()` passes on the user's host (cv2.getBuildInformation contains "hevc" or "h265") — the real OpenCV 4.13 wheel has HEVC
    - Test: `probe_host()` returns a dict with all the keys CONTEXT.md §"host" schema requires (os, kernel, cpu_model, cpu_cores_physical, cpu_cores_logical, ram_gb, wsl_version, wsl2_networking_mode)
    - Test: `probe_dvr_reachable("127.0.0.1", 1, timeout=0.1)` returns False quickly (nothing is listening on port 1)
    - Test: `detect_wsl2_networking_mode()` returns one of {"mirrored", "nat", "bridged", "unknown"} without raising
  </behavior>
  <action>
    1. Create `cameras.yaml` at repo root. Copy the EXACT structure from CONTEXT.md §"Phase 0 Test Surface: ALL 4 CAMERAS" → cameras.yaml initial draft. Do not paraphrase:

    ```yaml
    # home_cctv cameras.yaml — committed Phase 0 artifact, consumed by Phase 1+
    # Ground truth: cameras.txt + terminal.txt
    dvr:
      host_env: DVR_IP
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

    2. Create `src/home_cctv/config/cameras.py` with `CameraConfig`, `CamerasFile`, `load_cameras`, `build_rtsp_url` per the interfaces block above. Use pydantic v2 (BaseModel), `yaml.safe_load`, and make `build_rtsp_url` format `rtsp://{user}:{pw}@{ip}:{port}{path}`. Normalize leading slash on the path.

    3. Create `src/home_cctv/phase0/__init__.py` with a one-line docstring.

    4. Create `src/home_cctv/phase0/host_probe.py` implementing:
       - `assert_ffmpeg_backend() -> str`: calls `cv2.getBuildInformation()`, uses regex `re.search(r"FFMPEG:\s+YES", info)`. Raises `RuntimeError("OpenCV wheel built without FFmpeg — TCP / stimeout env vars are silently ignored. Install opencv-python-headless==4.13.0.92 (see PITFALLS §1.2).")` on failure. Returns a snippet of FFMPEG-related build lines on success.
       - `assert_hevc_decoder() -> bool`: parses `cv2.getBuildInformation()` and searches (case-insensitive) for the substring `"hevc"` or `"h265"`. **No subprocess fallback** — the system `ffmpeg` binary is irrelevant; only the FFmpeg bundled inside the `opencv-python-headless` wheel matters. If the build info does not mention either, raise `RuntimeError("OpenCV wheel lacks HEVC decoder — pip install opencv-python-headless==4.13.0.92 rebuild required. All 4 cameras are H.265 per cameras.txt; without HEVC they will open but decode to green/black.")`.
       - `detect_wsl2_networking_mode() -> Literal["mirrored","nat","bridged","unknown"]`: reads `/proc/net/route`, parses default gateway hex. `172.*` or `10.255.*` → "nat"; `192.168.*` → "mirrored"; else "bridged". Returns "unknown" on any exception.
       - `probe_host() -> dict`: uses `platform.uname()` + `psutil.virtual_memory()` + `psutil.cpu_count(logical=False|True)`. Attempts `wsl.exe --version` via subprocess with a 2s timeout; falls back to `/proc/version`. Returns dict with keys: os, kernel, cpu_model, cpu_cores_physical, cpu_cores_logical, ram_gb (rounded to 2dp), wsl2_networking_mode, wsl_version.

    5. Create `src/home_cctv/phase0/network_probe.py` implementing:
       - `probe_dvr_reachable(host, port, *, timeout=3.0) -> bool`: `socket.create_connection((host, port), timeout=timeout)` inside try/except OSError.
       - `measure_handshake_latency(rtsp_url, *, samples=3) -> Optional[float]`: Calls `assert_capture_options_active()` then opens N captures via `cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)`, measures time to `isOpened() + read()`, returns mean in ms. Sleeps 0.2s between samples.
       - `step_concurrent_sessions(rtsp_url, *, max_n=6) -> tuple[int,int]`: Answers SUMMARY.md §6 Q6 (DVR connection cap). Opens captures 1..max_n, keeps them open in a list, reads one frame from each. Returns `(last_ok, tested)`. Releases all captures in a finally block.

    6. Create `weights.lock.json` at repo root as a scaffold. Sizes + cold-start timings are populated automatically by `download_model_bundle()` → `persist_weights_lock()` on first successful run (Task 2). **Important:** because every startup asserts sizes against this file, the scaffold MUST mark every entry `verified: false` so the startup check knows to ignore size-0 placeholders. Once Task 2 has run once on the real host, commit the populated file.
    ```json
    {
      "_note": "Weight bundle manifest. Populated by phase0.model_bundle.download_model_bundle() -> persist_weights_lock() on first successful run. Commit after first pass so subsequent startups verify cache integrity. The `cold_start_ms` block is also filled in by the same call, via warmup_model_bundle().",
      "cache_root": "$HOME/.cache/home_cctv/models",
      "weights": {
        "yolo26n.pt": { "size_bytes": 0, "verified": false },
        "yolov8n.pt": { "size_bytes": 0, "verified": false },
        "yolo26n_openvino_model": { "size_bytes": 0, "verified": false, "is_directory": true },
        "deepface_arcface": { "size_bytes": 0, "verified": false },
        "deepface_retinaface": { "size_bytes": 0, "verified": false },
        "easyocr_english": { "size_bytes": 0, "verified": false, "is_directory": true }
      },
      "cold_start_ms": { "yolo_openvino": 0, "deepface": 0, "easyocr": 0 }
    }
    ```

    7. Create `tests/test_cameras_yaml.py` exercising the 8 behaviors above. Use a `REPO = Path(__file__).parent.parent` fixture to locate cameras.yaml. Use `monkeypatch.setenv` + `load_settings()` for the Settings fixture.

    8. Create `tests/test_host_probe.py`:
       - `test_assert_ffmpeg_backend_on_real_cv2` — real host, expects pass
       - `test_assert_hevc_decoder_on_real_cv2` — real host, expects pass
       - `test_assert_ffmpeg_backend_raises_when_missing` — `unittest.mock.patch` on `cv2.getBuildInformation` returning `"Video I/O:\n    FFMPEG: NO\n"`, expects `RuntimeError(match="FFmpeg")`
       - `test_probe_host_returns_required_keys` — asserts every key from the schema is present
       - `test_dvr_reachable_negative_case` — `probe_dvr_reachable("127.0.0.1", 1, timeout=0.2)` is False

    9. Run `uv run pytest tests/test_cameras_yaml.py tests/test_host_probe.py -x -q`. All tests must pass. If `assert_hevc_decoder` fails on the real host, STOP and install a HEVC-capable FFmpeg (`sudo apt install ffmpeg`) and/or a HEVC-enabled OpenCV wheel — this is exactly the ENV-05 failure mode the plan is meant to surface.
  </action>
  <verify>
    <automated>uv run pytest tests/test_cameras_yaml.py tests/test_host_probe.py -x -q &amp;&amp; uv run python -c "from home_cctv.config.cameras import load_cameras; c = load_cameras('cameras.yaml'); assert len(c.cameras) == 4; assert c.cameras[2].nal_unit_0_workaround_required is True; assert c.cameras[3].nal_unit_0_workaround_required is True; print('cameras.yaml OK')"</automated>
  </verify>
  <done>
    - `cameras.yaml` exists at repo root with all 4 cameras, verbatim structure from CONTEXT.md
    - `uv run pytest tests/test_cameras_yaml.py tests/test_host_probe.py -x -q` passes
    - `grep -q "nal_unit_0_workaround_required: true" cameras.yaml` matches twice (Cam 3 and Cam 4)
    - `grep -q "FFMPEG:.*YES" src/home_cctv/phase0/host_probe.py` exits 0 (regex pattern present)
    - `grep -q "hevc" src/home_cctv/phase0/host_probe.py` exits 0
    - `weights.lock.json` committed with the 6-entry weights manifest
    - On the real host: `uv run python -c "from home_cctv.phase0.host_probe import assert_ffmpeg_backend, assert_hevc_decoder; print(assert_ffmpeg_backend()); assert assert_hevc_decoder()"` exits 0
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Model bundle downloader + verifier + Phase0Report writer + offline --mp4 dry-run of the harness</name>
  <files>src/home_cctv/phase0/model_bundle.py, src/home_cctv/phase0/report.py, src/home_cctv/phase0/sanity.py, src/home_cctv/__main__.py, tests/test_report_schema.py, tests/test_model_bundle.py</files>
  <read_first>
    - src/home_cctv/phase0/host_probe.py (Task 1)
    - src/home_cctv/phase0/network_probe.py (Task 1)
    - src/home_cctv/config/cameras.py (Task 1)
    - src/home_cctv/ingest/capture.py (Plan 02)
    - src/home_cctv/ingest/supervisor.py (Plan 02)
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"Phase 0 Measurement Harness Format" (EXACT JSON schema — copy into code)
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"Model Pre-Download"
    - .planning/research/SUMMARY.md §2 (model names, versions) and §5 pitfall #3 (cold-start downloads)
  </read_first>
  <behavior>
    - Test: `Phase0Report` dataclass round-trips through JSON — `write_report(tmp_path, report); Phase0Report.from_json(tmp_path)` returns an equal object
    - Test: `write_report` produces JSON whose top-level keys exactly match the CONTEXT.md schema: timestamp_utc, host, opencv, env_vars_loaded, credentials_masked_in_logs, disk_space, dvr, cameras, model_bundle, blockers_resolved, phase0_verdict, notes
    - Test: `report.model_bundle["cold_start_ms"]` is a dict with exactly the keys `yolo_openvino`, `deepface`, `easyocr`, each an integer ≥ 0 (B1 — CONTEXT.md §"Phase 0 Measurement Harness Format" locks this sub-schema)
    - Test: `CameraResult` serializes to keys matching CONTEXT.md per-camera schema (name, sub_path, main_path, codec, advertised_fps, measured_fps, width, height, capture_duration_sec, frames_decoded, frames_corrupted, decode_errors, hang_events). For Cam 3/4 a `nal_unit_0_workaround_required: true` key is emitted.
    - Test: `verify_model_bundle(cache_dir)` with no cached weights returns a report with `yolo26n_pt_cached=False`, does NOT raise
    - Test: `verify_model_bundle(cache_dir)` after touching a fake `yolo26n.pt` of the right size returns `yolo26n_pt_cached=True`
    - Test: `verify_model_bundle(cache_dir)` return value ALWAYS contains a `cold_start_ms` key whose value is a dict with exactly `yolo_openvino`, `deepface`, `easyocr` (values may be 0 when warmup has not yet run — the key and the three sub-keys must never be missing)
    - Test: After a successful `download_model_bundle(cache_dir)` (marked `@pytest.mark.slow`), `verify_model_bundle(cache_dir)["cold_start_ms"]` has all three values > 0 AND `weights.lock.json` on disk contains the same values (asserting `persist_weights_lock` was called automatically — no manual shell snippet)
    - Test: `per_camera_exit_ok` for Cam 1 with measured_fps=24, advertised_fps=25, frames_corrupted=0, hang_events=0 → True
    - Test: `per_camera_exit_ok` for Cam 1 with measured_fps=10 (>20% off 25) → False
    - Test: `per_camera_exit_ok` for Cam 3 with frames_decoded=950, frames_corrupted=30, hang_events=0, measured_fps=11.6 → True (ratio 950/980=0.969 ≥ 0.95)
    - Test: `per_camera_exit_ok` for Cam 3 with frames_decoded=900, frames_corrupted=100 → False (ratio 0.9)
    - Test: `run_phase0(settings, mp4_override="tests/fixtures/sample_720p25.mp4", duration_sec=5, skip_model_bundle=True)` runs a single short pass against the fixture MP4, writes PHASE0-REPORT.json with phase0_verdict populated
    - Test: stdout summary printed by `print_stdout_summary(report)` is non-empty and contains every camera name from the report
  </behavior>
  <action>
    1. Create `src/home_cctv/phase0/model_bundle.py`:
       - `WEIGHTS_LOCK_PATH = Path(__file__).resolve().parents[3] / "weights.lock.json"` — anchored to repo root (`src/home_cctv/phase0/model_bundle.py` → `parents[3]` reaches the repo root). Relative paths break as soon as cwd != repo root (e.g. when pytest or a systemd unit changes directory).
       - `download_model_bundle(cache_dir: Path) -> dict`: Downloads each weight via the standard library APIs:
         * YOLO26n + YOLOv8n: `from ultralytics import YOLO; m = YOLO("yolo26n.pt")` which auto-downloads to `cache_dir / "yolo26n.pt"` (move if needed). Same for `yolov8n.pt`.
         * YOLO OpenVINO IR export: `m.export(format="openvino", half=False, dynamic=False, imgsz=640)` then move the resulting `yolo26n_openvino_model/` folder into `cache_dir`.
         * DeepFace ArcFace + RetinaFace: `from deepface import DeepFace; DeepFace.build_model("ArcFace"); DeepFace.build_model("retinaface")` (both cache under `~/.deepface/weights/`; record paths).
         * EasyOCR English: `import easyocr; reader = easyocr.Reader(['en'], gpu=False, model_storage_directory=str(cache_dir / 'easyocr_english'))` — forces download on first call.
         After every download succeeds, call `warmup_model_bundle(cache_dir)` (below) to wall-clock each cold-start, then call `persist_weights_lock(bundle_with_cold_starts)` so `weights.lock.json` is updated with both the real byte sizes AND the three `cold_start_ms` integers in one atomic step. Never make the caller run a separate shell snippet to populate the lock file. Returns a dict mapping name → {path, size_bytes, cold_start_ms?}.
       - `warmup_model_bundle(cache_dir: Path) -> dict[str, int]`: Executes exactly ONE warm-up call per model and records wall-clock deltas with `time.perf_counter()`. Returns a dict with these three keys, values are integer milliseconds:
         * `yolo_openvino`: load the OpenVINO IR export with `ultralytics.YOLO(str(cache_dir / "yolo26n_openvino_model"))` (or the `.xml` path), then run `model(np.zeros((640, 640, 3), dtype=np.uint8), verbose=False)`. `t0 = time.perf_counter()` wraps the `model(...)` call only; measure inference, not load.
         * `deepface`: `from deepface import DeepFace; DeepFace.represent(img_path=np.zeros((224, 224, 3), dtype=np.uint8), model_name="ArcFace", detector_backend="skip", enforce_detection=False)`. Wrap in `t0 = time.perf_counter()` / `t1 = time.perf_counter()`.
         * `easyocr`: `import easyocr; reader = easyocr.Reader(["en"], gpu=False, model_storage_directory=str(cache_dir / "easyocr_english")); reader.readtext(np.zeros((100, 300, 3), dtype=np.uint8))`. Wrap only the `readtext` call.
         Each delta is `int(round((t1 - t0) * 1000))`. All three values MUST be positive integers (≥ 1) on a working host; assert-style log-and-continue if any is zero, do not raise.
       - `verify_model_bundle(cache_dir: Path) -> dict`: Reads `WEIGHTS_LOCK_PATH`, for each entry stats the file/dir, compares size to the lock entry if `verified: true`. Returns dict with keys: `yolov8n_pt_cached`, `yolo26n_pt_cached`, `yolo_openvino_export_cached`, `deepface_arcface_cached`, `deepface_retinaface_cached`, `easyocr_english_cached`, `total_weights_size_mb`, AND `cold_start_ms: {"yolo_openvino": int, "deepface": int, "easyocr": int}` read straight from `weights.lock.json` (if the lock file was already populated by a previous run). Missing or size-mismatched weights are reported as False (do NOT raise; let the caller decide to redownload). If the lock file has no `cold_start_ms` block yet, populate with `{"yolo_openvino": 0, "deepface": 0, "easyocr": 0}` so the schema is never missing the key.
       - `persist_weights_lock(bundle: dict)`: writes the current sizes AND the `cold_start_ms` dict back to `WEIGHTS_LOCK_PATH` with `verified: true`. Called automatically at the end of a successful `download_model_bundle()` — no manual shell snippet needed anywhere.

    2. Create `src/home_cctv/phase0/report.py`. Use `@dataclass` with `from_json`/`to_json` helpers. Match the CONTEXT.md schema EXACTLY:

    ```python
    """Phase0Report writer. Schema mirrors CONTEXT.md §Phase 0 Measurement Harness Format."""
    from __future__ import annotations
    import json
    from dataclasses import dataclass, field, asdict
    from datetime import datetime, timezone
    from pathlib import Path
    from typing import Optional

    # Canonical path for the COMMITTED artifact. Tests and dry-runs MUST NOT
    # write to this path — they must pass an explicit report_path (tmp_path in
    # tests, or REPORT_PATH.with_suffix(".dryrun.json") in --mp4 dry-run mode).
    REPORT_PATH = Path(".planning/phases/00-environment-sanity/PHASE0-REPORT.json")

    @dataclass
    class CameraResult:
        name: str
        sub_path: str
        main_path: str
        codec: str
        advertised_fps: int
        measured_fps: float
        width: int
        height: int
        capture_duration_sec: int
        frames_decoded: int
        frames_corrupted: int
        decode_errors: int
        hang_events: int
        nal_unit_0_workaround_required: bool = False
        exit_ok: bool = False

    @dataclass
    class Phase0Report:
        timestamp_utc: str
        host: dict
        opencv: dict
        env_vars_loaded: list[str]
        credentials_masked_in_logs: bool
        disk_space: dict
        dvr: dict
        cameras: dict  # str(id) -> CameraResult.as_dict
        model_bundle: dict
        blockers_resolved: dict
        phase0_verdict: str  # "pass" | "fail" | "aborted"
        notes: str = ""

        def to_dict(self) -> dict:
            return asdict(self)

        @classmethod
        def from_json(cls, path: Path) -> "Phase0Report":
            """Load a previously-written report. `cameras` is a plain dict[str, dict]
            in the schema, so shallow ``cls(**data)`` unpack is correct.
            """
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(**data)

    def new_report() -> Phase0Report:
        return Phase0Report(
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            host={}, opencv={}, env_vars_loaded=[],
            credentials_masked_in_logs=True, disk_space={}, dvr={},
            cameras={}, model_bundle={},
            blockers_resolved={
                "Q1_host_fps_baseline": False,
                "Q2_wsl2_networking": False,
                "Q3_opencv_ffmpeg_backend": False,
                "Q4_ffplay_flags_translated": False,
                "Q5_reconnect_watchdog_sketch": "designed but not yet implemented",
                "Q6_dvr_connection_cap": False,
            },
            phase0_verdict="aborted",
            notes="",
        )

    def write_report(report: Phase0Report, path: Path) -> Path:
        """Write report to an EXPLICIT path. No default — callers must decide.
        Tests pass `tmp_path / "report.json"`. Dry-run mode passes
        `REPORT_PATH.with_suffix(".dryrun.json")`. Only the live harness passes
        `REPORT_PATH` itself.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, default=str)
        return path

    def print_stdout_summary(report: Phase0Report) -> None:
        print("=" * 72)
        print(f"Phase 0 — {report.timestamp_utc}   verdict={report.phase0_verdict}")
        print("-" * 72)
        for cam_id, c in report.cameras.items():
            mark = "PASS" if c.get("exit_ok") else "FAIL"
            print(f"  [{mark}] cam{cam_id} {c['name']:20s} "
                  f"fps={c['measured_fps']:.2f}/{c['advertised_fps']}  "
                  f"decoded={c['frames_decoded']}  corrupted={c['frames_corrupted']}  "
                  f"errors={c['decode_errors']}  hangs={c['hang_events']}")
        print("-" * 72)
        print(f"blockers: {report.blockers_resolved}")
        print("=" * 72)
    ```

    3. Create `src/home_cctv/phase0/sanity.py`. Implements `run_phase0` and `per_camera_exit_ok`. Per-camera exit criteria are verbatim from CONTEXT.md §"Per-camera exit criteria":

    ```python
    """Phase 0 measurement harness. Sweeps all 4 cameras sequentially for
    PHASE0_DURATION_SEC each, writes PHASE0-REPORT.json, prints stdout summary.
    """
    from __future__ import annotations
    import logging
    import os
    import time
    from pathlib import Path
    from typing import Optional

    import cv2

    from home_cctv.config.env import Settings
    from home_cctv.config.cameras import load_cameras, build_rtsp_url, CameraConfig
    from home_cctv.ingest.capture import open_frame_source
    from home_cctv.ingest.display import DisplaySink
    from home_cctv.ingest.supervisor import ShutdownSupervisor
    from home_cctv.phase0 import host_probe, network_probe, model_bundle
    from home_cctv.phase0.report import (
        Phase0Report, CameraResult, new_report, write_report, print_stdout_summary,
    )

    _LOG = logging.getLogger("home_cctv.phase0")

    PHASE0_DURATION_SEC: int = 1800  # 30 minutes per camera per CONTEXT.md
    _FPS_TOLERANCE: float = 0.20     # ±20%

    def per_camera_exit_ok(cam: CameraConfig, c: CameraResult) -> bool:
        # Rule 1: fps within ±20% of advertised
        lo = cam.native_fps * (1 - _FPS_TOLERANCE)
        hi = cam.native_fps * (1 + _FPS_TOLERANCE)
        if not (lo <= c.measured_fps <= hi):
            return False
        # Rule 2: zero hangs
        if c.hang_events != 0:
            return False
        # Rule 3: clean-frame ratio
        total = c.frames_decoded + c.frames_corrupted
        if total == 0:
            return False
        if cam.nal_unit_0_workaround_required:
            # Cam 3/4: allow decode errors but ratio must be >= 0.95
            return (c.frames_decoded / total) >= 0.95
        # Cam 1/2: zero green frames allowed
        return c.frames_corrupted == 0

    def _capture_one(
        cam: CameraConfig, rtsp_or_mp4: str, out_dir: Path,
        duration_sec: int, supervisor: ShutdownSupervisor, show: bool,
    ) -> CameraResult:
        source_id = f"cam{cam.id}:{cam.name}"
        fs = open_frame_source(rtsp_or_mp4, camera_id=source_id)
        supervisor.register(fs)
        sink = DisplaySink.open(source_id=source_id, out_dir=out_dir, show=show)

        result = CameraResult(
            name=cam.name, sub_path=cam.sub_path, main_path=cam.main_path,
            codec=cam.codec, advertised_fps=cam.native_fps, measured_fps=0.0,
            width=cam.native_width, height=cam.native_height,
            capture_duration_sec=0, frames_decoded=0, frames_corrupted=0,
            decode_errors=0, hang_events=0,
            nal_unit_0_workaround_required=cam.nal_unit_0_workaround_required,
        )
        try:
            fs.open()
            _LOG.info("[%s] capture_started target=%s duration=%ds", source_id, cam.sub_path, duration_sec)
            t_start = time.monotonic()
            while not supervisor.stop_event.is_set():
                if time.monotonic() - t_start >= duration_sec:
                    break
                ok, frame = fs.read()
                if not ok:
                    # EOF heuristic is FILE-ONLY (W5). Live RTSP relies purely on
                    # the duration_sec wall-clock loop — Cam 3/4 produce NAL-unit-0
                    # decode errors early by design (CONTEXT.md §"New Facts Learned
                    # This Session" #2) and must NEVER trigger an early exit.
                    if fs.is_file_source and fs.stats.decode_errors > 0 and fs.stats.frames_decoded > 0 and time.monotonic() - t_start >= 0.5:
                        break
                    continue
                sink.write(frame)
            result.capture_duration_sec = int(time.monotonic() - t_start)
            result.frames_decoded = fs.stats.frames_decoded
            result.frames_corrupted = fs.stats.frames_corrupted
            result.decode_errors = fs.stats.decode_errors
            result.hang_events = fs.stats.hang_events
            result.measured_fps = round(fs.stats.measured_fps, 2)
        finally:
            sink.close()
            fs.release()
        result.exit_ok = per_camera_exit_ok(cam, result)
        _LOG.info("[%s] capture_done decoded=%d corrupted=%d fps=%.2f exit_ok=%s",
                  source_id, result.frames_decoded, result.frames_corrupted,
                  result.measured_fps, result.exit_ok)
        return result

    def run_phase0(
        settings: Settings, *,
        mp4_override: Optional[str] = None,
        duration_sec: int = PHASE0_DURATION_SEC,
        show: bool = False,
        skip_model_bundle: bool = False,
        skip_network_probe: bool = False,
        report_path: Optional[Path] = None,
    ) -> Phase0Report:
        from home_cctv.phase0.report import REPORT_PATH
        # Canonical artifact is reserved for live runs. Dry-run / tests must
        # never overwrite it — they get a .dryrun.json sibling or an explicit
        # path. B3 in the Phase 0 checker run.
        if report_path is None:
            report_path = (
                REPORT_PATH if mp4_override is None
                else REPORT_PATH.with_suffix(".dryrun.json")
            )
        report = new_report()

        # --- Host / OpenCV probes (Q2, Q3) ---
        report.host = host_probe.probe_host()
        try:
            ffmpeg_snippet = host_probe.assert_ffmpeg_backend()
            hevc_ok = host_probe.assert_hevc_decoder()
        except RuntimeError as exc:
            report.notes += f"STARTUP FAIL: {exc}\n"
            report.phase0_verdict = "fail"
            write_report(report, report_path); print_stdout_summary(report)
            return report
        report.opencv = {
            "version": cv2.__version__,
            "ffmpeg_backend": True,
            "hevc_decoder": hevc_ok,
            "build_info_snippet": ffmpeg_snippet,
            "num_threads": cv2.getNumThreads(),
        }
        report.blockers_resolved["Q3_opencv_ffmpeg_backend"] = True
        report.blockers_resolved["Q2_wsl2_networking"] = report.host["wsl2_networking_mode"] in {"mirrored", "bridged"}

        # --- env vars + masked ---
        report.env_vars_loaded = [
            k for k in ("DVR_IP", "DVR_PORT", "DVR_USER", "EVENT_IMAGE_DIR", "DB_PATH")
            if os.environ.get(k)
        ]
        report.credentials_masked_in_logs = True

        # --- disk space ---
        import shutil
        du = shutil.disk_usage(str(settings.EVENT_IMAGE_DIR.parent))
        report.disk_space = {
            "event_image_dir_path": str(settings.EVENT_IMAGE_DIR),
            "event_image_dir_filesystem": "ext4",
            "free_gb": round(du.free / (1024 ** 3), 2),
            "on_drvfs": str(settings.EVENT_IMAGE_DIR).startswith(("/mnt/c/", "/mnt/d/")),
        }

        # --- Cameras ---
        cams = load_cameras(Path("cameras.yaml"))

        # --- DVR reachability + connection cap (Q6) ---
        if mp4_override is None and not skip_network_probe:
            reachable = network_probe.probe_dvr_reachable(settings.DVR_IP, settings.DVR_PORT, timeout=3.0)
            report.dvr = {"reachable": reachable, "ip": settings.DVR_IP, "port": settings.DVR_PORT}
            if not reachable:
                report.notes += (
                    f"DVR at {settings.DVR_IP}:{settings.DVR_PORT} unreachable from WSL2 "
                    f"(networking_mode={report.host['wsl2_networking_mode']}). "
                    f"Fix .wslconfig networkingMode=mirrored or add a bridged adapter.\n"
                )
                report.phase0_verdict = "fail"
                write_report(report, report_path); print_stdout_summary(report)
                return report
            lat = network_probe.measure_handshake_latency(
                build_rtsp_url(settings, cams.cameras[0], stream="sub"), samples=3,
            )
            report.dvr["handshake_latency_ms_mean"] = round(lat or 0.0, 2)
            last_ok, tested = network_probe.step_concurrent_sessions(
                build_rtsp_url(settings, cams.cameras[0], stream="sub"), max_n=6,
            )
            report.dvr["max_concurrent_sessions_last_ok"] = last_ok
            report.dvr["max_concurrent_sessions_tested"] = tested
            report.blockers_resolved["Q6_dvr_connection_cap"] = True
        else:
            report.dvr = {"reachable": True, "ip": "offline-mp4", "port": 0}

        # --- Model bundle ---
        # B1 invariant: `report.model_bundle["cold_start_ms"]` must ALWAYS be a
        # dict with exactly the keys yolo_openvino/deepface/easyocr, even when
        # the bundle was skipped (dry-run mode). Consumers of the report rely
        # on the key existing.
        if not skip_model_bundle:
            try:
                model_bundle.download_model_bundle(settings.MODEL_CACHE_DIR)
            except Exception as exc:
                _LOG.warning("model_bundle download incomplete: %r", exc)
            report.model_bundle = model_bundle.verify_model_bundle(settings.MODEL_CACHE_DIR)
            if "cold_start_ms" not in report.model_bundle:
                report.model_bundle["cold_start_ms"] = {"yolo_openvino": 0, "deepface": 0, "easyocr": 0}
        else:
            report.model_bundle = {
                "skipped": True,
                "cold_start_ms": {"yolo_openvino": 0, "deepface": 0, "easyocr": 0},
            }

        # --- Camera sweep ---
        supervisor = ShutdownSupervisor()
        all_ok = True
        for cam in cams.cameras:
            target = mp4_override if mp4_override else build_rtsp_url(settings, cam, stream="sub")
            cresult = _capture_one(cam, target, settings.EVENT_IMAGE_DIR, duration_sec, supervisor, show)
            report.cameras[str(cam.id)] = asdict_shim(cresult)
            if not cresult.exit_ok:
                all_ok = False
            if supervisor.stop_event.is_set():
                report.phase0_verdict = "aborted"
                break

        if report.phase0_verdict != "aborted":
            report.blockers_resolved["Q1_host_fps_baseline"] = True
            report.blockers_resolved["Q4_ffplay_flags_translated"] = True
            report.phase0_verdict = "pass" if all_ok else "fail"

        write_report(report, report_path)
        print_stdout_summary(report)
        return report

    def asdict_shim(c: CameraResult) -> dict:
        from dataclasses import asdict
        return asdict(c)
    ```

    4. Wire `--phase0` into `src/home_cctv/__main__.py`. In `main()` after logging + supervisor setup:

    ```python
    if args.phase0:
        from home_cctv.phase0.sanity import run_phase0, PHASE0_DURATION_SEC
        duration_sec = 5 if args.mp4 else PHASE0_DURATION_SEC  # short for --mp4 dry-run
        report = run_phase0(
            settings,
            mp4_override=args.mp4,
            duration_sec=duration_sec,
            show=args.show,
            skip_model_bundle=bool(args.mp4),    # skip download in dry-run mode
            skip_network_probe=bool(args.mp4),   # skip DVR probes in dry-run mode
            report_path=None,                    # live → canonical path, dry-run → .dryrun.json
        )
        return 0 if report.phase0_verdict == "pass" else 1
    ```

    (Import of `PHASE0_DURATION_SEC` is now inside the `if args.phase0:` block above, alongside `run_phase0`.)

    5. Create `tests/test_model_bundle.py`:
       - `test_verify_empty_cache_no_raise(tmp_path)` — `verify_model_bundle(tmp_path)` returns a dict with all `_cached=False`, does not raise
       - `test_verify_after_touching_yolo(tmp_path)` — touch a 100KB `yolo26n.pt` in tmp_path, verify returns `yolo26n_pt_cached=True`
       - `test_download_bundle_integration(tmp_path)` marked `@pytest.mark.slow` — actually calls `download_model_bundle(tmp_path)`; expects final verify to show all `_cached=True`. Skip by default; enable with `-m slow`.

    6. Create `tests/test_report_schema.py`:
       - `test_new_report_has_context_md_keys()` — every top-level field from CONTEXT.md schema is present
       - `test_write_and_read_roundtrip(tmp_path)` — build a fresh `new_report()`, call `write_report(report, tmp_path / "r.json")`, then `Phase0Report.from_json(tmp_path / "r.json")` returns an object whose `to_dict()` equals the original `to_dict()` (full round-trip through JSON). Ensures `from_json` is defined (B4 would have surfaced here).
       - `test_per_camera_exit_ok_cam1_ok()` — measured_fps=24, advertised=25, 0 corrupted → True
       - `test_per_camera_exit_ok_cam1_fps_too_low()` — measured_fps=10, advertised=25 → False
       - `test_per_camera_exit_ok_cam3_ratio_ok()` — nal_unit_0_workaround_required=True, 950/980 = 0.969 → True
       - `test_per_camera_exit_ok_cam3_ratio_fail()` — 900/1000 = 0.90 → False
       - `test_per_camera_exit_ok_cam1_any_corruption_fails()` — nal_unit_0_workaround_required=False, 1 corrupted → False
       - `test_offline_dry_run_mp4(tmp_path, monkeypatch, fake_settings)` — runs `run_phase0(settings, mp4_override=str(FIXTURES/"sample_720p25.mp4"), duration_sec=5, skip_model_bundle=True, skip_network_probe=True, report_path=tmp_path/"dry.json")` — asserts the REPORT at `tmp_path / "dry.json"` is written, contains 4 camera entries, `phase0_verdict` populated. The same MP4 is reused for all 4 cameras in dry-run mode (the point is to exercise the harness shell, not the per-camera feed). **CRITICAL:** The test must also assert that `.planning/phases/00-environment-sanity/PHASE0-REPORT.json` was NOT modified by the dry-run (take a pre-run mtime/content snapshot if the file exists, compare post-run). This enforces B3 — dry-runs and tests never overwrite the committed artifact.
       - `test_stdout_summary_contains_camera_names(capsys)` — build a minimal Phase0Report with 4 cams, call `print_stdout_summary`, assert each of `exterior_red`, `interior_orange`, `exterior_blue`, `interior_green` appears in captured stdout.

    7. Run `uv run pytest tests/ -x -q -m 'not slow'`. All non-slow tests must pass. Run the offline dry-run manually: `uv run python -m home_cctv --phase0 --mp4 tests/fixtures/sample_720p25.mp4` and verify PHASE0-REPORT.json is written with 4 camera rows and the harness exits cleanly.
  </action>
  <verify>
    <automated>uv run pytest tests/test_report_schema.py tests/test_model_bundle.py -x -q -m "not slow" &amp;&amp; uv run python -m home_cctv --phase0 --mp4 tests/fixtures/sample_720p25.mp4 &amp;&amp; uv run python -c "import json; from pathlib import Path; dry = Path('.planning/phases/00-environment-sanity/PHASE0-REPORT.dryrun.json'); assert dry.exists(), 'dry-run report missing'; r = json.loads(dry.read_text()); assert len(r['cameras']) == 4; assert set(r['cameras']['1'].keys()) >= {'name','sub_path','main_path','codec','advertised_fps','measured_fps','frames_decoded','frames_corrupted','decode_errors','hang_events'}; assert 'cold_start_ms' in r['model_bundle'] and set(r['model_bundle']['cold_start_ms'].keys()) == {'yolo_openvino','deepface','easyocr'}; print('dry-run report schema OK, verdict=', r['phase0_verdict'])"</automated>
  </verify>
  <done>
    - All non-slow tests in `tests/test_report_schema.py` and `tests/test_model_bundle.py` pass
    - `uv run python -m home_cctv --phase0 --mp4 tests/fixtures/sample_720p25.mp4` writes `.planning/phases/00-environment-sanity/PHASE0-REPORT.dryrun.json` (NOT the canonical `PHASE0-REPORT.json`) with 4 camera rows — B3
    - If a committed `PHASE0-REPORT.json` exists, its mtime is UNCHANGED after the dry-run (enforced by `test_offline_dry_run_mp4` via mtime snapshot) — B3
    - PHASE0-REPORT.dryrun.json contains the full CONTEXT.md schema: top-level `host`, `opencv`, `env_vars_loaded`, `credentials_masked_in_logs`, `disk_space`, `dvr`, `cameras`, `model_bundle`, `blockers_resolved`, `phase0_verdict`
    - `report.model_bundle["cold_start_ms"]` dict is always present with exactly the keys `yolo_openvino`, `deepface`, `easyocr` (may be 0 in dry-run because `skip_model_bundle=True`, but the key MUST exist) — B1
    - `Phase0Report.from_json(path)` round-trips a written report back to an equal object — B4
    - `write_report(report, path)` requires `path` as an explicit argument (no default) — B3
    - `grep -q "PHASE0_DURATION_SEC = 1800" src/home_cctv/phase0/sanity.py` exits 0
    - `grep -q "per_camera_exit_ok" src/home_cctv/phase0/sanity.py` exits 0
    - `grep -q "nal_unit_0_workaround_required" src/home_cctv/phase0/sanity.py` exits 0
    - `grep -q "def from_json" src/home_cctv/phase0/report.py` exits 0 — B4
    - `grep -q "warmup_model_bundle" src/home_cctv/phase0/model_bundle.py` exits 0 — B1
    - `grep -q "persist_weights_lock" src/home_cctv/phase0/model_bundle.py` exits 0 AND is called from `download_model_bundle` — W6
    - `grep -q "parents\[3\]" src/home_cctv/phase0/model_bundle.py` exits 0 — W3 (absolute WEIGHTS_LOCK_PATH)
    - Stdout summary prints a PASS/FAIL per camera
  </done>
</task>

<task type="checkpoint:human-verify" gate="blocking">
  <name>Task 3: Live 4-camera 30-minute sweep against the real DVR + commit PHASE0-REPORT.json + weights.lock.json</name>
  <files>.planning/phases/00-environment-sanity/PHASE0-REPORT.json, weights.lock.json, $HOME/.cache/home_cctv/models/**</files>
  <read_first>
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"Success Criteria" + §"Per-camera exit criteria"
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"Phase 0 Test Surface: ALL 4 CAMERAS"
    - src/home_cctv/phase0/sanity.py (Task 2)
    - src/home_cctv/phase0/model_bundle.py (Task 2)
    - cameras.yaml (Task 1)
    - .env (user-owned — must contain real DVR creds)
  </read_first>
  <what-built>
    All code needed for the live Phase 0 sweep:
    - `cameras.yaml` + loader (Task 1)
    - Host / FFmpeg / HEVC / WSL2 networking probes (Task 1)
    - DVR reachability + handshake + connection-cap probes (Task 1)
    - Model bundle downloader + verifier (Task 2)
    - 4-camera sequential sweep harness producing PHASE0-REPORT.json (Task 2)
    - `--phase0` CLI flag wired into `__main__.py` (Task 2)
    - Offline `--mp4` dry-run of the harness, proven green on fixture MP4 (Task 2)
  </what-built>
  <how-to-verify>
    The automated parts of Phase 0 are green. Now the executor and the human run the LIVE 4-camera 30-minute sweep on the real WSL2 host. This is 2 hours of wall-clock time (4 × 30 min) and must be done once with real DVR credentials in `.env`. The executor performs the operational steps and the human confirms the verdict.

    **Pre-flight (automated by the executor):**

    1. Confirm `.env` exists at repo root and contains real DVR credentials pointing at `192.168.1.10:554` with a working user/pass. If missing, STOP and ask the user to populate it from `.env.example`.

    2. Confirm the HEVC decoder is present and WSL2 is configured:
       ```
       uv run python -c "from home_cctv.phase0.host_probe import assert_ffmpeg_backend, assert_hevc_decoder, detect_wsl2_networking_mode; print(assert_ffmpeg_backend()); assert assert_hevc_decoder(); print('wsl2_mode=', detect_wsl2_networking_mode())"
       ```
       Expected: `FFMPEG: YES` in snippet, no RuntimeError, networking mode is `mirrored` or `bridged`. If `nat` or DVR unreachable, STOP and point the user at `.wslconfig` fix (add `networkingMode=mirrored` under `[wsl2]`).

    3. Pre-download + warm-up the model bundle (one-time, ~10–20 minutes, ~1.5 GB). **`download_model_bundle` now calls `warmup_model_bundle` and `persist_weights_lock` automatically** — no manual persist snippet needed (W6):
       ```
       uv run python -c "
       from home_cctv.config.env import load_settings
       from home_cctv.phase0.model_bundle import download_model_bundle, verify_model_bundle
       s = load_settings()
       download_model_bundle(s.MODEL_CACHE_DIR)   # downloads, warms up, persists weights.lock.json
       v = verify_model_bundle(s.MODEL_CACHE_DIR)
       print(v)
       "
       ```
       Expected: `total_weights_size_mb` around 1400; all 6 `_cached` flags `True`; `cold_start_ms` dict with three positive integer ms values. Commit the auto-populated `weights.lock.json`.

    4. Run the live Phase 0 sweep:
       ```
       uv run python -m home_cctv --phase0
       ```
       This captures Cam 1 → Cam 2 → Cam 3 → Cam 4, 30 minutes each, sequentially. Total ~2 hours. Progress is logged to `$LOG_DIR/home_cctv.log` with per-camera prefixes. The harness writes `.planning/phases/00-environment-sanity/PHASE0-REPORT.json` on completion (or on Ctrl+C with `phase0_verdict=aborted`).

    5. Review the report:
       ```
       cat .planning/phases/00-environment-sanity/PHASE0-REPORT.json | uv run python -m json.tool
       ```
       Expected per-camera values:
       - Cam 1 exterior_red:  measured_fps ≈ 20–30 (target 25 ±20%), frames_corrupted = 0, hang_events = 0
       - Cam 2 interior_orange: measured_fps ≈ 9.6–14.4, frames_corrupted = 0, hang_events = 0
       - Cam 3 exterior_blue:  measured_fps ≈ 9.6–14.4, frames_decoded/(frames_decoded+frames_corrupted) ≥ 0.95, hang_events = 0
       - Cam 4 interior_green: measured_fps ≈ 20–30, frames_decoded/(frames_decoded+frames_corrupted) ≥ 0.95, hang_events = 0

       Expected top-level: `phase0_verdict: "pass"`, `blockers_resolved.Q1_host_fps_baseline: true`, `Q2_wsl2_networking: true`, `Q3_opencv_ffmpeg_backend: true`, `Q4_ffplay_flags_translated: true`, `Q6_dvr_connection_cap: true`, `Q5_reconnect_watchdog_sketch: "designed but not yet implemented"`.

       Expected `model_bundle.cold_start_ms`: three positive integers (per CONTEXT.md §"Phase 0 Measurement Harness Format"):
       - `yolo_openvino`: ~300–1500 ms (OpenVINO IR inference on a 640×640 zero tensor, CPU)
       - `deepface`: ~3000–12000 ms (ArcFace cold load + first represent call)
       - `easyocr`: ~2000–8000 ms (EasyOCR reader cold load + first readtext call)
       If any value is 0, `warmup_model_bundle` did not run — investigate before committing.

    6. Human verification steps:
       - [ ] Confirm all 4 cameras produced rows in the JSON
       - [ ] Confirm `phase0_verdict == "pass"`
       - [ ] Spot-check 2 random JPEGs from `$EVENT_IMAGE_DIR/phase0_probe/cam1_exterior_red/` — they should show the actual camera feed, not green/black
       - [ ] Spot-check 2 JPEGs from `$EVENT_IMAGE_DIR/phase0_probe/cam4_interior_green/` — same
       - [ ] Grep the log file for the real password: `grep -c "$DVR_PASS" $LOG_DIR/home_cctv.log` must return `0`
       - [ ] Ctrl+C test: start `uv run python -m home_cctv --mp4 tests/fixtures/sample_720p25.mp4`, press Ctrl+C, confirm process exits in under 2 seconds with no orphan ffmpeg (`pgrep -a ffmpeg` returns nothing after exit)
       - [ ] B3 canonical-path protection: run `uv run python -m home_cctv --phase0 --mp4 tests/fixtures/sample_720p25.mp4` with the committed `PHASE0-REPORT.json` already in place, then confirm its mtime is UNCHANGED. The dry-run should have written `.planning/phases/00-environment-sanity/PHASE0-REPORT.dryrun.json` instead.
       - [ ] `model_bundle.cold_start_ms` in the report has positive integers for all three models (B1)

    7. If `phase0_verdict == "fail"`:
       - If one camera fps is off: log the measured value in PHASE0-REPORT.json's `notes` and decide whether to re-run (transient DVR load) or tune `stimeout` / `analyzeduration`
       - If Cam 3/4 ratio < 0.95: the NAL workaround is insufficient — investigate (likely need to also lower `probesize` or increase `reconnect_delay_max`). Re-test.
       - If `dvr.reachable == false`: networking is wrong. Fix `.wslconfig` and retry.
       - If HEVC missing: `sudo apt install ffmpeg libavcodec-extra` and retry.

    8. Commit:
       - `PHASE0-REPORT.json` (real values, always committed per CONTEXT.md §"Report path")
       - `weights.lock.json` (populated with real byte sizes)
       - Do NOT commit `$EVENT_IMAGE_DIR/phase0_probe/` (it's under `data/` which is gitignored)

    9. Update CONTEXT.md "notes" field in PHASE0-REPORT.json with a one-line summary: e.g. `"Phase 0 sweep: 2026-04-14, all 4 cams pass, Cam 3 corruption ratio 3.6%, Cam 4 corruption ratio 1.9%, mirrored WSL2 networking confirmed, DVR concurrent cap measured at 5."`
  </how-to-verify>
  <resume-signal>
    Type "approved" once:
    - [ ] PHASE0-REPORT.json exists and is committed (canonical-path write came from the live sweep, not from any dry-run or test)
    - [ ] phase0_verdict == "pass"
    - [ ] All 4 camera rows meet their exit criteria (ok, per_camera_exit_ok === True)
    - [ ] Model bundle cached; weights.lock.json committed with real sizes AND `cold_start_ms` populated (auto-persisted by download_model_bundle — B1 + W6)
    - [ ] `model_bundle.cold_start_ms.{yolo_openvino, deepface, easyocr}` are all positive integers
    - [ ] No DVR password appears in log files
    - [ ] Ctrl+C test: clean exit in < 2 s, no orphan ffmpeg
    - [ ] `--phase0 --mp4` dry-run did NOT touch the committed PHASE0-REPORT.json (B3)

    Or describe any blocker (e.g., "Cam 3 ratio 0.92 — needs retry with longer probesize").
  </resume-signal>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| DVR network → process | Untrusted DVR firmware produces H.265 streams with known NAL anomalies |
| Model weights URL → filesystem | First-run downloads from Ultralytics/DeepFace/EasyOCR mirrors |
| PHASE0-REPORT.json → git commit | Report is committed; must not contain credentials |
| WSL2 `.wslconfig` → kernel networking | Wrong mode makes DVR unreachable |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-00-15 | Information Disclosure | PHASE0-REPORT.json committed to git | mitigate | Report schema has no DVR_PASS field; `env_vars_loaded` is a list of KEY names only; DVR `ip` recorded but that's a LAN address. Human verification step greps PHASE0-REPORT.json and log files for the real password before commit. |
| T-00-16 | Tampering | Model weight corruption on first download | mitigate | `verify_model_bundle` re-verifies sizes at every startup against `weights.lock.json`; corrupt cache triggers redownload (Task 2 `download_model_bundle` on failure). |
| T-00-17 | Spoofing | Malicious replacement of weight URLs by compromised mirror | accept | Single-user home project; threat model does not cover supply-chain. Documented. |
| T-00-18 | Denial of Service | Cam 3/4 NAL anomaly causes FFmpeg to crash mid-sweep | mitigate | `fflags=+discardcorrupt|err_detect=ignore_err` in canonical OPENCV_FFMPEG_CAPTURE_OPTIONS (Plan 01); 95% clean-ratio threshold in `per_camera_exit_ok` tolerates the expected decode errors |
| T-00-19 | Denial of Service | DVR cap exceeded during concurrent-session probe kicks BitVision app off | accept | `step_concurrent_sessions` runs before the sweep and documents the cap; user is advised in README to close BitVision phone app before running |
| T-00-20 | Information Disclosure | `cameras.yaml` committed to git with DVR vendor info | accept | Only vendor string "Cantonk/HeroSpeed OEM" is committed; no IPs, no creds (those stay in `.env`) |
| T-00-21 | Tampering | PHASE0-REPORT.json edited by hand before commit | accept | Single-operator project; values are advisory for downstream phases, not a security control |
</threat_model>

<verification>
- `cameras.yaml` exists, loads, validates, produces 4 cameras with correct paths + NAL flags
- `assert_ffmpeg_backend` and `assert_hevc_decoder` pass on the user's host
- `run_phase0(mp4_override=...)` offline dry-run produces a valid PHASE0-REPORT.json
- Live sweep (Task 3 checkpoint): 4 cameras × 30 min, all per-camera exit criteria met, verdict "pass"
- PHASE0-REPORT.json committed with real measurements
- weights.lock.json committed with real byte sizes
- Model bundle present under `$HOME/.cache/home_cctv/models/`
- No DVR password in any log file or committed artifact
- `pgrep -a ffmpeg` returns empty after Ctrl+C
</verification>

<success_criteria>
Phase 0 is DONE when Plan 03 ships:
1. `uv run python -m home_cctv --phase0` runs end-to-end on the user's Windows 11 + WSL2 Ubuntu 24.04 host and writes `PHASE0-REPORT.json` with `phase0_verdict: "pass"`
2. All 4 cameras captured for 30 minutes each, per-camera exit criteria met
3. Missing `.env` / missing FFmpeg / missing HEVC / <1 GB free / DrvFs path all produce clear startup errors (Plan 01 + Plan 03 host probe + Plan 01 disk guard)
4. Ctrl+C exits cleanly in under 2 s with all resources released (Plan 02 supervisor)
5. `--mp4 path.mp4` runs the same harness loop against a file (Plan 02 FrameSource + Plan 03 dry-run mode)
6. PHASE0-REPORT.json answers SUMMARY.md §6 blockers Q1, Q2, Q3, Q4, Q6 with measured values; Q5 has a committed design sketch
7. Full model bundle (YOLO26n + YOLOv8n + OpenVINO IR + DeepFace ArcFace + RetinaFace + EasyOCR English) cached and verified at every startup
8. `cameras.yaml` committed with exact CONTEXT.md structure
9. `weights.lock.json` committed with real byte sizes
</success_criteria>

<output>
After completion, create `.planning/phases/00-environment-sanity/00-03-SUMMARY.md` documenting:
- Real measured values per camera (fps, frames, corruption ratios)
- WSL2 networking mode detected
- DVR concurrent session cap measured
- Model bundle total size on disk
- Any tuning done to the canonical env options string to hit the per-camera exit criteria
- Any deviations from CONTEXT.md and their rationale
- Phase 1 handoff notes: what FrameSource / Settings / cameras.yaml interfaces Phase 1 should consume, and which SUMMARY.md §6 blockers remain (Q5, Q7–Q12)
</output>
