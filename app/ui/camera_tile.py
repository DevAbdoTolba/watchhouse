"""A single camera tile: header (label, status, sub/main toggle) over a video panel."""

from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.cameras import Camera
from app.core.config import Settings
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


class CameraTile(QFrame):
    """Header + VideoPanel for one camera. Owns the StreamWorker thread."""

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
        hl.addWidget(self._toggle)

        # Video
        self._video = VideoPanel(self)

        outer.addWidget(header)
        outer.addWidget(self._video, 1)

        # Worker
        self._worker = StreamWorker(
            camera.url(self._current, settings),
            label=f"CAM{camera.index}",
            parent=self,
        )
        self._worker.frame_ready.connect(self._video.set_frame)
        self._worker.status_changed.connect(self._on_status)

    # Public

    def start(self) -> None:
        self._worker.start()

    def shutdown(self, wait_ms: int = 3000) -> None:
        self._worker.request_stop()
        self._worker.wait(wait_ms)

    def apply_settings(self, settings: Settings) -> None:
        """Swap the active settings (typically when DVR IP changed)
        and live-switch the worker URL without restart."""
        self._settings = settings
        self._worker.set_url(self._camera.url(self._current, settings))

    # Internal

    @Slot(bool)
    def _on_toggle(self, checked: bool) -> None:
        self._current = "main" if checked else "sub"
        self._toggle.setText(self._current.upper())
        self._video.set_message("Switching to " + self._current + " stream")
        self._worker.set_url(self._camera.url(self._current, self._settings))

    @Slot(str)
    def _on_status(self, status: str) -> None:
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
