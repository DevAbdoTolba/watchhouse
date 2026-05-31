"""Main window: toolbar over a 2x2 camera grid over a status bar, plus a
toggleable bottom-docked admin log console."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.core.cameras import default_cameras
from app.core.config import Settings, persist_dvr_ip, persist_env_values, with_dvr_ip
from app.core.discovery import DiscoveryResult, DiscoveryWorker
from app.core.ip_cache import load as load_ip_cache, record_hit as record_ip_hit
from app.core.log import bus
from app.core.playback_probe import PlaybackProbeWorker
from app.core.probe import ProbeWorker
from app.core import camera_names
from app.core.analyzer import SegmentAnalyzer
from app.core.events import EventConfig
from app.core.notifier import TelegramNotifier
from app.core.live_detector import LiveDetector
from app.core.recorder import RecorderSupervisor
from app.ui.camera_names_dialog import CameraNamesDialog
from app.ui.telegram_dialog import TelegramDialog
from app.ui.camera_tile import CameraTile
from app.ui.grid_focus import GridFocus
from app.ui.console_panel import ConsolePanel
from app.ui.playback_view import PlaybackView
from app.ui.wipe_dialog import WipeDialog


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._probe: ProbeWorker | None = None
        self._discovery: DiscoveryWorker | None = None
        self._pbprobe: PlaybackProbeWorker | None = None
        self._recorder: RecorderSupervisor | None = None
        self._analyzer: SegmentAnalyzer | None = None
        self.setWindowTitle("Watchhouse")
        self.setMinimumSize(880, 560)
        self._size_to_screen()

        self._cameras = default_cameras()
        # Cameras armed to trigger event detection. From DETECTION_CAMERAS
        # (defaults to all); the user can still toggle each tile live.
        self._armed_cameras: set[int] = {
            c.index for c in self._cameras if c.index in settings.detection_cameras
        }
        # User-editable display names (persisted next to .env), shown in the
        # tile header and used as the Telegram caption label. A blank/unset
        # name falls back to the camera's built-in location.
        self._camera_names: dict[int, str] = camera_names.load(settings.env_path)
        self._cam_labels: dict[int, str] = {
            c.index: self._effective_cam_name(c) for c in self._cameras
        }
        # Off-device push alerts; no-op unless TELEGRAM_* is configured in .env.
        self._notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            min_interval_s=settings.telegram_min_interval_s,
            notify_ongoing=settings.event_notify_ongoing,
            commands_enabled=settings.telegram_commands,
            state_dir=settings.env_path,
            events_dir=settings.events_dir,
            parent=self,
        )
        self._notifier.set_cam_labels(self._cam_labels)

        # Real-time alert tier: instant person/vehicle alerts off the live
        # preview frames (separate from the delayed segment analyzer).
        self._live_detector = LiveDetector(
            settings.detection_model,
            set(self._armed_cameras),
            conf=settings.live_conf,
            cooldown_s=settings.live_cooldown_s,
            parent=self,
        )
        self._live_detector.live_alert.connect(self._on_live_alert)

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

        # RECONNECT ALL — live-mode only, visible at a glance because it's
        # the most time-sensitive action.
        self._reconnect_btn = QPushButton("↺  RECONNECT ALL", bar)
        self._reconnect_btn.setObjectName("ToolbarAction")
        self._reconnect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reconnect_btn.setMinimumHeight(30)
        self._reconnect_btn.clicked.connect(self._reconnect_all)

        # CONSOLE toggle — always visible, frequently used.
        self._console_btn = QPushButton("CONSOLE", bar)
        self._console_btn.setObjectName("ToolbarAction")
        self._console_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._console_btn.setMinimumHeight(30)
        self._console_btn.setCheckable(True)
        self._console_btn.clicked.connect(self._toggle_console)

        # ⚙ dropdown — infrequent / diagnostic / destructive actions grouped
        # so they don't crowd the toolbar on every launch.
        self._system_btn = QPushButton("⚙", bar)
        self._system_btn.setObjectName("ToolbarAction")
        self._system_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._system_btn.setMinimumHeight(30)
        self._system_btn.setFixedWidth(40)
        self._system_btn.setToolTip("System actions")

        system_menu = QMenu(self._system_btn)
        system_menu.setStyleSheet(
            f"QMenu {{ background: #1f242e; border: 1px solid #2a3040; color: #ebe7e1; }}"
            f"QMenu::item {{ padding: 6px 20px; }}"
            f"QMenu::item:selected {{ background: #2a2017; color: #c69561; }}"
            f"QMenu::separator {{ background: #2a3040; height: 1px; margin: 4px 0; }}"
        )
        self._act_probe    = system_menu.addAction("Test DVR Connection")
        self._act_discover = system_menu.addAction("Discover DVR on LAN")
        self._act_pbprobe  = system_menu.addAction("Probe Playback Protocols")
        system_menu.addSeparator()
        self._act_rename   = system_menu.addAction("Rename Cameras…")
        self._act_rename.setToolTip("Set a friendly name per camera (alerts + tiles)")
        self._act_telegram = system_menu.addAction("Telegram Alerts…")
        self._act_telegram.setToolTip("Link a Telegram bot for off-device push alerts")
        system_menu.addSeparator()
        act_wipe = system_menu.addAction("Wipe Data…")
        act_wipe.setToolTip("Delete recordings, caches and stored data (PIN-gated)")

        self._act_probe.triggered.connect(self._run_probe)
        self._act_discover.triggered.connect(self._run_discovery)
        self._act_pbprobe.triggered.connect(self._run_pbprobe)
        self._act_rename.triggered.connect(self._open_rename_dialog)
        self._act_telegram.triggered.connect(self._open_telegram_dialog)
        act_wipe.triggered.connect(self._open_wipe_dialog)

        self._system_btn.setMenu(system_menu)

        layout.addWidget(brand)
        layout.addWidget(version)
        layout.addSpacing(20)
        layout.addWidget(self._mode_live_btn)
        layout.addWidget(self._mode_pb_btn)
        layout.addWidget(spacer)
        layout.addWidget(self._reconnect_btn)
        layout.addWidget(self._console_btn)
        layout.addWidget(self._system_btn)
        return bar

    def _open_wipe_dialog(self) -> None:
        dlg = WipeDialog(self._settings, expected_pin="123", parent=self)
        dlg.exec()

    def _open_telegram_dialog(self) -> None:
        dlg = TelegramDialog(
            self._settings.telegram_bot_token,
            self._settings.telegram_chat_id,
            commands_enabled=self._settings.telegram_commands,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        token, chat_id, commands = dlg.values()
        persist_env_values(self._settings, {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_CHAT_ID": chat_id,
            "TELEGRAM_COMMANDS": "1" if commands else "0",
        })
        self._settings = replace(
            self._settings, telegram_bot_token=token, telegram_chat_id=chat_id,
            telegram_commands=commands,
        )
        self._notifier.configure(token, chat_id, commands_enabled=commands)
        bus.info("APP", "Telegram settings updated")

    def _open_rename_dialog(self) -> None:
        dlg = CameraNamesDialog(self._cameras, dict(self._camera_names), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._camera_names = dlg.names()
        camera_names.save(self._settings.env_path, self._camera_names)
        self._cam_labels = {
            c.index: self._effective_cam_name(c) for c in self._cameras
        }
        for tile, cam in zip(self._tiles, self._cameras):
            tile.set_display_name(self._cam_labels[cam.index])
        self._notifier.set_cam_labels(self._cam_labels)
        bus.info("APP", "camera names updated")

    @Slot(int, str)
    def _on_tile_renamed(self, cam_index: int, raw: str) -> None:
        name = camera_names.clean_name(raw)
        cam = next((c for c in self._cameras if c.index == cam_index), None)
        if cam is None:
            return
        default = getattr(cam, "location", None) or getattr(cam, "label", None) or ""
        # Typing the default (or clearing the field) means "no custom name".
        if not name or name == default:
            self._camera_names.pop(cam_index, None)
        else:
            self._camera_names[cam_index] = name
        camera_names.save(self._settings.env_path, self._camera_names)
        self._cam_labels = {
            c.index: self._effective_cam_name(c) for c in self._cameras
        }
        for tile, c in zip(self._tiles, self._cameras):
            tile.set_display_name(self._cam_labels[c.index])
        self._notifier.set_cam_labels(self._cam_labels)
        bus.info("APP", f"cam{cam_index} renamed -> {self._cam_labels[cam_index]}")

    def _set_mode(self, mode: str) -> None:
        if mode == "live":
            self._mode_live_btn.setChecked(True)
            self._mode_pb_btn.setChecked(False)
            self._stack.setCurrentIndex(0)
            self._reconnect_btn.setVisible(True)
            bus.info("APP", "switched to LIVE mode")
        else:
            self._mode_live_btn.setChecked(False)
            self._mode_pb_btn.setChecked(True)
            self._stack.setCurrentIndex(1)
            self._reconnect_btn.setVisible(False)
            self._playback_view.refresh_library()
            bus.info("APP", "switched to PLAYBACK mode")

    def _effective_cam_name(self, cam) -> str:
        """Custom name if set, else the camera's built-in location, else label."""
        return (
            self._camera_names.get(cam.index)
            or getattr(cam, "location", None)
            or getattr(cam, "label", None)
            or f"camera {cam.index}"
        )

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
            tile.event_arm_toggled.connect(self._on_arm_toggled)
            tile.rename_committed.connect(self._on_tile_renamed)
            # Feed live preview frames to the real-time alert tier (throttled in
            # the tile to ~2 fps). Without this the LiveDetector gets no frames
            # and never fires — the whole instant-alert path is dead.
            tile.frame_tapped.connect(self._live_detector.submit)
            tiles.append(tile)
            row, col = divmod(i, 2)
            grid.addWidget(tile, row, col)

        self._grid_focus = GridFocus(
            grid, tiles,
            index_of=lambda t: t._camera.index,
            set_selected=lambda t, on: t.set_focus_selected(on),
            parent=self,
        )
        for tile in tiles:
            tile.double_clicked.connect(self._grid_focus.handle_double_click)
        for tile, cam in zip(tiles, cameras):
            tile.set_display_name(self._cam_labels[cam.index])
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

        self._status_ai = QLabel("AI: off", bar)
        self._status_ai.setObjectName("StatusBarText")

        self._status_clock = QLabel("", bar)
        self._status_clock.setObjectName("StatusBarText")

        layout.addWidget(self._status_dvr)
        layout.addWidget(spacer)
        layout.addWidget(self._status_recorder)
        layout.addWidget(self._status_ai)
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
        if self._analyzer is not None:
            # Finalize held cross-segment events while the recorder's segments
            # still exist on disk, then stop the worker and the recorder.
            self._analyzer.flush_all()
            self._analyzer.request_stop()
            self._analyzer.wait(3000)
        if self._recorder is not None:
            self._recorder.stop(wait_ms=5000)
        for tile in self._tiles:
            tile.shutdown(wait_ms=2000)
        self._playback_view.shutdown()
        if self._notifier is not None:
            self._notifier.shutdown()  # stop the Telegram reply listener thread
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
        self._act_pbprobe.setEnabled(False)
        self._pbprobe.finished_with.connect(lambda _r: self._act_pbprobe.setEnabled(True))
        self._pbprobe.start()
        if not self._console.isVisible():
            self._console.show()

    def _run_discovery(self) -> None:
        if self._discovery is not None and self._discovery.isRunning():
            bus.info("DISC", "discovery already in progress")
            return
        cached = load_ip_cache(self._settings.env_path)
        priority = tuple(r.ip for r in cached if r.ip != self._settings.dvr_ip)
        self._discovery = DiscoveryWorker(
            self._settings.dvr_port,
            priority_ips=priority,
            parent=self,
        )
        self._discovery.completed.connect(self._on_discovery_done)
        self._act_discover.setEnabled(False)
        self._discovery.start()

    def _on_discovery_done(self, result: DiscoveryResult) -> None:
        self._act_discover.setEnabled(True)
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
        if self._settings.detection_enabled:
            self._start_analyzer()
            if self._analyzer is not None:
                self._recorder.segment_closed.connect(self._analyzer.enqueue)
        self._recorder.start()

    def _start_analyzer(self) -> None:
        if self._analyzer is not None:
            return
        model = self._settings.detection_model
        if not model.is_file():
            bus.warn("AI", f"model missing ({model}); detection disabled this session")
            self._status_ai.setText("AI: no model")
            return
        event_cfg = EventConfig(
            enabled=self._settings.event_extraction_enabled,
            pre_roll_s=self._settings.event_pre_roll_s,
            post_roll_s=self._settings.event_post_roll_s,
            merge_gap_s=self._settings.event_merge_gap_s,
            min_hits=self._settings.event_min_hits,
        )
        self._analyzer = SegmentAnalyzer(
            model_path=model,
            conf=self._settings.detection_conf,
            sample_seconds=self._settings.detection_sample_seconds,
            event_cfg=event_cfg,
            events_dir=self._settings.events_dir,
            recording_dir=self._settings.recording_dir,
            cam_ids=[cam.index for cam in self._cameras],
            armed=set(self._armed_cameras),
            max_duration_s=self._settings.event_max_duration_s,
            hold_timeout_s=self._settings.event_hold_timeout_s,
            parent=self,
        )
        self._analyzer.totals_changed.connect(self._on_ai_totals)
        self._analyzer.event_extracted.connect(self._on_event_extracted)
        self._analyzer.start()
        self._status_ai.setText(self._ai_status_text(0, 0))

    def _ai_status_text(self, person_segments: int, vehicle_segments: int) -> str:
        n = len(self._armed_cameras)
        total = len(self._cameras)
        return f"AI: {n}/{total} armed  {person_segments}P / {vehicle_segments}V"

    @Slot(int, bool)
    def _on_arm_toggled(self, cam_index: int, armed: bool) -> None:
        if armed:
            self._armed_cameras.add(cam_index)
        else:
            self._armed_cameras.discard(cam_index)
        if self._analyzer is not None:
            self._analyzer.set_armed(set(self._armed_cameras))
        self._live_detector.set_armed(set(self._armed_cameras))
        # refresh the armed count in the status bar without waiting for a scan
        self._status_ai.setText(self._ai_status_text(0, 0))

    def _on_live_alert(self, cam_id: int, title: str, thumb_path: str) -> None:
        label = self._cam_labels.get(cam_id, f"camera {cam_id}")
        self._notifier.notify_live(label, title, thumb_path)

    def _on_event_extracted(self, clip) -> None:
        cams = "+".join(f"cam{c}" for c in clip.cams_captured) or "none"
        bus.info(
            "EVT",
            f"event saved: cam{clip.cam_id} triggered {clip.start_at:%H:%M:%S} "
            f"{clip.label}  ({cams})  -> {clip.folder}",
        )
        self._notifier.notify(clip, self._cam_labels.get(clip.cam_id))
        # Immediately surface the new event in the playback Events gallery
        # (fast path past the 30s timer; also handles day rollover). Best-effort
        # so a refresh hiccup can never break the notification above.
        try:
            self._playback_view.note_new_event(getattr(clip, "start_at", None))
        except Exception as e:  # pragma: no cover - defensive
            bus.warn("EVT", f"events-list live refresh failed: {e!s}")

    def _on_ai_totals(self, person_segments: int, vehicle_segments: int) -> None:
        self._status_ai.setText(self._ai_status_text(person_segments, vehicle_segments))

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
