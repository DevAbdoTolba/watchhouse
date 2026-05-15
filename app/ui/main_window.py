"""Main window: toolbar over a 2x2 camera grid over a status bar, plus a
toggleable bottom-docked admin log console."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QGuiApplication, QKeySequence, QShortcut
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
from app.core.config import Settings, persist_dvr_ip, with_dvr_ip
from app.core.discovery import DiscoveryResult, DiscoveryWorker
from app.core.log import bus
from app.core.probe import ProbeWorker
from app.ui.camera_tile import CameraTile
from app.ui.console_panel import ConsolePanel


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._probe: ProbeWorker | None = None
        self._discovery: DiscoveryWorker | None = None
        self.setWindowTitle("CCTV Console")
        self.setMinimumSize(880, 560)
        self._size_to_screen()

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

        # Console dock
        self._console = ConsolePanel(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._console)
        self._console.hide()  # start closed; user toggles via button or Ctrl+L
        self._console.visibilityChanged.connect(self._sync_console_button)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._toggle_console)

        self._refresh_clock = QTimer(self)
        self._refresh_clock.timeout.connect(self._update_status_bar)
        self._refresh_clock.start(1000)

        bus.info("APP", f"CCTV Console v{__version__} starting")

    # Build

    def _size_to_screen(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        w = min(1280, int(screen.width() * 0.92))
        h = min(760, int(screen.height() * 0.92))
        self.resize(w, h)
        self.move(
            screen.left() + (screen.width() - w) // 2,
            screen.top() + (screen.height() - h) // 2,
        )

    def _build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 16, 0)
        layout.setSpacing(14)

        brand = QLabel("CCTV CONSOLE", bar)
        brand.setObjectName("Brand")

        version = QLabel(f"v{__version__}", bar)
        version.setObjectName("Version")

        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._probe_btn = QPushButton("TEST DVR", bar)
        self._probe_btn.setObjectName("ToolbarAction")
        self._probe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._probe_btn.setMinimumHeight(30)
        self._probe_btn.clicked.connect(self._run_probe)

        self._discover_btn = QPushButton("DISCOVER", bar)
        self._discover_btn.setObjectName("ToolbarAction")
        self._discover_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._discover_btn.setMinimumHeight(30)
        self._discover_btn.clicked.connect(self._run_discovery)

        self._console_btn = QPushButton("CONSOLE", bar)
        self._console_btn.setObjectName("ToolbarAction")
        self._console_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._console_btn.setMinimumHeight(30)
        self._console_btn.setCheckable(True)
        self._console_btn.clicked.connect(self._toggle_console)

        self._reconnect_btn = QPushButton("RECONNECT ALL", bar)
        self._reconnect_btn.setObjectName("ToolbarAction")
        self._reconnect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reconnect_btn.setMinimumHeight(30)
        self._reconnect_btn.setMinimumWidth(140)
        self._reconnect_btn.clicked.connect(self._reconnect_all)

        layout.addWidget(brand)
        layout.addWidget(version)
        layout.addWidget(spacer)
        layout.addWidget(self._probe_btn)
        layout.addWidget(self._discover_btn)
        layout.addWidget(self._console_btn)
        layout.addWidget(self._reconnect_btn)
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

        self._status_dvr = QLabel(self._dvr_status_text(settings), bar)
        self._status_dvr.setObjectName("StatusBarText")

        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._status_clock = QLabel("", bar)
        self._status_clock.setObjectName("StatusBarText")

        layout.addWidget(self._status_dvr)
        layout.addWidget(spacer)
        layout.addWidget(self._status_clock)
        return bar

    @staticmethod
    def _dvr_status_text(settings: Settings) -> str:
        return f"DVR {settings.dvr_ip}:{settings.dvr_port}  user {settings.dvr_user}"

    # Lifecycle

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        for tile in self._tiles:
            tile.start()
        # Kick off DVR probe shortly after the window has painted, so the
        # log entries appear in the console if the user opens it.
        QTimer.singleShot(400, self._run_probe)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._refresh_clock.stop()
        for tile in self._tiles:
            tile.shutdown(wait_ms=2000)
        if self._probe is not None and self._probe.isRunning():
            self._probe.wait(2000)
        if self._discovery is not None and self._discovery.isRunning():
            self._discovery.wait(3000)
        super().closeEvent(event)

    # Actions

    def _reconnect_all(self) -> None:
        bus.info("APP", "user requested RECONNECT ALL")
        for tile in self._tiles:
            tile.shutdown(wait_ms=500)
        for tile in self._tiles:
            tile.start()

    def _toggle_console(self) -> None:
        self._console.setVisible(not self._console.isVisible())

    def _sync_console_button(self, visible: bool) -> None:
        self._console_btn.setChecked(visible)

    def _run_probe(self) -> None:
        if self._probe is not None and self._probe.isRunning():
            bus.info("PROBE", "probe already running; ignoring re-trigger")
            return
        self._probe = ProbeWorker(self._settings, parent=self)
        self._probe.finished_with.connect(self._on_probe_done)
        self._probe.start()

    def _on_probe_done(self, ok: bool, _summary: str) -> None:
        if not ok:
            bus.info("APP", "probe failed; auto-running discovery on local subnet")
            self._run_discovery()

    def _run_discovery(self) -> None:
        if self._discovery is not None and self._discovery.isRunning():
            bus.info("DISC", "discovery already in progress")
            return
        self._discovery = DiscoveryWorker(self._settings.dvr_port, parent=self)
        self._discovery.completed.connect(self._on_discovery_done)
        self._discover_btn.setEnabled(False)
        self._discovery.start()

    def _on_discovery_done(self, result: DiscoveryResult) -> None:
        self._discover_btn.setEnabled(True)
        if result.found_ip is None:
            bus.warn("APP", "discovery finished with no DVR found")
            return
        if result.found_ip == self._settings.dvr_ip:
            bus.info("APP", f"discovered IP {result.found_ip} matches current; nothing to update")
            return
        bus.info("APP", f"switching DVR IP {self._settings.dvr_ip} -> {result.found_ip}")
        self._settings = with_dvr_ip(self._settings, result.found_ip)
        self._status_dvr.setText(self._dvr_status_text(self._settings))
        for tile in self._tiles:
            tile.apply_settings(self._settings)
        persist_dvr_ip(self._settings, result.found_ip)

    def _update_status_bar(self) -> None:
        from datetime import datetime
        self._status_clock.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
