"""Environment-based configuration loaded from .env.

Loader searches a chain of likely locations so the same code works both
in dev (project root) and when the standalone exe is dropped anywhere
on the user's machine:

    1. directory of the exe (or app package in dev)
    2. current working directory at launch
    3. parent of (1) up to 3 levels — covers `dist/exe` next to project

Each candidate is logged to the bus so the user can see which `.env`
won (or that none was found).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.core.log import bus


def _candidate_dirs() -> list[Path]:
    paths: list[Path] = []
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent)
    paths.append(Path.cwd())
    paths.append(Path(__file__).resolve().parents[2])  # project root in dev
    # one and two levels up from each — covers dist/CCTV.exe next to .env
    extra: list[Path] = []
    for p in paths:
        if p.parent != p:
            extra.append(p.parent)
        if p.parent.parent != p.parent:
            extra.append(p.parent.parent)
    paths.extend(extra)
    # de-dupe preserving order
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in paths:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _find_env() -> Path | None:
    for d in _candidate_dirs():
        candidate = d / ".env"
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class Settings:
    dvr_ip: str
    dvr_port: int
    dvr_user: str
    dvr_pass: str
    cam_defaults: tuple[str, str, str, str]
    env_path: Path | None

    @classmethod
    def load(cls) -> "Settings":
        env_path = _find_env()
        if env_path is not None:
            bus.info("CFG", f".env loaded from {env_path}")
            load_dotenv(env_path, override=False)
        else:
            tried = ", ".join(str(d) for d in _candidate_dirs())
            bus.warn("CFG", f"no .env found. Searched: {tried}")
            bus.warn("CFG", "DVR credentials will fall back to defaults; streams will likely fail.")

        s = cls(
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
            env_path=env_path,
        )
        bus.info(
            "CFG",
            f"settings: dvr={s.dvr_ip}:{s.dvr_port} user={s.dvr_user} "
            f"pass={'set' if s.dvr_pass else 'EMPTY'} defaults={s.cam_defaults}",
        )
        return s


def _norm(value: str) -> str:
    v = value.strip().lower()
    return v if v in ("sub", "main") else "sub"
