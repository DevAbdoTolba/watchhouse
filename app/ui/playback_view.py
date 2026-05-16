"""Playback mode: calendar + camera checkboxes + 2x2 video grid + timeline + transport."""

from __future__ import annotations

from datetime import date as _date, datetime, time as _time, timedelta

from PySide6.QtCore import QDate, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.cameras import Camera
from app.core.clip_library import Clip, clips_for_day, dates_with_clips, find_clip_at, scan
from app.core.config import Settings
from app.core.log import bus
from app.core.playback_player import PlaybackPlayer
from app.ui import theme
from app.ui.camera_tile import VideoPanel
from app.ui.icon_button import IconButton
from app.ui.import_clip_dialog import ImportClipDialog
from app.ui.timeline_widget import TimelineWidget


class PlaybackTile(QFrame):
    """Header + VideoPanel + own PlaybackPlayer for one camera in playback mode."""

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

        hl.addWidget(self._name)
        hl.addWidget(self._sub, 1)

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

    def play(self) -> None: self._player.play()
    def pause(self) -> None: self._player.pause()
    def toggle(self) -> None: self._player.toggle()
    def set_speed(self, s: float) -> None: self._player.set_speed(s)

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
        self._cursor: datetime = datetime.combine(self._selected_day, _time(0, 0))
        self._is_playing = False
        self._speed = 1.0

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

        self._timeline = TimelineWidget([c.index for c in cameras], parent=self)
        self._timeline.seek_requested.connect(self._on_timeline_seek)
        right.addWidget(self._timeline)

        right.addWidget(self._build_transport())

        right_wrap = QWidget(self)
        right_wrap.setLayout(right)
        root.addWidget(right_wrap, 1)

        # Refresh library every 30s so newly recorded clips appear
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_library)
        self._refresh_timer.start(30_000)

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

        title2 = QLabel("CAMERAS", side)
        title2.setObjectName("SidebarHeading")
        v.addWidget(title2)

        self._cam_checkboxes: dict[int, QCheckBox] = {}
        for cam in self._cameras:
            cb = QCheckBox(cam.label, side)
            cb.setChecked(True)
            cb.toggled.connect(lambda checked, i=cam.index: self._on_cam_toggled(i, checked))
            self._cam_checkboxes[cam.index] = cb
            v.addWidget(cb)

        v.addSpacing(8)

        title3 = QLabel("LIBRARY", side)
        title3.setObjectName("SidebarHeading")
        v.addWidget(title3)

        self._import_btn = QPushButton("IMPORT CLIP", side)
        self._import_btn.setObjectName("SidebarAction")
        self._import_btn.setToolTip(
            "Add a manually-exported DVR video to the playback library"
        )
        self._import_btn.clicked.connect(self._on_import_clip)
        v.addWidget(self._import_btn)

        v.addStretch(1)
        return side

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
        return wrap, tiles

    def _build_transport(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("TransportBar")
        bar.setFixedHeight(44)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(10)

        self._jump_back_btn = IconButton(IconButton.KIND_SKIP_BACK, "30s", bar)
        self._jump_back_btn.setToolTip("Jump back 30 seconds")
        self._jump_back_btn.clicked.connect(lambda: self._jump_relative(-30))

        self._play_btn = IconButton(IconButton.KIND_PLAY, "", bar)
        self._play_btn.setFixedSize(46, 32)
        self._play_btn.setToolTip("Play / Pause")
        self._play_btn.clicked.connect(self._toggle_play)

        self._jump_fwd_btn = IconButton(IconButton.KIND_SKIP_FWD, "30s", bar)
        self._jump_fwd_btn.setToolTip("Jump forward 30 seconds")
        self._jump_fwd_btn.clicked.connect(lambda: self._jump_relative(30))

        h.addWidget(self._jump_back_btn)
        h.addWidget(self._play_btn)
        h.addWidget(self._jump_fwd_btn)

        h.addSpacing(16)

        self._speed_buttons: dict[float, QPushButton] = {}
        for s in (0.5, 1.0, 2.0, 4.0):
            b = QPushButton(f"{s:g}x", bar)
            b.setObjectName("SpeedButton")
            b.setCheckable(True)
            b.setFixedSize(36, 24)
            b.setChecked(s == 1.0)
            b.clicked.connect(lambda _checked, sp=s: self._set_speed(sp))
            self._speed_buttons[s] = b
            h.addWidget(b)

        h.addStretch(1)

        self._cursor_label = QLabel("--:--:--", bar)
        self._cursor_label.setObjectName("StatusBarText")
        h.addWidget(self._cursor_label)

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
        # Clear previous formats and mark days that have clips
        default_fmt = QTextCharFormat()
        self._calendar.setDateTextFormat(QDate(), default_fmt)
        with_clips = dates_with_clips(self._library)
        hl_fmt = QTextCharFormat()
        hl_fmt.setForeground(QColor(theme.ACCENT))
        hl_fmt.setFontWeight(QFont.Weight.Bold)
        for d in with_clips:
            self._calendar.setDateTextFormat(QDate(d.year, d.month, d.day), hl_fmt)

    # --- Slots ---

    def _on_date_changed(self) -> None:
        qd = self._calendar.selectedDate()
        self._selected_day = _date(qd.year(), qd.month(), qd.day())
        self._timeline.set_day(self._selected_day)
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
            if tile._camera.index in self._selected_cams:
                if self._is_playing:
                    tile.play()
                else:
                    tile.pause()

    def _jump_relative(self, seconds: int) -> None:
        self._cursor = self._cursor + timedelta(seconds=seconds)
        self._load_all_at_cursor()

    def _set_speed(self, s: float) -> None:
        self._speed = s
        for sp, btn in self._speed_buttons.items():
            btn.setChecked(sp == s)
        for tile in self._tiles:
            tile.set_speed(s)

    def _advance_cursor(self) -> None:
        if not self._is_playing:
            return
        # Advance by elapsed wall time * speed
        self._cursor = self._cursor + timedelta(seconds=0.25 * self._speed)
        self._timeline.set_playhead(self._cursor)
        self._update_cursor_label()

    def _update_cursor_label(self) -> None:
        self._cursor_label.setText(self._cursor.strftime("%Y-%m-%d  %H:%M:%S"))

    def shutdown(self) -> None:
        self._refresh_timer.stop()
        self._cursor_tick.stop()
        for t in self._tiles:
            t.shutdown(wait_ms=1500)
