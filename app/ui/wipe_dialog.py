"""Wipe-data modal.

Chrome-style: PIN gate, multi-select categories, irreversible. Each
category is a (label, scan(), wipe()) tuple so future stores (SQLite
event DB, faces gallery, snapshot store) plug in by adding one row.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.config import Settings
from app.core.log import bus


@dataclass
class WipeCategory:
    key: str
    label: str
    description: str
    scan: Callable[[], tuple[int, int]]   # returns (item_count, byte_total)
    wipe: Callable[[], tuple[int, int]]   # returns (items_removed, bytes_freed)


# --- Default scanners / wipers ---

def _scan_dir(path: Path, glob: str = "**/*") -> tuple[int, int]:
    if not path.is_dir():
        return 0, 0
    total = 0
    count = 0
    for p in path.glob(glob):
        if p.is_file():
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                continue
    return count, total


def _wipe_dir(path: Path) -> tuple[int, int]:
    count, total = _scan_dir(path)
    if path.is_dir():
        try:
            shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            bus.error("WIPE", f"failed to wipe {path}: {e!s}")
            return 0, 0
    return count, total


def _scan_file(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return 0, 0
    try:
        return 1, path.stat().st_size
    except OSError:
        return 0, 0


def _wipe_file(path: Path) -> tuple[int, int]:
    items, size = _scan_file(path)
    if items:
        try:
            path.unlink()
        except OSError as e:
            bus.error("WIPE", f"failed to delete {path}: {e!s}")
            return 0, 0
    return items, size


def build_default_categories(settings: Settings) -> list[WipeCategory]:
    cats: list[WipeCategory] = []

    rec_dir = settings.recording_dir
    cats.append(WipeCategory(
        key="recordings",
        label="Recorded videos",
        description=f"All MP4 segments under  {rec_dir}",
        scan=lambda: _scan_dir(rec_dir, "**/*.mp4"),
        wipe=lambda: _wipe_dir(rec_dir),
    ))

    cache_path = (settings.env_path.parent / ".cctv-known-dvrs.json") if settings.env_path else None
    if cache_path:
        cats.append(WipeCategory(
            key="ip_cache",
            label="DVR IP cache",
            description=f"Last-known DVR addresses ({cache_path.name})",
            scan=lambda: _scan_file(cache_path),
            wipe=lambda: _wipe_file(cache_path),
        ))

    # Future hookup points (stubs return 0 today, no-op when wiped):
    cats.append(WipeCategory(
        key="event_db",
        label="Event database",
        description="SQLite event log (future v0.5+)",
        scan=lambda: (0, 0),
        wipe=lambda: (0, 0),
    ))
    cats.append(WipeCategory(
        key="faces",
        label="Face gallery + embeddings",
        description="Known-faces gallery and cached embeddings (future v0.6+)",
        scan=lambda: (0, 0),
        wipe=lambda: (0, 0),
    ))
    cats.append(WipeCategory(
        key="snapshots",
        label="Triggered snapshots",
        description="Event-triggered main-stream JPEGs (future v0.5+)",
        scan=lambda: (0, 0),
        wipe=lambda: (0, 0),
    ))

    return cats


# --- Dialog ---

def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        f /= 1024.0
        if f < 1024.0:
            return f"{f:.2f} {u}"
    return f"{f:.2f} PB"


class WipeDialog(QDialog):
    """Chrome-style data wipe dialog. Always confirm with the PIN."""

    def __init__(self, settings: Settings, expected_pin: str = "123", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WipeDialog")
        self.setWindowTitle("Wipe Data")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._expected_pin = expected_pin
        self._categories = build_default_categories(settings)
        self._checks: dict[str, QCheckBox] = {}
        self._summary_labels: dict[str, QLabel] = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(14)

        title = QLabel("WIPE DATA", self)
        title.setObjectName("DialogTitle")
        warn = QLabel(
            "This is permanent. Pick what to remove, then confirm with the PIN.",
            self,
        )
        warn.setObjectName("DialogSubtitle")
        warn.setWordWrap(True)
        v.addWidget(title)
        v.addWidget(warn)

        sep = QFrame(self)
        sep.setObjectName("DialogSeparator")
        sep.setFixedHeight(1)
        v.addWidget(sep)

        for cat in self._categories:
            row = QWidget(self)
            r = QHBoxLayout(row)
            r.setContentsMargins(0, 0, 0, 0)
            r.setSpacing(10)
            cb = QCheckBox(cat.label, row)
            cb.setChecked(False)
            self._checks[cat.key] = cb
            count, total = cat.scan()
            summary = QLabel(self._summary_text(cat, count, total), row)
            summary.setObjectName("WipeRowSummary")
            self._summary_labels[cat.key] = summary
            if count == 0 and "future" in cat.description.lower():
                cb.setEnabled(False)
                summary.setStyleSheet(f"color: {self._color('TEXT_DIM')};")
            r.addWidget(cb, 1)
            r.addWidget(summary)
            v.addWidget(row)
            sub = QLabel(cat.description, self)
            sub.setObjectName("WipeRowSub")
            sub.setWordWrap(True)
            v.addWidget(sub)

        v.addSpacing(6)

        # PIN row
        pin_row = QHBoxLayout()
        pin_row.setSpacing(10)
        pin_label = QLabel("PIN", self)
        pin_label.setObjectName("DialogFieldLabel")
        self._pin_input = QLineEdit(self)
        self._pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pin_input.setPlaceholderText("Required to confirm")
        self._pin_input.setMaximumWidth(140)
        pin_row.addWidget(pin_label)
        pin_row.addWidget(self._pin_input)
        pin_row.addStretch(1)
        v.addLayout(pin_row)

        self._error_label = QLabel("", self)
        self._error_label.setObjectName("DialogError")
        v.addWidget(self._error_label)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.setObjectName("ToolbarAction")
        cancel.setMinimumHeight(30)
        cancel.clicked.connect(self.reject)
        self._wipe_btn = QPushButton("WIPE SELECTED", self)
        self._wipe_btn.setObjectName("DangerAction")
        self._wipe_btn.setMinimumHeight(30)
        self._wipe_btn.setMinimumWidth(150)
        self._wipe_btn.clicked.connect(self._on_wipe)
        btn_row.addWidget(cancel)
        btn_row.addWidget(self._wipe_btn)
        v.addLayout(btn_row)

    @staticmethod
    def _summary_text(cat: WipeCategory, count: int, total: int) -> str:
        if "future" in cat.description.lower():
            return "(not in this version)"
        if count == 0:
            return "empty"
        return f"{count} item(s)  ·  {_human_bytes(total)}"

    @staticmethod
    def _color(name: str) -> str:
        from app.ui import theme
        return getattr(theme, name)

    def _on_wipe(self) -> None:
        pin = self._pin_input.text().strip()
        if pin != self._expected_pin:
            self._error_label.setText("Incorrect PIN.")
            self._pin_input.selectAll()
            self._pin_input.setFocus()
            return

        selected = [c for c in self._categories if self._checks[c.key].isChecked()]
        if not selected:
            self._error_label.setText("Select at least one category.")
            return

        bus.warn("WIPE", f"user requested wipe of: {', '.join(c.key for c in selected)}")
        for cat in selected:
            items, freed = cat.wipe()
            bus.warn(
                "WIPE",
                f"  {cat.label}: removed {items} item(s), freed {_human_bytes(freed)}",
            )
        bus.info("WIPE", "wipe complete")
        self.accept()
