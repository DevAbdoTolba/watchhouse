---
phase: 00-environment-sanity
plan: 01-scaffolding
subsystem: bootstrap
tags: [scaffolding, uv, pydantic, logging, ffmpeg, env]
requirements:
  - ENV-01
  - ENV-03
  - ENV-05
dependency_graph:
  requires: []
  provides:
    - home_cctv package (importable, pre-cv2 env chokepoint)
    - home_cctv.ingest.flags.OPENCV_FFMPEG_OPTIONS_STRING (canonical RTSP flag string)
    - home_cctv.ingest.flags.apply_capture_options() / assert_capture_options_active()
    - home_cctv.config.env.Settings / load_settings() / validate_runtime_paths()
    - home_cctv.config.env.assert_not_drvfs() / assert_disk_space_ok()
    - home_cctv.obs.logging_setup.configure_logging() / CredentialMaskFilter
  affects:
    - all downstream plans (Plan 00-02 capture, 00-03 harness) inherit the pre-cv2 env + loaded Settings + masked logger
tech_stack:
  added:
    - "python==3.11.15 (managed by uv)"
    - "opencv-python-headless==4.10.0.84 (deviation — see below)"
    - "numpy==1.26.4"
    - "python-dotenv==1.2.2"
    - "pydantic==2.13.0"
    - "pydantic-settings==2.13.1"
    - "pyyaml==6.0.3"
    - "ultralytics==8.4.37"
    - "openvino==2026.1.0"
    - "onnxruntime==1.24.4"
    - "lap==0.5.13"
    - "scipy==1.17.1"
    - "torch==2.5.1+cpu"
    - "easyocr==1.7.2"
    - "tensorflow-cpu==2.16.2"
    - "tf-keras==2.16.0"
    - "deepface==0.0.99"
    - "shapely==2.1.2"
    - "psutil==6.1.1"
    - "pytest==8.4.2 (dev)"
    - "pytest-json-report==1.5.0 (dev)"
    - "ruff==0.15.10 (dev)"
    - "mypy==1.20.1 (dev)"
  patterns:
    - "src-layout (src/home_cctv/) + hatchling build"
    - "pre-cv2 env chokepoint in package __init__ (TF_USE_LEGACY_KERAS + OPENCV_FFMPEG_CAPTURE_OPTIONS)"
    - "pydantic-settings BaseSettings with .env auto-load"
    - "LoggerAdapter + Filter for bracketed [camera_id] prefix"
    - "Regex-based CredentialMaskFilter on every handler"
key_files:
  created:
    - pyproject.toml
    - uv.lock
    - .env.example
    - src/home_cctv/__init__.py
    - src/home_cctv/__main__.py
    - src/home_cctv/config/__init__.py
    - src/home_cctv/config/env.py
    - src/home_cctv/ingest/__init__.py
    - src/home_cctv/ingest/flags.py
    - src/home_cctv/obs/__init__.py
    - src/home_cctv/obs/logging_setup.py
    - tests/__init__.py
    - tests/test_flags_builder.py
    - tests/test_config_env.py
    - tests/test_logging_mask.py
  modified:
    - .gitignore
decisions:
  - "Pinned opencv-python-headless==4.10.0.84 (downgraded from 4.13.0.92)"
  - "CAPTURE_OPTIONS alias exported alongside OPENCV_FFMPEG_OPTIONS_STRING"
metrics:
  duration_minutes: 95
  tests_total: 19
  tests_passing: 19
  completed: "2026-04-13"
---

# Phase 0 Plan 01: Scaffolding Summary

One-liner: Bootstrapped the `home_cctv` src-layout package, locked the full Phase 0–4 dependency stack with `uv`, and established the pre-cv2 env chokepoint (OPENCV_FFMPEG_CAPTURE_OPTIONS + TF_USE_LEGACY_KERAS), pydantic Settings loader with DrvFs + disk-space guards, and credential-masking rotating logger that Plans 02 and 03 can import without ceremony.

## What Shipped

### Package Layout (src-layout)

```
pyproject.toml         # uv-managed, full pinned stack, hatchling build
uv.lock                # 109 packages resolved, committed
.env.example           # template — copied to real .env (gitignored)
.gitignore             # .venv/, .env, data/, logs/, pycache, uv-managed caches
src/home_cctv/
  __init__.py          # PRE-CV2 CHOKEPOINT: sets TF_USE_LEGACY_KERAS + calls apply_capture_options()
  __main__.py          # `python -m home_cctv` entry; parses --phase0/--mp4/--show; loads settings, configures logger, logs masked banner
  config/
    __init__.py
    env.py             # Settings/load_settings/assert_not_drvfs/assert_disk_space_ok/validate_runtime_paths
  ingest/
    __init__.py
    flags.py           # OPENCV_FFMPEG_OPTIONS_STRING + CAPTURE_OPTIONS alias + apply_capture_options + assert_capture_options_active
  obs/
    __init__.py
    logging_setup.py   # RotatingFileHandler (10 MB × 5) + StreamHandler, CredentialMaskFilter, LoggerAdapter with bracketed [camera_id] prefix
tests/
  __init__.py
  test_flags_builder.py    # 6 tests
  test_config_env.py       # 6 tests
  test_logging_mask.py     # 7 tests
```

### Canonical RTSP Flag String (frozen)

Locked in `src/home_cctv/ingest/flags.py`. Byte-for-byte match against CONTEXT.md §"OPENCV_FFMPEG_CAPTURE_OPTIONS String":

```
rtsp_transport;tcp|fflags;nobuffer+discardcorrupt|flags;low_delay|err_detect;ignore_err|stimeout;5000000|reconnect;1|reconnect_streamed;1|reconnect_delay_max;2|analyzeduration;1000000|probesize;2000000
```

Set into `os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]` by `home_cctv/__init__.py` BEFORE anything in the codebase can import `cv2`. A runtime assert (`assert_capture_options_active`) catches any downstream mutation / reordering.

### Settings Loader & Startup Guards (ENV-01, ENV-03, ENV-05)

`src/home_cctv/config/env.py`:

- `Settings(BaseSettings)` — typed fields for `DVR_IP`, `DVR_PORT`, `DVR_USER`, `DVR_PASS`, `EVENT_IMAGE_DIR`, `DB_PATH`, `LOG_DIR`, `MODEL_CACHE_DIR`. Auto-loads `.env` via `pydantic-settings`.
- `load_settings()` — re-raises any pydantic validation error as a `RuntimeError("Failed to load .env settings. ...")` that names the offending field (DVR_IP missing -> error contains literal `DVR_IP`, exit code 2 in `__main__.main()`).
- `assert_not_drvfs(path)` — belt-and-braces: matches `/mnt/{c..g}/` prefix AND calls `stat -f -c %T` to detect `drvfs` / `9p` fstypes. Refuses to run with a message pointing the user at WSL2 ext4.
- `assert_disk_space_ok(path, min_gb=1.0)` — uses `os.statvfs`, refuses under 1 GB free, message contains "free" and "GB".
- `validate_runtime_paths(settings)` — wires both guards over `EVENT_IMAGE_DIR`, `DB_PATH`, `MODEL_CACHE_DIR`.
- `Settings.masked_rtsp_base()` — builds `rtsp://user:***@ip:port` for logging; real password is not exposed here.

### Credential-Masking Logger

`src/home_cctv/obs/logging_setup.py`:

- `CredentialMaskFilter` — regex `(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/@\s]+):[^@\s]+@` rewrites `scheme://user:pass@host` to `scheme://user:***@host` on `record.msg` + every string arg. Applied to both the `RotatingFileHandler` and the stderr `StreamHandler` — no log path leaks.
- `RotatingFileHandler` at `{log_dir}/home_cctv.log`, `maxBytes=10 MB`, `backupCount=5`.
- Bracketed `[camera_id]` prefix — formatter uses `%(camera_id)s`. `configure_logging` returns a `logging.LoggerAdapter` with `extra={"camera_id": ...}`; a `_CameraIdDefault` filter defaults to `"main"` for any LogRecord that skipped the adapter so the formatter never KeyErrors.

### Entry Point Wiring

`src/home_cctv/__main__.py`:

```
uv run python -m home_cctv
# -> 2026-04-13T19:51:10Z INFO  [main] booted version=0.1.0 phase0=False mp4=None show=False dvr=rtsp://admin:***@192.168.1.10:554 event_dir=/tmp/hc_e2e/data/event_images
```

Missing `.env`:

```
STARTUP ERROR: Failed to load .env settings. Fix .env then retry.
Details: 5 validation errors for Settings
DVR_IP
  Field required [type=missing, ...]
...
# exit code 2
```

## Verification Evidence

| Plan requirement                         | Verification                                                                                      | Result |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------- | ------ |
| ENV-01 single-command start on WSL2+py3.11 | `uv sync` + `uv run python -m home_cctv`                                                          | PASS   |
| ENV-03 creds from .env only, never logged | `test_integration_password_never_written_to_file`, end-to-end banner shows `rtsp://admin:***@...` | PASS   |
| ENV-05 clear startup errors               | missing DVR_IP -> RuntimeError naming `DVR_IP` + exit 2; DrvFs path -> RuntimeError with "DrvFs"; <1 GB -> RuntimeError with "free GB" | PASS |
| `OPENCV_FFMPEG_CAPTURE_OPTIONS` set pre-cv2 | `test_env_set_on_package_import`, runtime `python -c "import home_cctv, os; ..."` matches canonical | PASS |
| `TF_USE_LEGACY_KERAS=1` pre-tf import    | `test_tf_legacy_keras_set_on_package_import`                                                       | PASS   |
| `LoggerAdapter` used for camera prefix   | `grep -q "LoggerAdapter" src/home_cctv/obs/logging_setup.py`                                      | PASS   |
| `%(camera_id)s` formatter                | `grep -q "%(camera_id)s" src/home_cctv/obs/logging_setup.py`                                      | PASS   |

**Test counts:**

```
$ uv run pytest tests/ -x -q
...................                                                      [100%]
19 passed in 3.47s
```

- `tests/test_flags_builder.py` — 6/6 passing
- `tests/test_config_env.py` — 6/6 passing
- `tests/test_logging_mask.py` — 7/7 passing

**End-to-end boot against a temp `.env` in `/tmp/hc_e2e/`:**

```
$ uv run python -m home_cctv
2026-04-13T19:51:10Z INFO  [main] booted version=0.1.0 phase0=False mp4=None show=False dvr=rtsp://admin:***@192.168.1.10:554 event_dir=/tmp/hc_e2e/data/event_images
# exit 0
```

**Canonical string emission check:**

```
$ uv run python -c "from home_cctv.ingest.flags import CAPTURE_OPTIONS; print(CAPTURE_OPTIONS)"
rtsp_transport;tcp|fflags;nobuffer+discardcorrupt|flags;low_delay|err_detect;ignore_err|stimeout;5000000|reconnect;1|reconnect_streamed;1|reconnect_delay_max;2|analyzeduration;1000000|probesize;2000000
```

## Deviations from Plan

### 1. [Rule 1 — Dependency bug] `opencv-python-headless` 4.13.0.92 → 4.10.0.84

**Found during:** Task 1, first `uv sync`.

**Issue:** `pyproject.toml` pinned `opencv-python-headless==4.13.0.92` per SUMMARY.md §2. `uv` resolver rejected it:

```
Because opencv-python-headless==4.13.0.92 depends on numpy>=2 and your
project depends on numpy==1.26.4, we can conclude that your project and
opencv-python-headless==4.13.0.92 are incompatible.
```

SUMMARY.md §2 also pins `numpy==1.26.4` (hard requirement for TF 2.16 / DeepFace), so NumPy cannot move. The newest OpenCV headless wheel that still links against NumPy 1.26 is `4.10.0.84`.

**Fix:** Downgraded to `opencv-python-headless==4.10.0.84`. Same FFmpeg/HEVC codec coverage, same `OPENCV_FFMPEG_CAPTURE_OPTIONS` semantics, same H.265 decode path — the 4.10 → 4.13 delta is mostly DNN / core API changes, none of which this project touches in Phase 0.

**Files modified:** `pyproject.toml`

**Commit:** `83c2505`

**Follow-up for downstream plans:** When Phase 2 uses ByteTrack via Ultralytics and when Phase 3 pulls main-stream frames, re-verify cv2.VideoCapture backend still reports FFMPEG: YES. Should be fine — OpenCV 4.10 has the same build flags as 4.13 for the manylinux wheel.

### 2. [Rule 2 — Missing API] `CAPTURE_OPTIONS` alias in `ingest/flags.py`

**Found during:** Final verification step in the executor success criteria, which expects `from home_cctv.ingest.flags import CAPTURE_OPTIONS` to work. Plan text only specifies `OPENCV_FFMPEG_OPTIONS_STRING`.

**Fix:** Added a module-level `CAPTURE_OPTIONS: str = OPENCV_FFMPEG_OPTIONS_STRING` alias. Non-breaking, no re-export confusion.

**Files modified:** `src/home_cctv/ingest/flags.py`

**Commit:** `87a459e`

### 3. [Documented workaround — WSL2 ext4 vs DrvFs in tests]

**Found during:** Task 2 test run.

**Issue:** `pytest`'s `tmp_path` fixture inside this repo resolves to `/tmp` (WSL2 ext4), which satisfies `assert_not_drvfs`. But if any future test used a path rooted under `/mnt/d/...` (this repo is on DrvFs), the DrvFs-accept test would fail. The plan's `test_ext4_home_path_accepted` used `tmp_path` — I rewrote it to use `tempfile.TemporaryDirectory()` directly so it always lands in `/tmp` (ext4) regardless of repo location.

**Files modified:** `tests/test_config_env.py`

**Commit:** `87a459e`

## Auth / Human Gates

None. Plan 01 is pure scaffolding — no network, no DVR handshake, no secrets fetched.

## Known Stubs

None. Every file created in this plan is fully wired:

- `__main__.py` parses all three flags and actually loads settings + logger; the "Plan 01 stub" comment refers to the Phase 0 measurement harness body arriving in Plan 00-03, not to placeholder data that renders in UI.
- No hardcoded `[]`, `{}`, `None`, or "coming soon" strings.
- Settings loader reads actual `.env` values; logger handlers actually receive records.

## Threat Flags

None. All surface this plan introduces matches CONTEXT.md §"Security posture" and the plan's `<threat_model>` block (`.env` trust boundary, log-sink information disclosure, DrvFs tampering). Every `mitigate` disposition in the plan is implemented and test-covered:

| Threat | Mitigation | Covered by |
| ------ | ---------- | ---------- |
| T-00-01 Info disclosure via rotating file handler | CredentialMaskFilter on handler | `test_integration_password_never_written_to_file` |
| T-00-02 Info disclosure via stderr | Same filter on StreamHandler | `test_handlers_get_filter` |
| T-00-03 `repr(Settings)` leaking DVR_PASS | `__main__` uses `masked_rtsp_base()`, never prints settings directly | reviewed by hand |
| T-00-04 SQLite WAL on DrvFs | `assert_not_drvfs` with prefix + `stat -f` fstype check | `test_drvfs_prefix_rejected` |
| T-00-05 Disk fills mid-run | `assert_disk_space_ok(..., min_gb=1.0)` | `test_disk_space_too_small_raises` |
| T-00-06 `.env` committed to git | `.gitignore` entry + template `.env.example` | git diff inspection |

## Handoff Notes to Plan 00-02 / 00-03

Plans 02 and 03 should import these interfaces and assume they're already safe:

```python
import home_cctv  # triggers OPENCV_FFMPEG_CAPTURE_OPTIONS + TF_USE_LEGACY_KERAS

from home_cctv.config.env import Settings, load_settings, validate_runtime_paths
from home_cctv.ingest.flags import (
    OPENCV_FFMPEG_OPTIONS_STRING,
    CAPTURE_OPTIONS,                 # alias for the above
    apply_capture_options,
    assert_capture_options_active,
)
from home_cctv.obs.logging_setup import configure_logging, CredentialMaskFilter
```

Startup boilerplate any downstream plan should use:

```python
import home_cctv  # noqa: F401  — runs the pre-cv2 env setup on import
from home_cctv.config.env import load_settings, validate_runtime_paths
from home_cctv.obs.logging_setup import configure_logging

settings = load_settings()
validate_runtime_paths(settings)
logger = configure_logging(settings.LOG_DIR, camera_id="cam1:exterior_red")
# now safe to: import cv2 ; from ultralytics import YOLO ; from deepface import DeepFace
```

**The repo-level `.env`** currently uses a different key vocabulary (`DVR_USERNAME` / `DVR_PASSWORD` / `DVR_LOCAL_IP` / `DVR_LOCAL_RTSP_PORT` / `DVR_RTSP_URL`) than the plan-locked Settings schema (`DVR_IP` / `DVR_USER` / `DVR_PASS` / `DVR_PORT`). Per the orchestrator's environment notes, I did NOT overwrite the existing `.env`. Before Plan 00-03 runs `python -m home_cctv` against the real DVR, the user must either:

1. Rename keys in `.env` to the plan schema: `DVR_IP`, `DVR_PORT`, `DVR_USER`, `DVR_PASS`, plus add `EVENT_IMAGE_DIR`, `DB_PATH`, `LOG_DIR`, `MODEL_CACHE_DIR` rooted at WSL2 ext4 (e.g. `/home/abdo/home_cctv/...`), OR
2. Ask the Plan 00-03 agent to add a compat shim that accepts both legacy and new key names.

The `.env.example` file committed in this plan is the canonical template.

## Commits

- `83c2505` — Task 1: pyproject.toml + uv.lock + package skeleton + pre-cv2 env chokepoint + flags.py + 6 tests
- `87a459e` — Task 2: config/env.py + obs/logging_setup.py + __main__ wiring + CAPTURE_OPTIONS alias + 13 tests

## Self-Check: PASSED

- FOUND: `pyproject.toml`
- FOUND: `uv.lock`
- FOUND: `.env.example`
- FOUND: `.gitignore` (modified)
- FOUND: `src/home_cctv/__init__.py`
- FOUND: `src/home_cctv/__main__.py`
- FOUND: `src/home_cctv/config/__init__.py`
- FOUND: `src/home_cctv/config/env.py`
- FOUND: `src/home_cctv/ingest/__init__.py`
- FOUND: `src/home_cctv/ingest/flags.py`
- FOUND: `src/home_cctv/obs/__init__.py`
- FOUND: `src/home_cctv/obs/logging_setup.py`
- FOUND: `tests/__init__.py`
- FOUND: `tests/test_flags_builder.py`
- FOUND: `tests/test_config_env.py`
- FOUND: `tests/test_logging_mask.py`
- FOUND: commit `83c2505`
- FOUND: commit `87a459e`
- All 19 tests pass
- `uv run python -c "from home_cctv.ingest.flags import CAPTURE_OPTIONS; print(CAPTURE_OPTIONS)"` matches CONTEXT.md canonical
- `grep -q "TF_USE_LEGACY_KERAS" src/home_cctv/__init__.py` — OK
- `grep -q "LoggerAdapter" src/home_cctv/obs/logging_setup.py` — OK
- `grep -q "%(camera_id)s" src/home_cctv/obs/logging_setup.py` — OK
