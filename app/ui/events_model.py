"""Virtualized model + lazy thumbnail loader for the playback Events list.

The Events list used to eagerly build one QListWidgetItem with a decoded
thumbnail QIcon for every session, on the UI thread — which lagged badly with
many events. This module provides:

  - `ThumbnailLoader`: a QThreadPool-backed loader that decodes a thumbnail
    (at icon size) off the UI thread, caches it in a bounded LRU, and signals
    when a path is ready. Only paths the view actually asks for are decoded.

  - `EventsModel`: a QAbstractListModel over the FILTERED, day-scoped list of
    EventSession. It reveals an initial page and grows via canFetchMore /
    fetchMore as the QListView scrolls (Twitter-style infinite scroll), so the
    view only ever realizes a window of rows. DecorationRole pulls from the
    loader's cache and kicks off a background decode on a miss — so scrolling a
    list of thousands only decodes the dozen thumbnails on screen.

Filters never re-scan disk: set_sessions() installs the already-built (sorted)
list, and the view rebuilds the model window in memory.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QPixmap

ICON_SIZE = QSize(112, 63)
PAGE_SIZE = 50
_CACHE_CAP = 400  # bounded LRU of decoded thumbnails


class _ThumbSignals(QObject):
    # Emitted from a worker thread; carries (path key, decoded pixmap). The
    # cross-thread queued connection delivers it on the loader's (UI) thread,
    # so the cache is only ever mutated there.
    done = Signal(str, QPixmap)


class _ThumbTask(QRunnable):
    """Decodes one thumbnail at icon size on a pool thread, then hands the
    result back via a queued signal (no shared state touched here)."""

    def __init__(self, key: str, signals: _ThumbSignals) -> None:
        super().__init__()
        self._key = key
        self._signals = signals

    def run(self) -> None:  # noqa: D401 (Qt)
        pm = QPixmap(self._key)
        if not pm.isNull():
            pm = pm.scaled(
                ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._signals.done.emit(self._key, pm)


class ThumbnailLoader(QObject):
    """Bounded-LRU thumbnail cache with async, deduplicated decoding.

    `pixmap(path)` returns a cached QPixmap or None; on a miss it schedules a
    background decode (once per path) and emits `ready(path)` when done so the
    model can re-emit dataChanged for the affected row(s)."""

    ready = Signal(str)  # path key that just became available

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._pending: set[str] = set()
        self._pool = QThreadPool.globalInstance()
        self._signals = _ThumbSignals()
        # Queued (default cross-thread) connection: _on_done runs on this
        # object's thread, so the cache is only ever mutated on the UI thread.
        self._signals.done.connect(self._on_done)

    def _on_done(self, key: str, pm: QPixmap) -> None:
        self._cache[key] = pm
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_CAP:
            self._cache.popitem(last=False)
        self._pending.discard(key)
        self.ready.emit(key)

    def pixmap(self, path: str | Path | None) -> QPixmap | None:
        """Return the cached pixmap, or None (scheduling a decode on a miss)."""
        if not path:
            return None
        key = str(path)
        pm = self._cache.get(key)
        if pm is not None:
            self._cache.move_to_end(key)
            return pm if not pm.isNull() else None
        if key not in self._pending:
            self._pending.add(key)
            self._pool.start(_ThumbTask(key, self._signals))
        return None

    # --- test/inspection helpers ---
    def cache_size(self) -> int:
        return len(self._cache)


class EventsModel(QAbstractListModel):
    """List model over the day-scoped, filtered EventSession list.

    Holds the full filtered list (`_all`) but only exposes the first
    `_loaded` rows; fetchMore reveals the next page. DisplayRole renders the
    same two-line label the old QListWidget built; DecorationRole pulls from
    the ThumbnailLoader (lazy + cached + async)."""

    def __init__(self, loader: ThumbnailLoader, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._all: list = []
        self._loaded = 0
        self._labels: dict[int, str] = {}  # trigger_cam -> display name
        self._loader = loader
        self._loader.ready.connect(self._on_thumb_ready)
        # path key -> set of currently-visible row ids awaiting that thumb.
        self._rows_for_path: dict[str, set[int]] = {}

    # --- public wiring ---

    def set_camera_labels(self, labels: dict[int, str]) -> None:
        self._labels = dict(labels)

    def set_sessions(self, sessions: list) -> None:
        """Install a new filtered list and reset the revealed window to one
        page. No disk access — purely in memory."""
        self.beginResetModel()
        self._all = list(sessions)
        self._loaded = min(PAGE_SIZE, len(self._all))
        self._rows_for_path.clear()
        self.endResetModel()

    def session_at(self, index: QModelIndex):
        if not index.isValid():
            return None
        row = index.row()
        if 0 <= row < len(self._all):
            return self._all[row]
        return None

    def total_count(self) -> int:
        return len(self._all)

    # --- QAbstractListModel ---

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return self._loaded

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: N802
        if parent.isValid():
            return False
        return self._loaded < len(self._all)

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:  # noqa: N802
        if parent.isValid():
            return
        remaining = len(self._all) - self._loaded
        if remaining <= 0:
            return
        take = min(PAGE_SIZE, remaining)
        self.beginInsertRows(QModelIndex(), self._loaded, self._loaded + take - 1)
        self._loaded += take
        self.endInsertRows()

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= self._loaded:
            return None
        s = self._all[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._label_for(s)
        if role == Qt.ItemDataRole.DecorationRole:
            thumb = getattr(s, "thumb", None)
            if not thumb:
                return None
            key = str(thumb)
            pm = self._loader.pixmap(key)
            if pm is not None:
                return pm
            # Remember which row wants this path so we can refresh it on ready.
            self._rows_for_path.setdefault(key, set()).add(row)
            return None  # default delegate draws nothing → neutral blank
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(0, ICON_SIZE.height() + 14)
        return None

    # --- internals ---

    def _label_for(self, s) -> str:
        cam_name = self._labels.get(s.trigger_cam) or f"cam{s.trigger_cam}"
        best = max((m.person_pct for m in s.members), default=0) if s.members else 0
        conf = f"  ·  {best}% human" if s.peak_person and best else ""
        if s.count > 1:
            span = f"{s.start_at:%H:%M:%S}–{s.end_at:%H:%M:%S}"
            line2 = f"{cam_name} · {s.duration_label} · {s.count} clips"
        else:
            span = f"{s.start_at:%H:%M:%S}"
            angles = len(s.members[0].clips) if s.members else 0
            line2 = f"{cam_name} · {angles} angles"
        return f"{span}   {s.pretty}{conf}\n{line2}"

    def _on_thumb_ready(self, key: str) -> None:
        rows = self._rows_for_path.pop(key, None)
        if not rows:
            return
        for row in rows:
            if 0 <= row < self._loaded:
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
