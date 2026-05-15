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
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.core.cameras import default_cameras
from app.core.config import Settings, persist_dvr_ip, with_dvr_ip
from app.core.discovery import DiscoveryResult, DiscoveryWorker
from app.core.ip_cache import load as load_ip_cache, record_hit as record_ip_hit
from app.core.log import bus
from app.core.playback_probe import PlaybackProbeWorker
from app.core.probe import ProbeWorker
from app.core.recorder import RecorderSupervisor
from app.ui.camera_tile import CameraTile
from app.ui.console_panel import ConsolePanel
from app.ui.playback_view import PlaybackView


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._probe: ProbeWorker | None = None
        self._discovery: DiscoveryWorker | None = None
        self._pbprobe: PlaybackProbeWorker | None = None
        self._recorder: RecorderSupervisor | None = None
        self.setWindowTitle("Watchhouse")
        self.setMinimumSize(880, 560)
        self._size_to_screen()

        self._cameras = default_cameras()

        toolbar = self._build_toolbar()
        live_widget, self._tiles = self._build_grid(self._cameras, settings)
        self._playback_view = PlaybackView(self._cameras, settings, parent=self)
        self._stack = QStackedWidget(self)
        self._stack.addWidget(live_widget)
        self._stack.addWidget(self._playback_view)
        self._stack.setCurrentIndex(0)
        status_bar = self._build_status_bar(settings)

        central = QWidget(self)
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(toolbar)
        cv.addWidget(self._stack, 1)
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

        bus.info("APP", f"Watchhouse v{__version__} starting")

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

        brand = QLabel("WATCHHOUSE", bar)
        brand.setObjectName("Brand")

        version = QLabel(f"v{__version__}", bar)
        version.setObjectName("Version")

        # LIVE / PLAYBACK mode toggle
        self._mode_live_btn = QPushButton("LIVE", bar)
        self._mode_live_btn.setObjectName("ModeToggle")
        self._mode_live_btn.setCheckable(True)
        self._mode_live_btn.setChecked(True)
        self._mode_live_btn.setMinimumHeight(30)
        self._mode_live_btn.setMinimumWidth(82)
        self._mode_live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_live_btn.clicked.connect(lambda: self._set_mode("live"))

        self._mode_pb_btn = QPushButton("PLAYBACK", bar)
        self._mode_pb_btn.setObjectName("ModeToggle")
        self._mode_pb_btn.setCheckable(True)
        self._mode_pb_btn.setMinimumHeight(30)
        self._mode_pb_btn.setMinimumWidth(82)
        self._mode_pb_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_pb_btn.clicked.connect(lambda: self._set_mode("playback"))

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

        self._pbprobe_btn = QPushButton("PROBE PLAYBACK", bar)
        self._pbprobe_btn.setObjectName("ToolbarAction")
        self._pbprobe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pbprobe_btn.setMinimumHeight(30)
        self._pbprobe_btn.setToolTip("Try common DVR playback protocols and report what works in the log console")
        self._pbprobe_btn.clicked.connect(self._run_pbprobe)

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
        layout.addSpacing(20)
        layout.addWidget(self._mode_live_btn)
        layout.addWidget(self._mode_pb_btn)
        layout.addWidget(spacer)
        layout.addWidget(self._probe_btn)
        layout.addWidget(self._discover_btn)
        layout.addWidget(self._pbprobe_btn)
        layout.addWidget(self._console_btn)
        layout.addWidget(self._reconnect_btn)
        return bar

    def _set_mode(self, mode: str) -> None:
        if mode == "live":
            self._mode_live_btn.setChecked(True)
            self._mode_pb_btn.setChecked(False)
            self._stack.setCurrentIndex(0)
            self._reconnect_btn.setVisible(True)
            self._probe_btn.setVisible(True)
            self._discover_btn.setVisible(True)
            self._pbprobe_btn.setVisible(True)
            bus.info("APP", "switched to LIVE mode")
        else:
            self._mode_live_btn.setChecked(False)
            self._mode_pb_btn.setChecked(True)
            self._stack.setCurrentIndex(1)
            # Hide live-only actions while in playback to keep the bar tidy
            self._reconnect_btn.setVisible(False)
            self._probe_btn.setVisible(False)
            self._discover_btn.setVisible(False)
            self._pbprobe_btn.setVisible(False)
            self._playback_view.refresh_library()
            bus.info("APP", "switched to PLAYBACK mode")

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

        self._status_recorder = QLabel("REC: off", bar)
        self._status_recorder.setObjectName("StatusBarText")

        self._status_clock = QLabel("", bar)
        self._status_clock.setObjectName("StatusBarText")

        layout.addWidget(self._status_dvr)
        layout.addWidget(spacer)
        layout.addWidget(self._status_recorder)
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
        # Start the recorder a moment after the live streams so they don't
        # race the DVR's connection cap.
        if self._settings.recording_enabled:
            QTimer.singleShot(1500, self._start_recorder)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._refresh_clock.stop()
        if self._recorder is not None:
            self._recorder.stop(wait_ms=5000)
        for tile in self._tiles:
            tile.shutdown(wait_ms=2000)
        self._playback_view.shutdown()
        if self._probe is not None and self._probe.isRunning():
            self._probe.wait(2000)
        if self._discovery is not None and self._discovery.isRunning():
            self._discovery.wait(3000)
        super().closeEvent(event)

    # Actions

    def _reconnect_all(self) -> None:
        bus.info("APP", "user requested RECONNECT ALL")
        for tile in self._tiles:
            tile.reconnect()

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
        if ok:
            # Configured IP works; refresh its position in the IP cache.
            record_ip_hit(self._settings.env_path, self._settings.dvr_ip)
        else:
            bus.info("APP", "probe failed; auto-running discovery on local subnet")
            self._run_discovery()

    def _run_pbprobe(self) -> None:
        if self._pbprobe is not None and self._pbprobe.isRunning():
            bus.info("PBPROBE", "playback probe already running; ignoring re-trigger")
            return
        self._pbprobe = PlaybackProbeWorker(self._settings, parent=self)
        self._pbprobe_btn.setEnabled(False)
        self._pbprobe.finished_with.connect(lambda _r: self._pbprobe_btn.setEnabled(True))
        self._pbprobe.start()
        # Open the console so the user actually sees what the probe finds
        if not self._console.isVisible():
            self._console.show()

    def _run_discovery(self) -> None:
        if self._discovery is not None and self._discovery.isRunning():
            bus.info("DISC", "discovery already in progress")
            return
        cached = load_ip_cache(self._settings.env_path)
        # MRU first; exclude the IP we already proved unreachable in the probe
        priority = tuple(r.ip for r in cached if r.ip != self._settings.dvr_ip)
        self._discovery = DiscoveryWorker(
            self._settings.dvr_port,
            priority_ips=priority,
            parent=self,
        )
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
        record_ip_hit(self._settings.env_path, result.found_ip)

    def _start_recorder(self) -> None:
        if self._recorder is not None:
            return
        self._recorder = RecorderSupervisor(self._settings, self._cameras, parent=self)
        self._recorder.stats_changed.connect(self._on_recorder_stats)
        self._recorder.start()

    def _on_recorder_stats(self, segments: int, total_bytes: int, active: int) -> None:
        if active == 0 and segments == 0:
            self._status_recorder.setText("REC: off")
            return
        gb = total_bytes / 1024 / 1024 / 1024
        self._status_recorder.setText(
            f"REC: {active}/{len(self._cameras)} cams  {segments} clips  {gb:.2f} GB"
        )

    def _update_status_bar(self) -> None:
        from datetime import datetime
        self._status_clock.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
