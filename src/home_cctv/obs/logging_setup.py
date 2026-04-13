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


def configure_logging(
    log_dir: Path, camera_id: str | None = None
) -> logging.LoggerAdapter:
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
        # already configured — just return a fresh adapter bound to this camera
        return adapter

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
