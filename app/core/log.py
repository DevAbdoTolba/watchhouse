"""Process-wide log bus. Anything in the app calls `log.info/warn/error`
and the console panel receives the entry via Qt signal."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QObject, Signal

Level = Literal["DEBUG", "INFO", "WARN", "ERROR"]


@dataclass(frozen=True)
class LogEntry:
    timestamp: float
    level: Level
    source: str
    message: str


class _LogBus(QObject):
    entry = Signal(LogEntry)

    def __init__(self) -> None:
        super().__init__()
        self._history: list[LogEntry] = []
        self._max_history = 5000

    # Replay so a late-attached console can backfill
    def history(self) -> list[LogEntry]:
        return list(self._history)

    def post(self, level: Level, source: str, message: str) -> None:
        entry = LogEntry(time.time(), level, source, message)
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]
        self.entry.emit(entry)

    def debug(self, source: str, message: str) -> None: self.post("DEBUG", source, message)
    def info(self, source: str, message: str) -> None:  self.post("INFO", source, message)
    def warn(self, source: str, message: str) -> None:  self.post("WARN", source, message)
    def error(self, source: str, message: str) -> None: self.post("ERROR", source, message)


bus = _LogBus()


def mask_url(url: str) -> str:
    """Redact the password portion of an rtsp://user:pass@host URL."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return url
