"""Manual clip import dialog.

Pick an MP4 you exported out of BitVision (or anywhere), tell Watchhouse
which camera it belongs to and what wall-clock time it started at, and
the file is copied into recordings/imported/ with the canonical name
pattern the library scanner already understands.

A future v0.4+ will read the timestamp burned into the footage with
computer vision and pre-fill the date/time field automatically; for
now, the human enters it.
"""

from __future__ import annotations

import shutil
from datetime import date as _date, datetime, time as _time
from pathlib import Path

from PySide6.QtCore import QDate, QTime, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.cameras import Camera
from app.core.clip_library import _parse_imported_name
from app.core.log import bus


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    f = float(n)
    for u in ("KB", "MB", "GB", "TB"):
        f /= 1024.0
        if f < 1024.0:
            return f"{f:.2f} {u}"
    return f"{f:.2f} PB"


def _safe_target(imported_dir: Path, cam_id: int, when: datetime) -> Path:
    """Return a non-clashing target path under imported_dir using the
    `cam{N}_YYYY-MM-DD_HH-MM-SS[.mp4]` pattern (the library's `cam_iso`
    matcher). If a file with that name already exists, suffixes
    `_2`, `_3`, ... are tried."""
    stem = f"cam{cam_id:02d}_{when.strftime('%Y-%m-%d_%H-%M-%S')}"
    candidate = imported_dir / f"{stem}.mp4"
    n = 2
    while candidate.exists():
        candidate = imported_dir / f"{stem}_{n}.mp4"
        n += 1
    return candidate


class ImportClipDialog(QDialog):
    """Pick an MP4 + camera + start datetime, copy into imported/.

    On accept, `result_path` and `result_when` are populated for the
    caller (None on cancel). Caller is responsible for triggering a
    library refresh."""

    def __init__(
        self,
        cameras: tuple[Camera, ...],
        imported_dir: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("WipeDialog")  # reuse the same dialog body styling
        self.setWindowTitle("Import Clip")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._cameras = cameras
        self._imported_dir = imported_dir
        self._source: Path | None = None
        self._source_size: int = 0

        self.result_path: Path | None = None
        self.result_when: datetime | None = None

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(14)

        title = QLabel("IMPORT CLIP", self)
        title.setObjectName("DialogTitle")
        sub = QLabel(
            "Pick a video you exported from the DVR, then set which camera and when it started.",
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

        # FILE row
        v.addWidget(self._field_label("FILE"))
        file_row = QHBoxLayout()
        file_row.setSpacing(8)
        self._path_edit = QLineEdit(self)
        self._path_edit.setReadOnly(True)
        self._path_edit.setPlaceholderText("No file selected")
        browse = QPushButton("Browse…", self)
        browse.setObjectName("ToolbarAction")
        browse.setMinimumHeight(30)
        browse.setMinimumWidth(110)
        browse.clicked.connect(self._on_browse)
        file_row.addWidget(self._path_edit, 1)
        file_row.addWidget(browse)
        v.addLayout(file_row)
        self._size_label = QLabel("", self)
        self._size_label.setObjectName("WipeRowSummary")
        v.addWidget(self._size_label)

        v.addSpacing(2)

        # CAMERA row
        v.addWidget(self._field_label("CAMERA"))
        self._cam_combo = QComboBox(self)
        for cam in cameras:
            self._cam_combo.addItem(f"{cam.label}    {cam.location}", userData=cam.index)
        self._cam_combo.setMinimumHeight(30)
        v.addWidget(self._cam_combo)

        v.addSpacing(2)

        # DATE / TIME row
        dt_row = QHBoxLayout()
        dt_row.setSpacing(16)
        date_col = QVBoxLayout()
        date_col.setSpacing(4)
        date_col.addWidget(self._field_label("DATE"))
        self._date_edit = QDateEdit(self)
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDisplayFormat("yyyy-MM-dd")
        self._date_edit.setDate(QDate.currentDate())
        self._date_edit.setMinimumHeight(30)
        date_col.addWidget(self._date_edit)
        time_col = QVBoxLayout()
        time_col.setSpacing(4)
        time_col.addWidget(self._field_label("TIME (24h)"))
        self._time_edit = QTimeEdit(self)
        self._time_edit.setDisplayFormat("HH:mm:ss")
        self._time_edit.setTime(QTime(12, 0, 0))
        self._time_edit.setMinimumHeight(30)
        time_col.addWidget(self._time_edit)
        dt_row.addLayout(date_col, 1)
        dt_row.addLayout(time_col, 1)
        v.addLayout(dt_row)

        v.addSpacing(4)

        # Preview of saved-as filename
        v.addWidget(self._field_label("WILL BE SAVED AS"))
        self._target_label = QLabel("(pick a file first)", self)
        self._target_label.setObjectName("WipeRowSummary")
        self._target_label.setWordWrap(True)
        v.addWidget(self._target_label)

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
        self._import_btn = QPushButton("IMPORT", self)
        self._import_btn.setObjectName("PrimaryAction")
        self._import_btn.setMinimumHeight(30)
        self._import_btn.setMinimumWidth(140)
        self._import_btn.setEnabled(False)
        self._import_btn.clicked.connect(self._on_import)
        btn_row.addWidget(cancel)
        btn_row.addWidget(self._import_btn)
        v.addLayout(btn_row)

        # Live preview as user changes fields
        self._cam_combo.currentIndexChanged.connect(self._refresh_target_preview)
        self._date_edit.dateChanged.connect(self._refresh_target_preview)
        self._time_edit.timeChanged.connect(self._refresh_target_preview)

    @staticmethod
    def _field_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("DialogFieldLabel")
        return lbl

    # --- Slots ---

    def _on_browse(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Pick a video to import",
            "",
            "Video files (*.mp4 *.mov *.mkv *.avi);;All files (*.*)",
        )
        if not path_str:
            return
        self._source = Path(path_str)
        try:
            self._source_size = self._source.stat().st_size
        except OSError:
            self._source_size = 0

        self._path_edit.setText(str(self._source))
        self._size_label.setText(f"size  ·  {_human_bytes(self._source_size)}" if self._source_size else "")
        self._error_label.setText("")

        # Try to recover camera + datetime from the filename
        parsed = _parse_imported_name(self._source)
        if parsed is not None:
            cam_id, when = parsed
            for i in range(self._cam_combo.count()):
                if self._cam_combo.itemData(i) == cam_id:
                    self._cam_combo.setCurrentIndex(i)
                    break
            self._date_edit.setDate(QDate(when.year, when.month, when.day))
            self._time_edit.setTime(QTime(when.hour, when.minute, when.second))

        self._import_btn.setEnabled(True)
        self._refresh_target_preview()

    def _refresh_target_preview(self) -> None:
        if self._source is None:
            return
        cam_id = self._cam_combo.currentData()
        d = self._date_edit.date()
        t = self._time_edit.time()
        when = datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second())
        target = _safe_target(self._imported_dir, cam_id, when)
        self._target_label.setText(str(target))

    def _on_import(self) -> None:
        if self._source is None or not self._source.is_file():
            self._error_label.setText("Pick a file first.")
            return
        cam_id = int(self._cam_combo.currentData())
        d = self._date_edit.date()
        t = self._time_edit.time()
        when = datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second())

        try:
            self._imported_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._error_label.setText(f"Cannot create import folder: {e!s}")
            return

        target = _safe_target(self._imported_dir, cam_id, when)
        bus.info("IMPORT", f"copying {self._source.name} -> {target.name}")
        try:
            shutil.copy2(self._source, target)
        except OSError as e:
            self._error_label.setText(f"Copy failed: {e!s}")
            bus.error("IMPORT", f"copy failed: {e!s}")
            return

        bus.info(
            "IMPORT",
            f"cam{cam_id:02d}  ·  {when:%Y-%m-%d %H:%M:%S}  ·  {_human_bytes(self._source_size)}",
        )
        self.result_path = target
        self.result_when = when
        self.accept()
