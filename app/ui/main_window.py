"""Main window: toolbar over a 2x2 camera grid over a status bar."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.core.cameras import default_cameras
from app.core.config import Settings
from app.ui.camera_tile import CameraTile


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self.setWindowTitle("CCTV Console")
        self.setMinimumSize(1024, 640)
        self.resize(1360, 820)

        cameras = default_cameras()

        toolbar = self._build_toolbar()
        grid_widget, self._tiles = self._build_grid(cameras, settings)
        status_bar = self._build_status_bar(settings)

        central = QWidget(self)
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(toolbar)
        cv.addWidget(grid_widget, 1)
        cv.addWidget(status_bar)
        self.setCentralWidget(central)

        self._refresh_clock = QTimer(self)
        self._refresh_clock.timeout.connect(self._update_status_bar)
        self._refresh_clock.start(1000)

    # Build

    def _build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 16, 0)
        layout.setSpacing(14)

        brand = QLabel("CCTV CONSOLE", bar)
        brand.setObjectName("Brand")
        sep = QLabel("/", bar)
        sep.setObjectName("BrandSeparator")
        version = QLabel(f"v{__version__}", bar)
        version.setObjectName("Version")

        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        reconnect = QPushButton("RECONNECT ALL", bar)
        reconnect.setObjectName("ToolbarAction")
        reconnect.setCursor(Qt.CursorShape.PointingHandCursor)
        reconnect.clicked.connect(self._reconnect_all)

        layout.addWidget(brand)
        layout.addWidget(sep)
        layout.addWidget(version)
        layout.addWidget(spacer)
        layout.addWidget(reconnect)
        return bar

    def _build_grid(self, cameras, settings: Settings) -> tuple[QWidget, list[CameraTile]]:
        wrap = QWidget(self)
        wrap.setObjectName("Grid")
        grid = QGridLayout(wrap)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(10)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        tiles: list[CameraTile] = []
        for i, cam in enumerate(cameras):
            default_stream = settings.cam_defaults[i]
            tile = CameraTile(cam, settings, default_stream, parent=wrap)
            tiles.append(tile)
            row, col = divmod(i, 2)
            grid.addWidget(tile, row, col)
        return wrap, tiles

    def _build_status_bar(self, settings: Settings) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(28)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(20)

        left = QLabel(f"DVR {settings.dvr_ip}:{settings.dvr_port}  user {settings.dvr_user}", bar)
        left.setObjectName("StatusBarText")

        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._status_clock = QLabel("", bar)
        self._status_clock.setObjectName("StatusBarText")

        layout.addWidget(left)
        layout.addWidget(spacer)
        layout.addWidget(self._status_clock)
        return bar

    # Lifecycle

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        for tile in self._tiles:
            tile.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._refresh_clock.stop()
        for tile in self._tiles:
            tile.shutdown(wait_ms=2000)
        super().closeEvent(event)

    # Actions

    def _reconnect_all(self) -> None:
        for tile in self._tiles:
            tile.shutdown(wait_ms=500)
        for tile in self._tiles:
            tile.start()

    def _update_status_bar(self) -> None:
        from datetime import datetime
        self._status_clock.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
