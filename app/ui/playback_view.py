"""Playback mode: calendar + camera checkboxes + 2x2 video grid + timeline + transport."""

from __future__ import annotations

from datetime import date as _date, datetime, time as _time, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QObject, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QFileDialog,
    QListView,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core import camera_names
from app.core.cameras import Camera
from app.core.clip_library import Clip, clips_for_day, dates_with_clips, find_clip_at, scan
from app.core.config import Settings
from app.core.event_library import (
    EventRecord,
    EventSession,
    event_dates,
    events_for_day,
    group_sessions,
)
from app.core.log import bus
from app.core.playback_player import PlaybackPlayer
from app.core.session_export import export_session
from app.ui import theme
from app.ui.camera_tile import VideoPanel
from app.ui.events_model import EventsModel, ThumbnailLoader
from app.ui.events_scan_worker import EventsScanWorker
from app.ui.grid_focus import GridFocus
from app.ui.icon_button import IconButton
from app.ui.import_clip_dialog import ImportClipDialog
from app.ui.timeline_drawer import TimelineDrawer


class _StayOpenMenu(QMenu):
    """A QMenu that stays open when a *checkable* action is clicked, so the user
    can toggle several items (e.g. the camera filter) in one pass. A plain
    QMenu dismisses on any trigger; here a checkable item toggles in place and
    only a non-checkable item, Esc, or click-away closes it."""

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt)
        action = self.activeAction()
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()  # toggles + emits toggled(); menu stays visible
            return
        super().mouseReleaseEvent(event)


class _SessionExportWorker(QObject):
    """Runs export_session off the UI thread (a concat of hours of clips can
    take a while). Lives on its own QThread; reports done via `finished`."""

    finished = Signal(bool, str)  # ok, out_path

    def __init__(self, session: EventSession, cam_id: int, out_path) -> None:
        super().__init__()
        self._session = session
        self._cam_id = cam_id
        self._out_path = out_path

    @Slot()
    def run(self) -> None:
        ok = False
        try:
            ok = export_session(self._session, self._cam_id, self._out_path)
        except Exception as e:  # never let a bad export crash the thread
            bus.warn("EVT", f"save-full-clip: export raised: {e!s}")
            ok = False
        self.finished.emit(ok, str(self._out_path))


class PlaybackTile(QFrame):
    """Header + VideoPanel + own PlaybackPlayer for one camera in playback mode."""

    double_clicked = Signal(int, bool)  # camera index, shift_held

    def __init__(self, camera: Camera, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CameraTile")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._camera = camera
        self._current_clip: Clip | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("TileHeader")
        header.setFixedHeight(34)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 0, 10, 0)
        hl.setSpacing(10)

        self._name = QLabel(camera.label, header)
        self._name.setObjectName("TileName")

        self._sub = QLabel("idle", header)
        self._sub.setObjectName("TileLocation")
        self._sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        # Shown only on the camera that triggered the currently-loaded event.
        self._badge = QLabel("◉ DETECTED", header)
        self._badge.setObjectName("TileDetected")
        self._badge.setVisible(False)

        hl.addWidget(self._name)
        hl.addWidget(self._sub, 1)
        hl.addWidget(self._badge)

        self._video = VideoPanel(self)
        self._video.set_message("No clip selected")

        outer.addWidget(header)
        outer.addWidget(self._video, 1)

        self._player = PlaybackPlayer(label=f"PB{camera.index}", parent=self)
        self._player.frame_ready.connect(self._video.set_frame)
        self._player.state_changed.connect(self._on_state)
        self._player.start()

    def shutdown(self, wait_ms: int = 2000) -> None:
        self._player.request_stop()
        self._player.wait(wait_ms)

    def load_for(self, when: datetime, day_clips: list[Clip]) -> None:
        match = find_clip_at(day_clips, when)
        if match is None:
            self._current_clip = None
            self._player.load(None)
            self._video.set_message("No recording at this time")
            self._sub.setText("no clip")
            return
        clip, offset = match
        if clip is not self._current_clip:
            self._current_clip = clip
            self._sub.setText(clip.path.name)
            self._player.load(clip.path, start_offset_s=offset)
        else:
            self._player.seek_seconds(offset)

    def load_path(self, path, sublabel: str = "") -> None:
        """Load a specific file at offset 0 (used by the Events view, where
        each tile plays one camera's clip of the selected event)."""
        self._current_clip = None
        if path is None:
            self._player.load(None)
            self._video.set_message("No clip for this camera")
            self._sub.setText("no clip")
            return
        self._sub.setText(sublabel or path.name)
        self._player.load(path, start_offset_s=0.0)

    def play(self) -> None: self._player.play()
    def pause(self) -> None: self._player.pause()
    def toggle(self) -> None: self._player.toggle()
    def set_speed(self, s: float) -> None: self._player.set_speed(s)
    def seek(self, seconds: float) -> None: self._player.seek_seconds(max(0.0, seconds))
    def set_overlay(self, track) -> None: self._player.set_overlay(track)
    def set_overlay_enabled(self, on: bool) -> None: self._player.set_overlay_enabled(on)

    def set_triggered(self, on: bool) -> None:
        """Mark this tile as the camera that detected the loaded event."""
        self._badge.setVisible(on)
        self.setProperty("triggered", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_focus_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        event.accept()  # own the press so the double-click is delivered here

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.double_clicked.emit(self._camera.index, shift)
        event.accept()

    @Slot(str)
    def _on_state(self, state: str) -> None:
        if state == "empty":
            self._sub.setText("no clip")
        elif state == "eof":
            self._sub.setText("end of clip")
        elif state == "playing":
            if self._current_clip:
                self._sub.setText(self._current_clip.path.name)
        elif state == "paused":
            self._sub.setText("paused")
        elif state == "error":
            self._sub.setText("error")


class _ScrubSlider(QSlider):
    """Horizontal slider where clicking anywhere on the groove jumps the handle
    to that spot (the default QSlider only page-steps toward a groove click)."""

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            span = self.maximum() - self.minimum()
            if span > 0 and self.width() > 0:
                frac = event.position().x() / self.width()
                frac = min(1.0, max(0.0, frac))
                value = self.minimum() + round(frac * span)
                self.setValue(value)
                self.sliderPressed.emit()
                self.sliderMoved.emit(value)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().mouseReleaseEvent(event)
        self.sliderReleased.emit()


class PlaybackView(QWidget):
    """The PLAYBACK mode's central widget. Owns the calendar, camera
    checkboxes, the 4 PlaybackTiles, the timeline, and the transport."""

    def __init__(self, cameras: tuple[Camera, ...], settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._cameras = cameras
        self._settings = settings
        self._library: dict[int, list[Clip]] = {}
        self._selected_cams: set[int] = {c.index for c in cameras}
        self._selected_day: _date = datetime.now().date()
        # "Follow latest" keeps the Events list pinned to the newest day that
        # has events so live detections appear without the user clicking the
        # calendar. Set False once the user deliberately browses an older day
        # (so we never yank them off history), back True when they return to
        # the newest day. True at startup. See _on_date_changed / _on_events_scanned.
        self._follow_latest_day: bool = True
        self._cursor: datetime = datetime.combine(self._selected_day, _time(0, 0))
        self._is_playing = False
        self._speed = 1.0
        self._step_seconds = 5
        # Events sub-mode: "recordings" (timeline scrubbing) | "events" (gallery)
        self._mode = "recordings"
        self._events: list[EventRecord] = []
        self._day_events: list[EventRecord] = []
        self._day_sessions: list[EventSession] = []
        self._current_event: EventRecord | None = None
        self._current_session: EventSession | None = None
        # Background "Save full clip" export (one at a time).
        self._export_thread: QThread | None = None
        self._export_worker: _SessionExportWorker | None = None
        # Background events scan (one at a time) feeds the in-memory list the
        # model filters against; filters never touch disk.
        self._scan_thread: QThread | None = None
        self._scan_worker: EventsScanWorker | None = None
        self._scan_rescan_pending = False
        self._all_sessions: list[EventSession] = []  # full, day-agnostic
        self._thumb_loader = ThumbnailLoader(self)
        self._event_pos = 0.0  # clip-relative seconds, for stepping + label
        self._event_duration = 0.0  # trigger clip length (s); 0 until known
        self._scrubbing = False  # user is dragging the scrub bar right now
        self._event_conf_min = 0.0  # min human-detection confidence to show (0..1)
        self._event_cam_filter: set[int] = {c.index for c in cameras}  # trigger cams to show
        self._cam_filter_actions: dict[int, QAction] = {}
        self._boxes_on = True  # draw detection boxes over event playback

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        # Left sidebar
        sidebar = self._build_sidebar()
        sidebar.setFixedWidth(220)
        root.addWidget(sidebar)

        # Right side: grid + timeline + transport
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)

        grid_wrap, self._tiles = self._build_grid()
        right.addWidget(grid_wrap, 1)

        self._timeline = TimelineDrawer([c.index for c in cameras], parent=self)
        self._timeline.seek_requested.connect(self._on_timeline_seek)
        right.addWidget(self._timeline)

        right.addWidget(self._build_transport())

        right_wrap = QWidget(self)
        right_wrap.setLayout(right)
        root.addWidget(right_wrap, 1)

        # Refresh every 30s so newly recorded clips / events appear
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._periodic_refresh)
        self._refresh_timer.start(30_000)

        # Bind ALL tile players' position/duration/state once. The handlers
        # check whether the emitting player is the current event's trigger
        # tile, so switching events needs no connect/disconnect churn.
        for tile in self._tiles:
            tile._player.position_changed.connect(self._on_player_position)
            tile._player.duration_known.connect(self._on_player_duration)
            tile._player.state_changed.connect(self._on_player_state)

        self.refresh_library()
        self._cursor_tick = QTimer(self)
        self._cursor_tick.timeout.connect(self._advance_cursor)
        self._cursor_tick.start(250)

    # --- Sidebar build ---

    def _build_sidebar(self) -> QWidget:
        side = QWidget(self)
        side.setObjectName("PlaybackSidebar")
        v = QVBoxLayout(side)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        # Mode toggle: scrub recordings vs. browse detected events.
        mode_row = QWidget(side)
        mr = QHBoxLayout(mode_row)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.setSpacing(0)
        self._mode_btns: dict[str, QPushButton] = {}
        for key, lbl in (("recordings", "RECORDINGS"), ("events", "EVENTS")):
            b = QPushButton(lbl, mode_row)
            b.setObjectName("ModeToggle")
            b.setCheckable(True)
            b.setChecked(key == self._mode)
            b.setFixedHeight(28)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c, k=key: self._set_mode(k))
            self._mode_btns[key] = b
            mr.addWidget(b)
        v.addWidget(mode_row)

        title = QLabel("DATE", side)
        title.setObjectName("SidebarHeading")
        v.addWidget(title)

        self._calendar = QCalendarWidget(side)
        self._calendar.setObjectName("PlaybackCalendar")
        self._calendar.setGridVisible(False)
        self._calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self._calendar.setNavigationBarVisible(True)
        self._calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self._calendar.setSelectedDate(QDate.currentDate())
        self._calendar.selectionChanged.connect(self._on_date_changed)
        v.addWidget(self._calendar)

        # --- Recordings section (cameras + import) ---
        self._rec_section = QWidget(side)
        rv = QVBoxLayout(self._rec_section)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(14)

        title2 = QLabel("CAMERAS", self._rec_section)
        title2.setObjectName("SidebarHeading")
        rv.addWidget(title2)

        self._cam_checkboxes: dict[int, QCheckBox] = {}
        for cam in self._cameras:
            cb = QCheckBox(cam.label, self._rec_section)
            cb.setChecked(True)
            cb.toggled.connect(lambda checked, i=cam.index: self._on_cam_toggled(i, checked))
            self._cam_checkboxes[cam.index] = cb
            rv.addWidget(cb)

        rv.addSpacing(8)

        title3 = QLabel("LIBRARY", self._rec_section)
        title3.setObjectName("SidebarHeading")
        rv.addWidget(title3)

        self._import_btn = QPushButton("IMPORT CLIP", self._rec_section)
        self._import_btn.setObjectName("SidebarAction")
        self._import_btn.setToolTip(
            "Add a manually-exported DVR video to the playback library"
        )
        self._import_btn.clicked.connect(self._on_import_clip)
        rv.addWidget(self._import_btn)
        v.addWidget(self._rec_section)

        # --- Events section (gallery of detected events) ---
        self._events_section = QWidget(side)
        ev = QVBoxLayout(self._events_section)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(8)

        self._events_heading = QLabel("EVENTS", self._events_section)
        self._events_heading.setObjectName("SidebarHeading")
        ev.addWidget(self._events_heading)

        filt_row = QWidget(self._events_section)
        fr = QHBoxLayout(filt_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setSpacing(6)
        fr.addWidget(QLabel("MIN HUMAN", filt_row))
        self._conf_combo = QComboBox(filt_row)
        self._conf_combo.setObjectName("EventConfFilter")
        for lbl, val in (("Any", 0.0), ("40%", 0.40), ("60%", 0.60),
                         ("75%", 0.75), ("90%", 0.90)):
            self._conf_combo.addItem(lbl, val)
        self._conf_combo.currentIndexChanged.connect(self._on_conf_filter_changed)
        fr.addWidget(self._conf_combo, 1)
        fr.addWidget(QLabel("CAMERAS", filt_row))
        fr.addWidget(self._build_camera_filter(filt_row), 1)
        ev.addWidget(filt_row)

        self._events_model = EventsModel(self._thumb_loader, self)
        self._events_model.set_camera_labels(self._cam_name_map())
        self._events_list = QListView(self._events_section)
        self._events_list.setObjectName("EventsList")
        self._events_list.setIconSize(QSize(112, 63))
        self._events_list.setSpacing(3)
        self._events_list.setUniformItemSizes(False)
        self._events_list.setModel(self._events_model)
        self._events_list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._events_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Virtualization fetches the next page as the view scrolls near bottom.
        self._events_list.selectionModel().currentChanged.connect(
            self._on_event_index_changed
        )
        ev.addWidget(self._events_list, 1)

        # Stitch the whole selected presence into one continuous file on demand.
        self._save_full_btn = QPushButton("SAVE FULL CLIP", self._events_section)
        self._save_full_btn.setObjectName("SidebarAction")
        self._save_full_btn.setToolTip(
            "Stitch every clip of the selected presence into one continuous "
            "video (can be large)"
        )
        self._save_full_btn.setEnabled(False)
        self._save_full_btn.clicked.connect(self._on_save_full_clip)
        ev.addWidget(self._save_full_btn)

        self._events_section.setVisible(False)
        v.addWidget(self._events_section, 1)

        v.addStretch(0)
        return side

    def _cam_display_name(self, cam: Camera, names: dict[int, str]) -> str:
        return names.get(cam.index) or cam.location or cam.label

    def _cam_name_map(self) -> dict[int, str]:
        """trigger_cam -> display name, for the events model's row labels."""
        names = camera_names.load(self._settings.env_path)
        return {c.index: self._cam_display_name(c, names) for c in self._cameras}

    def _build_camera_filter(self, parent: QWidget) -> QToolButton:
        """Multi-select dropdown filtering events by their trigger camera."""
        names = camera_names.load(self._settings.env_path)
        btn = QToolButton(parent)
        btn.setObjectName("EventCamFilter")
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        menu = _StayOpenMenu(btn)
        menu.setStyleSheet(
            """
            QMenu {
                background: #1f242e;
                border: 1px solid #2a3040;
                padding: 6px;
            }
            QMenu::item {
                padding: 6px 14px;
                border-radius: 4px;
                color: #ebe7e1;
            }
            QMenu::item:selected {
                background: #2a2017;
                color: #c69561;
            }
            """
        )

        self._cam_filter_all = QAction("All", menu)
        self._cam_filter_all.setCheckable(True)
        self._cam_filter_all.setChecked(True)
        self._cam_filter_all.toggled.connect(self._on_cam_filter_all)
        menu.addAction(self._cam_filter_all)
        menu.addSeparator()

        for cam in self._cameras:
            act = QAction(self._cam_display_name(cam, names), menu)
            act.setCheckable(True)
            act.setChecked(cam.index in self._event_cam_filter)
            act.toggled.connect(lambda on, i=cam.index: self._on_cam_filter_toggled(i, on))
            menu.addAction(act)
            self._cam_filter_actions[cam.index] = act

        btn.setMenu(menu)
        self._cam_filter_btn = btn
        self._sync_cam_filter_label()
        return btn

    def _sync_cam_filter_label(self) -> None:
        names = camera_names.load(self._settings.env_path)
        total = len(self._cameras)
        n = len(self._event_cam_filter)
        if n == total:
            text = "All"
        elif n == 1:
            only = next(iter(self._event_cam_filter))
            cam = next((c for c in self._cameras if c.index == only), None)
            text = self._cam_display_name(cam, names) if cam else "1 cam"
        else:
            text = f"{n} cams"
        self._cam_filter_btn.setText(text)
        # Keep the "All" checkbox in sync without re-triggering its slot.
        self._cam_filter_all.blockSignals(True)
        self._cam_filter_all.setChecked(n == total)
        self._cam_filter_all.blockSignals(False)

    @Slot(bool)
    def _on_cam_filter_all(self, on: bool) -> None:
        self._event_cam_filter = {c.index for c in self._cameras} if on else set()
        for idx, act in self._cam_filter_actions.items():
            act.blockSignals(True)
            act.setChecked(idx in self._event_cam_filter)
            act.blockSignals(False)
        self._sync_cam_filter_label()
        self._apply_event_filters()

    def _on_cam_filter_toggled(self, cam_id: int, on: bool) -> None:
        if on:
            self._event_cam_filter.add(cam_id)
        else:
            self._event_cam_filter.discard(cam_id)
        self._sync_cam_filter_label()
        self._apply_event_filters()

    @Slot()
    def _on_import_clip(self) -> None:
        dlg = ImportClipDialog(
            cameras=self._cameras,
            imported_dir=self._settings.recording_dir / "imported",
            parent=self,
        )
        if dlg.exec() != ImportClipDialog.DialogCode.Accepted:
            return
        if dlg.result_path is None or dlg.result_when is None:
            return
        bus.info("PLAYBACK", f"library refresh after import of {dlg.result_path.name}")
        self.refresh_library()
        # Jump to the day + a couple seconds past the start so the player
        # has frames to draw.
        when = dlg.result_when
        self._selected_day = when.date()
        self._calendar.setSelectedDate(QDate(when.year, when.month, when.day))
        self._timeline.set_day(self._selected_day)
        self._cursor = when + timedelta(seconds=2)
        self._load_all_at_cursor()

    # --- Events mode ---

    def _set_mode(self, mode: str) -> None:
        if mode not in ("recordings", "events"):
            return
        self._mode = mode
        for key, btn in self._mode_btns.items():
            btn.setChecked(key == mode)
        self._rec_section.setVisible(mode == "recordings")
        self._events_section.setVisible(mode == "events")
        self._timeline.setVisible(mode == "recordings")
        # Scrub bar is the events-mode scrubber; recordings use the timeline.
        self._scrub_row.setVisible(mode == "events")
        # Stepping is finer inside short event clips.
        self._set_step(1 if mode == "events" else 5)
        self._is_playing = False
        self._play_btn.set_kind(IconButton.KIND_PLAY)
        for t in self._tiles:
            t.pause()
            if mode == "recordings":
                t.set_triggered(False)
                t.set_overlay_enabled(False)
                t.set_overlay(None)
        self._highlight_calendar_dates()
        if mode == "events":
            self._current_session = None
            self._current_event = None
            self._save_full_btn.setEnabled(False)
            self._reset_scrub()
            self.refresh_events()
            for t in self._tiles:
                t.load_path(None)
            self._cursor_label.setText("Select an event")
        else:
            self._load_all_at_cursor()

    def refresh_events(self) -> None:
        """Kick off a background scan of the events tree. The scan reads every
        event.json (slow), so it runs on a worker thread; the model/UI consume
        the already-built session list on completion. Overlapping scans are
        coalesced into a single trailing rescan."""
        if self._scan_thread is not None:
            self._scan_rescan_pending = True
            return
        thread = QThread(self)
        worker = EventsScanWorker(self._settings.events_dir)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_events_scanned)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._scan_thread = thread
        self._scan_worker = worker
        thread.start()

    @Slot(list, list)
    def _on_events_scanned(self, events: list, sessions: list) -> None:
        self._scan_thread = None
        self._scan_worker = None
        self._events = events
        self._all_sessions = sessions
        # Day-rollover safety: while following the latest day, advance the
        # selection to the newest day that actually has events (e.g. a brand
        # new event after midnight lands under tomorrow's folder). Skipped when
        # the user is browsing history, so we never interrupt them.
        self._maybe_follow_latest_day()
        self._highlight_calendar_dates()
        self._apply_event_filters()
        if self._scan_rescan_pending:
            self._scan_rescan_pending = False
            self.refresh_events()

    def _maybe_follow_latest_day(self) -> None:
        """If in follow-latest mode and the scan revealed events on a day newer
        than the current selection, move the selection (and the calendar) to
        that newest day so live detections always surface. No-op when browsing
        history (`_follow_latest_day` is False)."""
        if not self._follow_latest_day:
            return
        days = event_dates(self._events)
        if not days:
            return
        newest = max(days)
        if newest > self._selected_day:
            self._set_selected_day(newest)

    def _set_selected_day(self, day: _date) -> None:
        """Point the selection + calendar at `day` without re-entering
        `_on_date_changed` (which would clobber the follow-latest decision)."""
        self._selected_day = day
        self._timeline.set_day(day)
        self._calendar.blockSignals(True)
        self._calendar.setSelectedDate(QDate(day.year, day.month, day.day))
        self._calendar.blockSignals(False)

    def note_new_event(self, when: datetime | None = None) -> None:
        """Fast path: a new event was just extracted. Trigger an immediate
        (threaded) rescan so it appears within ~1s instead of waiting on the
        30s timer, and — when following the latest day — jump the selection to
        the new event's day so a post-midnight event is visible without a
        restart. Called best-effort from MainWindow._on_event_extracted."""
        if (when is not None and self._follow_latest_day
                and when.date() > self._selected_day):
            self._set_selected_day(when.date())
            self._highlight_calendar_dates()
            self._apply_event_filters()
        self.refresh_events()

    def _apply_event_filters(self) -> None:
        """Re-filter the in-memory session list (day + camera + confidence) and
        reset the model window to the first page. No disk access — only
        refresh_events re-scans. Selection is preserved by session_id."""
        prev_sid = self._current_session.session_id if self._current_session else None
        day_sessions = group_sessions(events_for_day(self._events, self._selected_day))
        self._day_sessions = [
            s for s in day_sessions
            if s.trigger_cam in self._event_cam_filter
            and any(m.person_conf >= self._event_conf_min for m in s.members)
        ]
        # Keep a flat per-event view for any legacy callers / position stepping.
        self._day_events = [m for s in self._day_sessions for m in s.members]
        self._events_model.set_camera_labels(self._cam_name_map())
        self._events_model.set_sessions(self._day_sessions)
        n = len(self._day_sessions)
        self._events_heading.setText(f"EVENTS — {self._selected_day:%b %d}  ({n})")
        if prev_sid is not None:
            for row, s in enumerate(self._day_sessions):
                if s.session_id == prev_sid:
                    self._select_event_row(row)
                    break

    def _select_event_row(self, row: int) -> None:
        """Highlight a row without restarting playback (guarded selection)."""
        if row < 0 or row >= self._events_model.total_count():
            return
        # Ensure the row is within the revealed window so it can be selected.
        while self._events_model.rowCount() <= row and self._events_model.canFetchMore():
            self._events_model.fetchMore()
        idx = self._events_model.index(row, 0)
        sel = self._events_list.selectionModel()
        sel.blockSignals(True)
        self._events_list.setCurrentIndex(idx)
        sel.blockSignals(False)

    def _on_event_index_changed(self, current, _previous) -> None:
        session = self._events_model.session_at(current)
        if session is None:
            return
        # Re-selecting the already-current session (e.g. when a refresh
        # restores the highlight) must not restart playback from 0.
        if (self._current_session is not None
                and session.session_id == self._current_session.session_id):
            return
        self._current_session = session
        self._save_full_btn.setEnabled(self._export_thread is None)
        ev = session.members[0]  # selecting a session plays its first member
        self._current_event = ev
        self._event_pos = 0.0
        # New event: reset the scrub bar; duration_known repopulates the total.
        self._reset_scrub()
        # Selecting an event starts it immediately - no separate Play click.
        self._is_playing = True
        self._play_btn.set_kind(IconButton.KIND_PAUSE)
        for tile in self._tiles:
            idx = tile._camera.index
            clip = ev.clips.get(idx)
            is_trigger = idx == ev.trigger_cam
            tile.set_triggered(is_trigger)
            # Box overlay only on the camera that was analysed (the trigger).
            tile.set_overlay(ev.tracks if is_trigger else None)
            tile.set_overlay_enabled(self._boxes_on and is_trigger and bool(ev.tracks))
            tile.load_path(clip, sublabel=(clip.name if clip else ""))
            if clip is not None:
                tile.play()
            else:
                tile.pause()
        self._cursor_label.setText(
            f"{ev.start_at:%Y-%m-%d %H:%M:%S}  ·  cam{ev.trigger_cam}  ·  {ev.pretty}"
        )

    @Slot()
    def _on_save_full_clip(self) -> None:
        session = self._current_session
        if session is None or self._export_thread is not None:
            return
        names = camera_names.load(self._settings.env_path)
        cam = next((c for c in self._cameras if c.index == session.trigger_cam), None)
        cam_name = self._cam_display_name(cam, names) if cam else f"cam{session.trigger_cam}"
        # Filesystem-safe default name.
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cam_name)
        default = f"{safe}_{session.start_at:%Y%m%d_%H%M%S}_full.mp4"
        out, _ = QFileDialog.getSaveFileName(
            self, "Save full clip", default, "MP4 video (*.mp4)"
        )
        if not out:
            return
        out_path = Path(out)

        self._save_full_btn.setEnabled(False)
        self._save_full_btn.setText("SAVING…")
        bus.info(
            "EVT",
            f"save-full-clip: exporting {session.count} clips "
            f"({session.duration_label}) of {cam_name} -> {out_path.name}",
        )

        thread = QThread(self)
        worker = _SessionExportWorker(session, session.trigger_cam, out_path)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_export_done)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._export_thread = thread
        self._export_worker = worker
        thread.start()

    @Slot(bool, str)
    def _on_export_done(self, ok: bool, out_path: str) -> None:
        self._export_thread = None
        self._export_worker = None
        self._save_full_btn.setText("SAVE FULL CLIP")
        self._save_full_btn.setEnabled(self._current_session is not None)
        if ok:
            try:
                size_mb = Path(out_path).stat().st_size / (1024 * 1024)
                bus.info("EVT", f"save-full-clip: done — {out_path} ({size_mb:.1f} MB)")
            except OSError:
                bus.info("EVT", f"save-full-clip: done — {out_path}")
        else:
            bus.warn("EVT", f"save-full-clip: export failed for {out_path}")

    def _toggle_boxes(self) -> None:
        self._boxes_on = self._boxes_btn.isChecked()
        ev = self._current_event
        for tile in self._tiles:
            is_trigger = ev is not None and tile._camera.index == ev.trigger_cam
            tile.set_overlay_enabled(
                self._boxes_on and is_trigger and ev is not None and bool(ev.tracks)
            )

    @Slot(int)
    def _on_conf_filter_changed(self, _index: int) -> None:
        self._event_conf_min = float(self._conf_combo.currentData() or 0.0)
        self._apply_event_filters()

    def _periodic_refresh(self) -> None:
        self.refresh_library()  # keep recordings/timeline current regardless
        if self._mode == "events":
            self.refresh_events()

    def _build_grid(self) -> tuple[QWidget, list[PlaybackTile]]:
        wrap = QWidget(self)
        wrap.setObjectName("Grid")
        g = QGridLayout(wrap)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(10)
        g.setRowStretch(0, 1)
        g.setRowStretch(1, 1)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)

        tiles: list[PlaybackTile] = []
        for i, cam in enumerate(self._cameras):
            t = PlaybackTile(cam, parent=wrap)
            tiles.append(t)
            row, col = divmod(i, 2)
            g.addWidget(t, row, col)

        self._grid_focus = GridFocus(
            g, tiles,
            index_of=lambda t: t._camera.index,
            set_selected=lambda t, on: t.set_focus_selected(on),
            parent=self,
        )
        for t in tiles:
            t.double_clicked.connect(self._grid_focus.handle_double_click)
        return wrap, tiles

    def _build_transport(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("TransportBar")
        bar.setFixedHeight(82)
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(14, 6, 14, 6)
        outer.setSpacing(6)

        # Scrub row (events mode only): [cur] [=====slider=====] [total].
        # Bound to the trigger camera's real decoder position; hidden in
        # recordings mode, where the TimelineDrawer is the scrubber instead.
        mono_font = QFont("Cascadia Code", 10)
        self._scrub_row = QWidget(bar)
        sr = QHBoxLayout(self._scrub_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(10)
        self._scrub_cur = QLabel("00:00", self._scrub_row)
        self._scrub_cur.setObjectName("ScrubTime")
        self._scrub_cur.setFont(mono_font)
        self._scrub = _ScrubSlider(Qt.Orientation.Horizontal, self._scrub_row)
        self._scrub.setObjectName("EventScrub")
        self._scrub.setRange(0, 1000)
        self._scrub.setEnabled(False)
        self._scrub.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scrub.sliderPressed.connect(self._scrub_pressed)
        self._scrub.sliderMoved.connect(self._scrub_moved)
        self._scrub.sliderReleased.connect(self._scrub_released)
        self._scrub_total = QLabel("--:--", self._scrub_row)
        self._scrub_total.setObjectName("ScrubTime")
        self._scrub_total.setFont(mono_font)
        sr.addWidget(self._scrub_cur)
        sr.addWidget(self._scrub, 1)
        sr.addWidget(self._scrub_total)
        outer.addWidget(self._scrub_row)

        row_wrap = QWidget(bar)
        h = QHBoxLayout(row_wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)

        self._jump_back_btn = IconButton(IconButton.KIND_SKIP_BACK, "", bar)
        self._jump_back_btn.setToolTip("Step back (uses the active step size)")
        self._jump_back_btn.clicked.connect(lambda: self._jump_relative(-self._step_seconds))

        self._play_btn = IconButton(IconButton.KIND_PLAY, "", bar)
        self._play_btn.setFixedSize(46, 32)
        self._play_btn.setToolTip("Play / Pause")
        self._play_btn.clicked.connect(self._toggle_play)

        self._jump_fwd_btn = IconButton(IconButton.KIND_SKIP_FWD, "", bar)
        self._jump_fwd_btn.setToolTip("Step forward (uses the active step size)")
        self._jump_fwd_btn.clicked.connect(lambda: self._jump_relative(self._step_seconds))

        h.addWidget(self._jump_back_btn)
        h.addWidget(self._play_btn)
        h.addWidget(self._jump_fwd_btn)

        h.addSpacing(16)

        step_label = QLabel("STEP", bar)
        step_label.setObjectName("DialogFieldLabel")
        h.addWidget(step_label)
        self._step_buttons: dict[int, QPushButton] = {}
        for sec, lbl in ((1, "1s"), (5, "5s"), (15, "15s"), (60, "1m")):
            b = QPushButton(lbl, bar)
            b.setObjectName("SpeedButton")
            b.setCheckable(True)
            b.setFixedSize(36, 24)
            b.setChecked(sec == self._step_seconds)
            b.clicked.connect(lambda _checked, s=sec: self._set_step(s))
            self._step_buttons[sec] = b
            h.addWidget(b)

        h.addSpacing(16)

        speed_label = QLabel("SPEED", bar)
        speed_label.setObjectName("DialogFieldLabel")
        h.addWidget(speed_label)
        # Compact speed stepper: ‹ [1×] › cycles .25 .5 1 2 4 8 16 without a
        # long button row or a dropdown. The middle pill shows the current
        # value (click to snap back to 1×); the arrows step and auto-disable
        # at the ends.
        self._speeds = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
        self._speed_idx = self._speeds.index(1.0)
        self._speed_down = QPushButton("‹", bar)
        self._speed_down.setObjectName("SpeedButton")
        self._speed_down.setFixedSize(24, 24)
        self._speed_down.setToolTip("Slower")
        self._speed_down.clicked.connect(lambda: self._step_speed(-1))
        self._speed_pill = QPushButton("1×", bar)
        self._speed_pill.setObjectName("SpeedButton")
        self._speed_pill.setCheckable(True)
        self._speed_pill.setChecked(True)
        self._speed_pill.setFixedSize(46, 24)
        self._speed_pill.setToolTip("Playback speed — click to reset to 1×")
        self._speed_pill.clicked.connect(lambda: self._set_speed(1.0))
        self._speed_up = QPushButton("›", bar)
        self._speed_up.setObjectName("SpeedButton")
        self._speed_up.setFixedSize(24, 24)
        self._speed_up.setToolTip("Faster")
        self._speed_up.clicked.connect(lambda: self._step_speed(1))
        h.addWidget(self._speed_down)
        h.addWidget(self._speed_pill)
        h.addWidget(self._speed_up)

        h.addSpacing(16)
        self._boxes_btn = QPushButton("BOXES", bar)
        self._boxes_btn.setObjectName("SpeedButton")
        self._boxes_btn.setCheckable(True)
        self._boxes_btn.setChecked(self._boxes_on)
        self._boxes_btn.setFixedSize(52, 24)
        self._boxes_btn.setToolTip("Show detection bounding boxes over event playback")
        self._boxes_btn.clicked.connect(self._toggle_boxes)
        h.addWidget(self._boxes_btn)

        h.addStretch(1)

        self._cursor_label = QLabel("--:--:--", bar)
        self._cursor_label.setObjectName("StatusBarText")
        h.addWidget(self._cursor_label)

        outer.addWidget(row_wrap)
        return bar

    # --- Library / day ---

    def refresh_library(self) -> None:
        self._library = scan(self._settings.recording_dir)
        self._timeline.set_segments(self._library)
        self._highlight_calendar_dates()
        # If the cursor is still parked at midnight (initial state) and we
        # have clips for the selected day, jump to a moment that's actually
        # covered by every camera that has a recording today (the LATEST
        # first-clip-start across cams, plus a few seconds of slack), so
        # all tiles can immediately show something instead of "no clip".
        if self._cursor.time() == _time(0, 0):
            latest_first: datetime | None = None
            for cam_id, clips in self._library.items():
                day_clips = clips_for_day(self._library, cam_id, self._selected_day)
                if not day_clips:
                    continue
                t0 = day_clips[0].start_at
                if latest_first is None or t0 > latest_first:
                    latest_first = t0
            if latest_first is not None:
                self._cursor = latest_first + timedelta(seconds=3)
                self._load_all_at_cursor()

    def _highlight_calendar_dates(self) -> None:
        # Clear previous formats and mark days that have content for this mode.
        default_fmt = QTextCharFormat()
        self._calendar.setDateTextFormat(QDate(), default_fmt)
        if self._mode == "events":
            days = event_dates(self._events)
        else:
            days = dates_with_clips(self._library)
        hl_fmt = QTextCharFormat()
        hl_fmt.setForeground(QColor(theme.ACCENT))
        hl_fmt.setFontWeight(QFont.Weight.Bold)
        for d in days:
            self._calendar.setDateTextFormat(QDate(d.year, d.month, d.day), hl_fmt)

    # --- Slots ---

    def _on_date_changed(self) -> None:
        qd = self._calendar.selectedDate()
        self._selected_day = _date(qd.year(), qd.month(), qd.day())
        self._timeline.set_day(self._selected_day)
        # Resume following the latest day only if the user landed on the newest
        # day that has events (or there are none yet); picking an older day means
        # they chose to browse history, so stop auto-advancing on them.
        days = event_dates(self._events)
        self._follow_latest_day = (not days) or (self._selected_day >= max(days))
        if self._mode == "events":
            self._apply_event_filters()
            return
        # Jump cursor to the start of the earliest clip that day (if any)
        first_dt: datetime | None = None
        for cam_id, clips in self._library.items():
            day_clips = clips_for_day(self._library, cam_id, self._selected_day)
            if not day_clips:
                continue
            t0 = day_clips[0].start_at
            if first_dt is None or t0 < first_dt:
                first_dt = t0
        if first_dt is None:
            first_dt = datetime.combine(self._selected_day, _time(0, 0))
        self._cursor = first_dt
        self._load_all_at_cursor()

    def _on_cam_toggled(self, cam_id: int, checked: bool) -> None:
        if checked:
            self._selected_cams.add(cam_id)
        else:
            self._selected_cams.discard(cam_id)
        self._timeline.set_selected_cams(self._selected_cams)
        self._load_all_at_cursor()

    def _on_timeline_seek(self, when: datetime) -> None:
        self._cursor = when
        self._load_all_at_cursor()

    def _load_all_at_cursor(self) -> None:
        for tile in self._tiles:
            if tile._camera.index not in self._selected_cams:
                tile.pause()
                continue
            day_clips = clips_for_day(self._library, tile._camera.index, self._selected_day)
            tile.load_for(self._cursor, day_clips)
            if self._is_playing:
                tile.play()
            else:
                tile.pause()
        self._timeline.set_playhead(self._cursor)
        self._update_cursor_label()

    def _toggle_play(self) -> None:
        self._is_playing = not self._is_playing
        self._play_btn.set_kind(IconButton.KIND_PAUSE if self._is_playing else IconButton.KIND_PLAY)
        for tile in self._tiles:
            if self._mode == "events":
                active = self._current_event is not None and tile._camera.index in self._current_event.clips
            else:
                active = tile._camera.index in self._selected_cams
            if not active:
                continue
            tile.play() if self._is_playing else tile.pause()

    def _jump_relative(self, seconds: int) -> None:
        if self._mode == "events":
            if self._current_event is None:
                return
            target = max(0.0, self._event_pos + seconds)
            if self._event_duration > 0:
                target = min(target, self._event_duration)
            self._event_pos = target
            for tile in self._tiles:
                if tile._camera.index in self._current_event.clips:
                    tile.seek(target)
            self._sync_scrub_to_pos(target)
            self._update_event_label()
            return
        self._cursor = self._cursor + timedelta(seconds=seconds)
        self._load_all_at_cursor()

    def _step_speed(self, direction: int) -> None:
        idx = max(0, min(len(self._speeds) - 1, self._speed_idx + direction))
        self._set_speed(self._speeds[idx])

    def _set_speed(self, s: float) -> None:
        # Snap to the nearest preset so the pill always shows a real value.
        if s in self._speeds:
            self._speed_idx = self._speeds.index(s)
        else:
            self._speed_idx = min(
                range(len(self._speeds)),
                key=lambda i: abs(self._speeds[i] - s),
            )
            s = self._speeds[self._speed_idx]
        self._speed = s
        self._speed_pill.setText(f"{s:g}×")
        self._speed_pill.setChecked(True)
        self._speed_down.setEnabled(self._speed_idx > 0)
        self._speed_up.setEnabled(self._speed_idx < len(self._speeds) - 1)
        for tile in self._tiles:
            tile.set_speed(s)

    def _set_step(self, seconds: int) -> None:
        self._step_seconds = seconds
        for sec, btn in self._step_buttons.items():
            btn.setChecked(sec == seconds)

    # --- Event scrub bar ---

    @staticmethod
    def _format_secs(seconds: float) -> str:
        """Seconds -> MM:SS (or H:MM:SS past an hour)."""
        total = int(max(0.0, seconds))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _trigger_tile(self) -> "PlaybackTile | None":
        """The reference tile for the current event (the trigger camera)."""
        if self._current_event is None:
            return None
        for tile in self._tiles:
            if tile._camera.index == self._current_event.trigger_cam:
                return tile
        return None

    def _is_trigger_sender(self) -> bool:
        """True when the signal's emitting player is the trigger tile's."""
        trig = self._trigger_tile()
        return trig is not None and self.sender() is trig._player

    def _reset_scrub(self) -> None:
        """Zero the scrub bar + labels until the next duration_known arrives."""
        self._event_duration = 0.0
        self._scrubbing = False
        self._scrub.blockSignals(True)
        self._scrub.setValue(0)
        self._scrub.blockSignals(False)
        self._scrub.setEnabled(False)
        self._scrub_cur.setText("00:00")
        self._scrub_total.setText("--:--")

    def _sync_scrub_to_pos(self, seconds: float) -> None:
        """Move the slider + current-time label to match `seconds` (no signals)."""
        if self._event_duration > 0:
            permille = round(seconds / self._event_duration * 1000)
            permille = min(1000, max(0, permille))
            self._scrub.blockSignals(True)
            self._scrub.setValue(permille)
            self._scrub.blockSignals(False)
        self._scrub_cur.setText(self._format_secs(seconds))

    def _scrub_on_duration(self, dur: float) -> None:
        self._event_duration = float(dur or 0.0)
        if self._event_duration > 0:
            self._scrub.setEnabled(True)
            self._scrub_total.setText(self._format_secs(self._event_duration))
        else:
            self._scrub.setEnabled(False)
            self._scrub_total.setText("--:--")

    def _scrub_on_position(self, pos: float) -> None:
        self._event_pos = float(pos or 0.0)
        if not self._scrubbing:
            self._sync_scrub_to_pos(self._event_pos)
        self._update_event_label()

    def _scrub_seek_to_permille(self, value: int) -> float:
        """Seek every angle of the current event to the given permille spot."""
        if self._event_duration <= 0:
            return 0.0
        seconds = value / 1000.0 * self._event_duration
        for tile in self._tiles:
            if (self._current_event is not None
                    and tile._camera.index in self._current_event.clips):
                tile.seek(seconds)
        self._event_pos = seconds
        self._scrub_cur.setText(self._format_secs(seconds))
        self._update_event_label()
        return seconds

    @Slot(float)
    def _on_player_position(self, pos: float) -> None:
        if self._mode == "events" and self._is_trigger_sender():
            self._scrub_on_position(pos)

    @Slot(float)
    def _on_player_duration(self, dur: float) -> None:
        if self._mode == "events" and self._is_trigger_sender():
            self._scrub_on_duration(dur)

    @Slot(str)
    def _on_player_state(self, state: str) -> None:
        # When the trigger clip ends, leave the bar pinned at the end.
        if (state == "eof" and self._mode == "events"
                and self._is_trigger_sender() and self._event_duration > 0):
            self._scrub.blockSignals(True)
            self._scrub.setValue(1000)
            self._scrub.blockSignals(False)
            self._scrub_cur.setText(self._format_secs(self._event_duration))

    @Slot()
    def _scrub_pressed(self) -> None:
        self._scrubbing = True

    @Slot(int)
    def _scrub_moved(self, value: int) -> None:
        self._scrub_seek_to_permille(value)

    @Slot()
    def _scrub_released(self) -> None:
        self._scrub_seek_to_permille(self._scrub.value())
        self._scrubbing = False

    def _advance_cursor(self) -> None:
        if not self._is_playing:
            return
        if self._mode == "events":
            # In events mode the trigger player's real position_changed drives
            # _event_pos + the scrub bar; nothing to estimate here.
            return
        # Advance by elapsed wall time * speed
        self._cursor = self._cursor + timedelta(seconds=0.25 * self._speed)
        self._timeline.set_playhead(self._cursor)
        self._update_cursor_label()

    def _update_event_label(self) -> None:
        ev = self._current_event
        if ev is None:
            return
        self._cursor_label.setText(
            f"{ev.start_at:%H:%M:%S}  +{self._event_pos:0.0f}s  ·  "
            f"cam{ev.trigger_cam}  ·  {ev.pretty}"
        )

    def _update_cursor_label(self) -> None:
        self._cursor_label.setText(self._cursor.strftime("%Y-%m-%d  %H:%M:%S"))

    def shutdown(self) -> None:
        self._refresh_timer.stop()
        self._cursor_tick.stop()
        self._scan_rescan_pending = False
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(1500)
        for t in self._tiles:
            t.shutdown(wait_ms=1500)
