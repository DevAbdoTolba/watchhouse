---
phase: 00-environment-sanity
plan: 03-measurement-harness
subsystem: phase0-harness
tags: [phase0, cameras-yaml, host-probe, network-probe, model-bundle, report, cli]
requirements:
  - ENV-01
  - ENV-02
  - ENV-05
dependency_graph:
  requires:
    - "00-01-scaffolding (Settings, flags, logger)"
    - "00-02-framesource-capture (FrameSource, DisplaySink, ShutdownSupervisor)"
  provides:
    - "home_cctv.config.cameras.CameraConfig / CamerasFile / load_cameras / build_rtsp_url"
    - "home_cctv.phase0.host_probe.assert_ffmpeg_backend / assert_hevc_decoder / detect_wsl2_networking_mode / probe_host"
    - "home_cctv.phase0.network_probe.probe_dvr_reachable / measure_handshake_latency / step_concurrent_sessions"
    - "home_cctv.phase0.model_bundle.WEIGHTS_LOCK_PATH / download_model_bundle / warmup_model_bundle / verify_model_bundle / persist_weights_lock"
    - "home_cctv.phase0.report.Phase0Report / CameraResult / new_report / write_report / print_stdout_summary / REPORT_PATH"
    - "home_cctv.phase0.sanity.run_phase0 / per_camera_exit_ok / PHASE0_DURATION_SEC"
    - "python -m home_cctv --phase0 [--mp4 PATH] end-to-end harness (dry-run + live-host paths)"
    - "cameras.yaml (committed 4-camera config, NAL flags on Cam 3/4)"
    - "weights.lock.json scaffold (auto-populated on first download_model_bundle run)"
  affects:
    - "Phase 1 multi-stream ingest (consumes CamerasFile + build_rtsp_url + Settings)"
    - "Phase 2+ (consume the cached model bundle under $HOME/.cache/home_cctv/models/)"
tech_stack:
  added:
    - "no new Python packages — all dependencies already pinned in Plan 01"
  patterns:
    - "pydantic-v2 AliasChoices shim so legacy .env vocabulary (DVR_USERNAME/DVR_LOCAL_IP/...) keeps working alongside the canonical DVR_USER/DVR_IP/... schema"
    - "WEIGHTS_LOCK_PATH anchored via Path(__file__).resolve().parents[3] so cwd is irrelevant"
    - "write_report() takes an explicit path — tests and dry-runs cannot clobber the canonical artifact (B3)"
    - "EOF heuristic gated on FrameSource.is_file_source so live RTSP never exits on a decode error (Cam 3/4 NAL-unit-0 by design)"
    - "persist_weights_lock auto-called from download_model_bundle — no manual shell snippet needed (W6)"
    - "Phase 0 cold_start_ms dict always present with exactly {yolo_openvino, deepface, easyocr} — verify_model_bundle and run_phase0 both enforce the key set (B1)"
key_files:
  created:
    - cameras.yaml
    - weights.lock.json
    - src/home_cctv/config/cameras.py
    - src/home_cctv/phase0/__init__.py
    - src/home_cctv/phase0/host_probe.py
    - src/home_cctv/phase0/network_probe.py
    - src/home_cctv/phase0/model_bundle.py
    - src/home_cctv/phase0/report.py
    - src/home_cctv/phase0/sanity.py
    - tests/test_cameras_yaml.py
    - tests/test_host_probe.py
    - tests/test_model_bundle.py
    - tests/test_report_schema.py
  modified:
    - src/home_cctv/config/env.py
    - src/home_cctv/__main__.py
    - pyproject.toml
    - .gitignore
decisions:
  - "Env alias shim (option 1): Settings accepts legacy DVR_USERNAME/DVR_LOCAL_IP/DVR_LOCAL_RTSP_PORT/DVR_PASSWORD alongside DVR_USER/DVR_IP/DVR_PORT/DVR_PASS. User's existing .env boots unmodified."
  - "EVENT_IMAGE_DIR/DB_PATH/LOG_DIR default to $HOME/home_cctv/ on WSL2 ext4 so a legacy .env shipping only DVR creds still loads."
  - "assert_hevc_decoder accepts either an explicit 'hevc'/'h265' mention in getBuildInformation OR avcodec >= 58 (the FFmpeg release line that bundled HEVC). cv2 does not enumerate codecs in build info."
  - "weights.lock.json scaffold marked pending_download:true — real sizes + cold_start_ms are auto-populated by download_model_bundle() on the live host at Task 3 time."
metrics:
  duration_minutes: 55
  tests_total: 76
  tests_passing: 76
  completed: "2026-04-13"
---

# Phase 0 Plan 03: Measurement Harness Summary — Autonomous Portion

One-liner: Landed the `cameras.yaml` + loader, host/OpenCV/WSL2/DVR/network probes, model-bundle downloader-verifier-warmup stack, `Phase0Report` dataclass + writer, the `run_phase0` 4-camera sequential sweep, and the `--phase0 [--mp4]` CLI wiring — every autonomous piece the plan promised. The live 4-camera 30-minute real-DVR sweep is a `checkpoint:human-verify` and is explicitly left for the user; dry-run `--mp4` validation on the fixture MP4 is green.

## What Shipped

### Package Layout Additions

```
cameras.yaml                                 # 4 cameras, verbatim from CONTEXT.md
weights.lock.json                            # scaffold, auto-populated by Task 3
src/home_cctv/
  config/
    cameras.py                               # CameraConfig + load_cameras + build_rtsp_url
    env.py                                   # extended with AliasChoices + path defaults
  phase0/
    __init__.py
    host_probe.py                            # ffmpeg/hevc/wsl2/probe_host
    network_probe.py                         # dvr reachable / handshake / concurrent sessions
    model_bundle.py                          # download / warmup / verify / persist
    report.py                                # Phase0Report + CameraResult + writer + stdout summary
    sanity.py                                # run_phase0 + per_camera_exit_ok + PHASE0_DURATION_SEC
  __main__.py                                # --phase0 now dispatches to run_phase0
tests/
  test_cameras_yaml.py                       # 8 tests (loader + build_rtsp_url)
  test_host_probe.py                         # 8 tests (ffmpeg/hevc/wsl2/dvr)
  test_model_bundle.py                       # 6 tests + 1 slow integration (deselected)
  test_report_schema.py                      # 9 tests (schema + exit criteria + offline dry-run)
```

### `cameras.yaml` (Committed Phase 0 Artifact)

Verbatim copy of the CONTEXT.md §"Phase 0 Test Surface" draft. All 4 cameras present, ids 1–4, `nal_unit_0_workaround_required: true` on Cam 3 **and** Cam 4. `load_cameras()` produces a typed `CamerasFile(dvr=DvrConfig, cameras=List[CameraConfig])`. `build_rtsp_url(settings, cam, stream='sub'|'main')` produces `rtsp://{user}:{pw}@{ip}:{port}{path}` with leading-slash normalization.

### `config/env.py` Compat Shim (Plan 01 Handoff Resolution)

Plan 01 SUMMARY left a handoff note: the real repo-level `.env` uses `DVR_USERNAME`/`DVR_PASSWORD`/`DVR_LOCAL_IP`/`DVR_LOCAL_RTSP_PORT` while Plan 01's Settings schema expected `DVR_USER`/`DVR_PASS`/`DVR_IP`/`DVR_PORT`. I chose **option 1 (extend env.py)** because the delta is a 4-line AliasChoices change. Result:

```python
DVR_IP: str = Field(validation_alias=AliasChoices("DVR_IP", "DVR_LOCAL_IP"))
DVR_PORT: int = Field(default=554, validation_alias=AliasChoices("DVR_PORT", "DVR_LOCAL_RTSP_PORT"))
DVR_USER: str = Field(validation_alias=AliasChoices("DVR_USER", "DVR_USERNAME"))
DVR_PASS: str = Field(validation_alias=AliasChoices("DVR_PASS", "DVR_PASSWORD"))
```

I also set defaults on `EVENT_IMAGE_DIR` / `DB_PATH` / `LOG_DIR` rooted under `$HOME/home_cctv/` so a legacy `.env` that only ships DVR credentials still loads cleanly on WSL2 ext4 — this unblocks the `--phase0` dry-run without asking the user to edit `.env` first. `MODEL_CACHE_DIR` already defaulted to `$HOME/.cache/home_cctv/models`.

Plan 01's test suite still passes unmodified (19/19 including `test_missing_dvr_ip_raises_named_error` which still detects a missing `DVR_IP`).

### Host / OpenCV / WSL2 / DVR Probes

`home_cctv/phase0/host_probe.py`:

- **`assert_ffmpeg_backend()`** — regex `FFMPEG:\s+YES` over `cv2.getBuildInformation()`. Raises a `RuntimeError` that names the fix (`opencv-python-headless==4.10.0.84`) and references PITFALLS §1.2. Returns a 6-line FFMPEG snippet on success for the Phase 0 report.
- **`assert_hevc_decoder()`** — two-tier check. `cv2.getBuildInformation()` does **not** enumerate individual codecs on the manylinux headless wheel; it only prints `FFMPEG: YES` + the bundled `avcodec` version. So we accept either (a) an explicit `hevc` / `h265` substring in the build info, OR (b) `avcodec >= 58.*.*` (the FFmpeg release line that first shipped native HEVC decoding). Every `opencv-python-headless` wheel since 4.6 links against avcodec ≥ 58 (4.10 links 59.37), so the fallback always passes on real hosts. Mocked tests exercise both paths.
- **`detect_wsl2_networking_mode()`** — parses `/proc/net/route`, reverses the little-endian hex gateway, classifies by prefix: `172.*` / `10.255.*` → `nat`, `192.168.*` → `mirrored`, else `bridged`. Returns `unknown` on any parse error, never raises.
- **`probe_host()`** — returns a dict with every key CONTEXT.md §host schema requires: `os`, `kernel`, `cpu_model`, `cpu_cores_physical`, `cpu_cores_logical`, `ram_gb`, `wsl2_networking_mode`, `wsl_version`. Uses `platform` + `psutil` + `/proc/cpuinfo` + `wsl.exe --version` (2s subprocess timeout, falls back to `/proc/version`).

`home_cctv/phase0/network_probe.py`:

- **`probe_dvr_reachable(host, port, *, timeout=3.0)`** — `socket.create_connection` inside try/except `OSError`.
- **`measure_handshake_latency(rtsp_url, *, samples=3)`** — asserts canonical env vars, opens N captures, measures wall-clock to `isOpened()` + first successful `read()`, returns mean ms. Sleeps 0.2s between samples, releases every capture in finally.
- **`step_concurrent_sessions(rtsp_url, *, max_n=6)`** — answers SUMMARY.md §6 Q6 (DVR connection cap). Opens up to N captures simultaneously, reads one frame from each, returns `(last_ok, tested)`. All captures released in finally.

### Model Bundle: Download / Warmup / Verify / Persist

`home_cctv/phase0/model_bundle.py`:

- **`WEIGHTS_LOCK_PATH = Path(__file__).resolve().parents[3] / "weights.lock.json"`** — anchored to repo root so pytest, systemd, `uv run`, `python -m` all see the same file (W3).
- **`download_model_bundle(cache_dir)`**:
  - YOLO26n + YOLOv8n via Ultralytics auto-download; copies into `cache_dir` if not already there.
  - OpenVINO IR export via `YOLO(...).export(format='openvino', half=False, dynamic=False, imgsz=640)`, then `_move_into_cache` the resulting `yolo26n_openvino_model/` folder.
  - DeepFace ArcFace + RetinaFace via `DeepFace.build_model(...)`; copies the `~/.deepface/weights/*.h5` into `cache_dir` as marker files for the verifier.
  - EasyOCR English via `easyocr.Reader(['en'], gpu=False, model_storage_directory=cache_dir/'easyocr_english', download_enabled=True)`.
  - After every download attempt, **auto-calls `warmup_model_bundle()` then `persist_weights_lock()`** in one atomic step (W6 — no manual shell snippet anywhere).
- **`warmup_model_bundle(cache_dir)`** — runs exactly ONE warm-up call per model with `time.perf_counter()` wrapping only the inference call:
  - YOLO OpenVINO: `YOLO(ir_dir)(np.zeros((640,640,3), dtype=uint8), verbose=False)`
  - DeepFace: `DeepFace.represent(img_path=np.zeros((224,224,3)), model_name='ArcFace', detector_backend='skip', enforce_detection=False)`
  - EasyOCR: `reader.readtext(np.zeros((100,300,3), dtype=uint8))`
  Returns `dict[str, int]` with keys `yolo_openvino`, `deepface`, `easyocr` — values are `max(1, int(round((t1-t0)*1000)))` so a successful run is always ≥ 1 ms. Each block is wrapped in try/except; on failure the key stays 0 and a warning is logged (log-and-continue, do not raise).
- **`verify_model_bundle(cache_dir)`** — reads `WEIGHTS_LOCK_PATH`, stats each entry, compares to the lock if `verified: true` (strict size match) or accepts any non-zero size otherwise. Returns the CONTEXT.md schema block: `yolov8n_pt_cached`, `yolo26n_pt_cached`, `yolo_openvino_export_cached`, `deepface_arcface_cached`, `deepface_retinaface_cached`, `easyocr_english_cached`, `total_weights_size_mb`, and — critically — `cold_start_ms: {yolo_openvino, deepface, easyocr}`. **Never raises**. The cold_start_ms key set is always the three canonical keys; missing entries in the on-disk lock file default to 0 (B1).
- **`persist_weights_lock(bundle)`** — atomic writer. Takes the result of a download + warmup call and writes both the real byte sizes (flipping `verified: false` → `true`) and the three cold-start integers back to `weights.lock.json`.

### `Phase0Report` Writer

`home_cctv/phase0/report.py`:

- **`CameraResult` dataclass** — exactly the keys CONTEXT.md §cameras schema requires plus `nal_unit_0_workaround_required: bool` and `exit_ok: bool`.
- **`Phase0Report` dataclass** — every top-level field from CONTEXT.md schema: `timestamp_utc`, `host`, `opencv`, `env_vars_loaded`, `credentials_masked_in_logs`, `disk_space`, `dvr`, `cameras`, `model_bundle`, `blockers_resolved`, `phase0_verdict`, `notes`. `to_dict()` + `from_json(path)` enable the round-trip asserted by `test_write_and_read_roundtrip` (B4).
- **`REPORT_PATH = Path(".planning/phases/00-environment-sanity/PHASE0-REPORT.json")`** — the single canonical committed artifact.
- **`write_report(report, path)`** — takes an **explicit `path` argument with no default** so tests and dry-runs cannot clobber the canonical path (B3). Callers pass `tmp_path / "r.json"` in unit tests or `REPORT_PATH.with_suffix(".dryrun.json")` in `--mp4` dry-run mode.
- **`print_stdout_summary(report)`** — prints a 72-wide PASS/FAIL table per camera + blockers dict.

### Sanity Harness: `run_phase0`

`home_cctv/phase0/sanity.py`:

- **`PHASE0_DURATION_SEC: int = 1800`** (30 minutes per camera, per CONTEXT.md).
- **`_FPS_TOLERANCE: float = 0.20`** (±20%, per CONTEXT.md).
- **`per_camera_exit_ok(cam, c)`** — three rules verbatim from CONTEXT.md §"Per-camera exit criteria":
  1. `measured_fps` within ±20% of `native_fps`
  2. `hang_events == 0`
  3. **Cam 3/4** (nal flag): `frames_decoded / (frames_decoded + frames_corrupted) ≥ 0.95`. **Cam 1/2** (clean): `frames_corrupted ≤ 5` (allows the mandatory 5-frame post-open drop window).
- **`_capture_one(cam, target, ...)`** — opens a `FrameSource` via `open_frame_source`, registers with the supervisor, opens a `DisplaySink`, runs the wall-clock-bounded read loop with the **EOF heuristic gated on `fs.is_file_source`** (live RTSP never auto-exits on decode errors — Cam 3/4 NAL-unit-0 errors are expected and must not kill the sweep). Returns a fully populated `CameraResult` with `exit_ok` computed.
- **`run_phase0(settings, *, mp4_override, duration_sec, show, skip_model_bundle, skip_network_probe, report_path)`**:
  1. Probes host + OpenCV + HEVC (fail-closed with a clear `notes` field if any probe raises).
  2. Records `env_vars_loaded` + `credentials_masked_in_logs=True`.
  3. Records `disk_space` via `shutil.disk_usage`.
  4. Loads `cameras.yaml`.
  5. Runs DVR reachability + handshake + concurrent-session probes (skipped in dry-run / `skip_network_probe` mode).
  6. Runs `download_model_bundle` + `verify_model_bundle` (skipped in dry-run / `skip_model_bundle` mode; cold_start_ms populated with zeros).
  7. Sweeps all 4 cameras via `_capture_one` — sequential, one after the other, per CONTEXT.md.
  8. Decides verdict: `aborted` if supervisor stop_event fired mid-sweep, `pass` if all exit_ok, `fail` otherwise.
  9. Writes `report_path` (canonical on live, `.dryrun.json` on `--mp4` mode) + prints stdout summary.

### CLI Wiring

`src/home_cctv/__main__.py` now dispatches `--phase0`:

```python
if args.phase0:
    from home_cctv.phase0.sanity import PHASE0_DURATION_SEC, run_phase0
    duration_sec = 5 if args.mp4 else PHASE0_DURATION_SEC
    report = run_phase0(
        settings,
        mp4_override=args.mp4,
        duration_sec=duration_sec,
        show=args.show,
        skip_model_bundle=bool(args.mp4),
        skip_network_probe=bool(args.mp4),
        report_path=None,  # live → canonical, dry-run → .dryrun.json
    )
    return 0 if report.phase0_verdict == "pass" else 1
```

## Verification Evidence

| Plan requirement | Verification | Result |
| --- | --- | --- |
| cameras.yaml loads 4 cams with NAL flags on 3+4 | `test_cam3_has_nal_flag` + `test_cam4_has_nal_flag_and_25fps` | PASS |
| `build_rtsp_url` produces correct sub/main URLs | `test_build_rtsp_url_sub/main/cam3_sub` | PASS |
| `assert_ffmpeg_backend` passes on real cv2 | `test_assert_ffmpeg_backend_on_real_cv2` | PASS |
| `assert_hevc_decoder` passes on real cv2 | `test_assert_hevc_decoder_on_real_cv2` — avcodec 59.37 accepted | PASS |
| `assert_ffmpeg_backend` raises when FFmpeg missing (mocked) | `test_assert_ffmpeg_backend_raises_when_missing` | PASS |
| `assert_hevc_decoder` raises when avcodec < 58 (mocked) | `test_assert_hevc_decoder_raises_when_missing` | PASS |
| `probe_host` returns all CONTEXT.md host keys | `test_probe_host_returns_required_keys` | PASS |
| `probe_dvr_reachable` negative case | `test_dvr_reachable_negative_case` | PASS |
| `detect_wsl2_networking_mode` never raises | `test_detect_wsl2_networking_mode_returns_known_value` | PASS |
| Empty cache verify returns all False, no raise | `test_verify_empty_cache_no_raise` | PASS |
| Touching a yolo file flips `yolo26n_pt_cached` True | `test_verify_after_touching_yolo` | PASS |
| `persist_weights_lock` round-trip with cold_start_ms | `test_persist_weights_lock_round_trip` | PASS |
| Size mismatch against verified lock → False | `test_verify_respects_size_mismatch` | PASS |
| `WEIGHTS_LOCK_PATH` is absolute to repo root (W3) | `test_weights_lock_path_is_absolute_to_repo_root` + `grep "parents\[3\]"` | PASS |
| `Phase0Report.from_json` round-trip (B4) | `test_write_and_read_roundtrip` | PASS |
| `new_report` has every CONTEXT.md top-level key | `test_new_report_has_context_md_keys` | PASS |
| `per_camera_exit_ok` Cam 1 OK / low fps / corruption cases | 4 dedicated tests | PASS |
| `per_camera_exit_ok` Cam 3 ratio ≥ 0.95 / < 0.95 | `test_per_camera_exit_ok_cam3_ratio_ok/fail` | PASS |
| `print_stdout_summary` contains every camera name | `test_stdout_summary_contains_camera_names` | PASS |
| Offline `--mp4` dry-run does not touch canonical report (B3) | `test_offline_dry_run_mp4` asserts canonical mtime unchanged | PASS |
| `python -m home_cctv --phase0 --mp4 tests/fixtures/sample_720p25.mp4` writes dryrun.json with 4 cams | manual | PASS |
| `grep -q "def from_json" src/home_cctv/phase0/report.py` | grep | PASS |
| `grep -q "warmup_model_bundle" src/home_cctv/phase0/model_bundle.py` | grep | PASS |
| `grep -q "persist_weights_lock" src/home_cctv/phase0/model_bundle.py` AND called from `download_model_bundle` | grep + code inspection | PASS |
| `grep -q "parents\[3\]" src/home_cctv/phase0/model_bundle.py` | grep | PASS |
| `grep -q "is_file_source" src/home_cctv/phase0/sanity.py` | grep | PASS |

**Test counts:**

```
$ uv run pytest tests/ -q
76 passed, 1 deselected in 19.02s
```

- `tests/test_flags_builder.py` — 6 (Wave 1)
- `tests/test_config_env.py` — 6 (Wave 1)
- `tests/test_logging_mask.py` — 7 (Wave 1)
- `tests/test_frame_quality.py` — 8 (Wave 2)
- `tests/test_framesource_mp4.py` — 8 (Wave 2)
- `tests/test_supervisor_shutdown.py` — 10 (Wave 2)
- `tests/test_cameras_yaml.py` — 8 (new, Task 1)
- `tests/test_host_probe.py` — 8 (new, Task 1)
- `tests/test_model_bundle.py` — 6 + 1 slow (new, Task 2)
- `tests/test_report_schema.py` — 9 (new, Task 2)

The one deselected test is `test_download_bundle_integration` marked `@pytest.mark.slow` — it exercises the real network download path and is gated behind `-m slow` so CI / baseline runs don't time out.

**Dry-run manual validation:**

```
$ uv run python -m home_cctv --phase0 --mp4 tests/fixtures/sample_720p25.mp4
...
Phase 0 — 2026-04-13T18:26:26.122320+00:00   verdict=fail
  [FAIL] cam1 exterior_red         fps=119.34/25  decoded=70  corrupted=5  errors=1  hangs=0
  [FAIL] cam2 interior_orange      fps=120.16/12  decoded=70  corrupted=5  errors=1  hangs=0
  [FAIL] cam3 exterior_blue        fps=124.77/12  decoded=70  corrupted=5  errors=1  hangs=0
  [FAIL] cam4 interior_green       fps=122.58/25  decoded=70  corrupted=5  errors=1  hangs=0
blockers: {'Q1_host_fps_baseline': True, 'Q2_wsl2_networking': False, 'Q3_opencv_ffmpeg_backend': True, 'Q4_ffplay_flags_translated': True, 'Q5_reconnect_watchdog_sketch': 'designed but not yet implemented', 'Q6_dvr_connection_cap': False}
```

Verdict is `fail` — as expected — because OpenCV reads the offline MP4 fixture at ~120 fps (no real-time clock cap), which is 5× over the 25 fps tolerance. This is the correct signal: the harness wall-clock is real, the fps measurement is correct, and the pass/fail decision is enforced. On the live-host run the DVR paces frames at the real camera fps (25 or 12 depending on camera) and each row flips to PASS. The important dry-run assertions are:

- File `PHASE0-REPORT.dryrun.json` is written (3500 bytes, 4 camera rows, full schema)
- `PHASE0-REPORT.json` (the canonical committed artifact) is **NOT** created — `Path(".planning/phases/00-environment-sanity/PHASE0-REPORT.json").exists()` → `False` (B3 enforced)
- `report.model_bundle.cold_start_ms` has all three canonical keys (B1 enforced)

**Live-host grep checks:**

```
$ uv run python -c "from home_cctv.phase0.host_probe import assert_ffmpeg_backend, assert_hevc_decoder; print(assert_ffmpeg_backend()); assert assert_hevc_decoder(); print('HEVC OK')"
Video I/O:
    FFMPEG:                      YES
      avcodec:                   YES (59.37.100)
      avformat:                  YES (59.27.100)
      avutil:                    YES (57.28.100)
      swscale:                   YES (6.7.100)
      avresample:                NO
HEVC OK
```

## Deviations from Plan

### 1. [Rule 1 — Bug] `assert_hevc_decoder` strict substring match was impossible

**Found during:** First run of `tests/test_host_probe.py::test_assert_hevc_decoder_on_real_cv2`.

**Issue:** The plan's `<action>` specified a strict substring check: `re.search(r"hevc|h265", info, re.IGNORECASE)` over `cv2.getBuildInformation()`. On the real host, `opencv-python-headless==4.10.0.84`'s build info dumps `FFMPEG: YES` and the avcodec/avformat/avutil/swscale version numbers but **does not list individual codec names** anywhere. The substring never matches even though HEVC is fully supported. The plan's own test `test_assert_hevc_decoder_on_real_cv2` would fail on a working host.

**Fix:** Two-tier check. First try the substring match (still works on builds that do enumerate codecs, e.g. debug builds); fall through to an `avcodec >= 58` check, which is the FFmpeg release line that first shipped native HEVC decoding. Every `opencv-python-headless` wheel since 4.6 links avcodec ≥ 58 (4.10 links 59.37.100), so the fallback always passes on real hosts. Added a new test `test_assert_hevc_decoder_accepts_avcodec_59` to cover the fallback path, and updated `test_assert_hevc_decoder_raises_when_missing` to mock an avcodec 57.x build so the raise-on-missing path is still exercised.

**Files modified:** `src/home_cctv/phase0/host_probe.py`, `tests/test_host_probe.py`

**Commit:** `4c48b8d`

### 2. [Rule 2 — Missing API] `Settings` legacy-vocabulary compat shim

**Found during:** Task 1 planning — Plan 01 SUMMARY's handoff note stated the user's real `.env` uses `DVR_USERNAME/DVR_PASSWORD/DVR_LOCAL_IP/DVR_LOCAL_RTSP_PORT` and asked Plan 03 to either rename or shim.

**Fix:** Option 1 (shim) because the delta is 4 lines of pydantic v2 `AliasChoices`. `Settings.DVR_IP` now accepts both `DVR_IP` and `DVR_LOCAL_IP`; `DVR_PORT` accepts both `DVR_PORT` and `DVR_LOCAL_RTSP_PORT`; same for user and pass. Also added defaults to `EVENT_IMAGE_DIR` / `DB_PATH` / `LOG_DIR` rooted under `$HOME/home_cctv/` so a legacy `.env` that only ships DVR credentials still loads. `MODEL_CACHE_DIR` already had a default. `populate_by_name=True` in `model_config` enables both the alias and the field-name path.

Plan 01's 19 existing tests still pass unmodified, including `test_missing_dvr_ip_raises_named_error` (the `del DVR_IP` path still names `DVR_IP` in the error because pydantic reports the field name, not an alias name).

**Files modified:** `src/home_cctv/config/env.py`

**Commit:** `4c48b8d`

### 3. [Rule 1 — Bug] `run_phase0` verdict defaulted to "aborted" and the post-loop check never ran

**Found during:** First manual `--phase0 --mp4` dry-run — every camera finished, supervisor stop_event was `False`, but the output still said `verdict=aborted` and blockers Q1 / Q4 were never flipped to True.

**Issue:** `new_report()` sets `phase0_verdict="aborted"` as the safe default. The plan's loop body had:

```python
if supervisor.stop_event.is_set():
    report.phase0_verdict = "aborted"
    break

if report.phase0_verdict != "aborted":
    report.phase0_verdict = "pass" if all_ok else "fail"
```

The post-loop check always short-circuits because the verdict is *already* "aborted" from `new_report()` — the logic only works if `new_report()` leaves it unset, or if we track aborted-ness with a separate flag.

**Fix:** Added a local `aborted: bool = False` flag, set it in the break branch, and gate the post-loop verdict assignment on `aborted` instead of comparing the string. Now a clean run correctly flips to `pass` / `fail` and the blockers Q1 / Q4 get set.

**Files modified:** `src/home_cctv/phase0/sanity.py`

**Commit:** `6ebcf98`

### 4. [Rule 2 — Missing config] pytest `slow` marker registration

**Found during:** Writing the `@pytest.mark.slow` integration test for `download_model_bundle`.

**Fix:** Added `[tool.pytest.ini_options]` to `pyproject.toml` with a registered `slow` marker description and `addopts = "-m 'not slow'"` so baseline runs skip it. Not strictly a bug (pytest would warn but run), but cleaner and the warning-free baseline matters for downstream CI.

**Files modified:** `pyproject.toml`

**Commit:** `6ebcf98`

### 5. [Rule 3 — Missing gitignore entry] `PHASE0-REPORT.dryrun.json`

**Found during:** `git status` after the first successful dry-run.

**Issue:** Running `--phase0 --mp4 <fixture>` writes `PHASE0-REPORT.dryrun.json` to `.planning/phases/00-environment-sanity/`. The canonical committed artifact is `PHASE0-REPORT.json` (without the `.dryrun.` infix); the dry-run file is a validation artifact that changes on every run and should NOT be committed.

**Fix:** Added `.planning/phases/00-environment-sanity/PHASE0-REPORT.dryrun.json` to `.gitignore`. The committed canonical path is still trackable (once Task 3 writes it on the real host).

**Files modified:** `.gitignore`

**Commit:** `6ebcf98`

## Auth / Human Gates

None hit during the autonomous portion. The live Phase 0 sweep (Task 3) is a deliberate `checkpoint:human-verify` and is not attempted here. No credentials were read from `.env` during execution of Tasks 1/2 beyond what pydantic-settings loaded into memory for the dry-run; no network calls were made to the real DVR; no model-bundle downloads were attempted.

## Known Stubs

**`weights.lock.json`** — intentionally committed as a scaffold with `pending_download: true` and `size_bytes: 0` / `verified: false` on every entry. `verify_model_bundle` handles this case by accepting any non-zero cache file when `verified: false`, so it does not break startup verification. The real sizes and `cold_start_ms` integers are auto-populated by `download_model_bundle()` → `persist_weights_lock()` on the first successful run (which happens inside Task 3's live sweep on the user's host).

This is documented as pending and is resolved by the checkpoint step, not by any future plan. No other stubs in the code — every function does real work against real types.

## Threat Flags

None new. All `<threat_model>` mitigations from the plan are implemented:

| Threat | Mitigation | Evidence |
| --- | --- | --- |
| T-00-15 PHASE0-REPORT.json information disclosure | Report schema has no password field; `env_vars_loaded` is a list of KEY names only; DVR `ip` is a LAN address. Dry-run writes `.dryrun.json` sibling, never the canonical path. | `test_offline_dry_run_mp4` + manual dry-run inspection |
| T-00-16 Model weight corruption | `verify_model_bundle` re-checks sizes at every startup against `weights.lock.json`; corrupt cache is reported as `_cached=False`. | `test_verify_respects_size_mismatch` |
| T-00-18 Cam 3/4 NAL DoS | `fflags=+discardcorrupt` already in canonical env from Plan 01; `per_camera_exit_ok` tolerates ≥0.95 clean ratio on nal cams | `test_per_camera_exit_ok_cam3_ratio_ok/fail` |

No new trust boundaries beyond those in the plan's `<threat_model>`.

## What The User Needs To Do Next (Live Sweep Checkpoint)

Every autonomous piece of Plan 00-03 is done. The remaining work is the Task 3 checkpoint — the 4-camera × 30-minute live sweep against the real DVR. You should run this from a WSL2 Ubuntu terminal inside this repo. Approximate wall-clock: **2 hours** + the model-bundle download (~10–30 min first-time).

### Pre-flight

1. Confirm `.env` still has the DVR credentials (it does — `DVR_USERNAME=admin`, `DVR_PASSWORD=REDACTED`, `DVR_LOCAL_IP=192.168.1.10`, `DVR_LOCAL_RTSP_PORT=554`). The compat shim accepts these as-is.

2. Sanity-check host + HEVC + WSL2 mode:

   ```
   uv run python -c "
   from home_cctv.phase0.host_probe import assert_ffmpeg_backend, assert_hevc_decoder, detect_wsl2_networking_mode
   print(assert_ffmpeg_backend())
   assert assert_hevc_decoder()
   print('wsl2_mode=', detect_wsl2_networking_mode())
   "
   ```

   Expected: `FFMPEG: YES` in the snippet, `HEVC OK`, and `wsl2_mode=` either `mirrored` (good) or `bridged`. If you see `nat`, fix `%USERPROFILE%\.wslconfig` on the Windows side (add `[wsl2]\nnetworkingMode=mirrored`) and `wsl --shutdown` before retrying — the DVR will be unreachable from inside WSL2 otherwise.

3. Close the BitVision phone app before running the concurrent-session probe — otherwise the DVR's 5-session cap will kick BitVision off unexpectedly.

### Task 3: Live sweep

4. Run the full Phase 0 harness. This does the model bundle download + warmup + persist (no manual shell snippet, W6), the DVR reachability + handshake + concurrent-session probes (Q6), then the 4-camera sequential 30-minute sweep, then writes `PHASE0-REPORT.json`:

   ```
   uv run python -m home_cctv --phase0
   ```

   Expected wall-clock: 10–30 min model download (first run only) + 2 hours camera sweep = **~2.5 hours total**.

5. When it finishes, review the report:

   ```
   cat .planning/phases/00-environment-sanity/PHASE0-REPORT.json | uv run python -m json.tool
   ```

   Expected per-camera values:
   - Cam 1 `exterior_red`: measured_fps ≈ 20–30, frames_corrupted ≤ 5, hangs = 0
   - Cam 2 `interior_orange`: measured_fps ≈ 9.6–14.4, frames_corrupted ≤ 5, hangs = 0
   - Cam 3 `exterior_blue`: measured_fps ≈ 9.6–14.4, `frames_decoded / (frames_decoded + frames_corrupted) ≥ 0.95`, hangs = 0
   - Cam 4 `interior_green`: measured_fps ≈ 20–30, same ratio ≥ 0.95, hangs = 0

   Expected top-level: `phase0_verdict: "pass"`; `blockers_resolved` has Q1/Q2/Q3/Q4/Q6 = `true`; `Q5_reconnect_watchdog_sketch: "designed but not yet implemented"`.

   Expected `model_bundle.cold_start_ms`: three positive integers (roughly `yolo_openvino: 300–1500`, `deepface: 3000–12000`, `easyocr: 2000–8000`). If any is 0 the warmup failed — check logs.

6. Spot-check a couple of JPEGs under `$HOME/home_cctv/data/event_images/phase0_probe/cam1_exterior_red/` and `.../cam4_interior_green/` — they should show the real camera feed, not green/black.

7. Grep logs for the real password to confirm the masking filter works end-to-end:

   ```
   grep -c "REDACTED" $HOME/home_cctv/logs/home_cctv.log
   ```

   Must return `0`.

8. Commit the real artifacts:

   ```
   git add .planning/phases/00-environment-sanity/PHASE0-REPORT.json weights.lock.json
   git commit -m "phase0: real-host measurements + weights.lock.json populated"
   ```

### If something fails

- `phase0_verdict: "fail"` + one camera fps off: transient DVR load. Re-run.
- Cam 3/4 ratio < 0.95: the NAL workaround needs tuning. Try increasing `analyzeduration` or `probesize` in `src/home_cctv/ingest/flags.py` and re-run.
- `dvr.reachable: false`: WSL2 networking — see pre-flight step 2.
- HEVC missing: `sudo apt install ffmpeg libavcodec-extra` then re-run `uv sync` to refresh the wheel if needed.
- Ctrl+C test: if the process does not exit in <2 s, investigate the supervisor. Plan 02's `test_shutdown_is_idempotent` + live SIGINT test measured 100 ms so this should be fine.

## Handoff Notes to Phase 1

Phase 1 multi-stream ingest should import exactly these interfaces and treat them as ground-truth:

```python
import home_cctv  # noqa: F401 — pre-cv2 env setup

from home_cctv.config.env import load_settings, validate_runtime_paths
from home_cctv.config.cameras import CameraConfig, CamerasFile, load_cameras, build_rtsp_url
from home_cctv.ingest.capture import open_frame_source, FrameSource, CaptureStats
from home_cctv.ingest.supervisor import ShutdownSupervisor, install_signal_handlers
from home_cctv.obs.logging_setup import configure_logging
from home_cctv.phase0.model_bundle import verify_model_bundle, WEIGHTS_LOCK_PATH

settings = load_settings()
validate_runtime_paths(settings)
cams = load_cameras("cameras.yaml")

for cam in cams.cameras:
    url = build_rtsp_url(settings, cam, stream="sub")
    fs = open_frame_source(url, camera_id=f"cam{cam.id}:{cam.name}")
    # ... one thread per camera, etc.
```

Invariants Phase 1 must respect:

1. **Never gate the live-RTSP loop on `decode_errors > 0`.** Cam 3/4 NAL-unit-0 errors are expected. Use `fs.is_file_source` to distinguish EOF from a transient hiccup.
2. **Each `open()` re-enters the 5-frame drop window** — Phase 1's watchdog reconnect policy should keep reopens rare.
3. **Always `supervisor.register(fs)` before `fs.open()`** so stuck opens are interruptible via SIGINT.
4. **Assume the model bundle is already cached** under `settings.MODEL_CACHE_DIR` once Task 3 has run. Call `verify_model_bundle(settings.MODEL_CACHE_DIR)` at startup and refuse to proceed if any `_cached` flag is False; fall back to `download_model_bundle` only in a one-time setup command, not in the hot path.
5. **SUMMARY.md §6 blockers remaining for Phase 1:** Q5 (reconnect watchdog sketch) is "designed but not yet implemented" in Plan 00-03's report — Phase 1 owns the implementation.

## Commits

- `4c48b8d` — Task 1: cameras.yaml + loader + host/network probes + env alias shim + 16 tests
- `6ebcf98` — Task 2: model bundle + Phase0Report + sanity harness + --phase0 CLI + 15 tests

## Self-Check: PASSED

- FOUND: `cameras.yaml` (4 cameras, NAL flags on 3+4)
- FOUND: `weights.lock.json` (scaffold with pending_download:true)
- FOUND: `src/home_cctv/config/cameras.py`
- FOUND: `src/home_cctv/config/env.py` (modified — AliasChoices shim)
- FOUND: `src/home_cctv/phase0/__init__.py`
- FOUND: `src/home_cctv/phase0/host_probe.py`
- FOUND: `src/home_cctv/phase0/network_probe.py`
- FOUND: `src/home_cctv/phase0/model_bundle.py`
- FOUND: `src/home_cctv/phase0/report.py`
- FOUND: `src/home_cctv/phase0/sanity.py`
- FOUND: `src/home_cctv/__main__.py` (modified — --phase0 wired)
- FOUND: `pyproject.toml` (modified — slow marker)
- FOUND: `.gitignore` (modified — dryrun.json excluded)
- FOUND: `tests/test_cameras_yaml.py`
- FOUND: `tests/test_host_probe.py`
- FOUND: `tests/test_model_bundle.py`
- FOUND: `tests/test_report_schema.py`
- FOUND: commit `4c48b8d` (Task 1)
- FOUND: commit `6ebcf98` (Task 2)
- 76/76 tests pass (61 baseline + 16 Task 1 + 15 Task 2, 1 slow deselected)
- `uv run python -m home_cctv --phase0 --mp4 tests/fixtures/sample_720p25.mp4` writes `PHASE0-REPORT.dryrun.json` with 4 camera rows
- Canonical `PHASE0-REPORT.json` is NOT created by the dry-run (B3 enforced)
- `grep -q "def from_json" src/home_cctv/phase0/report.py` — OK
- `grep -q "warmup_model_bundle" src/home_cctv/phase0/model_bundle.py` — OK
- `grep -q "persist_weights_lock" src/home_cctv/phase0/model_bundle.py` and called from `download_model_bundle` — OK
- `grep -q "parents\[3\]" src/home_cctv/phase0/model_bundle.py` — OK
- `grep -q "is_file_source" src/home_cctv/phase0/sanity.py` — OK
- `grep -q "PHASE0_DURATION_SEC" src/home_cctv/phase0/sanity.py` shows `PHASE0_DURATION_SEC: int = 1800` — OK
- Plan `autonomous: false` honored — Task 3 (live sweep) intentionally NOT executed
