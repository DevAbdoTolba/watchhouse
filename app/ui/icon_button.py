"""Compact transport-style icon buttons.

Each button is a small QPushButton that paints its own glyph (skip-back,
skip-forward, play, pause) via QPainter. Single-color icons that pick
up the current text color so they hover/check the same as text buttons.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPolygonF
from PySide6.QtWidgets import QPushButton

from app.ui import theme


class IconButton(QPushButton):
    """A QPushButton that paints a vector icon over a styled background."""

    KIND_PLAY = "play"
    KIND_PAUSE = "pause"
    KIND_SKIP_BACK = "skip_back"
    KIND_SKIP_FWD = "skip_fwd"

    def __init__(self, kind: str, label: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("IconButton")
        self._kind = kind
        self._label = label  # tiny text under the icon (e.g. "30s")
        self.setFixedSize(46, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_kind(self, kind: str) -> None:
        if kind != self._kind:
            self._kind = kind
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        # Let QSS paint the background/border first
        super().paintEvent(_event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Pick icon color: ACCENT on hover/checked, TEXT otherwise
        if self.isChecked() or self.underMouse():
            color = QColor(theme.ACCENT)
        else:
            color = QColor(theme.TEXT)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)

        # Centre the icon glyph in roughly the upper 22 px so the optional
        # label can sit underneath
        cx = self.width() / 2
        cy = 14
        size = 9  # half-extent

        if self._kind == self.KIND_PLAY:
            poly = QPolygonF([
                QPointF(cx - size / 2, cy - size),
                QPointF(cx + size, cy),
                QPointF(cx - size / 2, cy + size),
            ])
            p.drawPolygon(poly)

        elif self._kind == self.KIND_PAUSE:
            bar_w = 3
            gap = 3
            p.drawRect(QRectF(cx - bar_w - gap / 2, cy - size, bar_w, size * 2))
            p.drawRect(QRectF(cx + gap / 2, cy - size, bar_w, size * 2))

        elif self._kind == self.KIND_SKIP_BACK:
            # vertical bar + leftward triangle
            bar_w = 2
            p.drawRect(QRectF(cx - size - 2, cy - size, bar_w, size * 2))
            poly = QPolygonF([
                QPointF(cx + size, cy - size),
                QPointF(cx - size + 2, cy),
                QPointF(cx + size, cy + size),
            ])
            p.drawPolygon(poly)

        elif self._kind == self.KIND_SKIP_FWD:
            bar_w = 2
            p.drawRect(QRectF(cx + size, cy - size, bar_w, size * 2))
            poly = QPolygonF([
                QPointF(cx - size, cy - size),
                QPointF(cx + size - 2, cy),
                QPointF(cx - size, cy + size),
            ])
            p.drawPolygon(poly)

        # Optional label under the icon
        if self._label:
            p.setPen(QColor(theme.TEXT_MUTED))
            font = QFont("Cascadia Code", 7)
            p.setFont(font)
            p.drawText(
                QRectF(0, self.height() - 12, self.width(), 12),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                self._label,
            )
