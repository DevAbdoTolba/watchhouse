"""Camera Links setup — teach the app which cameras share a crossing.

Define a directional link between two cameras: the frame edge a person crosses
on each, a rough transit time, and a human name + both direction labels. The
matcher then fuses the same movement seen on both cameras into ONE event
("Front door — went out") instead of two separate per-camera alerts.

Call values() after exec() == Accepted to get the edited list of Links.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.camera_links import EDGES, Link

_EDGE_LABELS = [("Left", "left"), ("Right", "right"),
                ("Top", "top"), ("Bottom", "bottom")]


class CameraLinksDialog(QDialog):
    def __init__(self, cameras, links, cam_labels=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WipeDialog")  # reuse shared dialog styling
        self.setWindowTitle("Camera Links")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._cameras = list(cameras)
        self._labels = dict(cam_labels or {})
        self._links: list[Link] = list(links or [])
        self._edit_row = -1  # index being edited, or -1 for "add new"

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(12)

        title = QLabel("CAMERA LINKS", self)
        title.setObjectName("DialogTitle")
        sub = QLabel(
            "Tie two cameras that share a crossing (e.g. a doorway). The same "
            "movement seen on both becomes ONE named event instead of two "
            "camera alerts. We match the motion (time + which frame edge + "
            "direction), not the person.",
            self,
        )
        sub.setObjectName("DialogSubtitle")
        sub.setWordWrap(True)
        v.addWidget(title)
        v.addWidget(sub)

        sep = QFrame(self)
        sep.setObjectName("DialogSeparator")
        sep.setFixedHeight(1)
        v.addWidget(sep)

        self._list = QListWidget(self)
        self._list.setMinimumHeight(110)
        self._list.itemSelectionChanged.connect(self._on_select)
        v.addWidget(self._list)

        # --- editor form ---------------------------------------------------
        self._name = QLineEdit(self)
        self._name.setPlaceholderText("Name, e.g. Front door")
        v.addLayout(self._row("Name", self._name))

        self._cam_a = self._camera_combo()
        self._edge_a = self._edge_combo()
        v.addLayout(self._pair_row("Camera A", self._cam_a, "leaves edge", self._edge_a))

        self._cam_b = self._camera_combo()
        self._edge_b = self._edge_combo()
        v.addLayout(self._pair_row("Camera B", self._cam_b, "enters edge", self._edge_b))

        self._transit = QDoubleSpinBox(self)
        self._transit.setRange(0.0, 120.0)
        self._transit.setValue(5.0)
        self._transit.setSuffix(" s")
        self._transit.setDecimals(1)
        v.addLayout(self._row("Transit time", self._transit))

        self._label_ab = QLineEdit(self)
        self._label_ab.setText("went out")
        v.addLayout(self._row("A → B says", self._label_ab))
        self._label_ba = QLineEdit(self)
        self._label_ba.setText("came in")
        v.addLayout(self._row("B → A says", self._label_ba))

        hint = QLabel(
            "Edge = the frame side the person crosses through: where they leave "
            "Camera A and where they appear on Camera B.", self)
        hint.setObjectName("DialogSubtitle")
        hint.setWordWrap(True)
        v.addWidget(hint)

        edit_btns = QHBoxLayout()
        self._add_btn = QPushButton("Add / Update link", self)
        self._add_btn.setObjectName("ToolbarAction")
        self._add_btn.setMinimumHeight(30)
        self._add_btn.clicked.connect(self._on_add_update)
        self._del_btn = QPushButton("Remove selected", self)
        self._del_btn.setObjectName("ToolbarAction")
        self._del_btn.setMinimumHeight(30)
        self._del_btn.clicked.connect(self._on_remove)
        edit_btns.addWidget(self._add_btn)
        edit_btns.addWidget(self._del_btn)
        edit_btns.addStretch(1)
        v.addLayout(edit_btns)

        sep2 = QFrame(self)
        sep2.setObjectName("DialogSeparator")
        sep2.setFixedHeight(1)
        v.addWidget(sep2)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.setObjectName("ToolbarAction")
        cancel.setMinimumHeight(30)
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save", self)
        save.setObjectName("ToolbarAction")
        save.setMinimumHeight(30)
        save.setMinimumWidth(110)
        save.setDefault(True)
        save.clicked.connect(self.accept)
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        v.addLayout(btn_row)

        self._refresh_list()

    # --- small builders ---------------------------------------------------
    def _row(self, tag: str, widget) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lab = QLabel(tag, self)
        lab.setObjectName("DialogFieldLabel")
        lab.setMinimumWidth(90)
        h.addWidget(lab)
        h.addWidget(widget, 1)
        return h

    def _pair_row(self, tag, cam_combo, mid, edge_combo) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lab = QLabel(tag, self)
        lab.setObjectName("DialogFieldLabel")
        lab.setMinimumWidth(90)
        midl = QLabel(mid, self)
        midl.setObjectName("DialogSubtitle")
        h.addWidget(lab)
        h.addWidget(cam_combo, 1)
        h.addWidget(midl)
        h.addWidget(edge_combo, 1)
        return h

    def _camera_combo(self) -> QComboBox:
        c = QComboBox(self)
        for cam in self._cameras:
            name = self._labels.get(cam.index) or cam.label
            c.addItem(f"{cam.index}: {name}", cam.index)
        return c

    def _edge_combo(self) -> QComboBox:
        c = QComboBox(self)
        for text, data in _EDGE_LABELS:
            c.addItem(text, data)
        return c

    # --- list <-> form ----------------------------------------------------
    def _link_text(self, lk: Link) -> str:
        return (f"{lk.name}   ·   cam{lk.cam_a} [{lk.edge_a}] ↔ "
                f"cam{lk.cam_b} [{lk.edge_b}]   ·   ~{lk.transit_s:g}s")

    def _refresh_list(self) -> None:
        self._list.clear()
        for lk in self._links:
            QListWidgetItem(self._link_text(lk), self._list)

    def _set_combo(self, combo: QComboBox, data) -> None:
        i = combo.findData(data)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _on_select(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._links):
            return
        self._edit_row = row
        lk = self._links[row]
        self._name.setText(lk.name)
        self._set_combo(self._cam_a, lk.cam_a)
        self._set_combo(self._edge_a, lk.edge_a if lk.edge_a in EDGES else "left")
        self._set_combo(self._cam_b, lk.cam_b)
        self._set_combo(self._edge_b, lk.edge_b if lk.edge_b in EDGES else "left")
        self._transit.setValue(lk.transit_s)
        self._label_ab.setText(lk.label_ab)
        self._label_ba.setText(lk.label_ba)
        self._add_btn.setText("Update link")

    def _on_add_update(self) -> None:
        cam_a = self._cam_a.currentData()
        cam_b = self._cam_b.currentData()
        if cam_a is None or cam_b is None or cam_a == cam_b:
            return  # need two different cameras
        lk = Link(
            name=(self._name.text().strip() or f"cam{cam_a}-cam{cam_b}"),
            cam_a=int(cam_a), edge_a=self._edge_a.currentData(),
            cam_b=int(cam_b), edge_b=self._edge_b.currentData(),
            transit_s=float(self._transit.value()),
            label_ab=(self._label_ab.text().strip() or "went out"),
            label_ba=(self._label_ba.text().strip() or "came in"),
        )
        if 0 <= self._edit_row < len(self._links):
            self._links[self._edit_row] = lk
        else:
            self._links.append(lk)
        self._edit_row = -1
        self._add_btn.setText("Add / Update link")
        self._list.clearSelection()
        self._refresh_list()

    def _on_remove(self) -> None:
        row = self._list.currentRow()
        if 0 <= row < len(self._links):
            del self._links[row]
            self._edit_row = -1
            self._refresh_list()

    def values(self) -> list[Link]:
        return list(self._links)
