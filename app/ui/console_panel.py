"""Bottom-docked admin log console. Toggleable, mono font, level-tinted rows.

Entries carry a `source` tag (CFG, REC, AI, EVT, CAM1..4, PB1..4, ...). The
titlebar filter collapses those tags into a few human categories so you can
watch one concern at a time - e.g. "AI / Events" while tuning detection, or
"Recording" while checking the rolling buffer."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.log import LogEntry, bus
from app.ui import theme


_LEVEL_COLOR = {
    "DEBUG": theme.TEXT_DIM,
    "INFO":  theme.TEXT_MUTED,
    "WARN":  theme.WARN,
    "ERROR": theme.ERROR,
}

# Filter categories, in display order. "All" is special-cased.
_CATEGORIES = ["All", "AI / Events", "Recording", "Cameras", "Playback", "System"]


def _category(source: str) -> str:
    s = source.upper()
    if s in ("AI", "EVT"):
        return "AI / Events"
    if s == "REC":
        return "Recording"
    if s.startswith("CAM"):
        return "Cameras"
    if s.startswith("PB"):
        return "Playback"
    return "System"  # CFG, APP, PROBE, CACHE, anything else


class ConsolePanel(QDockWidget):
    """Dock at the bottom. Open/closed state is owned by MainWindow."""

    MAX_ENTRIES = 5000

    def __init__(self, parent=None) -> None:
        super().__init__("Console", parent)
        self.setObjectName("Console")
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea
                             | Qt.DockWidgetArea.TopDockWidgetArea)

        # Every entry is retained so the filter can re-render without losing
        # history; the view itself only ever holds the matching subset.
        self._entries: list[LogEntry] = []
        self._filter = "All"

        self.setTitleBarWidget(self._build_titlebar())

        body = QWidget(self)
        body.setObjectName("ConsoleBody")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self._view = QPlainTextEdit(body)
        self._view.setObjectName("ConsoleView")
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(self.MAX_ENTRIES)
        self._view.setWordWrapMode(self._view.wordWrapMode().NoWrap)
        v.addWidget(self._view, 1)
        self.setWidget(body)

        # Pre-fill with anything that happened before the dock was created
        for entry in bus.history():
            self._store(entry)
            if self._matches(entry):
                self._render(entry)
        bus.entry.connect(self._append)

    def _build_titlebar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("ConsoleTitlebar")
        bar.setFixedHeight(28)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 8, 0)
        h.setSpacing(10)

        title = QLabel("CONSOLE", bar)
        title.setObjectName("ConsoleTitle")

        self._filter_box = QComboBox(bar)
        self._filter_box.setObjectName("ConsoleFilter")
        self._filter_box.addItems(_CATEGORIES)
        self._filter_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self._filter_box.setToolTip("Show only one category of log lines")
        self._filter_box.currentTextChanged.connect(self._on_filter_changed)

        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        clear = QPushButton("CLEAR", bar)
        clear.setObjectName("ConsoleAction")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(self._on_clear)

        close = QPushButton("CLOSE", bar)
        close.setObjectName("ConsoleAction")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.hide)

        h.addWidget(title)
        h.addWidget(self._filter_box)
        h.addWidget(spacer)
        h.addWidget(clear)
        h.addWidget(close)
        return bar

    # Internal

    def _matches(self, entry: LogEntry) -> bool:
        return self._filter == "All" or _category(entry.source) == self._filter

    def _store(self, entry: LogEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > self.MAX_ENTRIES:
            self._entries = self._entries[-self.MAX_ENTRIES:]

    def _render(self, entry: LogEntry) -> None:
        ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
        color = _LEVEL_COLOR.get(entry.level, theme.TEXT_MUTED)
        line = f"{ts}  {entry.level:<5}  {entry.source:<6}  {entry.message}"

        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.insertText(line + "\n", fmt)
        # Auto-scroll only if user is already near the bottom (don't yank them
        # away from a manual scroll).
        sb = self._view.verticalScrollBar()
        if sb.value() >= sb.maximum() - 40:
            sb.setValue(sb.maximum())

    @Slot(LogEntry)
    def _append(self, entry: LogEntry) -> None:
        self._store(entry)
        if self._matches(entry):
            self._render(entry)

    @Slot(str)
    def _on_filter_changed(self, category: str) -> None:
        self._filter = category
        self._view.clear()
        for entry in self._entries:
            if self._matches(entry):
                self._render(entry)

    @Slot()
    def _on_clear(self) -> None:
        self._entries.clear()
        self._view.clear()
