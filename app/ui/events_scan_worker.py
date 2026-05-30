"""Threaded event scan: keeps scan_events (reads every event.json) off the UI.

scan_events walks the recordings/events tree and parses a JSON sidecar per
event, which can take a while with many events. Running it on the UI thread
froze playback. This worker runs scan_events + group_sessions on a QThread and
returns the full, sorted (newest-first) session list via a signal; the model /
view just consume the already-built list.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from app.core.event_library import group_sessions, scan_events
from app.core.log import bus


class EventsScanWorker(QObject):
    """Scans events off the UI thread; emits the full session list when done."""

    finished = Signal(list, list)  # (events: list[EventRecord], sessions)

    def __init__(self, events_dir: Path) -> None:
        super().__init__()
        self._events_dir = events_dir

    @Slot()
    def run(self) -> None:
        events: list = []
        sessions: list = []
        try:
            events = scan_events(self._events_dir)
            sessions = group_sessions(events)
        except Exception as e:  # never let a bad scan kill the thread
            bus.warn("EVT", f"events scan failed: {e!s}")
            events, sessions = [], []
        self.finished.emit(events, sessions)
