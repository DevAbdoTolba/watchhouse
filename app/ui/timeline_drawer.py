"""Two-strip timeline.

Top: a slim 24-hour overview minimap with a draggable / resizable
viewport rectangle. The rectangle defines the field-of-view that the
zoomed detail strip below renders.

Bottom: the detail strip - same per-camera lanes, but only the slice
of the day that's currently inside the overview's viewport. Clicking
the detail strip seeks to that exact moment; because the detail strip
spans only the viewport range, the click resolution scales with how
narrow you've made the viewport (a 30-second viewport gives you
~tenth-of-a-second click precision on a typical width).

Drag inside the viewport rectangle to pan it. Drag either edge to
resize it. Click outside the rectangle on the overview to recenter
the viewport at that point.
"""

from __future__ import annotations

from datetime import date as _date, datetime, time as _time, timedelta

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from app.core.clip_library import Clip
from app.core import dvr_time
from app.ui import theme


_DAY_SECONDS = 86_400.0
_MIN_VIEWPORT_SECONDS = 5.0


def _seconds_of_time(t) -> float:
    return float(t.hour * 3600 + t.minute * 60 + t.second)


class _Strip(QWidget):
    """One timeline strip: per-camera lanes + adaptive hour/minute axis.

    Renders only the slice (`_view_start_s` .. `_view_end_s`) of `_day`."""

    seek_requested = Signal(datetime)
    reset_requested = Signal()   # double-click: zoom back out to the whole day

    LANE_HEIGHT = 14
    LANE_PADDING = 3
    GUTTER_LEFT = 56
    GUTTER_RIGHT = 12
    AXIS_RESERVED = 22  # space below the lanes for the axis line + labels

    def __init__(self, cameras: list[int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Timeline")
        self._cameras = list(cameras)
        self._segments_by_cam: dict[int, list[Clip]] = {}
        self._events_by_cam: dict[int, list] = {}    # green: (start, end) spans
        self._pinned_by_cam: dict[int, list] = {}    # blue: permanent ranges
        self._selected: set[int] = set(self._cameras)
        self._day: _date = datetime.now().date()
        self._playhead: datetime | None = None
        self._view_start_s: float = 0.0
        self._view_end_s: float = _DAY_SECONDS

        n_lanes = max(1, len(self._cameras))
        self.setFixedHeight(
            n_lanes * (self.LANE_HEIGHT + self.LANE_PADDING) + self.AXIS_RESERVED
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # --- Public ---

    def set_day(self, day: _date) -> None:
        self._day = day
        self.update()

    def set_segments(self, by_cam: dict[int, list[Clip]]) -> None:
        self._segments_by_cam = by_cam
        self.update()

    def set_events(self, by_cam: dict) -> None:
        self._events_by_cam = by_cam or {}
        self.update()

    def set_pinned(self, by_cam: dict) -> None:
        self._pinned_by_cam = by_cam or {}
        self.update()

    def set_selected(self, selected: set[int]) -> None:
        self._selected = set(selected)
        self.update()

    def set_playhead(self, when: datetime | None) -> None:
        self._playhead = when
        self.update()

    def set_view_range(self, start_s: float, end_s: float) -> None:
        if end_s - start_s < _MIN_VIEWPORT_SECONDS:
            end_s = start_s + _MIN_VIEWPORT_SECONDS
        self._view_start_s = max(0.0, start_s)
        self._view_end_s = min(_DAY_SECONDS, end_s)
        self.update()

    # --- Mapping ---

    def _seconds_in_view(self) -> float:
        return max(_MIN_VIEWPORT_SECONDS, self._view_end_s - self._view_start_s)

    def _usable_w(self) -> float:
        return max(1.0, float(self.width() - self.GUTTER_LEFT - self.GUTTER_RIGHT))

    def _seconds_to_x(self, sec: float) -> float:
        rel = (sec - self._view_start_s) / self._seconds_in_view()
        return self.GUTTER_LEFT + rel * self._usable_w()

    def _x_to_seconds(self, x: float) -> float:
        rel = (x - self.GUTTER_LEFT) / self._usable_w()
        return self._view_start_s + max(0.0, min(1.0, rel)) * self._seconds_in_view()

    def _axis_step(self) -> int:
        span = self._seconds_in_view()
        # pick a step that gives ~6-12 ticks across the strip
        for step in (1, 5, 10, 30, 60, 300, 600, 1800, 3600, 7200, 10_800):
            if span / step <= 12:
                return step
        return 14_400

    def _format_tick(self, s: float, step: int) -> str:
        # Label only: shift to DVR display time. Tick POSITION uses raw `s`.
        s = int(dvr_time.shift_seconds_of_day(s)) % 86_400
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if step >= 3600:
            return f"{h:02d}:00"
        if step >= 60:
            return f"{h:02d}:{m:02d}"
        return f"{m:02d}:{sec:02d}"

    # --- Paint ---

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor(theme.SURFACE))

        font = QFont("Cascadia Code", 9)
        p.setFont(font)
        fm = QFontMetrics(font)

        for i, cam_id in enumerate(self._cameras):
            y = i * (self.LANE_HEIGHT + self.LANE_PADDING) + self.LANE_PADDING
            p.fillRect(
                QRectF(self.GUTTER_LEFT, y, self._usable_w(), self.LANE_HEIGHT),
                QColor(theme.SURFACE_2),
            )
            is_active = cam_id in self._selected
            if self.GUTTER_LEFT >= 32:
                p.setPen(QColor(theme.TEXT if is_active else theme.TEXT_DIM))
                p.drawText(
                    10,
                    int(y + (self.LANE_HEIGHT + fm.ascent()) / 2 - fm.descent() / 2),
                    f"CAM 0{cam_id}",
                )

            for clip in self._segments_by_cam.get(cam_id, []):
                if clip.start_at.date() != self._day:
                    continue
                s_start = _seconds_of_time(clip.start_at)
                end_dt = clip.end_at_estimated
                if end_dt.date() != self._day:
                    s_end = _DAY_SECONDS - 1.0
                else:
                    s_end = _seconds_of_time(end_dt)
                # cull entirely outside view
                if s_end < self._view_start_s or s_start > self._view_end_s:
                    continue
                x0 = self._seconds_to_x(max(s_start, self._view_start_s))
                x1 = self._seconds_to_x(min(s_end, self._view_end_s))
                if x1 - x0 < 2:
                    x1 = x0 + 2
                color = QColor(theme.TL_REC if is_active else theme.BORDER_2)
                p.fillRect(QRectF(x0, y + 1, x1 - x0, self.LANE_HEIGHT - 2), color)

            # Middle (blue, locked) then front (green, events) layers, painted
            # over the recording so the front wins where ranges overlap.
            if is_active:
                pinned = self._pinned_by_cam.get(cam_id, [])
                self._draw_spans(p, pinned, theme.TL_PINNED, y)
                self._draw_spans(p, self._events_by_cam.get(cam_id, []),
                                 theme.TL_EVENT, y)
                # Lock marker: a thin blue bar drawn LAST, so a locked event
                # (which stays green on top) still visibly shows it's locked.
                self._draw_spans(p, pinned, theme.TL_PINNED, y, height=3)

        axis_y = len(self._cameras) * (self.LANE_HEIGHT + self.LANE_PADDING) + 4
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(self.GUTTER_LEFT, axis_y, self.width() - self.GUTTER_RIGHT, axis_y)
        p.setPen(QColor(theme.TEXT_MUTED))
        p.setFont(QFont("Cascadia Code", 8))
        step = self._axis_step()
        first = int(self._view_start_s // step) * step
        s = first
        while s <= self._view_end_s + 1:
            if s >= self._view_start_s - 1:
                x = self._seconds_to_x(s)
                p.drawLine(int(x), axis_y, int(x), axis_y + 4)
                p.drawText(int(x) - 18, axis_y + 16, self._format_tick(s, step))
            s += step

        if self._playhead is not None and self._playhead.date() == self._day:
            ph_s = _seconds_of_time(self._playhead)
            if self._view_start_s <= ph_s <= self._view_end_s:
                x = self._seconds_to_x(ph_s)
                p.setPen(QPen(QColor(theme.ACCENT), 2))
                p.drawLine(int(x), 0, int(x), axis_y)

    def _draw_spans(self, p, spans, color, y, height=None) -> None:
        """Paint coloured bands for (start_dt, end_dt) ranges on one lane,
        clipped to the current day + view window (same mapping as clips). A
        small `height` draws a thin top bar (used as a lock marker)."""
        h = (self.LANE_HEIGHT - 2) if height is None else height
        for s_dt, e_dt in spans:
            if e_dt.date() < self._day or s_dt.date() > self._day:
                continue
            s_start = 0.0 if s_dt.date() < self._day else _seconds_of_time(s_dt)
            s_end = (_DAY_SECONDS - 1.0 if e_dt.date() > self._day
                     else _seconds_of_time(e_dt))
            if s_end < self._view_start_s or s_start > self._view_end_s:
                continue
            x0 = self._seconds_to_x(max(s_start, self._view_start_s))
            x1 = self._seconds_to_x(min(s_end, self._view_end_s))
            if x1 - x0 < 2:
                x1 = x0 + 2
            p.fillRect(QRectF(x0, y + 1, x1 - x0, h), QColor(color))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        s = self._x_to_seconds(event.position().x())
        s = max(0.0, min(_DAY_SECONDS - 1.0, s))
        target = datetime.combine(self._day, _time()) + timedelta(seconds=s)
        self.seek_requested.emit(target)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # Double-click anywhere on a strip zooms back out to the full day, so
        # the user can escape a tight zoom without dragging the handles.
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_requested.emit()
            event.accept()


class _OverviewStrip(_Strip):
    """The 24h minimap. Lanes are thinner, no per-camera label gutter, and a
    draggable accent rectangle on top defines the viewport range that the
    sibling detail strip should render."""

    LANE_HEIGHT = 4
    LANE_PADDING = 2
    GUTTER_LEFT = 10
    GUTTER_RIGHT = 10
    AXIS_RESERVED = 22
    HANDLE_PX = 6

    viewport_changed = Signal(float, float)  # start_s, end_s
    user_zoomed = Signal()                    # user grabbed/recentred the viewport

    def __init__(self, cameras: list[int], parent: QWidget | None = None) -> None:
        super().__init__(cameras, parent)
        # The overview itself always renders the full day
        self._view_start_s = 0.0
        self._view_end_s = _DAY_SECONDS
        # The selectable viewport (what the detail strip should show)
        self._vp_start_s: float = 0.0
        self._vp_end_s: float = _DAY_SECONDS
        self._drag: str | None = None  # "left" | "right" | "inside"
        self._drag_anchor_s: float = 0.0
        self._drag_anchor_vp: tuple[float, float] = (0.0, _DAY_SECONDS)

    def viewport(self) -> tuple[float, float]:
        return (self._vp_start_s, self._vp_end_s)

    def set_viewport(self, start_s: float, end_s: float, *, emit: bool = True) -> None:
        if end_s - start_s < _MIN_VIEWPORT_SECONDS:
            end_s = start_s + _MIN_VIEWPORT_SECONDS
        self._vp_start_s = max(0.0, min(_DAY_SECONDS - _MIN_VIEWPORT_SECONDS, start_s))
        self._vp_end_s = max(self._vp_start_s + _MIN_VIEWPORT_SECONDS, min(_DAY_SECONDS, end_s))
        self.update()
        if emit:
            self.viewport_changed.emit(self._vp_start_s, self._vp_end_s)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        x0 = self._seconds_to_x(self._vp_start_s)
        x1 = self._seconds_to_x(self._vp_end_s)
        axis_y = len(self._cameras) * (self.LANE_HEIGHT + self.LANE_PADDING) + 4
        rect = QRectF(x0, 0, max(2.0, x1 - x0), axis_y)
        fill = QColor(theme.ACCENT)
        fill.setAlpha(55)
        p.fillRect(rect, fill)
        p.setPen(QPen(QColor(theme.ACCENT), 1))
        p.drawRect(rect)
        # Edge handles - solid accent slivers for grip affordance
        p.setBrush(QColor(theme.ACCENT))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(QRectF(x0 - 1, 0, 2, axis_y))
        p.drawRect(QRectF(x1 - 1, 0, 2, axis_y))

    def _handle_at(self, x: float) -> str | None:
        x0 = self._seconds_to_x(self._vp_start_s)
        x1 = self._seconds_to_x(self._vp_end_s)
        if abs(x - x0) <= self.HANDLE_PX:
            return "left"
        if abs(x - x1) <= self.HANDLE_PX:
            return "right"
        if x0 < x < x1:
            return "inside"
        return None

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        x = ev.position().x()
        if self._drag is None:
            handle = self._handle_at(x)
            if handle in ("left", "right"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif handle == "inside":
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            return
        s_now = max(0.0, min(_DAY_SECONDS, self._x_to_seconds(x)))
        vs, ve = self._drag_anchor_vp
        if self._drag == "left":
            self.set_viewport(min(s_now, ve - _MIN_VIEWPORT_SECONDS), ve)
        elif self._drag == "right":
            self.set_viewport(vs, max(s_now, vs + _MIN_VIEWPORT_SECONDS))
        else:  # "inside"
            delta = s_now - self._drag_anchor_s
            new_s = vs + delta
            new_e = ve + delta
            if new_s < 0:
                new_e -= new_s
                new_s = 0.0
            if new_e > _DAY_SECONDS:
                new_s -= new_e - _DAY_SECONDS
                new_e = _DAY_SECONDS
            self.set_viewport(new_s, new_e)

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        # Any deliberate grab/pan/recenter here is the user taking over the
        # zoom; tell the drawer so it stops auto-fitting on the next refresh.
        self.user_zoomed.emit()
        x = ev.position().x()
        handle = self._handle_at(x)
        if handle is not None:
            self._drag = handle
            self._drag_anchor_s = self._x_to_seconds(x)
            self._drag_anchor_vp = (self._vp_start_s, self._vp_end_s)
            if handle == "inside":
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        # Click outside the viewport: recenter on click, keep width
        center = self._x_to_seconds(x)
        half = (self._vp_end_s - self._vp_start_s) / 2.0
        self.set_viewport(center - half, center + half)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        self._drag = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class TimelineDrawer(QWidget):
    """Composite widget: overview minimap on top, detail strip below.

    Drop-in replacement for the old TimelineWidget - same public API
    (`set_day`, `set_segments`, `set_selected_cams`, `set_playhead`,
    `seek_requested` signal)."""

    seek_requested = Signal(datetime)

    def __init__(self, cameras: list[int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TimelineDrawer")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        self._overview = _OverviewStrip(cameras, self)
        self._detail = _Strip(cameras, self)
        v.addWidget(self._overview)
        v.addWidget(self._detail)

        # Auto-fit state: the timeline opens zoomed to the range that actually
        # has recordings (the rolling window), not an empty 24h day. We stop
        # auto-fitting once the user manually zooms/pans, and resume on a day
        # change or a double-click reset.
        self._day: _date = datetime.now().date()
        self._segments_by_cam: dict[int, list[Clip]] = {}
        self._user_zoomed = False

        self._overview.viewport_changed.connect(self._detail.set_view_range)
        self._overview.user_zoomed.connect(self._on_user_zoomed)
        self._detail.seek_requested.connect(self.seek_requested.emit)
        # Double-click on the overview minimap re-fits the viewport to the
        # recorded range (not the empty whole day). The detail strip is the
        # playback scrubber, so its double-click is deliberately not connected.
        self._overview.reset_requested.connect(self._reset_to_recordings)
        # Default until segments arrive: full-day view, all visible.
        self._overview.set_viewport(0.0, _DAY_SECONDS, emit=False)
        self._detail.set_view_range(0.0, _DAY_SECONDS)

    # Pass-through API
    def set_day(self, day: _date) -> None:
        if day != self._day:
            # New day selected -> resume auto-fitting for that day's clips.
            self._user_zoomed = False
        self._day = day
        self._overview.set_day(day)
        self._detail.set_day(day)

    def set_segments(self, by_cam: dict[int, list[Clip]]) -> None:
        self._segments_by_cam = by_cam
        self._overview.set_segments(by_cam)
        self._detail.set_segments(by_cam)
        self._maybe_autofit()

    def set_events(self, by_cam: dict) -> None:
        self._overview.set_events(by_cam)
        self._detail.set_events(by_cam)

    def set_pinned(self, by_cam: dict) -> None:
        self._overview.set_pinned(by_cam)
        self._detail.set_pinned(by_cam)

    def selected_range(self) -> tuple:
        """The (start, end) wall-clock window currently framed by the overview
        viewport rectangle — what PIN / UNPIN act on."""
        vs, ve = self._overview.viewport()
        base = datetime.combine(self._day, _time())
        return base + timedelta(seconds=vs), base + timedelta(seconds=ve)

    def set_selected_cams(self, selected: set[int]) -> None:
        self._overview.set_selected(selected)
        self._detail.set_selected(selected)

    def set_playhead(self, when: datetime | None) -> None:
        self._overview.set_playhead(when)
        self._detail.set_playhead(when)

    def _on_user_zoomed(self) -> None:
        """User grabbed/panned the viewport; stop auto-fitting so a refresh
        doesn't yank their chosen zoom away."""
        self._user_zoomed = True

    def _reset_to_recordings(self) -> None:
        """Double-click: re-fit to the recorded range (or the whole day if
        nothing is recorded) and resume auto-fitting."""
        self._user_zoomed = False
        self._maybe_autofit(force=True)

    def _recorded_span_s(self) -> tuple[float, float] | None:
        """Min start .. max end (seconds-of-day) across all clips on the
        current day, or None if there are none."""
        lo = _DAY_SECONDS
        hi = 0.0
        found = False
        for clips in self._segments_by_cam.values():
            for clip in clips:
                if clip.start_at.date() != self._day:
                    continue
                s_start = _seconds_of_time(clip.start_at)
                end_dt = clip.end_at_estimated
                s_end = (
                    _DAY_SECONDS if end_dt.date() != self._day
                    else _seconds_of_time(end_dt)
                )
                lo = min(lo, s_start)
                hi = max(hi, s_end)
                found = True
        return (lo, hi) if found else None

    def _maybe_autofit(self, force: bool = False) -> None:
        """Zoom the viewport to the recorded range unless the user has taken
        over the zoom. A little padding keeps the first/last clip off the edge."""
        if self._user_zoomed and not force:
            return
        span = self._recorded_span_s()
        if span is None:
            if force:
                self._overview.set_viewport(0.0, _DAY_SECONDS)
            return
        lo, hi = span
        pad = max(60.0, (hi - lo) * 0.05)
        start = max(0.0, lo - pad)
        end = min(_DAY_SECONDS, hi + pad)
        if end - start < _MIN_VIEWPORT_SECONDS:
            end = min(_DAY_SECONDS, start + _MIN_VIEWPORT_SECONDS)
        self._overview.set_viewport(start, end)
