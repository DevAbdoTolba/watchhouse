"""Playback transport bar: play / step / speed / boxes controls plus the
events-mode scrub row.

Extracted from PlaybackView (which had grown into a god-class): the bar owns
its widgets, the speed preset cycling, and the permille<->seconds scrub math,
and talks to the view only through signals and small setters. The view keeps
the playback *state* (mode, cursor, current event); the bar is pure controls.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.ui.icon_button import IconButton


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


class TransportBar(QWidget):
    """Bottom control strip for PLAYBACK mode. State in, intents out."""

    play_toggled = Signal()        # the play/pause button was clicked
    step_requested = Signal(int)   # signed seconds (sign = direction)
    speed_changed = Signal(float)  # a new playback speed was chosen
    boxes_toggled = Signal(bool)   # detection-box overlay on/off
    scrub_pressed = Signal()       # user grabbed the scrub handle
    seek_requested = Signal(float) # seconds into the event clip
    scrub_released = Signal()      # user let go of the scrub handle

    _SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)

    def __init__(self, boxes_on: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TransportBar")
        self.setFixedHeight(82)
        self._step_seconds = 5
        self._speed_idx = self._SPEEDS.index(1.0)
        self._duration = 0.0   # current event clip length (s); 0 until known
        self._scrubbing = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 6, 14, 6)
        outer.setSpacing(6)

        # Scrub row (events mode only): [cur] [=====slider=====] [total].
        # Bound to the trigger camera's real decoder position; hidden in
        # recordings mode, where the TimelineDrawer is the scrubber instead.
        mono_font = QFont("Cascadia Code", 10)
        self._scrub_row = QWidget(self)
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
        self._scrub.sliderPressed.connect(self._on_scrub_pressed)
        self._scrub.sliderMoved.connect(self._on_scrub_moved)
        self._scrub.sliderReleased.connect(self._on_scrub_released)
        self._scrub_total = QLabel("--:--", self._scrub_row)
        self._scrub_total.setObjectName("ScrubTime")
        self._scrub_total.setFont(mono_font)
        sr.addWidget(self._scrub_cur)
        sr.addWidget(self._scrub, 1)
        sr.addWidget(self._scrub_total)
        outer.addWidget(self._scrub_row)

        row_wrap = QWidget(self)
        h = QHBoxLayout(row_wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)

        jump_back = IconButton(IconButton.KIND_SKIP_BACK, "", self)
        jump_back.setToolTip("Step back (uses the active step size)")
        jump_back.clicked.connect(
            lambda: self.step_requested.emit(-self._step_seconds))

        self._play_btn = IconButton(IconButton.KIND_PLAY, "", self)
        self._play_btn.setFixedSize(46, 32)
        self._play_btn.setToolTip("Play / Pause")
        self._play_btn.clicked.connect(self.play_toggled)

        jump_fwd = IconButton(IconButton.KIND_SKIP_FWD, "", self)
        jump_fwd.setToolTip("Step forward (uses the active step size)")
        jump_fwd.clicked.connect(
            lambda: self.step_requested.emit(self._step_seconds))

        h.addWidget(jump_back)
        h.addWidget(self._play_btn)
        h.addWidget(jump_fwd)

        h.addSpacing(16)

        step_label = QLabel("STEP", self)
        step_label.setObjectName("DialogFieldLabel")
        h.addWidget(step_label)
        self._step_buttons: dict[int, QPushButton] = {}
        for sec, lbl in ((1, "1s"), (5, "5s"), (15, "15s"), (60, "1m")):
            b = QPushButton(lbl, self)
            b.setObjectName("SpeedButton")
            b.setCheckable(True)
            b.setFixedSize(36, 24)
            b.setChecked(sec == self._step_seconds)
            b.clicked.connect(lambda _checked, s=sec: self.set_step(s))
            self._step_buttons[sec] = b
            h.addWidget(b)

        h.addSpacing(16)

        speed_label = QLabel("SPEED", self)
        speed_label.setObjectName("DialogFieldLabel")
        h.addWidget(speed_label)
        # Compact speed stepper: ‹ [1×] › cycles the presets without a long
        # button row or a dropdown. The middle pill shows the current value
        # (click to snap back to 1×); the arrows auto-disable at the ends.
        self._speed_down = QPushButton("‹", self)
        self._speed_down.setObjectName("SpeedButton")
        self._speed_down.setFixedSize(24, 24)
        self._speed_down.setToolTip("Slower")
        self._speed_down.clicked.connect(lambda: self._step_speed(-1))
        self._speed_pill = QPushButton("1×", self)
        self._speed_pill.setObjectName("SpeedButton")
        self._speed_pill.setCheckable(True)
        self._speed_pill.setChecked(True)
        self._speed_pill.setFixedSize(46, 24)
        self._speed_pill.setToolTip("Playback speed — click to reset to 1×")
        self._speed_pill.clicked.connect(lambda: self.set_speed(1.0))
        self._speed_up = QPushButton("›", self)
        self._speed_up.setObjectName("SpeedButton")
        self._speed_up.setFixedSize(24, 24)
        self._speed_up.setToolTip("Faster")
        self._speed_up.clicked.connect(lambda: self._step_speed(1))
        h.addWidget(self._speed_down)
        h.addWidget(self._speed_pill)
        h.addWidget(self._speed_up)

        h.addSpacing(16)
        self._boxes_btn = QPushButton("BOXES", self)
        self._boxes_btn.setObjectName("SpeedButton")
        self._boxes_btn.setCheckable(True)
        self._boxes_btn.setChecked(boxes_on)
        self._boxes_btn.setFixedSize(52, 24)
        self._boxes_btn.setToolTip("Show detection bounding boxes over event playback")
        self._boxes_btn.clicked.connect(
            lambda: self.boxes_toggled.emit(self._boxes_btn.isChecked()))
        h.addWidget(self._boxes_btn)

        h.addStretch(1)

        self._cursor_label = QLabel("--:--:--", self)
        self._cursor_label.setObjectName("StatusBarText")
        h.addWidget(self._cursor_label)

        outer.addWidget(row_wrap)

    # --- state setters (called by the view) --------------------------------

    def set_playing(self, on: bool) -> None:
        self._play_btn.set_kind(
            IconButton.KIND_PAUSE if on else IconButton.KIND_PLAY)

    def set_step(self, seconds: int) -> None:
        self._step_seconds = seconds
        for sec, btn in self._step_buttons.items():
            btn.setChecked(sec == seconds)

    def set_speed(self, s: float) -> None:
        # Snap to the nearest preset so the pill always shows a real value.
        if s in self._SPEEDS:
            self._speed_idx = self._SPEEDS.index(s)
        else:
            self._speed_idx = min(
                range(len(self._SPEEDS)),
                key=lambda i: abs(self._SPEEDS[i] - s),
            )
            s = self._SPEEDS[self._speed_idx]
        self._speed_pill.setText(f"{s:g}×")
        self._speed_pill.setChecked(True)
        self._speed_down.setEnabled(self._speed_idx > 0)
        self._speed_up.setEnabled(self._speed_idx < len(self._SPEEDS) - 1)
        self.speed_changed.emit(s)

    def speed(self) -> float:
        return self._SPEEDS[self._speed_idx]

    def boxes_on(self) -> bool:
        return self._boxes_btn.isChecked()

    def is_scrubbing(self) -> bool:
        return self._scrubbing

    def set_cursor_text(self, text: str) -> None:
        self._cursor_label.setText(text)

    def set_scrub_visible(self, on: bool) -> None:
        self._scrub_row.setVisible(on)

    def reset_scrub(self) -> None:
        """Zero the scrub bar + labels until the next set_duration arrives."""
        self._duration = 0.0
        self._scrubbing = False
        self._scrub.blockSignals(True)
        self._scrub.setValue(0)
        self._scrub.blockSignals(False)
        self._scrub.setEnabled(False)
        self._scrub_cur.setText("00:00")
        self._scrub_total.setText("--:--")

    def set_duration(self, dur: float) -> None:
        self._duration = float(dur or 0.0)
        if self._duration > 0:
            self._scrub.setEnabled(True)
            self._scrub_total.setText(self._format_secs(self._duration))
        else:
            self._scrub.setEnabled(False)
            self._scrub_total.setText("--:--")

    def sync_scrub(self, seconds: float) -> None:
        """Follow the real decode position (no signals; no-op mid-drag)."""
        if self._scrubbing:
            return
        if self._duration > 0:
            permille = round(seconds / self._duration * 1000)
            self._scrub.blockSignals(True)
            self._scrub.setValue(min(1000, max(0, permille)))
            self._scrub.blockSignals(False)
        self._scrub_cur.setText(self._format_secs(seconds))

    def pin_scrub_to_end(self) -> None:
        """Clip ended: leave the bar parked at the end."""
        if self._duration <= 0:
            return
        self._scrub.blockSignals(True)
        self._scrub.setValue(1000)
        self._scrub.blockSignals(False)
        self._scrub_cur.setText(self._format_secs(self._duration))

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _format_secs(seconds: float) -> str:
        """Seconds -> MM:SS (or H:MM:SS past an hour)."""
        total = int(max(0.0, seconds))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _step_speed(self, direction: int) -> None:
        idx = max(0, min(len(self._SPEEDS) - 1, self._speed_idx + direction))
        self.set_speed(self._SPEEDS[idx])

    def _seek_to_permille(self, value: int) -> None:
        if self._duration <= 0:
            return
        seconds = value / 1000.0 * self._duration
        self._scrub_cur.setText(self._format_secs(seconds))
        self.seek_requested.emit(seconds)

    @Slot()
    def _on_scrub_pressed(self) -> None:
        self._scrubbing = True
        self.scrub_pressed.emit()

    @Slot(int)
    def _on_scrub_moved(self, value: int) -> None:
        self._seek_to_permille(value)

    @Slot()
    def _on_scrub_released(self) -> None:
        self._seek_to_permille(self._scrub.value())
        self._scrubbing = False
        self.scrub_released.emit()
