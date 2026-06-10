"""Playback mode: calendar + camera checkboxes + 2x2 video grid + timeline + transport."""

from __future__ import annotations

import time
from datetime import date as _date, datetime, time as _time, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QImage,
    QTextCharFormat,
)
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core import camera_names
from app.core import dvr_time
from app.core import footage_spans
from app.core.cameras import Camera
from app.core.clip_library import Clip, clips_for_day, dates_with_clips, find_clip_at, scan
from app.core.pins import Pins
from app.core.config import Settings
from app.core.event_library import EventRecord, EventSession
from app.core.log import bus
from app.core.playback_player import PlaybackPlayer
from app.core.session_export import export_session
from app.ui import theme
from app.ui.camera_tile import VideoPanel
from app.ui.events_sidebar import EventsSidebar
from app.ui.grid_focus import GridFocus
from app.ui.import_clip_dialog import ImportClipDialog
from app.ui.timeline_drawer import TimelineDrawer
from app.ui.transport_bar import TransportBar


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
    tail_waiting = Signal(int)          # cam index: at the live edge, needs fresher clips

    def __init__(self, camera: Camera, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CameraTile")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._camera = camera
        self._current_clip: Clip | None = None
        # Continuous playback: recordings are separate 3-min files, so when one
        # ends we roll straight into the next instead of dead-stopping.
        self._day_clips: list[Clip] = []
        self._auto_advance = False   # on in recordings mode, off in events mode
        self._playing = False        # play *intent* (survives the per-clip eof pause)

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
        self._player.frame_ready.connect(self._on_frame)
        self._player.state_changed.connect(self._on_state)
        self._video.resized.connect(self._player.set_display_size)
        self._player.set_display_size(self._video.width(), self._video.height())
        self._player.start()

        # At the live edge the next 3-min segment isn't finalized yet (no moov
        # atom -> unopenable); poll for it instead of dead-stopping.
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(2000)
        self._retry_timer.timeout.connect(self._retry_tail)

    def shutdown(self, wait_ms: int = 2000) -> None:
        self._player.request_stop()
        self._player.wait(wait_ms)

    @Slot(QImage)
    def _on_frame(self, image: QImage) -> None:
        self._video.set_frame(image)
        # Ack so the player emits the next frame; it drops frames (never
        # queues) while one is in flight — see PlaybackPlayer.ack_frame.
        self._player.ack_frame()

    def set_day_clips(self, clips: list[Clip]) -> None:
        """Refresh this camera's clip list (the view re-pushes it as new tail
        segments finalize) so auto-advance can find the next file."""
        self._day_clips = clips

    def set_auto_advance(self, on: bool) -> None:
        self._auto_advance = on
        if not on:
            self._retry_timer.stop()

    def load_for(self, when: datetime, day_clips: list[Clip]) -> None:
        self._day_clips = day_clips
        self._retry_timer.stop()
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

    def _next_clip(self) -> Clip | None:
        """The earliest clip that starts after the current one (None at the
        live edge). Tolerates gaps — it just jumps to whatever's next."""
        if self._current_clip is None:
            return None
        cur = self._current_clip.start_at
        later = [c for c in self._day_clips if c.start_at > cur]
        return min(later, key=lambda c: c.start_at) if later else None

    def _advance_to_next(self) -> None:
        nxt = self._next_clip()
        if nxt is not None:
            self._retry_timer.stop()
            self._current_clip = nxt
            self._sub.setText(nxt.path.name)
            self._player.load(nxt.path, start_offset_s=0.0)
            self._player.play()
        else:
            # Live edge: the next segment isn't on disk / finalized yet.
            self._sub.setText("recording…")
            self.tail_waiting.emit(self._camera.index)
            self._retry_timer.start()

    @Slot()
    def _retry_tail(self) -> None:
        if not (self._auto_advance and self._playing):
            self._retry_timer.stop()
            return
        if self._next_clip() is not None:
            self._advance_to_next()
        else:
            self.tail_waiting.emit(self._camera.index)

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

    def play(self) -> None:
        self._playing = True
        self._player.play()

    def pause(self) -> None:
        self._playing = False
        self._retry_timer.stop()
        self._player.pause()

    def toggle(self) -> None:
        self._playing = not self._playing
        if not self._playing:
            self._retry_timer.stop()
        self._player.toggle()
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
        # Roll into the next 3-min segment instead of stopping at the boundary.
        if state == "eof" and self._auto_advance and self._playing:
            self._advance_to_next()
            return
        # A still-recording (unfinalized) segment can't be opened yet — wait for
        # it to finalize rather than showing a dead "error".
        if state == "error" and self._auto_advance and self._playing:
            self._sub.setText("recording…")
            self.tail_waiting.emit(self._camera.index)
            self._retry_timer.start()
            return
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
        self._last_lib_scan = 0.0  # throttles live-edge rescans
        # Permanent ('pinned', blue) footage: imported clips, user-pinned ranges,
        # and a live "keep new recording" window. The pruner reads the same file.
        self._pins = Pins.load(settings.env_path)
        self._selected_cams: set[int] = {c.index for c in cameras}
        self._selected_day: _date = datetime.now().date()
        self._cursor: datetime = datetime.combine(self._selected_day, _time(0, 0))
        self._is_playing = False
        self._speed = 1.0  # mirror of the transport's speed, for the cursor estimate
        # Events sub-mode: "recordings" (timeline scrubbing) | "events" (gallery)
        self._mode = "recordings"
        self._current_event: EventRecord | None = None
        # Background "Save full clip" export (one at a time).
        self._export_thread: QThread | None = None
        self._export_worker: _SessionExportWorker | None = None
        self._event_pos = 0.0  # clip-relative seconds, for stepping + label
        self._event_duration = 0.0  # trigger clip length (s); 0 until known
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

        self._transport = self._build_transport()
        right.addWidget(self._transport)

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
            tile.tail_waiting.connect(self._on_tile_tail_waiting)
            tile.set_auto_advance(self._mode == "recordings")

        self.refresh_library()
        self._cursor_tick = QTimer(self)
        self._cursor_tick.timeout.connect(self._advance_cursor)
        self._cursor_tick.start(250)

        self._refresh_pin_status()
        self._timeline.set_pinned(self._pinned_spans_by_cam())

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

        # Blue layer: lock/unlock the footage inside the window you frame on the
        # timeline overview (drag the overview rectangle to set the window).
        self._pin_btn = QPushButton("🔒 LOCK FOOTAGE", self._rec_section)
        self._pin_btn.setObjectName("SidebarAction")
        self._pin_btn.setToolTip(
            "Lock the footage inside the framed window (drag the overview "
            "rectangle) so it is kept forever and never auto-deleted (blue). "
            "Empty gaps in the window are not locked.")
        self._pin_btn.clicked.connect(self._pin_range)
        rv.addWidget(self._pin_btn)

        self._unpin_btn = QPushButton("UNLOCK FOOTAGE", self._rec_section)
        self._unpin_btn.setObjectName("SidebarAction")
        self._unpin_btn.setToolTip(
            "Unlock footage inside the framed window (it resumes normal "
            "auto-delete)")
        self._unpin_btn.clicked.connect(self._unpin_range)
        rv.addWidget(self._unpin_btn)

        self._pin_status = QLabel("", self._rec_section)
        self._pin_status.setObjectName("KeepStatus")
        self._pin_status.setWordWrap(True)
        self._pin_status.setStyleSheet(
            f"color:{theme.TEXT_MUTED}; font-size:11px; padding:1px;")
        rv.addWidget(self._pin_status)
        v.addWidget(self._rec_section)

        # --- Events section (gallery of detected events) ---
        self._events_sidebar = EventsSidebar(self._cameras, self._settings, side)
        self._events_sidebar.session_activated.connect(self._on_session_activated)
        self._events_sidebar.sessions_changed.connect(self._on_sessions_changed)
        self._events_sidebar.day_jump_requested.connect(self._on_day_jump)
        self._events_sidebar.save_full_requested.connect(self._on_save_full_clip)
        self._events_sidebar.setVisible(False)
        v.addWidget(self._events_sidebar, 1)

        v.addStretch(0)
        return side

    def _cam_display_name(self, cam: Camera, names: dict[int, str]) -> str:
        return camera_names.display_name(cam, names)

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
        self._events_sidebar.setVisible(mode == "events")
        self._timeline.setVisible(mode == "recordings")
        # Scrub bar is the events-mode scrubber; recordings use the timeline.
        self._transport.set_scrub_visible(mode == "events")
        # Stepping is finer inside short event clips.
        self._transport.set_step(1 if mode == "events" else 5)
        self._is_playing = False
        self._transport.set_playing(False)
        for t in self._tiles:
            t.pause()
            # Auto-advance across 3-min segments only in recordings mode; events
            # mode plays one finite clip per camera.
            t.set_auto_advance(mode == "recordings")
            if mode == "recordings":
                t.set_triggered(False)
                t.set_overlay_enabled(False)
                t.set_overlay(None)
        self._highlight_calendar_dates()
        if mode == "events":
            self._events_sidebar.reset_selection()
            self._current_event = None
            self._reset_scrub()
            self._events_sidebar.refresh()
            for t in self._tiles:
                t.load_path(None)
            self._transport.set_cursor_text("Select an event")
        else:
            # Entering recordings mode: make sure the timeline's green/blue
            # layers reflect the currently selected day (not a stale one).
            self._events_sidebar.set_day(self._selected_day)
            self._load_all_at_cursor()

    @Slot(object)
    def _on_day_jump(self, day: _date) -> None:
        """Follow-latest (or a fresh post-midnight event) wants the selection
        on a newer day: move the calendar + timeline, then re-filter."""
        self._set_selected_day(day)
        self._events_sidebar.set_day(day)
        self._highlight_calendar_dates()

    @Slot(list)
    def _on_sessions_changed(self, sessions: list) -> None:
        """The sidebar re-filtered: refresh the timeline's green (events) and
        blue (pinned/kept) layers and the calendar's bold day markers."""
        spans: dict[int, list] = {}
        for s in sessions:
            spans.setdefault(s.trigger_cam, []).append((s.start_at, s.end_at))
        self._timeline.set_events(spans)
        self._timeline.set_pinned(self._pinned_spans_by_cam())
        self._highlight_calendar_dates()

    def _set_selected_day(self, day: _date) -> None:
        """Point the selection + calendar at `day` without re-entering
        `_on_date_changed` (which would clobber the follow-latest decision)."""
        self._selected_day = day
        self._timeline.set_day(day)
        self._calendar.blockSignals(True)
        self._calendar.setSelectedDate(QDate(day.year, day.month, day.day))
        self._calendar.blockSignals(False)

    def note_new_event(self, when: datetime | None = None) -> None:
        """Fast path: a new event was just extracted — surface it within ~1s.
        Called best-effort from MainWindow; the sidebar owns the rescan and
        the follow-latest day-jump policy."""
        self._events_sidebar.note_new_event(when)

    def _pinned_spans_by_cam(self) -> dict:
        return footage_spans.pinned_spans_by_cam(
            self._library, self._pins, self._cameras,
            self._settings.recording_dir / "imported")

    def _pin_range(self) -> None:
        start, end = self._timeline.selected_range()
        if (end - start).total_seconds() < 1:
            self._pin_status.setText("Frame a window on the timeline first.")
            return
        # Lock only footage that actually exists in the window — never the gaps.
        subs = footage_spans.recorded_subranges(self._library, start, end)
        if not subs:
            self._pin_status.setText("No footage in that window to lock.")
            return
        for s, e in subs:
            self._pins.add_range(s, e)
        self._timeline.set_pinned(self._pinned_spans_by_cam())
        self._refresh_pin_status()
        bus.info("REC", f"locked {len(subs)} footage span(s) in "
                        f"{start:%H:%M:%S}–{end:%H:%M:%S}")

    def _unpin_range(self) -> None:
        start, end = self._timeline.selected_range()
        before = len(self._pins.ranges)
        self._pins.remove_range(start, end)
        removed = before - len(self._pins.ranges)
        self._timeline.set_pinned(self._pinned_spans_by_cam())
        self._refresh_pin_status()
        bus.info("REC", f"unpinned {removed} range(s) in {start:%H:%M:%S}–{end:%H:%M:%S}")

    def _refresh_pin_status(self) -> None:
        if getattr(self, "_pin_status", None) is None:
            return
        n = len(self._pins.ranges)
        keeping = " · ● keeping live" if self._pins.keep_from is not None else ""
        self._pin_status.setText(
            (f"🔒 {n} locked range(s)" if n else "nothing locked") + keeping)

    @Slot(object)
    def _on_session_activated(self, session) -> None:
        """The user picked a session in the sidebar: play its first member."""
        self._events_sidebar.set_save_enabled(self._export_thread is None)
        ev = session.members[0]  # selecting a session plays its first member
        self._current_event = ev
        self._event_pos = 0.0
        # New event: reset the scrub bar; duration_known repopulates the total.
        self._reset_scrub()
        # Selecting an event starts it immediately - no separate Play click.
        self._is_playing = True
        self._transport.set_playing(True)
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
        self._transport.set_cursor_text(
            f"{dvr_time.shift(ev.start_at):%Y-%m-%d %H:%M:%S}  ·  cam{ev.trigger_cam}  ·  {ev.pretty}"
        )

    @Slot()
    def _on_save_full_clip(self) -> None:
        session = self._events_sidebar.current_session()
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

        self._events_sidebar.set_save_busy(True)
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
        self._events_sidebar.set_save_busy(False)
        if ok:
            detail = out_path
            try:
                size_mb = Path(out_path).stat().st_size / (1024 * 1024)
                bus.info("EVT", f"save-full-clip: done — {out_path} ({size_mb:.1f} MB)")
                detail = f"{out_path}\n\n{size_mb:.1f} MB"
            except OSError:
                bus.info("EVT", f"save-full-clip: done — {out_path}")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("Clip saved")
            box.setText("Full clip saved successfully.")
            box.setInformativeText(detail)
            open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            if box.clickedButton() is open_btn:
                self._reveal_in_explorer(out_path)
        else:
            bus.warn("EVT", f"save-full-clip: export failed for {out_path}")
            QMessageBox.warning(
                self,
                "Save failed",
                "Could not save the full clip — no video was written.\n\n"
                "The event may have no clip for its trigger camera, or ffmpeg "
                "could not join the segments. See the admin log (console) for "
                "the exact reason.",
            )

    def _reveal_in_explorer(self, path_str: str) -> None:
        """Open the file browser with the saved file selected (Windows Explorer
        /select), falling back to opening the containing folder."""
        p = Path(path_str)
        try:
            import subprocess
            subprocess.Popen(["explorer", f"/select,{p}"])
            return
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))

    @Slot(bool)
    def _toggle_boxes(self, on: bool) -> None:
        self._boxes_on = on
        ev = self._current_event
        for tile in self._tiles:
            is_trigger = ev is not None and tile._camera.index == ev.trigger_cam
            tile.set_overlay_enabled(
                self._boxes_on and is_trigger and ev is not None and bool(ev.tracks)
            )

    def _periodic_refresh(self) -> None:
        # The library scan walks the whole recordings tree on the UI thread;
        # don't pay for it while the playback view is hidden (live mode).
        # Switching to PLAYBACK triggers refresh_library() anyway.
        if not self.isVisible():
            return
        self.refresh_library()
        if self._mode == "events":
            self._events_sidebar.refresh()

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

    def _build_transport(self) -> TransportBar:
        bar = TransportBar(boxes_on=self._boxes_on, parent=self)
        bar.play_toggled.connect(self._toggle_play)
        bar.step_requested.connect(self._jump_relative)
        bar.speed_changed.connect(self._on_speed_changed)
        bar.boxes_toggled.connect(self._toggle_boxes)
        bar.seek_requested.connect(self._on_scrub_seek)
        return bar

    # --- Library / day ---

    def refresh_library(self) -> None:
        self._last_lib_scan = time.monotonic()
        self._library = scan(self._settings.recording_dir)
        self._timeline.set_segments(self._library)
        # Keep each tile's clip list current so auto-advance can roll into newly
        # finalized tail segments without waiting on a full reload.
        for tile in self._tiles:
            tile.set_day_clips(
                clips_for_day(self._library, tile._camera.index, self._selected_day))
        # Re-read pins so the blue layer reflects any KEEP/pin changes made in
        # the live view since we were last here.
        self._pins = Pins.load(self._settings.env_path)
        self._timeline.set_pinned(self._pinned_spans_by_cam())
        self._refresh_pin_status()
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
            days = self._events_sidebar.event_days()
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
        # The sidebar owns the follow-latest decision and re-filters for the
        # new day in BOTH modes (its sessions_changed refreshes the green +
        # blue timeline layers, which recordings mode shows too).
        self._events_sidebar.user_selected_day(self._selected_day)
        if self._mode == "events":
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
        self._transport.set_playing(self._is_playing)
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
            self._transport.sync_scrub(target)
            self._update_event_label()
            return
        self._cursor = self._cursor + timedelta(seconds=seconds)
        self._load_all_at_cursor()

    @Slot(float)
    def _on_speed_changed(self, s: float) -> None:
        self._speed = s  # the gap-cursor estimate in _advance_cursor uses it
        for tile in self._tiles:
            tile.set_speed(s)

    # --- Event scrub bar (controls live in TransportBar) ---

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

    def _sender_tile(self) -> "PlaybackTile | None":
        snd = self.sender()
        for t in self._tiles:
            if t._player is snd:
                return t
        return None

    def _lead_tile(self) -> "PlaybackTile | None":
        """The selected tile that drives the recordings playhead — the first
        selected camera that currently has a clip loaded."""
        for t in self._tiles:
            if t._camera.index in self._selected_cams and t._current_clip is not None:
                return t
        return None

    @Slot(int)
    def _on_tile_tail_waiting(self, cam_id: int) -> None:
        # A tile hit the live edge; rescan so a just-finalized next segment is
        # on the lists, then the tile's retry tick rolls into it. All four
        # tiles retry every 2s when parked at the edge — one scan per few
        # seconds is plenty for a 3-minute segment roll.
        if time.monotonic() - self._last_lib_scan < 5.0:
            return
        self.refresh_library()

    def _reset_scrub(self) -> None:
        """Zero the scrub bar + labels until the next duration_known arrives."""
        self._event_duration = 0.0
        self._transport.reset_scrub()

    def _scrub_on_duration(self, dur: float) -> None:
        self._event_duration = float(dur or 0.0)
        self._transport.set_duration(self._event_duration)

    def _scrub_on_position(self, pos: float) -> None:
        self._event_pos = float(pos or 0.0)
        self._transport.sync_scrub(self._event_pos)  # no-op mid-drag
        self._update_event_label()

    @Slot(float)
    def _on_scrub_seek(self, seconds: float) -> None:
        """User scrubbed: seek every angle of the current event."""
        if self._current_event is None:
            return
        for tile in self._tiles:
            if tile._camera.index in self._current_event.clips:
                tile.seek(seconds)
        self._event_pos = seconds
        self._update_event_label()

    @Slot(float)
    def _on_player_position(self, pos: float) -> None:
        if self._mode == "events" and self._is_trigger_sender():
            self._scrub_on_position(pos)
            return
        # Recordings: drive the playhead off the lead tile's REAL decode
        # position so the marker tracks the picture (no free-running drift).
        if self._mode == "recordings" and self._is_playing:
            tile = self._sender_tile()
            if (tile is not None and tile is self._lead_tile()
                    and tile._current_clip is not None):
                self._cursor = tile._current_clip.start_at + timedelta(seconds=pos)
                self._timeline.set_playhead(self._cursor)
                self._update_cursor_label()

    @Slot(float)
    def _on_player_duration(self, dur: float) -> None:
        if self._mode == "events" and self._is_trigger_sender():
            self._scrub_on_duration(dur)

    @Slot(str)
    def _on_player_state(self, state: str) -> None:
        # When the trigger clip ends, leave the bar pinned at the end.
        if (state == "eof" and self._mode == "events"
                and self._is_trigger_sender() and self._event_duration > 0):
            self._transport.pin_scrub_to_end()

    def _advance_cursor(self) -> None:
        if not self._is_playing:
            return
        if self._mode == "events":
            # In events mode the trigger player's real position_changed drives
            # _event_pos + the scrub bar; nothing to estimate here.
            return
        # If a lead tile is actually playing a clip, its real position drives
        # the cursor (see _on_player_position). Only estimate when nothing is
        # playing — i.e. parked in a gap or waiting at the live edge.
        if self._lead_tile() is not None:
            return
        self._cursor = self._cursor + timedelta(seconds=0.25 * self._speed)
        self._timeline.set_playhead(self._cursor)
        self._update_cursor_label()

    def _update_event_label(self) -> None:
        ev = self._current_event
        if ev is None:
            return
        self._transport.set_cursor_text(
            f"{dvr_time.shift(ev.start_at):%H:%M:%S}  +{self._event_pos:0.0f}s  ·  "
            f"cam{ev.trigger_cam}  ·  {ev.pretty}"
        )

    def _update_cursor_label(self) -> None:
        self._transport.set_cursor_text(
            dvr_time.shift(self._cursor).strftime("%Y-%m-%d  %H:%M:%S"))

    def shutdown(self) -> None:
        self._refresh_timer.stop()
        self._cursor_tick.stop()
        self._events_sidebar.shutdown()
        for t in self._tiles:
            t.shutdown(wait_ms=1500)
