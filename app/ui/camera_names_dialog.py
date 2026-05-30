"""Rename-cameras modal: one free-text display name per camera.

Names persist (.cctv-camera-names.json) and feed both the tile header and
the Telegram alert captions. A blank field reverts that camera to its
built-in location label (shown as the field's placeholder).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core import camera_names


class CameraNamesDialog(QDialog):
    """Edit per-camera names. Call names() after exec() == Accepted."""

    def __init__(self, cameras, current: dict[int, str], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WipeDialog")  # reuse the shared dialog styling
        self.setWindowTitle("Rename Cameras")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._inputs: dict[int, QLineEdit] = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(14)

        title = QLabel("RENAME CAMERAS", self)
        title.setObjectName("DialogTitle")
        sub = QLabel(
            "Give each camera a friendly name for alerts and tiles. "
            "Leave a field blank to use its default.",
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

        for cam in cameras:
            row = QWidget(self)
            r = QHBoxLayout(row)
            r.setContentsMargins(0, 0, 0, 0)
            r.setSpacing(10)
            tag = QLabel(cam.label, row)  # stable "CAM 01" identifier
            tag.setObjectName("DialogFieldLabel")
            tag.setMinimumWidth(64)
            edit = QLineEdit(row)
            edit.setMaxLength(camera_names.MAX_NAME_LEN)
            edit.setPlaceholderText(cam.location)  # default shown as ghost text
            edit.setText(current.get(cam.index, ""))
            self._inputs[cam.index] = edit
            r.addWidget(tag)
            r.addWidget(edit, 1)
            v.addWidget(row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
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

    def names(self) -> dict[int, str]:
        """Cleaned {index: name}; blank fields are omitted."""
        out: dict[int, str] = {}
        for idx, edit in self._inputs.items():
            name = camera_names.clean_name(edit.text())
            if name:
                out[idx] = name
        return out
