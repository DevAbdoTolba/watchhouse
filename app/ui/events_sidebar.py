"""Events gallery sidebar: filters + virtualized session list + scan lifecycle.

Extracted from PlaybackView (R3, final seam): the sidebar owns the event
data (scanned records, grouped sessions, day/camera/confidence filtering,
follow-latest-day policy) and its widgets; the view owns the shared day
selection, calendar, timeline and tiles, and reacts through signals:

  session_activated(EventSession)  the user picked a session to play
  sessions_changed(list)           the day-filtered sessions (timeline layers)
  day_jump_requested(date)         follow-latest wants the calendar moved
  save_full_requested()            the SAVE FULL CLIP button

The view drives the sidebar with set_day / user_selected_day /
note_new_event / refresh; the export flow reads current_session() and
flips set_save_busy().
"""

from __future__ import annotations

from datetime import date as _date, datetime

from PySide6.QtCore import QSize, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core import camera_names
from app.core.config import Settings
from app.core.event_library import (
    EventRecord,
    EventSession,
    event_dates,
    events_for_day,
    group_sessions,
)
from app.ui import theme
from app.ui.events_model import EventsModel, ThumbnailLoader
from app.ui.events_scan_worker import EventsScanWorker


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


class EventsSidebar(QWidget):
    """The EVENTS half of the playback sidebar. Data + filters in, intents out."""

    session_activated = Signal(object)   # EventSession
    sessions_changed = Signal(list)      # day-filtered list[EventSession]
    day_jump_requested = Signal(object)  # datetime.date
    save_full_requested = Signal()

    def __init__(self, cameras, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._cameras = tuple(cameras)
        self._settings = settings
        self._events: list[EventRecord] = []
        self._all_sessions: list[EventSession] = []  # full, day-agnostic
        self._day_sessions: list[EventSession] = []
        self._current_session: EventSession | None = None
        self._selected_day: _date = datetime.now().date()
        # "Follow latest" keeps the list pinned to the newest day that has
        # events so live detections appear without the user clicking the
        # calendar. Cleared when they browse an older day, restored when they
        # return to the newest. True at startup.
        self._follow_latest_day = True
        self._event_conf_min = 0.0  # min human-detection confidence (0..1)
        self._event_cam_filter: set[int] = {c.index for c in self._cameras}
        self._cam_filter_actions: dict[int, QAction] = {}
        # Background scan (one at a time) feeds the in-memory list the model
        # filters against; filters never touch disk.
        self._scan_thread: QThread | None = None
        self._scan_worker: EventsScanWorker | None = None
        self._scan_rescan_pending = False
        self._events_scanning = False  # drives the list's loading/end footer
        self._thumb_loader = ThumbnailLoader(self)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        self._heading = QLabel("EVENTS", self)
        self._heading.setObjectName("SidebarHeading")
        v.addWidget(self._heading)

        filt_row = QWidget(self)
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
        v.addWidget(filt_row)

        self._model = EventsModel(self._thumb_loader, self)
        self._model.set_camera_labels(self._cam_name_map())
        self._list = QListView(self)
        self._list.setObjectName("EventsList")
        self._list.setIconSize(QSize(112, 63))
        self._list.setSpacing(3)
        self._list.setUniformItemSizes(False)
        self._list.setModel(self._model)
        self._list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Virtualization fetches the next page as the view scrolls near bottom.
        self._list.selectionModel().currentChanged.connect(self._on_index_changed)
        # Quiet scroll-state feedback so the list never *looks* finished while
        # there is more above/below or a scan is still running. A thin muted
        # hint above (earlier events) and a footer below (more / loading / end).
        hint_css = f"color:{theme.TEXT_MUTED}; font-size:11px; padding:1px;"
        self._top_hint = QLabel("↑ earlier events above", self)
        self._top_hint.setObjectName("EventsScrollHint")
        self._top_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._top_hint.setStyleSheet(hint_css)
        self._top_hint.setVisible(False)
        v.addWidget(self._top_hint)
        v.addWidget(self._list, 1)
        self._status = QLabel("", self)
        self._status.setObjectName("EventsScrollHint")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(hint_css)
        v.addWidget(self._status)
        sb = self._list.verticalScrollBar()
        sb.valueChanged.connect(self._update_scroll_feedback)
        sb.rangeChanged.connect(lambda *_: self._update_scroll_feedback())
        self._model.modelReset.connect(self._update_scroll_feedback)
        self._model.rowsInserted.connect(lambda *_: self._update_scroll_feedback())

        # Stitch the whole selected presence into one continuous file on demand.
        self._save_btn = QPushButton("SAVE FULL CLIP", self)
        self._save_btn.setObjectName("SidebarAction")
        self._save_btn.setToolTip(
            "Stitch every clip of the selected presence into one continuous "
            "video (can be large)"
        )
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self.save_full_requested)
        v.addWidget(self._save_btn)

    # --- Day / refresh API (called by the view) -----------------------------

    def user_selected_day(self, day: _date) -> None:
        """The user picked a day on the calendar. Resume following the latest
        day only if they landed on the newest day that has events (or there
        are none yet); an older day means they chose to browse history."""
        self._selected_day = day
        days = event_dates(self._events)
        self._follow_latest_day = (not days) or (day >= max(days))
        self._apply_filters()

    def set_day(self, day: _date) -> None:
        """Programmatic day move (follow-latest jump, import); re-filter."""
        self._selected_day = day
        self._apply_filters()

    def note_new_event(self, when: datetime | None = None) -> None:
        """Fast path: a new event was just extracted. When following the
        latest day, jump the selection to the new event's day (so a
        post-midnight event is visible without a restart), then rescan."""
        if (when is not None and self._follow_latest_day
                and when.date() > self._selected_day):
            self.day_jump_requested.emit(when.date())
        self.refresh()

    def refresh(self) -> None:
        """Kick off a background scan of the events tree. The scan reads every
        event.json (slow), so it runs on a worker thread; the model/UI consume
        the already-built session list on completion. Overlapping scans are
        coalesced into a single trailing rescan."""
        self._events_scanning = True
        self._update_scroll_feedback()
        if self._scan_thread is not None:
            self._scan_rescan_pending = True
            return
        thread = QThread(self)
        worker = EventsScanWorker(self._settings.events_dir)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_scanned)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._scan_thread = thread
        self._scan_worker = worker
        thread.start()

    def shutdown(self) -> None:
        self._scan_rescan_pending = False
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(1500)

    # --- Selection / save API ------------------------------------------------

    def current_session(self) -> EventSession | None:
        return self._current_session

    def reset_selection(self) -> None:
        self._current_session = None
        self._save_btn.setEnabled(False)

    def event_days(self) -> set:
        return event_dates(self._events)

    def set_save_enabled(self, on: bool) -> None:
        self._save_btn.setEnabled(on)

    def set_save_busy(self, busy: bool) -> None:
        """Export started/finished: lock the button while a save runs."""
        if busy:
            self._save_btn.setEnabled(False)
            self._save_btn.setText("SAVING…")
        else:
            self._save_btn.setText("SAVE FULL CLIP")
            self._save_btn.setEnabled(self._current_session is not None)

    # --- Internals -------------------------------------------------------------

    @Slot(list, list)
    def _on_scanned(self, events: list, sessions: list) -> None:
        self._scan_thread = None
        self._scan_worker = None
        self._events_scanning = False
        self._events = events
        self._all_sessions = sessions
        # Day-rollover safety: while following the latest day, advance the
        # selection to the newest day that actually has events (e.g. a brand
        # new event after midnight lands under tomorrow's folder). Skipped when
        # the user is browsing history, so we never interrupt them. The view
        # moves the calendar and calls set_day(), which re-filters.
        days = event_dates(self._events)
        if self._follow_latest_day and days and max(days) > self._selected_day:
            self.day_jump_requested.emit(max(days))
        else:
            self._apply_filters()
        if self._scan_rescan_pending:
            self._scan_rescan_pending = False
            self.refresh()

    def _apply_filters(self) -> None:
        """Re-filter the in-memory session list (day + camera + confidence) and
        reset the model window to the first page. No disk access — only
        refresh() re-scans. Selection is preserved by session_id."""
        prev_sid = self._current_session.session_id if self._current_session else None
        day_sessions = group_sessions(events_for_day(self._events, self._selected_day))
        self._day_sessions = [
            s for s in day_sessions
            if s.trigger_cam in self._event_cam_filter
            and any(m.person_conf >= self._event_conf_min for m in s.members)
        ]
        self._model.set_camera_labels(self._cam_name_map())
        self._model.set_sessions(self._day_sessions)
        n = len(self._day_sessions)
        self._heading.setText(f"EVENTS — {self._selected_day:%b %d}  ({n})")
        if prev_sid is not None:
            for row, s in enumerate(self._day_sessions):
                if s.session_id == prev_sid:
                    self._select_row(row)
                    break
        self.sessions_changed.emit(list(self._day_sessions))
        self._update_scroll_feedback()

    def _select_row(self, row: int) -> None:
        """Highlight a row without restarting playback (guarded selection)."""
        if row < 0 or row >= self._model.total_count():
            return
        # Ensure the row is within the revealed window so it can be selected.
        while self._model.rowCount() <= row and self._model.canFetchMore():
            self._model.fetchMore()
        idx = self._model.index(row, 0)
        sel = self._list.selectionModel()
        sel.blockSignals(True)
        self._list.setCurrentIndex(idx)
        sel.blockSignals(False)

    def _on_index_changed(self, current, _previous) -> None:
        session = self._model.session_at(current)
        if session is None:
            return
        # Re-selecting the already-current session (e.g. when a refresh
        # restores the highlight) must not restart playback from 0.
        if (self._current_session is not None
                and session.session_id == self._current_session.session_id):
            return
        self._current_session = session
        self.session_activated.emit(session)

    def _update_scroll_feedback(self) -> None:
        """Keep the list's quiet header/footer hints in sync with scroll state:
        loading during a scan, '↓ more below' / '↑ earlier above' while there's
        off-screen content, and a settled '— end · N events —' when caught up."""
        if self._events_scanning:
            self._status.setText("Loading…")
            self._top_hint.setVisible(False)
            return
        total = self._model.total_count()
        if total == 0:
            self._status.setText("")
            self._top_hint.setVisible(False)
            return
        sb = self._list.verticalScrollBar()
        more_below = self._model.canFetchMore() or sb.value() < sb.maximum()
        if more_below:
            self._status.setText("↓ more below")
        else:
            self._status.setText(f"— end · {total} event{'s' if total != 1 else ''} —")
        self._top_hint.setVisible(sb.value() > 0)

    # --- Filters ----------------------------------------------------------------

    def _cam_name_map(self) -> dict[int, str]:
        """trigger_cam -> display name, for the model's row labels."""
        names = camera_names.load(self._settings.env_path)
        return {c.index: camera_names.display_name(c, names)
                for c in self._cameras}

    @Slot(int)
    def _on_conf_filter_changed(self, _index: int) -> None:
        self._event_conf_min = float(self._conf_combo.currentData() or 0.0)
        self._apply_filters()

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
            act = QAction(camera_names.display_name(cam, names), menu)
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
            text = camera_names.display_name(cam, names) if cam else "1 cam"
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
        self._apply_filters()

    def _on_cam_filter_toggled(self, cam_id: int, on: bool) -> None:
        if on:
            self._event_cam_filter.add(cam_id)
        else:
            self._event_cam_filter.discard(cam_id)
        self._sync_cam_filter_label()
        self._apply_filters()
