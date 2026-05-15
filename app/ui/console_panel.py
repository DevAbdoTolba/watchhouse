"""Bottom-docked admin log console. Toggleable, mono font, level-tinted rows."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
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


class ConsolePanel(QDockWidget):
    """Dock at the bottom. Open/closed state is owned by MainWindow."""

    def __init__(self, parent=None) -> None:
        super().__init__("Console", parent)
        self.setObjectName("Console")
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea
                             | Qt.DockWidgetArea.TopDockWidgetArea)
        self.setTitleBarWidget(self._build_titlebar())

        body = QWidget(self)
        body.setObjectName("ConsoleBody")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self._view = QPlainTextEdit(body)
        self._view.setObjectName("ConsoleView")
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(5000)
        self._view.setWordWrapMode(self._view.wordWrapMode().NoWrap)
        v.addWidget(self._view, 1)
        self.setWidget(body)

        # Pre-fill with anything that happened before the dock was created
        for entry in bus.history():
            self._append(entry)
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

        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        clear = QPushButton("CLEAR", bar)
        clear.setObjectName("ConsoleAction")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(lambda: self._view.clear())

        close = QPushButton("CLOSE", bar)
        close.setObjectName("ConsoleAction")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.hide)

        h.addWidget(title)
        h.addWidget(spacer)
        h.addWidget(clear)
        h.addWidget(close)
        return bar

    @Slot(LogEntry)
    def _append(self, entry: LogEntry) -> None:
        ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
        color = _LEVEL_COLOR.get(entry.level, theme.TEXT_MUTED)
        line = f"{ts}  {entry.level:<5}  {entry.source:<6}  {entry.message}"

        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.insertText(line + "\n", fmt)
        # Auto-scroll only if user is already near the bottom (don't yank them away from a manual scroll)
        bar = self._view.verticalScrollBar()
        if bar.value() >= bar.maximum() - 40:
            bar.setValue(bar.maximum())
