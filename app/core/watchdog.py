"""External watchdog: keeps Watchhouse alive across silent deaths (BUG-009).

The main app dies or its analyzer hangs from time to time and, with nothing
watching, stays dead until someone notices and relaunches it by hand. This
runs the SAME frozen exe in a separate, detached process (``Watchhouse.exe
--watchdog``) that the main app spawns at startup. Because it is its own
process it survives the app crashing, and because it relaunches from the
folder that holds ``.env`` it also guarantees the restarted app reloads the
right config (language included).

Liveness is judged by a heartbeat file the running app touches every few
seconds — this catches both a full crash (file stops updating, process gone)
and a hang (file stops updating, process lingers). On a stale heartbeat the
watchdog kills orphaned ffmpeg + the hung instance, relaunches the app, and
pings Telegram. A clean user quit writes a shutdown marker so the watchdog
steps aside instead of fighting the user.

State lives in the recording dir so both processes share it:
  .watchhouse.heartbeat        app liveness (mtime)
  .watchhouse-watchdog.json    {"enabled": bool} — the GUI toggle
  .watchhouse-watchdog.lock    watchdog liveness (mtime) — single-watchdog guard
  .watchhouse.shutdown         clean-quit marker (mtime)
  watchdog.log                 append-only diagnosis trail
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

_HEARTBEAT = ".watchhouse.heartbeat"
_STATE = ".watchhouse-watchdog.json"
_LOCK = ".watchhouse-watchdog.lock"
_SHUTDOWN = ".watchhouse.shutdown"
_LOGFILE = "watchdog.log"

_CHECK_INTERVAL_S = 20.0
_DEFAULT_IDLE_MIN = 2.0     # heartbeat older than this => app down/hung
_LOCK_STALE_S = 70.0        # another watchdog's lock older than this => it's gone
_RESTART_GRACE_S = 90.0     # after a relaunch, give the app this long to boot
_SHUTDOWN_FRESH_S = 180.0   # a clean-quit marker counts for this long
_MAX_RESTARTS = 5           # within the window before backing off
_RESTART_WINDOW_S = 1800.0  # 30 min
_BACKOFF_S = 300.0          # cooldown once crash-looping is detected

# Windows process-creation flags for a fully detached child.
_DETACHED = 0x00000008          # DETACHED_PROCESS
_NEW_GROUP = 0x00000200         # CREATE_NEW_PROCESS_GROUP
_NO_WINDOW = 0x08000000         # CREATE_NO_WINDOW


# ---- shared-state helpers (used by both the app and the watchdog) ----------

def _p(rec_dir: Path, name: str) -> Path:
    return Path(rec_dir) / name


def touch_heartbeat(rec_dir: Path) -> None:
    """Called frequently by the running app to prove it's alive."""
    try:
        _p(rec_dir, _HEARTBEAT).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _age(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def enabled(rec_dir: Path, default: bool = True) -> bool:
    """The GUI toggle, persisted so both processes agree across restarts."""
    try:
        data = json.loads(_p(rec_dir, _STATE).read_text(encoding="utf-8"))
        return bool(data.get("enabled", default))
    except (OSError, ValueError):
        return default


def seed_default(rec_dir: Path, default: bool) -> None:
    """Write the toggle state once, on first run, from the env default."""
    if not _p(rec_dir, _STATE).exists():
        set_enabled(rec_dir, default)


def set_enabled(rec_dir: Path, value: bool) -> None:
    try:
        _p(rec_dir, _STATE).write_text(
            json.dumps({"enabled": bool(value)}), encoding="utf-8")
    except OSError:
        pass


def mark_shutdown(rec_dir: Path) -> None:
    """The app calls this on a clean quit so the watchdog won't relaunch it."""
    try:
        _p(rec_dir, _SHUTDOWN).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _shutdown_fresh(rec_dir: Path) -> bool:
    age = _age(_p(rec_dir, _SHUTDOWN))
    return age is not None and age <= _SHUTDOWN_FRESH_S


def _watchdog_alive(rec_dir: Path) -> bool:
    age = _age(_p(rec_dir, _LOCK))
    return age is not None and age <= _LOCK_STALE_S


def _self_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "app"]


def _launch_cwd(settings) -> str | None:
    """Folder to launch from so the child reloads the right .env (and language)."""
    env_path = getattr(settings, "env_path", None)
    if env_path:
        return str(Path(env_path).parent)
    return None


def spawn_if_enabled(settings) -> bool:
    """Start a detached watchdog if the toggle is on and none is running.

    Safe to call repeatedly (e.g. from the heartbeat tick): it no-ops when a
    watchdog lock is fresh. Returns True if a watchdog was launched."""
    rec_dir = Path(settings.recording_dir)
    if not enabled(rec_dir):
        return False
    if _watchdog_alive(rec_dir):
        return False
    try:
        subprocess.Popen(
            _self_cmd() + ["--watchdog"],
            cwd=_launch_cwd(settings),
            creationflags=_DETACHED | _NEW_GROUP | _NO_WINDOW,
            close_fds=True,
        )
        return True
    except OSError:
        return False


# ---- the watchdog process --------------------------------------------------

def _log(rec_dir: Path, msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(_p(rec_dir, _LOGFILE), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _kill_orphans(rec_dir: Path) -> None:
    """Kill leftover ffmpeg (they keep recording over a dead analyzer and mask
    the death) and any Watchhouse instance that is NOT this watchdog."""
    me = os.getpid()
    if not getattr(sys, "frozen", False):
        # In dev the image is python.exe — don't kill the IDE/other tools.
        subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"],
                       capture_output=True)
        return
    image = Path(sys.executable).name  # Watchhouse.exe
    subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True)
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True).stdout
    except OSError:
        return
    for row in out.splitlines():
        parts = [c.strip('" ') for c in row.split('","')]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid != me:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)


def _relaunch(settings, rec_dir: Path) -> bool:
    try:
        subprocess.Popen(
            _self_cmd(),
            cwd=_launch_cwd(settings),
            creationflags=_DETACHED | _NEW_GROUP,
            close_fds=True,
        )
        return True
    except OSError as exc:
        _log(rec_dir, f"relaunch FAILED: {exc}")
        return False


def _alert(settings, rec_dir: Path, key: str, **kw) -> None:
    token = getattr(settings, "telegram_bot_token", "")
    chat = getattr(settings, "telegram_chat_id", "")
    if not (token and chat):
        return
    try:
        from app.core import telegram_text as tg
        from app.core.telegram_api import api_send_message
        api_send_message(token, chat, tg.t(settings.telegram_lang, key, **kw))
    except Exception as exc:  # noqa: BLE001 — never let an alert kill the loop
        _log(rec_dir, f"telegram alert failed: {exc}")


def run_watchdog() -> int:
    """Entry point for ``Watchhouse.exe --watchdog``. Blocks until the toggle
    is turned off or a clean shutdown is seen."""
    from app.core.config import Settings
    from app.core import dvr_time

    settings = Settings.load()
    rec_dir = Path(settings.recording_dir)
    rec_dir.mkdir(parents=True, exist_ok=True)
    dvr_time.set_offset_minutes(getattr(settings, "dvr_time_offset_minutes", 0))

    if _watchdog_alive(rec_dir):
        _log(rec_dir, "another watchdog is already running; exiting")
        return 0

    idle_min = float(getattr(settings, "watchdog_idle_minutes", _DEFAULT_IDLE_MIN))
    idle_s = max(60.0, idle_min * 60.0)
    _log(rec_dir, f"watchdog up (idle threshold {idle_s/60:.1f} min)")

    restarts: deque[float] = deque()
    grace_until = time.monotonic() + _RESTART_GRACE_S  # let a fresh app boot
    backoff_until = 0.0

    while True:
        _p(rec_dir, _LOCK).write_text(str(time.time()), encoding="utf-8")

        if _shutdown_fresh(rec_dir):
            _log(rec_dir, "clean shutdown seen; watchdog stepping aside")
            return 0
        if not enabled(rec_dir):
            _log(rec_dir, "watchdog disabled via toggle; exiting")
            return 0

        now = time.monotonic()
        if now < grace_until or now < backoff_until:
            time.sleep(_CHECK_INTERVAL_S)
            continue

        age = _age(_p(rec_dir, _HEARTBEAT))
        if age is None or age > idle_s:
            state = "no heartbeat" if age is None else f"heartbeat {age:.0f}s old"
            _log(rec_dir, f"app looks down ({state}) — recovering")

            # Crash-loop guard: too many restarts in the window => back off.
            while restarts and now - restarts[0] > _RESTART_WINDOW_S:
                restarts.popleft()
            if len(restarts) >= _MAX_RESTARTS:
                _log(rec_dir, "crash-looping; backing off, no restart this cycle")
                _alert(settings, rec_dir, "wd_looping",
                       n=len(restarts), mins=int(_RESTART_WINDOW_S / 60))
                backoff_until = now + _BACKOFF_S
                restarts.clear()
                time.sleep(_CHECK_INTERVAL_S)
                continue

            _kill_orphans(rec_dir)
            if _relaunch(settings, rec_dir):
                restarts.append(now)
                grace_until = time.monotonic() + _RESTART_GRACE_S
                _alert(settings, rec_dir, "wd_restarted",
                       when=f"{dvr_time.now():%H:%M}")
                _log(rec_dir, "relaunched Watchhouse")

        time.sleep(_CHECK_INTERVAL_S)
