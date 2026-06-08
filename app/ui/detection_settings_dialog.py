"""Detection-confidence modal: a per-camera person-confidence floor.

A floor is the minimum confidence a 'person' must reach on that camera. 0 means
uncapped — keep every person the model reports. Raise it only for a noisy view
(e.g. a door camera that mistakes rubbish bags for people) so just real,
confident people pass. Values persist in .cctv-detection.json and apply live.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core import detection_prefs


class DetectionSettingsDialog(QDialog):
    """Edit per-camera person-confidence floors. Call values() after Accepted."""

    def __init__(self, cameras, current: dict[int, float],
                 cam_labels: dict[int, str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WipeDialog")  # reuse the shared dialog styling
        self.setWindowTitle("Detection Confidence")
        self.setModal(True)
        self.setMinimumWidth(480)
        self._spins: dict[int, QDoubleSpinBox] = {}
        labels = dict(cam_labels or {})

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(14)

        title = QLabel("DETECTION CONFIDENCE", self)
        title.setObjectName("DialogTitle")
        sub = QLabel(
            "Minimum confidence a person must reach on each camera. "
            "0.00 = uncapped (keep every person). Raise it for a noisy view "
            "so only confident, real people get through.",
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
            name = labels.get(cam.index) or getattr(cam, "location", "") or cam.label
            tag = QLabel(f"{cam.label} · {name}", row)
            tag.setObjectName("DialogFieldLabel")
            tag.setMinimumWidth(180)
            spin = QDoubleSpinBox(row)
            spin.setRange(0.0, detection_prefs.MAX_FLOOR)
            spin.setSingleStep(0.01)
            spin.setDecimals(2)
            spin.setValue(float(current.get(cam.index, 0.0)))
            spin.setSpecialValueText("uncapped")  # shown when value == 0.00
            spin.setMinimumWidth(110)
            self._spins[cam.index] = spin
            r.addWidget(tag)
            r.addStretch(1)
            r.addWidget(spin)
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

    def values(self) -> dict[int, float]:
        """{cam_index: floor} for every camera (0.0 = uncapped)."""
        return {idx: round(spin.value(), 2) for idx, spin in self._spins.items()}
