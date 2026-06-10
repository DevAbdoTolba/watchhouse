"""Main window: toolbar over a 2x2 camera grid over a status bar, plus a
toggleable bottom-docked admin log console."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime

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
from app.core.perf import UiLoopProbe
from app.core.pipeline import Pipeline
from app.core.pins import Pins
from app.core.playback_probe import PlaybackProbeWorker
from app.core.probe import ProbeWorker
from app.core import camera_names
from app.core import detect_regions as dr
from app.core import detlog
from app.core import dvr_time
from app.core import camera_links
from app.core import detection_prefs
from app.core import watchdog
from app.ui.camera_tile import CameraTile
from app.ui.grid_focus import GridFocus
from app.ui.console_panel import ConsolePanel
from app.ui.playback_view import PlaybackView
from app.ui.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        # Make every displayed time match the DVR's (non-DST) clock. Pure display
        # shift; recording filenames/lookups stay on PC time.
        dvr_time.set_offset_minutes(settings.dvr_time_offset_minutes)
        # Detection history log: one line per analyzed segment (kept/dropped
        # confidences) so "app down" vs "threshold too high" is always visible.
        detlog.set_path(settings.recording_dir / "detections.log")
        # Permanent-keep ('● KEEPING') state lives here (the live view) and is
        # persisted via Pins; the pruner and the playback timeline read the file.
        self._pins = Pins.load(settings.env_path)
        self._keep_timer = QTimer(self)
        self._keep_timer.timeout.connect(self._update_keep_counter)
        self._kept_size_cache = (0.0, 0)  # (monotonic measured at, bytes)
        self._probe: ProbeWorker | None = None
        self._discovery: DiscoveryWorker | None = None
        self._pbprobe: PlaybackProbeWorker | None = None
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
        # Per-camera person-confidence floors: the .env baseline overlaid with the
        # user's saved UI edits (.cctv-detection.json). A cam absent here is
        # uncapped (keeps every person the model reports); a noisy view (door cam)
        # carries a high floor so rubbish bags don't read as people. This is the
        # ONLY person gate now (the global floor was removed).
        self._person_floors: dict[int, float] = {
            **settings.detection_person_conf_by_cam,
            **detection_prefs.load(settings.env_path),
        }
        # Per-camera live-alert detect rectangles (drag on the tile).
        self._detect_regions = dr.load(settings.env_path)
        self._camera_links = camera_links.load(settings.env_path)

        # The whole backend (recorder -> analyzer -> events -> collision ->
        # Telegram + the live alert tier) lives behind one seam; this window
        # only renders its signals and forwards the user's preference edits.
        self._pipeline = Pipeline(
            settings, self._cameras, self._armed_cameras,
            self._person_floors, self._detect_regions,
            self._camera_links, self._cam_labels, parent=self,
        )
        self._pipeline.recorder_stats.connect(self._on_recorder_stats)
        self._pipeline.ai_totals.connect(self._on_ai_totals)
        self._pipeline.ai_model_missing.connect(
            lambda: self._status_ai.setText("AI: no model"))
        self._pipeline.event_extracted.connect(self._on_event_extracted)

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

        # Event-loop lag probe: one PERF summary line per ~30s window; WARN
        # when a tick stalls, so UI-thread regressions are caught by numbers.
        self._ui_probe = UiLoopProbe(self)

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
            "QMenu { background: #1f242e; border: 1px solid #2a3040; color: #ebe7e1; }"
            "QMenu::item { padding: 6px 20px; }"
            "QMenu::item:selected { background: #2a2017; color: #c69561; }"
            "QMenu::separator { background: #2a3040; height: 1px; margin: 4px 0; }"
        )
        self._act_probe    = system_menu.addAction("Test DVR Connection")
        self._act_discover = system_menu.addAction("Discover DVR on LAN")
        self._act_pbprobe  = system_menu.addAction("Probe Playback Protocols")
        system_menu.addSeparator()
        act_settings = system_menu.addAction("Settings…")
        act_settings.setToolTip(
            "Cameras, detection, Telegram, camera links, system (incl. "
            "watchdog + wipe)")

        self._act_probe.triggered.connect(self._run_probe)
        self._act_discover.triggered.connect(self._run_discovery)
        self._act_pbprobe.triggered.connect(self._run_pbprobe)
        act_settings.triggered.connect(self._open_settings_dialog)

        self._system_btn.setMenu(system_menu)

        # KEEP RECORDING — live-mode toggle: keep ALL footage from now on
        # (nothing auto-deleted) with a running since/length/size counter.
        self._keep_status = QLabel("", bar)
        self._keep_status.setObjectName("Version")
        self._keep_status.setStyleSheet("color:#8a91a0; font-size:11px;")
        # Recorder-style toggle: red ● = start keeping all footage, red ❚❚ = stop.
        self._keep_btn = QPushButton("●", bar)
        self._keep_btn.setObjectName("ToolbarAction")
        self._keep_btn.setCheckable(True)
        self._keep_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._keep_btn.setMinimumHeight(30)
        self._keep_btn.setFixedWidth(42)
        self._keep_btn.setStyleSheet("color:#e0524a; font-size:16px; font-weight:bold;")
        self._keep_btn.setToolTip(
            "● Keep ALL recording from now on (nothing auto-deleted); ❚❚ to stop")
        self._keep_btn.toggled.connect(self._toggle_keep)

        layout.addWidget(brand)
        layout.addWidget(version)
        layout.addSpacing(20)
        layout.addWidget(self._mode_live_btn)
        layout.addWidget(self._mode_pb_btn)
        layout.addWidget(spacer)
        layout.addWidget(self._keep_status)
        layout.addWidget(self._keep_btn)
        layout.addWidget(self._reconnect_btn)
        layout.addWidget(self._console_btn)
        layout.addWidget(self._system_btn)
        return bar

    def _open_settings_dialog(self) -> None:
        wd_before = watchdog.enabled(self._settings.recording_dir,
                                     default=self._settings.watchdog_enabled)
        dlg = SettingsDialog(
            self._cameras, self._settings,
            names=dict(self._camera_names),
            floors=dict(self._person_floors),
            links=self._camera_links,
            cam_labels=self._cam_labels,
            watchdog_on=wd_before,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self._apply_camera_names(dlg.cameras_page.names())

        # Keep only the meaningful (capped) cameras; 0 means uncapped.
        self._person_floors = {
            c: f for c, f in dlg.detection_page.values().items() if f > 0.0
        }
        detection_prefs.save(self._settings.env_path, self._person_floors)
        self._pipeline.set_person_floors(self._person_floors)

        self._camera_links = dlg.links_page.values()
        camera_links.save(self._settings.env_path, self._camera_links)
        self._pipeline.set_links(self._camera_links)

        # Telegram is applied only on change: reconfigure restarts the reply
        # poller and clears the debounce state, so don't churn it needlessly.
        token, chat_id, commands, lang = dlg.telegram_page.values()
        if (token, chat_id, commands, lang) != (
                self._settings.telegram_bot_token,
                self._settings.telegram_chat_id,
                self._settings.telegram_commands,
                self._settings.telegram_lang):
            persist_env_values(self._settings, {
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_CHAT_ID": chat_id,
                "TELEGRAM_COMMANDS": "1" if commands else "0",
                "TELEGRAM_LANG": lang,
            })
            self._settings = replace(
                self._settings, telegram_bot_token=token,
                telegram_chat_id=chat_id, telegram_commands=commands,
                telegram_lang=lang,
            )
            self._pipeline.configure_telegram(
                token, chat_id, commands_enabled=commands, lang=lang)

        wd_after = dlg.system_page.watchdog_enabled()
        if wd_after != wd_before:
            self._apply_watchdog(wd_after)
        bus.info("APP", "settings saved")

    def _apply_camera_names(self, names: dict[int, str]) -> None:
        """Persist custom names and refresh every surface that shows them."""
        self._camera_names = names
        camera_names.save(self._settings.env_path, self._camera_names)
        self._cam_labels = {
            c.index: self._effective_cam_name(c) for c in self._cameras
        }
        for tile, cam in zip(self._tiles, self._cameras):
            tile.set_display_name(self._cam_labels[cam.index])
        self._pipeline.set_cam_labels(self._cam_labels)

    @Slot(int, str)
    def _on_tile_renamed(self, cam_index: int, raw: str) -> None:
        name = camera_names.clean_name(raw)
        cam = next((c for c in self._cameras if c.index == cam_index), None)
        if cam is None:
            return
        default = getattr(cam, "location", None) or getattr(cam, "label", None) or ""
        names = dict(self._camera_names)
        # Typing the default (or clearing the field) means "no custom name".
        if not name or name == default:
            names.pop(cam_index, None)
        else:
            names[cam_index] = name
        self._apply_camera_names(names)
        bus.info("APP", f"cam{cam_index} renamed -> {self._cam_labels[cam_index]}")

    def _set_mode(self, mode: str) -> None:
        if mode == "live":
            self._mode_live_btn.setChecked(True)
            self._mode_pb_btn.setChecked(False)
            self._stack.setCurrentIndex(0)
            self._reconnect_btn.setVisible(True)
            self._keep_btn.setVisible(True)
            self._keep_status.setVisible(True)
            bus.info("APP", "switched to LIVE mode")
        else:
            self._mode_live_btn.setChecked(False)
            self._mode_pb_btn.setChecked(True)
            self._stack.setCurrentIndex(1)
            self._reconnect_btn.setVisible(False)
            self._keep_btn.setVisible(False)
            self._keep_status.setVisible(False)
            self._playback_view.refresh_library()
            bus.info("APP", "switched to PLAYBACK mode")

    def _toggle_keep(self, on: bool) -> None:
        if on:
            if self._pins.keep_from is None:
                self._pins.set_keep_from(datetime.now())
            self._kept_size_cache = (0.0, 0)  # force a fresh size measure
            self._keep_timer.start(1000)
            self._keep_btn.setText("❚❚")
        else:
            self._pins.stop_keep(datetime.now())
            self._keep_timer.stop()
            self._keep_btn.setText("●")
            bus.info("REC", "keep-recording off; kept window frozen as permanent")
        self._update_keep_counter()

    def _kept_size_bytes(self, cutoff_ts: float) -> int:
        total = 0
        base = self._settings.recording_dir
        if not base.is_dir():
            return 0
        for d in base.glob("cam*"):
            if not d.is_dir():
                continue
            for mp4 in d.glob("*.mp4"):
                try:
                    st = mp4.stat()
                    if st.st_mtime >= cutoff_ts:
                        total += st.st_size
                except OSError:
                    continue
        return total

    def _update_keep_counter(self) -> None:
        if getattr(self, "_keep_status", None) is None:
            return
        kf = self._pins.keep_from
        if kf is None:
            self._keep_status.setText("")
            return
        secs = max(0, int((datetime.now() - kf).total_seconds()))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        # Stat-walking every segment each second froze the UI; the byte count
        # only needs to be fresh-ish, so re-measure at most every 10 s.
        measured_at, size = self._kept_size_cache
        now = time.monotonic()
        if measured_at == 0.0 or now - measured_at >= 10.0:
            size = self._kept_size_bytes(kf.timestamp())
            self._kept_size_cache = (now, size)
        mb = size / 1024 / 1024
        self._keep_status.setText(
            f"● {dvr_time.shift(kf):%H:%M:%S} · {h}:{m:02d}:{s:02d} · {mb:.0f} MB")

    def _effective_cam_name(self, cam) -> str:
        return camera_names.display_name(cam, self._camera_names)

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
            # Feed live preview frames to the real-time alert tier (sampled in
            # the stream worker at ~2 fps). Without this the LiveDetector gets
            # no frames and never fires — the whole instant-alert path is dead.
            tile.frame_tapped.connect(self._pipeline.live_detector.submit)
            tile.set_detect_region(self._detect_regions.get(cam.index, dr.DEFAULT))
            tile.detect_region_changed.connect(self._on_detect_region_changed)
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
        self._ui_probe.start()  # idempotent across repeated shows
        for tile in self._tiles:
            tile.start()
        # Restore the KEEP-recording state once if it was left on last session.
        if not getattr(self, "_keep_restored", False):
            self._keep_restored = True
            if self._pins.keep_from is not None:
                self._keep_btn.blockSignals(True)
                self._keep_btn.setChecked(True)
                self._keep_btn.blockSignals(False)
                self._keep_btn.setText("❚❚")
                self._keep_timer.start(1000)
            self._update_keep_counter()
        # Kick off DVR probe shortly after the window has painted, so the
        # log entries appear in the console if the user opens it.
        QTimer.singleShot(400, self._run_probe)
        # Recorder, analyzer, watchdog heartbeat — all backend.
        self._pipeline.start()

    def _apply_watchdog(self, on: bool) -> None:
        rec_dir = self._settings.recording_dir
        watchdog.set_enabled(rec_dir, on)
        if on:
            watchdog.touch_heartbeat(rec_dir)
            watchdog.spawn_if_enabled(self._settings)
            bus.info("WD", "auto-restart watchdog ON")
        else:
            # The running watchdog notices the toggle next loop and exits.
            bus.info("WD", "auto-restart watchdog OFF")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # Tell the watchdog this is a deliberate quit so it doesn't relaunch us.
        watchdog.mark_shutdown(self._settings.recording_dir)
        self._ui_probe.stop()
        self._refresh_clock.stop()
        self._keep_timer.stop()
        # Tiles first (no more live frames), then the backend can stop.
        for tile in self._tiles:
            tile.shutdown(wait_ms=2000)
        self._pipeline.shutdown()
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
        self._pipeline.set_armed(self._armed_cameras)

    def _on_detect_region_changed(self, cam_index: int) -> None:
        tile = next((t for t in self._tiles if t._camera.index == cam_index), None)
        if tile is None:
            return
        self._detect_regions[cam_index] = tile.detect_region()
        dr.save(self._settings.env_path, self._detect_regions)
        self._pipeline.set_regions(self._detect_regions)
        bus.info("LIVE", f"cam{cam_index}: live detect zone updated")
        # refresh the armed count in the status bar without waiting for a scan
        self._status_ai.setText(self._ai_status_text(0, 0))

    def _on_event_extracted(self, clip) -> None:
        # Immediately surface the new event in the playback Events gallery
        # (fast path past the 30s timer; also handles day rollover). Best-effort
        # so a refresh hiccup can never break the notification path behind it.
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
        self._status_clock.setText(dvr_time.now().strftime("%Y-%m-%d  %H:%M:%S"))
