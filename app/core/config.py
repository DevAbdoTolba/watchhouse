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
    recording_segment_minutes: int    # default 3 (fast evidence/angle availability)
    recording_retention_minutes: int  # default 90 (1.5h sliding window)
    # Detection (v0.4+)
    detection_enabled: bool
    detection_conf: float
    detection_sample_seconds: float
    detection_model: Path
    detection_cameras: tuple[int, ...]  # cameras armed for events on startup
    # Event extraction (v0.4.2)
    event_extraction_enabled: bool
    event_pre_roll_s: float
    event_post_roll_s: float
    event_merge_gap_s: float
    event_min_hits: int
    # Cross-segment stitching (v0.4.19)
    event_max_duration_s: float    # force-split a continuous presence past this
    event_hold_timeout_s: float    # flush a held (open) event after this wall time
    events_dir: Path
    # Telegram push notifications (v0.4.13). Off unless both are set.
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_min_interval_s: float
    telegram_commands: bool        # interactive reply commands (poll getUpdates)
    telegram_map_cap: int          # how many recent sent messages stay replyable
    telegram_lang: str             # message language: "en" | "ar"
    # Real-time alert tier (live preview frames; instant, separate from segments)
    live_conf: float
    live_cooldown_s: float
    # Instant quick-clip (pre-event ring buffer): a ~30s clip from buffered live
    # frames, saved + pushed in seconds instead of waiting for a segment.
    live_quick_clip: bool
    live_pre_roll_s: float
    live_post_roll_s: float
    live_autosend_clip: bool       # push the quick clip to Telegram when ready
    # Presence-aware notifications (v0.4.20): send the recurring "still present"
    # pings. Off keeps only arrival + cleared. On by default (user wants pings).
    event_notify_ongoing: bool

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
            recording_segment_minutes=int(os.environ.get("RECORDING_SEGMENT_MINUTES", "3")),
            recording_retention_minutes=_retention_minutes(),
            detection_enabled=_truthy(os.environ.get("DETECTION_ENABLED", "1")),
            detection_conf=float(os.environ.get("DETECTION_CONF", "0.35")),
            detection_sample_seconds=float(os.environ.get("DETECTION_SAMPLE_SECONDS", "1.0")),
            detection_model=_resolve_model_path(os.environ.get("DETECTION_MODEL")),
            detection_cameras=_detection_cameras(),
            event_extraction_enabled=_truthy(os.environ.get("EVENT_EXTRACTION_ENABLED", "1")),
            event_pre_roll_s=float(os.environ.get("EVENT_PRE_ROLL_SECONDS", "5")),
            event_post_roll_s=float(os.environ.get("EVENT_POST_ROLL_SECONDS", "5")),
            event_merge_gap_s=float(os.environ.get("EVENT_MERGE_GAP_SECONDS", "5")),
            event_min_hits=max(1, int(os.environ.get("EVENT_MIN_HITS", "2"))),
            event_max_duration_s=float(os.environ.get("EVENT_MAX_DURATION_SECONDS", "120")),
            event_hold_timeout_s=_event_hold_timeout_s(),
            events_dir=Path(os.environ.get("EVENT_DIR", str(rec_dir / "events"))),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            telegram_min_interval_s=float(os.environ.get("TELEGRAM_MIN_INTERVAL_SECONDS", "20")),
            telegram_commands=_truthy(os.environ.get("TELEGRAM_COMMANDS", "1")),
            telegram_map_cap=max(1, int(os.environ.get("TELEGRAM_MAP_CAP", "5000"))),
            telegram_lang=os.environ.get("TELEGRAM_LANG", "en").strip().lower(),
            live_conf=float(os.environ.get("LIVE_CONF", "0.40")),
            live_cooldown_s=float(os.environ.get("LIVE_COOLDOWN_SECONDS", "45")),
            live_quick_clip=_truthy(os.environ.get("LIVE_QUICK_CLIP", "0")),
            live_pre_roll_s=float(os.environ.get("LIVE_PRE_ROLL_SECONDS", "2")),
            live_post_roll_s=float(os.environ.get("LIVE_POST_ROLL_SECONDS", "8")),
            live_autosend_clip=_truthy(os.environ.get("LIVE_AUTOSEND_CLIP", "0")),
            event_notify_ongoing=_truthy(os.environ.get("EVENT_NOTIFY_ONGOING", "1")),
        )
        bus.info(
            "CFG",
            f"settings: dvr={s.dvr_ip}:{s.dvr_port} user={s.dvr_user} "
            f"pass={'set' if s.dvr_pass else 'EMPTY'} defaults={s.cam_defaults}",
        )
        bus.info(
            "CFG",
            f"recording: enabled={s.recording_enabled} stream={s.recording_stream} "
            f"segment={s.recording_segment_minutes}min "
            f"retention={_fmt_minutes(s.recording_retention_minutes)} "
            f"dir={s.recording_dir}",
        )
        bus.info(
            "CFG",
            f"detection: enabled={s.detection_enabled} conf={s.detection_conf:g} "
            f"sample={s.detection_sample_seconds:g}s "
            f"model={'found' if s.detection_model.is_file() else 'MISSING'} "
            f"({s.detection_model.name}) armed_cams={s.detection_cameras}",
        )
        bus.info(
            "CFG",
            f"events: enabled={s.event_extraction_enabled} "
            f"pre={s.event_pre_roll_s:g}s post={s.event_post_roll_s:g}s "
            f"merge_gap={s.event_merge_gap_s:g}s min_hits={s.event_min_hits} "
            f"max_dur={s.event_max_duration_s:g}s hold={s.event_hold_timeout_s:g}s "
            f"dir={s.events_dir}",
        )
        bus.info(
            "CFG",
            f"telegram: {'configured' if (s.telegram_bot_token and s.telegram_chat_id) else 'not set'} "
            f"(min_interval={s.telegram_min_interval_s:g}s notify_ongoing={s.event_notify_ongoing} "
            f"commands={s.telegram_commands})",
        )
        return s


def _resolve_model_path(override: str | None) -> Path:
    """Locate yolov8n.onnx. Order: explicit override, a models/ folder next
    to the exe (user-droppable), the PyInstaller bundle, then the dev tree."""
    if override:
        return Path(override)
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "models" / "yolov8n.onnx")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "app" / "resources" / "models" / "yolov8n.onnx")
    candidates.append(Path(__file__).resolve().parents[1] / "resources" / "models" / "yolov8n.onnx")
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]  # report the most likely intended location even if missing


def _detection_cameras() -> tuple[int, ...]:
    """Cameras armed for event detection on startup. `DETECTION_CAMERAS` is a
    comma list of camera numbers (e.g. "2,4" for inside-only); unset = all."""
    raw = os.environ.get("DETECTION_CAMERAS")
    if not raw:
        return (1, 2, 3, 4)
    out = [int(p) for p in (s.strip() for s in raw.split(",")) if p.isdigit()]
    return tuple(out) if out else (1, 2, 3, 4)


def _retention_minutes() -> int:
    """Read retention from env. MINUTES takes precedence; DAYS is the legacy
    fallback for users who still have `RECORDING_RETENTION_DAYS=` in `.env`."""
    if (mins := os.environ.get("RECORDING_RETENTION_MINUTES")) is not None:
        return max(1, int(mins))
    if (days := os.environ.get("RECORDING_RETENTION_DAYS")) is not None:
        return max(1, int(days)) * 1440
    return 90  # default: 90-minute sliding window


def _event_hold_timeout_s() -> float:
    """How long a held (still-open) event waits for its contiguous next segment
    before being flushed on its own. Default = 2x the segment length (so a
    normal rollover always arrives first), floored at 5 minutes. Explicit
    EVENT_HOLD_TIMEOUT_SECONDS overrides. Kept well under retention so a held
    event never references an already-pruned segment."""
    if (raw := os.environ.get("EVENT_HOLD_TIMEOUT_SECONDS")) is not None:
        return max(60.0, float(raw))
    # No explicit override: the hold tracks segment length (2x, so the next
    # rollover always lands first). Change RECORDING_SEGMENT_MINUTES and this
    # follows automatically; set EVENT_HOLD_TIMEOUT_SECONDS to decouple them.
    seg_min = int(os.environ.get("RECORDING_SEGMENT_MINUTES", "3"))
    hold = max(5.0, 2 * seg_min) * 60.0
    retention_s = _retention_minutes() * 60.0
    # Never let a hold outlive the rolling buffer; leave a one-segment margin.
    return min(hold, max(60.0, retention_s - seg_min * 60.0))


def _fmt_minutes(m: int) -> str:
    if m >= 1440 and m % 1440 == 0:
        return f"{m // 1440}d"
    if m >= 60:
        h = m / 60.0
        return f"{h:g}h"
    return f"{m}min"


def _norm(value: str) -> str:
    v = value.strip().lower()
    return v if v in ("sub", "main") else "sub"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def with_dvr_ip(settings: "Settings", new_ip: str) -> "Settings":
    """Return a new Settings instance with `dvr_ip` swapped."""
    return replace(settings, dvr_ip=new_ip)


def _default_env_path() -> Path:
    """Where to create a brand-new .env when none was loaded at startup: next to
    the exe when frozen, else the project root in dev."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path(__file__).resolve().parents[2] / ".env"


def persist_env_values(settings: "Settings", values: dict[str, str]) -> bool:
    """Update (or append) one or more KEY=value lines in the loaded .env,
    preserving every other line verbatim. Creates the .env at a sensible
    default location if none was loaded yet. Returns True on success.

    Secrets in `values` are written as-is to the (gitignored) .env but never
    logged here — callers log a masked summary if needed.
    """
    path = settings.env_path
    if path is None:
        path = _default_env_path()
        bus.info("CFG", f"no .env was loaded; creating {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    except OSError as e:
        bus.error("CFG", f"failed to read .env for update: {e!s}")
        return False

    remaining = dict(values)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, val in remaining.items():  # keys not already present -> append
        lines.append(f"{key}={val}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        bus.error("CFG", f"failed to write .env: {e!s}")
        return False
    bus.info("CFG", f"persisted {', '.join(values)} to {path}")
    return True


def persist_dvr_ip(settings: "Settings", new_ip: str) -> bool:
    """Write `DVR_IP=<new_ip>` into the loaded .env. Thin wrapper around
    persist_env_values kept for existing callers."""
    return persist_env_values(settings, {"DVR_IP": new_ip})
