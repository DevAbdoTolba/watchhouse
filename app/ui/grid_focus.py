"""Camera focus/maximize controller shared by the live and playback grids.

Interactions (work in any 2x2 camera grid):
  - double-click a tile          -> maximize just that one
  - double-click again           -> back to the full 2x2
  - shift + double-click tiles   -> mark each as selected (accumulates)
  - release shift                -> focus all selected tiles together
  - double-click (no shift)      -> back to the full 2x2

Focused subsets are laid out to fill the whole grid area - no empty cells,
no wasted black margins - with tuned arrangements for 1, 2 and 3 cameras.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QGridLayout


def positions_for(n: int) -> list[tuple[int, int, int, int]]:
    """(row, col, rowspan, colspan) for n tiles over a 2x2 base grid."""
    if n <= 1:
        return [(0, 0, 2, 2)]                       # one tile fills everything
    if n == 2:
        return [(0, 0, 2, 1), (0, 1, 2, 1)]          # two full-height columns
    if n == 3:
        return [(0, 0, 2, 1), (0, 1, 1, 1), (1, 1, 1, 1)]  # big left + 2 stacked
    return [(0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 1, 1)]  # 2x2


class GridFocus(QObject):
    def __init__(self, grid: QGridLayout, tiles: list, index_of, set_selected,
                 parent=None) -> None:
        super().__init__(parent)
        self._grid = grid
        self._tiles = tiles
        self._index_of = index_of          # tile -> int camera index
        self._set_selected = set_selected  # (tile, bool) -> highlight
        self._focus: set[int] = set()      # empty == show all
        self._pending: set[int] = set()    # shift-accumulated selection
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # Wired to each tile's double_clicked(index, shift_held) signal.
    def handle_double_click(self, index: int, shift: bool) -> None:
        if shift:
            if index in self._pending:
                self._pending.discard(index)
            else:
                self._pending.add(index)
            self._refresh_selection()
            return
        # Plain double-click: collapse out of focus, or maximize this one.
        self._pending.clear()
        self._refresh_selection()
        self._focus = set() if self._focus else {index}
        self._apply()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt)
        if (event.type() == QEvent.Type.KeyRelease
                and event.key() == Qt.Key.Key_Shift and self._pending):
            all_idx = {self._index_of(t) for t in self._tiles}
            # Selecting every camera is just the normal grid - don't enter a
            # focus state (which would also block the next double-click maximize).
            self._focus = set() if self._pending >= all_idx else set(self._pending)
            self._pending.clear()
            self._refresh_selection()
            self._apply()
        return False

    def reset(self) -> None:
        self._focus.clear()
        self._pending.clear()
        self._refresh_selection()
        self._apply()

    def _refresh_selection(self) -> None:
        for t in self._tiles:
            self._set_selected(t, self._index_of(t) in self._pending)

    def _apply(self) -> None:
        focus = self._focus or {self._index_of(t) for t in self._tiles}
        visible = sorted((t for t in self._tiles if self._index_of(t) in focus),
                         key=self._index_of)
        for t in self._tiles:
            self._grid.removeWidget(t)
        layout = positions_for(len(visible))
        for i, t in enumerate(visible):
            r, c, rs, cs = layout[i]
            self._grid.addWidget(t, r, c, rs, cs)
            t.setVisible(True)
        for t in self._tiles:
            if t not in visible:
                t.setVisible(False)
