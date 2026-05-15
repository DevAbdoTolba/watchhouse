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
from dataclasses import dataclass, replace
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
    # Recording
    recording_enabled: bool
    recording_dir: Path
    recording_stream: str          # "sub" | "main"
    recording_segment_minutes: int  # default 30
    recording_retention_days: int   # default 7

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

        # Recording dir defaults to <env_dir>/recordings, or cwd/recordings in dev
        rec_dir_default = (env_path.parent if env_path else Path.cwd()) / "recordings"
        rec_dir = Path(os.environ.get("RECORDING_DIR", str(rec_dir_default)))

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
            recording_enabled=_truthy(os.environ.get("RECORDING_ENABLED", "1")),
            recording_dir=rec_dir,
            recording_stream=_norm(os.environ.get("RECORDING_STREAM", "sub")),
            recording_segment_minutes=int(os.environ.get("RECORDING_SEGMENT_MINUTES", "30")),
            recording_retention_days=int(os.environ.get("RECORDING_RETENTION_DAYS", "7")),
        )
        bus.info(
            "CFG",
            f"settings: dvr={s.dvr_ip}:{s.dvr_port} user={s.dvr_user} "
            f"pass={'set' if s.dvr_pass else 'EMPTY'} defaults={s.cam_defaults}",
        )
        bus.info(
            "CFG",
            f"recording: enabled={s.recording_enabled} stream={s.recording_stream} "
            f"segment={s.recording_segment_minutes}min retention={s.recording_retention_days}d "
            f"dir={s.recording_dir}",
        )
        return s


def _norm(value: str) -> str:
    v = value.strip().lower()
    return v if v in ("sub", "main") else "sub"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def with_dvr_ip(settings: "Settings", new_ip: str) -> "Settings":
    """Return a new Settings instance with `dvr_ip` swapped."""
    return replace(settings, dvr_ip=new_ip)


def persist_dvr_ip(settings: "Settings", new_ip: str) -> bool:
    """Write `DVR_IP=<new_ip>` into the .env that was loaded at startup.

    Returns True on success. If no .env was loaded (env_path is None) or
    the file is missing, returns False and logs the reason. Other lines
    in the file are preserved verbatim.
    """
    path = settings.env_path
    if path is None or not path.is_file():
        bus.warn("CFG", f"cannot persist DVR_IP; no writable .env (env_path={path})")
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        bus.error("CFG", f"failed to read .env for update: {e!s}")
        return False
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _ = stripped.split("=", 1)
        if key.strip() == "DVR_IP":
            lines[i] = f"DVR_IP={new_ip}"
            found = True
            break
    if not found:
        lines.append(f"DVR_IP={new_ip}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        bus.error("CFG", f"failed to write .env: {e!s}")
        return False
    bus.info("CFG", f"persisted DVR_IP={new_ip} to {path}")
    return True
