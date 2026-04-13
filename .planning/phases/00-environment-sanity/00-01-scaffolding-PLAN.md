---
phase: 00-environment-sanity
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - pyproject.toml
  - uv.lock
  - .gitignore
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
  - tests/test_config_env.py
  - tests/test_flags_builder.py
  - tests/test_logging_mask.py
autonomous: true
requirements:
  - ENV-01
  - ENV-03
  - ENV-05
user_setup: []

must_haves:
  truths:
    - "User runs `uv sync` in a fresh clone and gets a working .venv with the full pinned stack"
    - "User runs `python -m home_cctv` and the process starts, loads .env, prints a boot banner that names the config source, and masks every credential"
    - "User deletes DVR_IP from .env, starts the pipeline, and sees a clear error naming the missing variable before any cv2 import"
    - "User points EVENT_IMAGE_DIR at a path with <1 GB free and sees a clear refuse-to-start error"
    - "User points EVENT_IMAGE_DIR at /mnt/c/... and sees a clear refuse-to-start error naming DrvFs as the reason"
    - "OPENCV_FFMPEG_CAPTURE_OPTIONS is present in os.environ at every cv2 import site in the codebase"
    - "No plaintext rtsp password ever reaches a log handler"
  artifacts:
    - path: pyproject.toml
      provides: "uv-managed dep manifest with full Phase 0→4 pinned stack"
      contains: "ultralytics==8.4.37"
    - path: src/home_cctv/__init__.py
      provides: "Pre-cv2 env export: TF_USE_LEGACY_KERAS + OPENCV_FFMPEG_CAPTURE_OPTIONS"
      contains: "OPENCV_FFMPEG_CAPTURE_OPTIONS"
    - path: src/home_cctv/config/env.py
      provides: "Pydantic Settings loader for DVR + path env vars with disk/FS validation"
      exports: ["Settings", "load_settings"]
    - path: src/home_cctv/ingest/flags.py
      provides: "Canonical OPENCV_FFMPEG_CAPTURE_OPTIONS builder"
      exports: ["OPENCV_FFMPEG_OPTIONS_STRING", "apply_capture_options"]
    - path: src/home_cctv/obs/logging_setup.py
      provides: "RotatingFileHandler + per-camera prefix + credential mask filter"
      exports: ["configure_logging", "CredentialMaskFilter"]
    - path: .env.example
      provides: "Committed template for DVR creds + paths"
      contains: "DVR_IP="
  key_links:
    - from: "src/home_cctv/__init__.py"
      to: "src/home_cctv/ingest/flags.py"
      via: "module-load-time import that sets os.environ BEFORE any cv2 import"
      pattern: "from home_cctv.ingest.flags import apply_capture_options"
    - from: "src/home_cctv/__main__.py"
      to: "src/home_cctv/config/env.py"
      via: "load_settings() called first in main()"
      pattern: "load_settings\\("
    - from: "src/home_cctv/obs/logging_setup.py"
      to: "logging.Logger"
      via: "CredentialMaskFilter added to every handler"
      pattern: "addFilter.*CredentialMaskFilter"
---

<objective>
Scaffold the `home_cctv` package, lock the full Phase 0→4 pinned stack into `pyproject.toml` via `uv`, and put in place the boot-time guarantees that every subsequent plan depends on: the `OPENCV_FFMPEG_CAPTURE_OPTIONS` string is in `os.environ` before anything imports `cv2`, `TF_USE_LEGACY_KERAS=1` is set before anything imports TensorFlow, `.env` is loaded via a pydantic `Settings` object with clear errors on missing vars / <1 GB free / DrvFs paths, and every log line passes through a credential-masking filter.

Purpose: Make Plans 02 and 03 boring. They should just import `home_cctv` and already have a safe environment, a loaded settings object, and a working logger with masked credentials. This plan locks ENV-01 (single-command start on WSL2+py3.11), ENV-03 (creds exclusively from `.env`, never logged), and ENV-05 (clear startup errors).

Output: Running `uv sync && uv run python -m home_cctv` in a fresh clone prints a boot banner and exits cleanly (full pipeline doesn't exist yet; Phase 0 harness ships in Plan 03).
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/phases/00-environment-sanity/00-CONTEXT.md
@.planning/research/SUMMARY.md
@.planning/research/STACK.md
@.planning/research/PITFALLS.md
@.planning/REQUIREMENTS.md
@CLAUDE.md
@cameras.txt
@terminal.txt

<interfaces>
<!-- Nothing to extract from existing code — this is the greenfield bootstrap plan. -->
<!-- The interfaces THIS plan produces are consumed by Plans 02 and 03: -->

Produced by this plan (Plans 02/03 will import these):

```python
# src/home_cctv/config/env.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DVR_IP: str
    DVR_PORT: int = 554
    DVR_USER: str
    DVR_PASS: str
    EVENT_IMAGE_DIR: Path
    DB_PATH: Path
    LOG_DIR: Path = Path("logs")
    MODEL_CACHE_DIR: Path = Path.home() / ".cache/home_cctv/models"

def load_settings() -> Settings: ...
def assert_disk_space_ok(path: Path, min_gb: float = 1.0) -> None: ...
def assert_not_drvfs(path: Path) -> None: ...

# src/home_cctv/ingest/flags.py
OPENCV_FFMPEG_OPTIONS_STRING: str  # exact canonical string
def apply_capture_options() -> None: ...  # sets os.environ

# src/home_cctv/obs/logging_setup.py
def configure_logging(log_dir: Path, camera_id: str | None = None) -> logging.Logger: ...
class CredentialMaskFilter(logging.Filter): ...
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Bootstrap pyproject.toml + uv + package skeleton + pre-cv2 env guarantees</name>
  <files>pyproject.toml, .gitignore, .env.example, src/home_cctv/__init__.py, src/home_cctv/__main__.py, src/home_cctv/config/__init__.py, src/home_cctv/ingest/__init__.py, src/home_cctv/ingest/flags.py, src/home_cctv/obs/__init__.py, tests/__init__.py, tests/test_flags_builder.py</files>
  <read_first>
    - .planning/phases/00-environment-sanity/00-CONTEXT.md (§"Repo & Package Layout", §"Dependency Management", §"OPENCV_FFMPEG_CAPTURE_OPTIONS String")
    - .planning/research/SUMMARY.md §2 "Recommended Stack (Pinned)" — ALL version pins
    - .planning/research/PITFALLS.md §1.2 "OpenCV wheel built without FFmpeg" and §1.1 "stimeout must be set before import cv2"
    - cameras.txt — confirms codec=H.265, per-camera paths (these go in Plan 03's cameras.yaml, not here)
    - terminal.txt — ground-truth ffplay flags being translated
    - CLAUDE.md
  </read_first>
  <behavior>
    - Test: `home_cctv.ingest.flags.OPENCV_FFMPEG_OPTIONS_STRING` equals exactly `rtsp_transport;tcp|fflags;nobuffer+discardcorrupt|flags;low_delay|err_detect;ignore_err|stimeout;5000000|reconnect;1|reconnect_streamed;1|reconnect_delay_max;2|analyzeduration;1000000|probesize;2000000`
    - Test: After `import home_cctv`, `os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]` matches that string
    - Test: After `import home_cctv`, `os.environ["TF_USE_LEGACY_KERAS"] == "1"`
    - Test: `home_cctv.ingest.flags.apply_capture_options()` is idempotent (calling twice leaves os.environ unchanged)
    - Test: Every token in the options string uses `;` as key/value sep and `|` as pair sep — no stray spaces, no `=`
  </behavior>
  <action>
    1. Create `pyproject.toml` at repo root with this exact structure (versions from SUMMARY.md §2 and CONTEXT.md "Initial pinned deps"):

    ```toml
    [project]
    name = "home_cctv"
    version = "0.1.0"
    description = "Local-only AI event pipeline over 4 RTSP streams (CPU-only, Windows+WSL2)"
    requires-python = ">=3.11,<3.12"
    dependencies = [
      "opencv-python-headless==4.13.0.92",
      "numpy==1.26.4",
      "python-dotenv==1.2.2",
      "pydantic>=2.10,<3",
      "pydantic-settings>=2.6,<3",
      "pyyaml>=6,<7",
      "ultralytics==8.4.37",
      "openvino==2026.1.0",
      "onnxruntime==1.24.4",
      "lap==0.5.13",
      "scipy>=1.13,<2",
      "torch==2.5.1+cpu",
      "easyocr==1.7.2",
      "tensorflow-cpu==2.16.2",
      "tf-keras==2.16.0",
      "deepface==0.0.99",
      "shapely>=2.0,<3",
      "psutil>=6,<7",
    ]

    [project.optional-dependencies]
    dev = [
      "pytest>=8,<9",
      "pytest-json-report>=1.5,<2",
      "ruff>=0.7,<1",
      "mypy>=1.13,<2",
    ]

    [project.scripts]
    home_cctv = "home_cctv.__main__:main"

    [tool.uv]
    index-strategy = "unsafe-best-match"

    [[tool.uv.index]]
    name = "pytorch-cpu"
    url = "https://download.pytorch.org/whl/cpu"
    explicit = true

    [tool.uv.sources]
    torch = { index = "pytorch-cpu" }

    [build-system]
    requires = ["hatchling"]
    build-backend = "hatchling.build"

    [tool.hatch.build.targets.wheel]
    packages = ["src/home_cctv"]
    ```

    2. Create `.gitignore` with at minimum: `.venv/`, `.env`, `uv.lock` is NOT gitignored (commit it), `data/`, `logs/`, `__pycache__/`, `*.pyc`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `.planning/phases/00-environment-sanity/PHASE0-REPORT.json` is NOT gitignored (committed artifact per CONTEXT.md).

    3. Create `.env.example` with EXACTLY these keys and placeholder values, no real creds:
    ```
    # Home CCTV AI Pipeline — environment template
    # Copy to `.env` and fill in. Never commit the real `.env` (see .gitignore).
    DVR_IP=192.168.1.10
    DVR_PORT=554
    DVR_USER=admin
    DVR_PASS=changeme
    EVENT_IMAGE_DIR=/home/youruser/home_cctv/data/event_images
    DB_PATH=/home/youruser/home_cctv/data/cctv_events.db
    LOG_DIR=/home/youruser/home_cctv/logs
    MODEL_CACHE_DIR=/home/youruser/.cache/home_cctv/models
    ```

    4. Create `src/home_cctv/ingest/flags.py` — the CANONICAL env string builder. This file MUST have NO imports of cv2 (or anything that transitively imports cv2). It defines:
    ```python
    """OPENCV_FFMPEG_CAPTURE_OPTIONS canonical builder.

    This module MUST be imported before any `import cv2` anywhere in the codebase.
    The string below is the ground-truth translation of terminal.txt ffplay flags +
    stimeout (per SUMMARY.md §2 and PITFALLS.md §1.1 — the only knob that actually
    unblocks a stalled cap.read() on WSL2).
    """
    import os

    # Exact string from CONTEXT.md — do not reformat, do not reorder.
    OPENCV_FFMPEG_OPTIONS_STRING: str = (
        "rtsp_transport;tcp"
        "|fflags;nobuffer+discardcorrupt"
        "|flags;low_delay"
        "|err_detect;ignore_err"
        "|stimeout;5000000"
        "|reconnect;1"
        "|reconnect_streamed;1"
        "|reconnect_delay_max;2"
        "|analyzeduration;1000000"
        "|probesize;2000000"
    )

    _ENV_KEY = "OPENCV_FFMPEG_CAPTURE_OPTIONS"

    def apply_capture_options() -> None:
        """Set OPENCV_FFMPEG_CAPTURE_OPTIONS in os.environ. Idempotent."""
        os.environ[_ENV_KEY] = OPENCV_FFMPEG_OPTIONS_STRING

    def assert_capture_options_active() -> None:
        """Runtime assert: env var matches canonical string (catches re-ordering bugs)."""
        actual = os.environ.get(_ENV_KEY, "")
        if actual != OPENCV_FFMPEG_OPTIONS_STRING:
            raise RuntimeError(
                f"OPENCV_FFMPEG_CAPTURE_OPTIONS mismatch.\n"
                f"  expected: {OPENCV_FFMPEG_OPTIONS_STRING}\n"
                f"  actual:   {actual}"
            )
    ```

    5. Create `src/home_cctv/__init__.py` — the pre-cv2 chokepoint. MUST be minimal and MUST set env vars before any heavy import:
    ```python
    """home_cctv package init.

    CRITICAL ordering: this file sets environment variables that every downstream
    module depends on. It is imported first on `python -m home_cctv`, on `import
    home_cctv`, and transitively whenever any submodule is imported. Nothing in
    this file may import cv2 or tensorflow — those imports would observe missing
    env vars.
    """
    import os

    # TF must see this before `import tensorflow` anywhere in the process.
    os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")

    # OpenCV/FFmpeg must see this before `import cv2` anywhere in the process.
    from home_cctv.ingest.flags import apply_capture_options  # noqa: E402
    apply_capture_options()

    __version__ = "0.1.0"
    ```

    6. Create empty package init files: `src/home_cctv/config/__init__.py`, `src/home_cctv/ingest/__init__.py`, `src/home_cctv/obs/__init__.py`, `tests/__init__.py`. Each: one docstring line, nothing else.

    7. Create `src/home_cctv/__main__.py` as a thin entry point. For Phase 0 scaffolding it only parses `--phase0 | --mp4 PATH | --show` flags and prints a stub banner — real harness lands in Plan 03:
    ```python
    """`python -m home_cctv` entry point."""
    from __future__ import annotations
    import argparse
    import sys

    # Importing the package runs the pre-cv2 env setup.
    import home_cctv  # noqa: F401

    def build_parser() -> argparse.ArgumentParser:
        p = argparse.ArgumentParser(prog="home_cctv")
        p.add_argument("--phase0", action="store_true", help="Run Phase 0 measurement harness")
        p.add_argument("--mp4", type=str, default=None, help="Path to offline MP4 for regression testing")
        p.add_argument("--show", action="store_true", help="Open cv2.imshow preview window (falls back if no display)")
        return p

    def main(argv: list[str] | None = None) -> int:
        args = build_parser().parse_args(argv)
        # Plan 01 stub: full harness arrives in Plan 03.
        print(f"home_cctv v{home_cctv.__version__} booted (phase0={args.phase0}, mp4={args.mp4}, show={args.show})")
        return 0

    if __name__ == "__main__":
        sys.exit(main())
    ```

    8. Create `tests/test_flags_builder.py` — the RED/GREEN test locking the canonical env string:
    ```python
    import os
    import home_cctv  # triggers apply_capture_options()
    from home_cctv.ingest.flags import (
        OPENCV_FFMPEG_OPTIONS_STRING,
        apply_capture_options,
        assert_capture_options_active,
    )

    EXPECTED = (
        "rtsp_transport;tcp"
        "|fflags;nobuffer+discardcorrupt"
        "|flags;low_delay"
        "|err_detect;ignore_err"
        "|stimeout;5000000"
        "|reconnect;1"
        "|reconnect_streamed;1"
        "|reconnect_delay_max;2"
        "|analyzeduration;1000000"
        "|probesize;2000000"
    )

    def test_canonical_string_matches_context_md():
        assert OPENCV_FFMPEG_OPTIONS_STRING == EXPECTED

    def test_env_set_on_package_import():
        assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == EXPECTED

    def test_tf_legacy_keras_set_on_package_import():
        assert os.environ["TF_USE_LEGACY_KERAS"] == "1"

    def test_apply_is_idempotent():
        apply_capture_options()
        apply_capture_options()
        assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == EXPECTED

    def test_runtime_assert_catches_mutation():
        import pytest
        saved = os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]
        try:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp"
            with pytest.raises(RuntimeError, match="OPENCV_FFMPEG_CAPTURE_OPTIONS mismatch"):
                assert_capture_options_active()
        finally:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = saved

    def test_no_spaces_or_equals_in_options():
        # ffmpeg parser wants key;val|key;val — reject accidental `=` or spaces
        assert "=" not in OPENCV_FFMPEG_OPTIONS_STRING
        assert " " not in OPENCV_FFMPEG_OPTIONS_STRING
    ```

    9. Run `uv sync` to create `.venv` + `uv.lock`. Commit `uv.lock` (it is NOT in .gitignore).

    10. Run `uv run pytest tests/test_flags_builder.py -x -q` and confirm all six tests pass.
  </action>
  <verify>
    <automated>uv sync &amp;&amp; uv run pytest tests/test_flags_builder.py -x -q &amp;&amp; uv run python -c "import home_cctv, os; assert os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'].startswith('rtsp_transport;tcp|fflags;nobuffer+discardcorrupt'); assert os.environ['TF_USE_LEGACY_KERAS'] == '1'; print('pre-cv2 env OK')" &amp;&amp; uv run python -m home_cctv</automated>
  </verify>
  <done>
    - `uv sync` exits 0; `.venv/` exists; `uv.lock` committed
    - `uv run python -m home_cctv` prints `home_cctv v0.1.0 booted (phase0=False, mp4=None, show=False)` and exits 0
    - `uv run pytest tests/test_flags_builder.py -x -q` reports 6 passed
    - `grep -q "OPENCV_FFMPEG_CAPTURE_OPTIONS" src/home_cctv/ingest/flags.py` exits 0
    - `grep -q "TF_USE_LEGACY_KERAS" src/home_cctv/__init__.py` exits 0
    - `grep -q "stimeout;5000000" src/home_cctv/ingest/flags.py` exits 0
    - `.env.example` committed; `.env` is in `.gitignore`
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Env/settings loader + disk/DrvFs/free-space guards + credential-masking rotating logger</name>
  <files>src/home_cctv/config/env.py, src/home_cctv/obs/logging_setup.py, src/home_cctv/__main__.py, tests/test_config_env.py, tests/test_logging_mask.py</files>
  <read_first>
    - src/home_cctv/ingest/flags.py (produced in Task 1)
    - src/home_cctv/__init__.py (produced in Task 1)
    - src/home_cctv/__main__.py (produced in Task 1 — this task wires settings+logging into it)
    - .planning/phases/00-environment-sanity/00-CONTEXT.md §"Logging Format", §"Storage paths" and per-camera exit criteria
    - .planning/research/PITFALLS.md §1.2 (FFmpeg backend), §2 Storage (DrvFs), §"Runner-ups" on credential leaking
    - .planning/research/SUMMARY.md §5 pitfall #2 (DB on ext4) and pitfall #5 (disk-space guard)
    - specs.md (`.env` schema), context.md (deployment topology), CLAUDE.md
  </read_first>
  <behavior>
    - Test: `load_settings()` with all required env vars present returns a `Settings` with the right types (IP str, port int, Path objects)
    - Test: `load_settings()` with DVR_IP missing raises a `ValueError` (or pydantic ValidationError) whose string contains the literal `DVR_IP` — before any cv2 or tensorflow import
    - Test: `assert_disk_space_ok(tmp_path, min_gb=999999)` raises `RuntimeError` whose message contains both "free" and "GB"
    - Test: `assert_not_drvfs(Path("/mnt/c/Users/foo"))` raises `RuntimeError` whose message contains "DrvFs" and "WSL2 ext4"
    - Test: `assert_not_drvfs(Path("/home/anything"))` does NOT raise
    - Test: `CredentialMaskFilter` transforms `"connecting to rtsp://admin:REDACTED@192.168.1.10:554/1"` → `"connecting to rtsp://admin:***@192.168.1.10:554/1"`
    - Test: Filter also masks `http://user:secret@host` → `http://user:***@host` (generic URL user:pass pattern)
    - Test: Filter attaches to RotatingFileHandler AND stderr StreamHandler, so nothing leaks via either path
    - Test: `configure_logging(tmp_log_dir)` writes lines containing the `[cam1:<name>]` prefix when called with `camera_id="cam1:exterior_red"`
    - Test: Log files are capped at 10 MB × 5 files (verify handler type + maxBytes + backupCount)
  </behavior>
  <action>
    1. Create `src/home_cctv/config/env.py`. Implements ENV-01, ENV-03, ENV-05. MUST NOT import cv2 or tensorflow. Per CONTEXT.md §"Storage paths" and SUMMARY.md §5 pitfall #2/#5:

    ```python
    """Environment and settings loader.

    Implements ENV-01 (start cleanly on WSL2+py3.11), ENV-03 (creds from .env only),
    ENV-05 (clear startup errors: missing env, <1 GB free, DrvFs path).
    """
    from __future__ import annotations
    import os
    import subprocess
    from pathlib import Path
    from typing import Optional

    from dotenv import load_dotenv
    from pydantic import Field, field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _MIN_FREE_GB: float = 1.0
    _DRVFS_PREFIXES: tuple[str, ...] = ("/mnt/c/", "/mnt/d/", "/mnt/e/", "/mnt/f/", "/mnt/g/")

    class Settings(BaseSettings):
        model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

        DVR_IP: str
        DVR_PORT: int = 554
        DVR_USER: str
        DVR_PASS: str
        EVENT_IMAGE_DIR: Path
        DB_PATH: Path
        LOG_DIR: Path = Path("logs")
        MODEL_CACHE_DIR: Path = Path.home() / ".cache/home_cctv/models"

        @field_validator("DVR_PORT")
        @classmethod
        def _port_range(cls, v: int) -> int:
            if not (1 <= v <= 65535):
                raise ValueError(f"DVR_PORT out of range: {v}")
            return v

        def masked_rtsp_base(self) -> str:
            """Return rtsp://user:***@ip:port — for logging."""
            return f"rtsp://{self.DVR_USER}:***@{self.DVR_IP}:{self.DVR_PORT}"

    def load_settings(env_file: Optional[Path] = None) -> Settings:
        """Load settings, raising a clear error naming the missing var if any."""
        if env_file is not None:
            load_dotenv(env_file, override=False)
        try:
            return Settings()
        except Exception as exc:  # pydantic ValidationError subclasses Exception
            # Re-raise with the offending field name up front so the user sees it first.
            msg = str(exc)
            raise RuntimeError(
                f"Failed to load .env settings. Fix .env then retry.\n"
                f"Details: {msg}\n"
                f"Template: .env.example"
            ) from exc

    def _detect_filesystem(path: Path) -> str:
        """Best-effort filesystem type of the mount containing `path`."""
        try:
            out = subprocess.run(
                ["stat", "-f", "-c", "%T", str(path)],
                capture_output=True, text=True, check=False, timeout=2,
            )
            fstype = out.stdout.strip() or "unknown"
            return fstype
        except Exception:
            return "unknown"

    def assert_not_drvfs(path: Path) -> None:
        """Refuse to run if EVENT_IMAGE_DIR / DB_PATH is on Windows DrvFs mount.

        DrvFs kills SQLite WAL (PITFALLS §2) and quietly corrupts the DB. We also
        scream on the mount-point prefix as a belt-and-braces check in case stat
        isn't available.
        """
        p = path.expanduser().resolve()
        prefix_hit = any(str(p).startswith(pfx) for pfx in _DRVFS_PREFIXES)
        fstype = _detect_filesystem(p if p.exists() else p.parent)
        if prefix_hit or fstype in {"drvfs", "9p"}:
            raise RuntimeError(
                f"Refusing to use {p} — it appears to live on Windows DrvFs "
                f"(fs type: {fstype}). SQLite WAL is unsafe on DrvFs. "
                f"Move EVENT_IMAGE_DIR/DB_PATH onto WSL2 ext4 (e.g. /home/<user>/...)."
            )

    def assert_disk_space_ok(path: Path, min_gb: float = _MIN_FREE_GB) -> None:
        p = path.expanduser().resolve()
        target = p if p.exists() else p.parent
        target.mkdir(parents=True, exist_ok=True)
        stat = os.statvfs(target)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb < min_gb:
            raise RuntimeError(
                f"Insufficient free space on {target}: {free_gb:.2f} GB free, "
                f"need at least {min_gb:.2f} GB. Free space before starting."
            )

    def validate_runtime_paths(settings: Settings) -> None:
        """Run all startup asserts against the loaded Settings."""
        for p in (settings.EVENT_IMAGE_DIR, settings.DB_PATH, settings.MODEL_CACHE_DIR):
            assert_not_drvfs(p)
        assert_disk_space_ok(settings.EVENT_IMAGE_DIR, _MIN_FREE_GB)
    ```

    2. Create `src/home_cctv/obs/logging_setup.py` — rotating file + credential-mask filter per CONTEXT.md §"Logging Format":

    ```python
    """Logging setup with credential masking and per-camera prefix."""
    from __future__ import annotations
    import logging
    import re
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    # Matches user:pass inside any scheme URL. Replace the password with ***.
    _CRED_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/@\s]+):[^@\s]+@")

    class CredentialMaskFilter(logging.Filter):
        """Masks passwords in `scheme://user:pass@host` URLs in log records.

        Applied to BOTH the record.msg and each arg. Covers rtsp://, http://, etc.
        """
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                record.msg = _CRED_RE.sub(r"\g<scheme>:***@", str(record.msg))
                if record.args:
                    masked_args = tuple(
                        _CRED_RE.sub(r"\g<scheme>:***@", str(a)) if isinstance(a, str) else a
                        for a in record.args
                    )
                    record.args = masked_args
            except Exception:
                pass
            return True

    # Per CONTEXT.md §"Logging Format", every line MUST carry a bracketed
    # `[cam1:<name>]` (or `[main]`) prefix. We inject camera_id via a
    # LoggerAdapter → extra={"camera_id": ...} and the formatter renders it.
    _FMT = "%(asctime)sZ %(levelname)-5s [%(camera_id)s] %(message)s"
    _DATEFMT = "%Y-%m-%dT%H:%M:%S"

    class _CameraIdDefault(logging.Filter):
        """Ensures every LogRecord has a `camera_id` attribute so the formatter
        never KeyErrors when a caller uses the raw logger instead of the adapter.
        Defaults to `main` when unset."""
        def filter(self, record: logging.LogRecord) -> bool:
            if not hasattr(record, "camera_id"):
                record.camera_id = "main"
            return True

    def configure_logging(log_dir: Path, camera_id: str | None = None) -> logging.LoggerAdapter:
        """Configure root logger with RotatingFileHandler + stderr handler.

        Both handlers get the CredentialMaskFilter, so nothing leaks via either path.
        """
        log_dir = Path(log_dir).expanduser().resolve()
        log_dir.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("home_cctv")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        effective_camera_id = camera_id or "main"
        adapter = logging.LoggerAdapter(logger, {"camera_id": effective_camera_id})

        if logger.handlers:
            return adapter  # already configured — just return a fresh adapter bound to this camera

        fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)
        mask = CredentialMaskFilter()
        default_cam = _CameraIdDefault()

        fh = RotatingFileHandler(
            log_dir / "home_cctv.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.addFilter(default_cam)
        fh.addFilter(mask)
        logger.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(default_cam)
        sh.addFilter(mask)
        logger.addHandler(sh)

        return adapter
    ```

    3. Wire the new loader + logger into `src/home_cctv/__main__.py`. Replace the stub `main()` with:

    ```python
    def main(argv: list[str] | None = None) -> int:
        args = build_parser().parse_args(argv)

        from home_cctv.config.env import load_settings, validate_runtime_paths
        from home_cctv.obs.logging_setup import configure_logging

        try:
            settings = load_settings()
            validate_runtime_paths(settings)
        except RuntimeError as exc:
            print(f"STARTUP ERROR: {exc}", file=sys.stderr)
            return 2

        logger = configure_logging(settings.LOG_DIR)
        logger.info(
            "booted version=%s phase0=%s mp4=%s show=%s dvr=%s event_dir=%s",
            home_cctv.__version__, args.phase0, args.mp4, args.show,
            settings.masked_rtsp_base(), settings.EVENT_IMAGE_DIR,
        )
        # Plan 01 stub; Plan 03 replaces with Phase 0 harness call.
        return 0
    ```

    4. Create `tests/test_config_env.py`:

    ```python
    import os
    from pathlib import Path
    import pytest

    from home_cctv.config.env import (
        Settings, load_settings, assert_disk_space_ok, assert_not_drvfs,
    )

    @pytest.fixture
    def good_env(tmp_path, monkeypatch):
        d = tmp_path / "img"; d.mkdir()
        db = tmp_path / "events.db"
        mc = tmp_path / "models"; mc.mkdir()
        lg = tmp_path / "logs"; lg.mkdir()
        monkeypatch.setenv("DVR_IP", "192.168.1.10")
        monkeypatch.setenv("DVR_PORT", "554")
        monkeypatch.setenv("DVR_USER", "admin")
        monkeypatch.setenv("DVR_PASS", "secret")
        monkeypatch.setenv("EVENT_IMAGE_DIR", str(d))
        monkeypatch.setenv("DB_PATH", str(db))
        monkeypatch.setenv("LOG_DIR", str(lg))
        monkeypatch.setenv("MODEL_CACHE_DIR", str(mc))
        return tmp_path

    def test_load_settings_happy(good_env):
        s = load_settings()
        assert s.DVR_IP == "192.168.1.10"
        assert s.DVR_PORT == 554
        assert s.DVR_USER == "admin"
        assert s.DVR_PASS == "secret"
        assert s.masked_rtsp_base() == "rtsp://admin:***@192.168.1.10:554"
        assert "secret" not in s.masked_rtsp_base()

    def test_missing_dvr_ip_raises_named_error(good_env, monkeypatch):
        monkeypatch.delenv("DVR_IP", raising=False)
        # Also block .env fallback by pointing cwd somewhere empty.
        monkeypatch.chdir(good_env)
        with pytest.raises(RuntimeError) as ei:
            load_settings()
        assert "DVR_IP" in str(ei.value)

    def test_disk_space_too_small_raises(tmp_path):
        with pytest.raises(RuntimeError, match=r"free.*GB|GB.*free"):
            assert_disk_space_ok(tmp_path, min_gb=9_999_999.0)

    def test_drvfs_prefix_rejected():
        with pytest.raises(RuntimeError, match="DrvFs"):
            assert_not_drvfs(Path("/mnt/c/Users/anyone/data"))

    def test_ext4_home_path_accepted(tmp_path):
        # tmp_path on WSL2 ext4 is fine; no raise
        assert_not_drvfs(tmp_path)

    def test_dvr_pass_not_in_masked_base(good_env):
        s = load_settings()
        assert "secret" not in s.masked_rtsp_base()
        assert "***" in s.masked_rtsp_base()
    ```

    5. Create `tests/test_logging_mask.py`. Note: `configure_logging` returns a
       `logging.LoggerAdapter`; use `adapter.logger.handlers` to access handlers.

    ```python
    import logging
    from pathlib import Path
    from home_cctv.obs.logging_setup import configure_logging, CredentialMaskFilter

    def _reset_logger():
        # Ensure a clean root logger between tests so handler dedup logic doesn't hide bugs
        lg = logging.getLogger("home_cctv")
        for h in list(lg.handlers):
            lg.removeHandler(h)

    def test_rtsp_password_masked_in_record():
        f = CredentialMaskFilter()
        rec = logging.LogRecord(
            "x", logging.INFO, "p", 1,
            "connecting to rtsp://admin:REDACTED@192.168.1.10:554/1", (), None,
        )
        f.filter(rec)
        assert "REDACTED" not in rec.msg
        assert "rtsp://admin:***@192.168.1.10:554/1" in rec.msg

    def test_http_basic_auth_masked():
        f = CredentialMaskFilter()
        rec = logging.LogRecord("x", logging.INFO, "p", 1, "GET http://user:secret@host/x", (), None)
        f.filter(rec)
        assert "secret" not in rec.msg
        assert "http://user:***@host/x" in rec.msg

    def test_handlers_get_filter(tmp_path):
        _reset_logger()
        adapter = configure_logging(tmp_path)
        handlers = adapter.logger.handlers
        assert handlers, "expected at least one handler"
        for h in handlers:
            assert any(isinstance(fl, CredentialMaskFilter) for fl in h.filters), (
                f"handler {h} missing CredentialMaskFilter — credentials could leak"
            )

    def test_rotating_file_handler_sized_10mb_x5(tmp_path):
        _reset_logger()
        from logging.handlers import RotatingFileHandler
        adapter = configure_logging(tmp_path)
        rfhs = [h for h in adapter.logger.handlers if isinstance(h, RotatingFileHandler)]
        assert rfhs, "expected a RotatingFileHandler"
        h = rfhs[0]
        assert h.maxBytes == 10 * 1024 * 1024
        assert h.backupCount == 5

    def test_integration_password_never_written_to_file(tmp_path):
        _reset_logger()
        adapter = configure_logging(tmp_path)
        adapter.info("dialing rtsp://admin:REDACTED@192.168.1.10:554/11 now")
        for h in adapter.logger.handlers:
            h.flush()
        log_file = Path(tmp_path).resolve() / "home_cctv.log"
        text = log_file.read_text(encoding="utf-8")
        assert "REDACTED" not in text
        assert "***" in text

    def test_bracketed_camera_prefix_rendered(tmp_path):
        """CONTEXT.md §Logging Format locks `[cam1:exterior_red]` bracketed prefix."""
        _reset_logger()
        adapter = configure_logging(tmp_path, camera_id="cam1:exterior_red")
        adapter.info("frame_ok size=1280x720 fps=24.1 frame_idx=1032")
        for h in adapter.logger.handlers:
            h.flush()
        log_file = Path(tmp_path).resolve() / "home_cctv.log"
        text = log_file.read_text(encoding="utf-8")
        assert "[cam1:exterior_red]" in text, f"missing bracketed prefix in: {text!r}"
        assert "frame_ok" in text

    def test_default_main_prefix_when_no_camera_id(tmp_path):
        """With no camera_id, the formatter falls back to `[main]`."""
        _reset_logger()
        adapter = configure_logging(tmp_path)
        adapter.info("booted")
        for h in adapter.logger.handlers:
            h.flush()
        text = (Path(tmp_path).resolve() / "home_cctv.log").read_text(encoding="utf-8")
        assert "[main]" in text
    ```

    6. Run `uv run pytest tests/ -x -q` — all tests must pass.

    7. Verify end-to-end:
    - Create a temp `.env` with valid values pointing at `/tmp/...` paths
    - `uv run python -m home_cctv` exits 0 and prints a masked banner
    - `mv .env .env.bak && uv run python -m home_cctv; mv .env.bak .env` shows `STARTUP ERROR` mentioning `DVR_IP` or similar and exit code 2
  </action>
  <verify>
    <automated>uv run pytest tests/ -x -q &amp;&amp; uv run python -c "from home_cctv.config.env import assert_not_drvfs; from pathlib import Path; import pytest;
try:
    assert_not_drvfs(Path('/mnt/c/tmp'))
    raise SystemExit('FAIL: drvfs not detected')
except RuntimeError:
    print('drvfs guard OK')"</automated>
  </verify>
  <done>
    - All tests in `tests/test_config_env.py` and `tests/test_logging_mask.py` pass
    - `uv run python -m home_cctv` with a valid `.env` exits 0 and logs a `rtsp://admin:***@...` banner (not the real password)
    - `uv run python -m home_cctv` with `.env` deleted prints `STARTUP ERROR` naming `DVR_IP` and exits with code 2
    - `grep -q "CredentialMaskFilter" src/home_cctv/obs/logging_setup.py` exits 0
    - `grep -q "LoggerAdapter" src/home_cctv/obs/logging_setup.py` exits 0 (bracketed `[cam1:<name>]` prefix injected via adapter, not name-based)
    - `grep -q "%(camera_id)s" src/home_cctv/obs/logging_setup.py` exits 0
    - `grep -q "_DRVFS_PREFIXES" src/home_cctv/config/env.py` exits 0
    - `grep -q "assert_disk_space_ok" src/home_cctv/config/env.py` exits 0
    - No handler in the configured logger lacks `CredentialMaskFilter` (enforced by `test_handlers_get_filter`)
  </done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| filesystem → process | `.env` file contents flow into Settings (DVR creds) |
| process → log sink | Log records may contain RTSP URLs with plaintext passwords |
| filesystem path → SQLite | `EVENT_IMAGE_DIR` / `DB_PATH` can point at unsafe DrvFs |
| DVR network → process | (out of scope for this plan — Plans 02/03) |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-00-01 | Information Disclosure | RotatingFileHandler log files | mitigate | `CredentialMaskFilter` on every handler; integration test `test_integration_password_never_written_to_file` asserts the real password never lands in the log file |
| T-00-02 | Information Disclosure | stderr StreamHandler | mitigate | Same filter attached to stderr handler; `test_handlers_get_filter` enforces it for every handler the logger owns |
| T-00-03 | Information Disclosure | Traceback / repr(Settings) | mitigate | `DVR_PASS` is a plain `str` (not exposed via `masked_rtsp_base`); do NOT `print(settings)` in `__main__`; use explicit `masked_rtsp_base()` |
| T-00-04 | Tampering | SQLite DB on DrvFs (`/mnt/c/...`) | mitigate | `assert_not_drvfs` on EVENT_IMAGE_DIR, DB_PATH, MODEL_CACHE_DIR at startup — refuse to run (PITFALLS §2) |
| T-00-05 | Denial of Service | Disk fills → pipeline writes partial images | mitigate | `assert_disk_space_ok(..., min_gb=1.0)` at startup refuses to run under 1 GB free |
| T-00-06 | Spoofing | `.env` committed to git by accident | mitigate | `.env` in `.gitignore`; `.env.example` only contains placeholders; no real creds ever in source |
| T-00-07 | Information Disclosure | `argparse --help` / error output | accept | argparse shows flag names only, no secrets — low risk |
| T-00-08 | Elevation of Privilege | Malicious .env injection via env vars | accept | Single-user WSL2 host; threat model assumes local trust; documented in CONTEXT.md §"Security posture" |
</threat_model>

<verification>
- `uv sync` creates `.venv` with all pinned deps from SUMMARY.md §2
- `uv run pytest tests/ -x -q` reports all tests passed
- `uv run python -m home_cctv` with valid `.env` exits 0 and prints a masked banner
- `uv run python -m home_cctv` with missing DVR_IP exits 2 and names the missing variable
- `grep -rn "REDACTED\|DVR_PASS" logs/` returns no hits after a logged run
- `python -c "import home_cctv; import os; assert 'OPENCV_FFMPEG_CAPTURE_OPTIONS' in os.environ"` exits 0
</verification>

<success_criteria>
Plan 01 is done when:
1. `uv sync` + `uv run python -m home_cctv` works end-to-end on the user's Windows 11 + WSL2 Ubuntu 24.04 + Python 3.11 host
2. Missing env var → clear startup error naming the variable (before any cv2 import)
3. `EVENT_IMAGE_DIR` on DrvFs or with <1 GB free → clear refuse-to-start error
4. No plaintext RTSP password can reach any configured log handler (integration test proves it)
5. `OPENCV_FFMPEG_CAPTURE_OPTIONS` is present in `os.environ` after `import home_cctv`, exactly matching the CONTEXT.md canonical string
6. `TF_USE_LEGACY_KERAS=1` is set before any TensorFlow import
7. All unit tests pass
</success_criteria>

<output>
After completion, create `.planning/phases/00-environment-sanity/00-01-SUMMARY.md` documenting:
- Package layout created
- Exact dep versions installed (from `uv.lock`)
- Tests passing
- Any deviations from CONTEXT.md
- Handoff notes to Plan 02 (what Settings + logger interface to import)
</output>
