"""24-hour timeline strip.

Renders four horizontal lanes (one per camera), each with colored bars
for the recorded segments on the selected date. Clicking anywhere
emits `seek_requested(datetime)` so the playback view can jump every
selected camera to the matching wall-clock moment.

The playhead is a vertical accent line indicating the current playback
position.
"""

from __future__ import annotations

from datetime import date as _date, datetime, time as _time, timedelta

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.core.clip_library import Clip
from app.ui import theme


class TimelineWidget(QWidget):
    seek_requested = Signal(datetime)

    LANE_HEIGHT = 14
    LANE_PADDING = 4
    GUTTER_LEFT = 56
    GUTTER_RIGHT = 16

    def __init__(self, cameras: list[int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Timeline")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cameras = cameras
        self._segments_by_cam: dict[int, list[Clip]] = {}
        self._selected: set[int] = set(cameras)
        self._day: _date = datetime.now().date()
        self._playhead: datetime | None = None

        n_lanes = max(1, len(self._cameras))
        self.setFixedHeight(
            n_lanes * (self.LANE_HEIGHT + self.LANE_PADDING) + 36  # hour axis at bottom
        )
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # --- Public ---

    def set_day(self, day: _date) -> None:
        self._day = day
        self.update()

    def set_segments(self, by_cam: dict[int, list[Clip]]) -> None:
        self._segments_by_cam = by_cam
        self.update()

    def set_selected_cams(self, selected: set[int]) -> None:
        self._selected = selected
        self.update()

    def set_playhead(self, when: datetime | None) -> None:
        self._playhead = when
        self.update()

    # --- Helpers ---

    def _time_to_x(self, t: datetime | _time) -> float:
        if isinstance(t, datetime):
            tt = t.time()
        else:
            tt = t
        seconds = tt.hour * 3600 + tt.minute * 60 + tt.second
        usable_w = self.width() - self.GUTTER_LEFT - self.GUTTER_RIGHT
        return self.GUTTER_LEFT + (seconds / 86400.0) * usable_w

    def _x_to_seconds(self, x: float) -> float:
        usable_w = self.width() - self.GUTTER_LEFT - self.GUTTER_RIGHT
        rel = max(0.0, min(1.0, (x - self.GUTTER_LEFT) / max(1.0, usable_w)))
        return rel * 86400.0

    # --- Painting ---

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(theme.SURFACE))

        # Lane labels + lane backgrounds
        label_font = QFont("Cascadia Code", 9)
        painter.setFont(label_font)
        fm = QFontMetrics(label_font)
        for i, cam_id in enumerate(self._cameras):
            y = i * (self.LANE_HEIGHT + self.LANE_PADDING) + self.LANE_PADDING
            # Lane background
            painter.fillRect(
                QRectF(self.GUTTER_LEFT, y, self.width() - self.GUTTER_LEFT - self.GUTTER_RIGHT, self.LANE_HEIGHT),
                QColor(theme.SURFACE_2),
            )
            # Label
            is_active = cam_id in self._selected
            painter.setPen(QColor(theme.TEXT if is_active else theme.TEXT_DIM))
            text = f"CAM 0{cam_id}"
            painter.drawText(
                10,
                y + (self.LANE_HEIGHT + fm.ascent()) / 2 - fm.descent() / 2,
                text,
            )

            # Segments
            for clip in self._segments_by_cam.get(cam_id, []):
                if clip.start_at.date() != self._day:
                    continue
                start_x = self._time_to_x(clip.start_at)
                end_dt = clip.end_at_estimated
                # Cap at midnight to keep within the lane
                end_of_day = datetime.combine(self._day, _time(23, 59, 59))
                if end_dt > end_of_day:
                    end_dt = end_of_day
                end_x = self._time_to_x(end_dt)
                if end_x <= start_x:
                    end_x = start_x + 2
                color = QColor(theme.ACCENT if is_active else theme.BORDER_2)
                painter.fillRect(
                    QRectF(start_x, y + 1, end_x - start_x, self.LANE_HEIGHT - 2),
                    color,
                )

        # Hour axis
        axis_y = len(self._cameras) * (self.LANE_HEIGHT + self.LANE_PADDING) + 4
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(self.GUTTER_LEFT, axis_y, self.width() - self.GUTTER_RIGHT, axis_y)
        painter.setPen(QColor(theme.TEXT_MUTED))
        axis_font = QFont("Cascadia Code", 8)
        painter.setFont(axis_font)
        for h in range(0, 25, 2):
            tt = _time(h % 24, 0)
            x = self._time_to_x(tt)
            painter.drawLine(int(x), axis_y, int(x), axis_y + 4)
            painter.drawText(int(x) - 8, axis_y + 16, f"{h:02d}")

        # Playhead
        if self._playhead is not None and self._playhead.date() == self._day:
            ph_x = self._time_to_x(self._playhead)
            painter.setPen(QPen(QColor(theme.ACCENT_STRONG if hasattr(theme, "ACCENT_STRONG") else theme.ACCENT), 1))
            painter.drawLine(int(ph_x), 0, int(ph_x), axis_y)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        seconds = int(self._x_to_seconds(event.position().x()))
        target = datetime.combine(self._day, _time()) + timedelta(seconds=seconds)
        self.seek_requested.emit(target)
