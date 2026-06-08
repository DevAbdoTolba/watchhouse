"""Persisted map: Telegram message_id -> the event it represents.

This is what lets the bot answer replies. When we send an event's thumbnail
(or a clip) Telegram returns a message_id; we remember which event folder /
camera / kind that message was, so a user replying to it can be served the
matching video(s).

Stored as JSON next to the loaded .env, capped to the most recent entries, and
guarded by a lock: sends happen on worker threads (QThreadPool) while the
poller reads on its own QThread.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.core.log import bus

_FILENAME = ".cctv-telegram-map.json"
_DEFAULT_CAP = 5000  # how many recent sent messages stay replyable


class TelegramMap:
    """Thread-safe message_id -> {f: folder, c: cam, k: kind} store.

    kind is "thumb" (the photo we pushed) or "video" (a clip we sent). The
    poller uses kind to decide what a reply should fetch.
    """

    def __init__(self, env_path: Path | None, cap: int = _DEFAULT_CAP) -> None:
        base = env_path.parent if env_path else Path.cwd()
        self._path = base / _FILENAME
        self._cap = max(1, int(cap))
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {
                    str(k): v for k, v in raw.items() if isinstance(v, dict)
                }
        except (OSError, ValueError, TypeError) as e:
            bus.warn("TG", f"could not read telegram map: {e!s}")

    def _save_locked(self) -> None:
        try:
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
        except OSError as e:
            bus.warn("TG", f"could not write telegram map: {e!s}")

    def record(self, message_id: int | None, folder: str, cam: int, kind: str,
               t: float = 0.0, extra: dict | None = None) -> None:
        if not message_id:
            return
        with self._lock:
            entry = {"f": folder, "c": int(cam), "k": kind}
            if t:
                entry["t"] = float(t)  # epoch secs; live alerts use it to find the clip
            if extra:
                # A collision carries the SECOND event here (f2/c2) so its buttons
                # can serve both clips / all union angles.
                entry.update(extra)
            self._data[str(message_id)] = entry
            overflow = len(self._data) - self._cap
            if overflow > 0:  # drop oldest insertions (dict preserves order)
                for key in list(self._data.keys())[:overflow]:
                    self._data.pop(key, None)
            self._save_locked()

    def lookup(self, message_id: int) -> dict | None:
        with self._lock:
            entry = self._data.get(str(message_id))
            return dict(entry) if entry else None
