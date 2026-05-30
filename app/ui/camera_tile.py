"""A single camera tile: header (label, status, sub/main toggle) over a video panel."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.core.cameras import Camera
from app.core.config import Settings
from app.core.log import bus
from app.core.stream import StreamWorker
from app.ui import theme


class StatusDot(QWidget):
    """Small filled circle. Color reflects connection state, never decoration."""

    SIZE = 9

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE + 4, self.SIZE + 4)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._status = "offline"

    @Slot(str)
    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = {
            "online":       QColor(theme.OK),
            "connecting":   QColor(theme.WARN),
            "reconnecting": QColor(theme.WARN),
            "offline":      QColor(theme.ERROR),
            "stopped":      QColor(theme.TEXT_DIM),
        }.get(self._status, QColor(theme.TEXT_DIM))
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        x = (self.width() - self.SIZE) // 2
        y = (self.height() - self.SIZE) // 2
        painter.drawEllipse(x, y, self.SIZE, self.SIZE)


class VideoPanel(QWidget):
    """Paints the current frame fit-to-bounds with letterboxing.

    Falls back to a status message centered on a slightly-darker fill when
    no frame is available (connecting, reconnecting, offline).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(160, 90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image: QImage | None = None
        self._message: str | None = "Connecting"

    @Slot(QImage)
    def set_frame(self, image: QImage) -> None:
        self._image = image
        self._message = None
        self.update()

    def set_message(self, message: str | None) -> None:
        self._image = None
        self._message = message
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.VIDEO_BG))

        if self._image is not None and not self._image.isNull():
            scaled = self._image.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
            return

        if self._message:
            pen = QPen(QColor(theme.TEXT_MUTED))
            painter.setPen(pen)
            font = painter.font()
            font.setPointSize(10)
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)


class _InfoButton(QPushButton):
    """A `(!)` badge whose tooltip refreshes live while the cursor hovers it.

    Qt's setToolTip() doesn't update an already-visible tooltip, so we re-issue
    QToolTip.showText on a timer for as long as the pointer is over the button,
    pulling the latest text from a provider callable each tick."""

    def __init__(self, text_provider, parent=None) -> None:
        super().__init__("!", parent)
        self._text_provider = text_provider
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)

    def _refresh(self) -> None:
        pos = self.mapToGlobal(self.rect().bottomLeft())
        QToolTip.showText(pos, self._text_provider(), self)

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt)
        self._refresh()
        self._timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt)
        self._timer.stop()
        QToolTip.hideText()
        super().leaveEvent(event)


class CameraTile(QFrame):
    """Header + VideoPanel for one camera. Owns the StreamWorker thread."""

    # (camera index, armed) - emitted when the user toggles event detection.
    event_arm_toggled = Signal(int, bool)
    # (camera index, shift_held) - emitted on double-click for focus/maximize.
    double_clicked = Signal(int, bool)

    def __init__(self, camera: Camera, settings: Settings, default_stream: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CameraTile")
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Without explicit Expanding, the tile inherits Preferred and the
        # grid hands it sizeHint width instead of column-stretch width,
        # which leaves the right column squished.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._camera = camera
        self._settings = settings
        self._current = default_stream if default_stream in ("sub", "main") else "sub"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header
        header = QWidget(self)
        header.setObjectName("TileHeader")
        header.setFixedHeight(40)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 0, 10, 0)
        hl.setSpacing(10)

        self._dot = StatusDot(header)

        name = QLabel(camera.label, header)
        name.setObjectName("TileName")

        location = QLabel(camera.location, header)
        location.setObjectName("TileLocation")
        # Don't let the location text dictate the column width; if the tile
        # narrows, the label squishes (and clips) before the column stretches.
        location.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        location.setMinimumWidth(0)

        spacer = QWidget(header)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        armed = camera.index in settings.detection_cameras
        self._arm = QPushButton("● EVENTS" if armed else "○ EVENTS", header)
        self._arm.setObjectName("EventArm")
        self._arm.setCheckable(True)
        self._arm.setChecked(armed)  # armed set comes from DETECTION_CAMERAS
        self._arm.setFixedHeight(22)
        self._arm.setCursor(Qt.CursorShape.PointingHandCursor)
        self._arm.setToolTip("Watch this camera for events (motion of people / vehicles).\n"
                             "When off, this camera still records but won't trigger event clips.")
        self._arm.toggled.connect(self._on_arm_toggled)

        self._fps = 0.0
        self._res = (0, 0)
        self._status = "connecting"

        self._info = _InfoButton(self._info_text, header)
        self._info.setObjectName("TileInfo")
        self._info.setFixedSize(22, 22)
        self._info.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._reload = QPushButton("↻", header)  # ↻ clockwise open circle arrow
        self._reload.setObjectName("TileReload")
        self._reload.setFixedSize(24, 22)
        self._reload.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reload.setToolTip("Reconnect this camera only")
        self._reload.clicked.connect(self._on_reload)

        self._toggle = QPushButton(self._current.upper(), header)
        self._toggle.setObjectName("StreamToggle")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(self._current == "main")
        self._toggle.setFixedHeight(22)
        self._toggle.setToolTip("Toggle between sub stream (low res, light) and main stream (high res, heavy)")
        self._toggle.toggled.connect(self._on_toggle)

        hl.addWidget(self._dot)
        hl.addWidget(name)
        hl.addWidget(location)
        hl.addWidget(spacer)
        hl.addWidget(self._info)
        hl.addWidget(self._arm)
        hl.addWidget(self._reload)
        hl.addWidget(self._toggle)

        # Video
        self._video = VideoPanel(self)

        outer.addWidget(header)
        outer.addWidget(self._video, 1)

        # Worker (created lazily-recreateable via _build_worker so RECONNECT
        # ALL can recover after the QThread has terminated; QThread can't be
        # start()-ed twice).
        self._worker: StreamWorker | None = None
        self._build_worker()

    def _build_worker(self) -> None:
        self._worker = StreamWorker(
            self._camera.url(self._current, self._settings),
            label=f"CAM{self._camera.index}",
            parent=self,
        )
        self._worker.frame_ready.connect(self._video.set_frame)
        self._worker.status_changed.connect(self._on_status)
        self._worker.stats_changed.connect(self._on_stats)

    # Public

    def start(self) -> None:
        assert self._worker is not None
        if self._worker.isRunning():
            return
        if self._worker.isFinished():
            # QThread is single-shot; replace it before starting.
            self._build_worker()
        self._worker.start()

    def shutdown(self, wait_ms: int = 3000) -> None:
        if self._worker is None:
            return
        self._worker.request_stop()
        self._worker.wait(wait_ms)

    def reconnect(self) -> None:
        """Smart reconnect.

        If the worker thread is alive, send it a soft `request_reopen`
        (drops the current cap and re-opens the URL inside the same thread).
        If it's already terminated, fully recreate the worker thread and
        start it.
        """
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_reopen()
            return
        self._build_worker()
        self._worker.start()

    def apply_settings(self, settings: Settings) -> None:
        """Swap the active settings (typically when DVR IP changed)
        and live-switch the worker URL without restart."""
        self._settings = settings
        if self._worker is not None and self._worker.isRunning():
            self._worker.set_url(self._camera.url(self._current, settings))
        else:
            # Worker not alive; just rebuild against the new settings on next start.
            self._build_worker()

    # Internal

    def is_event_armed(self) -> bool:
        return self._arm.isChecked()

    def set_focus_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        # Own the press on the tile body so the double-click is delivered here
        # (Qt only sends mouseDoubleClickEvent to the press recipient). Child
        # buttons accept their own presses first, so they're unaffected.
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.double_clicked.emit(self._camera.index, shift)
        event.accept()

    @Slot()
    def _on_reload(self) -> None:
        bus.info(f"CAM{self._camera.index}", "user requested reconnect (this camera)")
        self._video.set_message("Reconnecting")
        self.reconnect()

    @Slot(bool)
    def _on_arm_toggled(self, armed: bool) -> None:
        self._arm.setText("● EVENTS" if armed else "○ EVENTS")
        bus.info(
            f"CAM{self._camera.index}",
            f"event detection {'armed' if armed else 'disarmed'}",
        )
        self.event_arm_toggled.emit(self._camera.index, armed)

    @Slot(bool)
    def _on_toggle(self, checked: bool) -> None:
        self._current = "main" if checked else "sub"
        self._toggle.setText(self._current.upper())
        self._video.set_message("Switching to " + self._current + " stream")
        self._update_info()
        self._worker.set_url(self._camera.url(self._current, self._settings))

    @Slot(float, int, int)
    def _on_stats(self, fps: float, w: int, h: int) -> None:
        self._fps = fps
        self._res = (w, h)
        self._update_info()

    def _info_text(self) -> str:
        w, h = self._res
        res = f"{w}x{h}" if w else "—"
        return (
            f"Camera {self._camera.index} · {self._camera.label}\n"
            f"Stream: {self._current.upper()}\n"
            f"Resolution: {res}\n"
            f"FPS: {self._fps:.1f}\n"
            f"Status: {self._status}"
        )

    def _update_info(self) -> None:
        # Warn-tint the badge if a supposedly-online stream is starved of frames.
        slow = self._status == "online" and self._fps and self._fps < 5.0
        self._info.setProperty("slow", "true" if slow else "false")
        self._info.style().unpolish(self._info)
        self._info.style().polish(self._info)

    @Slot(str)
    def _on_status(self, status: str) -> None:
        self._status = status
        self._update_info()
        self._dot.set_status(status)
        if status == "online":
            self._video.set_message(None)
        elif status == "connecting":
            self._video.set_message("Connecting")
        elif status == "reconnecting":
            self._video.set_message("Reconnecting")
        elif status == "offline":
            self._video.set_message("Stream offline")
        elif status == "stopped":
            self._video.set_message("Stopped")
