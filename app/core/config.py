"""Environment-based configuration loaded from .env."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _app_root() -> Path:
    """Folder containing the .env at runtime.

    Frozen exe: directory of the exe.
    Dev: project root (parent of the app package).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    dvr_ip: str
    dvr_port: int
    dvr_user: str
    dvr_pass: str
    cam_defaults: tuple[str, str, str, str]

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(_app_root() / ".env", override=False)
        return cls(
            dvr_ip=os.environ.get("DVR_IP", "192.168.1.10"),
            dvr_port=int(os.environ.get("DVR_PORT", "554")),
            dvr_user=os.environ.get("DVR_USER", "admin"),
            dvr_pass=os.environ.get("DVR_PASS", ""),
            cam_defaults=(
                _norm(os.environ.get("CAM1_DEFAULT", "sub")),
                _norm(os.environ.get("CAM2_DEFAULT", "sub")),
                _norm(os.environ.get("CAM3_DEFAULT", "sub")),
                _norm(os.environ.get("CAM4_DEFAULT", "sub")),
            ),
        )


def _norm(value: str) -> str:
    v = value.strip().lower()
    return v if v in ("sub", "main") else "sub"
