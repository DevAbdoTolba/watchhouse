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
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MIN_FREE_GB: float = 1.0
_DRVFS_PREFIXES: tuple[str, ...] = (
    "/mnt/c/",
    "/mnt/d/",
    "/mnt/e/",
    "/mnt/f/",
    "/mnt/g/",
)


_DEFAULT_DATA_ROOT: Path = Path.home() / "home_cctv"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Plan 00-03 handoff: the user's repo-level `.env` uses a legacy key
    # vocabulary (DVR_USERNAME / DVR_PASSWORD / DVR_LOCAL_IP /
    # DVR_LOCAL_RTSP_PORT). We accept both the canonical names and the legacy
    # aliases via ``AliasChoices`` so an existing `.env` works unmodified.
    DVR_IP: str = Field(
        validation_alias=AliasChoices("DVR_IP", "DVR_LOCAL_IP"),
    )
    DVR_PORT: int = Field(
        default=554,
        validation_alias=AliasChoices("DVR_PORT", "DVR_LOCAL_RTSP_PORT"),
    )
    DVR_USER: str = Field(
        validation_alias=AliasChoices("DVR_USER", "DVR_USERNAME"),
    )
    DVR_PASS: str = Field(
        validation_alias=AliasChoices("DVR_PASS", "DVR_PASSWORD"),
    )
    # Path fields default under $HOME/home_cctv/ so a legacy `.env` that only
    # ships DVR credentials still boots cleanly on WSL2 ext4. Users who set
    # the canonical keys explicitly continue to override.
    EVENT_IMAGE_DIR: Path = Field(
        default=_DEFAULT_DATA_ROOT / "data" / "event_images",
    )
    DB_PATH: Path = Field(
        default=_DEFAULT_DATA_ROOT / "data" / "cctv_events.db",
    )
    LOG_DIR: Path = Field(default=_DEFAULT_DATA_ROOT / "logs")
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
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
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
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
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
