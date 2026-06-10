"""Unified Settings window: every preference in one tabbed dialog.

Replaces the four scattered ⚙-menu modals (Rename Cameras, Detection
Confidence, Telegram Alerts, Camera Links) plus the watchdog toggle and the
Wipe Data entry point. Each tab is a self-contained page widget holding the
same form logic the standalone dialogs had; MainWindow reads the page
getters after exec() == Accepted and applies/persists everything at once.

Telegram's Detect / Test network calls run on the global QThreadPool so the
modal never freezes; results return to the GUI thread via a queued signal.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core import camera_names
from app.core import detection_prefs
from app.core import telegram_api
from app.core.camera_links import EDGES, Link
from app.core.config import Settings
from app.ui.wipe_dialog import WipeDialog

_EDGE_LABELS = [("Left", "left"), ("Right", "right"),
                ("Top", "top"), ("Bottom", "bottom")]

_TAB_CSS = """
QTabWidget::pane { border: 1px solid #2a3040; border-top: none; }
QTabBar::tab {
    background: #161a22; color: #8a91a0; border: 1px solid #2a3040;
    border-bottom: none; padding: 7px 18px; margin-right: 2px;
}
QTabBar::tab:selected { background: #1f242e; color: #c69561; }
QTabBar::tab:hover:!selected { color: #ebe7e1; }
"""


def _subtitle(text: str, parent: QWidget) -> QLabel:
    lab = QLabel(text, parent)
    lab.setObjectName("DialogSubtitle")
    lab.setWordWrap(True)
    return lab


class _CamerasPage(QWidget):
    """One free-text display name per camera (alerts + tile headers)."""

    def __init__(self, cameras, current: dict[int, str], parent=None) -> None:
        super().__init__(parent)
        self._inputs: dict[int, QLineEdit] = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)
        v.addWidget(_subtitle(
            "Give each camera a friendly name for alerts and tiles. "
            "Leave a field blank to use its default.", self))
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
        v.addStretch(1)

    def names(self) -> dict[int, str]:
        """Cleaned {index: name}; blank fields are omitted."""
        out: dict[int, str] = {}
        for idx, edit in self._inputs.items():
            name = camera_names.clean_name(edit.text())
            if name:
                out[idx] = name
        return out


class _DetectionPage(QWidget):
    """Per-camera person-confidence floor (0 = uncapped)."""

    def __init__(self, cameras, current: dict[int, float],
                 cam_labels: dict[int, str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._spins: dict[int, QDoubleSpinBox] = {}
        labels = dict(cam_labels or {})
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)
        v.addWidget(_subtitle(
            "Minimum confidence a person must reach on each camera. "
            "0.00 = uncapped (keep every person). Raise it for a noisy view "
            "so only confident, real people get through.", self))
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
        v.addStretch(1)

    def values(self) -> dict[int, float]:
        """{cam_index: floor} for every camera (0.0 = uncapped)."""
        return {idx: round(spin.value(), 2) for idx, spin in self._spins.items()}


class _NetSignals(QObject):
    done = Signal(str, bool, str, str)  # kind, ok, chat_id, message


class _NetTask(QRunnable):
    """Run a blocking Bot API helper off the UI thread."""

    def __init__(self, kind: str, token: str, chat_id: str,
                 signals: _NetSignals) -> None:
        super().__init__()
        self._kind = kind
        self._token = token
        self._chat_id = chat_id
        self._signals = signals

    def run(self) -> None:
        if self._kind == "detect":
            cid, msg = telegram_api.detect_chat_id(self._token)
            self._signals.done.emit("detect", cid is not None, cid or "", msg)
        else:
            ok, msg = telegram_api.send_test(self._token, self._chat_id)
            self._signals.done.emit("test", ok, self._chat_id, msg)


class _TelegramPage(QWidget):
    """Bot token + chat id (Detect / Test), reply commands, language."""

    def __init__(self, token: str, chat_id: str, commands_enabled: bool,
                 lang: str, parent=None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._signals = _NetSignals()
        self._signals.done.connect(self._on_net_done)

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)
        v.addWidget(_subtitle(
            "Get event alerts (photo + caption) pushed to Telegram. Create a "
            "bot with @BotFather, paste its token below, send your bot any "
            "message, then click Detect to fill the chat ID.", self))

        # Bot token row (password-masked, with a Show toggle).
        tok_row = QWidget(self)
        tr = QHBoxLayout(tok_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(10)
        tok_tag = QLabel("Bot token", tok_row)
        tok_tag.setObjectName("DialogFieldLabel")
        tok_tag.setMinimumWidth(78)
        self._token_edit = QLineEdit(tok_row)
        self._token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_edit.setPlaceholderText("123456789:AA…")
        self._token_edit.setText(token or "")
        self._show_chk = QCheckBox("Show", tok_row)
        self._show_chk.toggled.connect(self._toggle_token_echo)
        tr.addWidget(tok_tag)
        tr.addWidget(self._token_edit, 1)
        tr.addWidget(self._show_chk)
        v.addWidget(tok_row)

        # Chat id row (with Detect button).
        chat_row = QWidget(self)
        cr = QHBoxLayout(chat_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(10)
        chat_tag = QLabel("Chat ID", chat_row)
        chat_tag.setObjectName("DialogFieldLabel")
        chat_tag.setMinimumWidth(78)
        self._chat_edit = QLineEdit(chat_row)
        self._chat_edit.setPlaceholderText("auto-filled by Detect, or paste it")
        self._chat_edit.setText(chat_id or "")
        self._detect_btn = QPushButton("Detect", chat_row)
        self._detect_btn.setObjectName("ToolbarAction")
        self._detect_btn.setMinimumHeight(30)
        self._detect_btn.clicked.connect(self._on_detect)
        cr.addWidget(chat_tag)
        cr.addWidget(self._chat_edit, 1)
        cr.addWidget(self._detect_btn)
        v.addWidget(chat_row)

        # Interactive reply commands toggle.
        self._commands_chk = QCheckBox(
            "Enable reply commands  (reply to an alert photo → that camera's "
            "clip; reply to a clip → the other angles; /help, /last)", self)
        self._commands_chk.setChecked(commands_enabled)
        v.addWidget(self._commands_chk)

        # Message language for what the bot sends to the family.
        lang_row = QWidget(self)
        lr = QHBoxLayout(lang_row)
        lr.setContentsMargins(0, 0, 0, 0)
        lr.setSpacing(10)
        lang_tag = QLabel("Language", lang_row)
        lang_tag.setObjectName("DialogFieldLabel")
        lang_tag.setMinimumWidth(78)
        self._lang_combo = QComboBox(lang_row)
        self._lang_combo.addItem("English", "en")
        self._lang_combo.addItem("العربية", "ar")
        self._lang_combo.setCurrentIndex(
            1 if str(lang).strip().lower().startswith("ar") else 0)
        lr.addWidget(lang_tag)
        lr.addWidget(self._lang_combo, 1)
        v.addWidget(lang_row)

        # Status line (Detect/Test feedback) + Send Test.
        self._status = _subtitle("", self)
        v.addWidget(self._status)
        test_row = QHBoxLayout()
        self._test_btn = QPushButton("Send Test", self)
        self._test_btn.setObjectName("ToolbarAction")
        self._test_btn.setMinimumHeight(30)
        self._test_btn.clicked.connect(self._on_test)
        test_row.addWidget(self._test_btn)
        test_row.addStretch(1)
        v.addLayout(test_row)
        v.addStretch(1)

    def _toggle_token_echo(self, show: bool) -> None:
        self._token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        )

    def _set_busy(self, busy: bool, note: str = "") -> None:
        self._detect_btn.setEnabled(not busy)
        self._test_btn.setEnabled(not busy)
        if note:
            self._status.setText(note)

    def _on_detect(self) -> None:
        token = self._token_edit.text().strip()
        if not token:
            self._status.setText("Enter the bot token first.")
            return
        self._set_busy(True, "Checking Telegram for your message…")
        self._pool.start(_NetTask("detect", token, "", self._signals))

    def _on_test(self) -> None:
        token = self._token_edit.text().strip()
        chat = self._chat_edit.text().strip()
        if not token or not chat:
            self._status.setText("Need both a bot token and a chat ID to test.")
            return
        self._set_busy(True, "Sending a test message…")
        self._pool.start(_NetTask("test", token, chat, self._signals))

    def _on_net_done(self, kind: str, ok: bool, chat_id: str, message: str) -> None:
        if kind == "detect" and ok and chat_id:
            self._chat_edit.setText(chat_id)
        self._set_busy(False, message)

    def values(self) -> tuple[str, str, bool, str]:
        """(token, chat_id, commands_enabled, lang); token/chat trimmed."""
        return (self._token_edit.text().strip(),
                self._chat_edit.text().strip(),
                self._commands_chk.isChecked(),
                self._lang_combo.currentData() or "en")


class _LinksPage(QWidget):
    """Directional camera links: one crossing seen by two cameras = ONE event."""

    def __init__(self, cameras, links, cam_labels=None, parent=None) -> None:
        super().__init__(parent)
        self._cameras = list(cameras)
        self._labels = dict(cam_labels or {})
        self._links: list[Link] = list(links or [])
        self._edit_row = -1  # index being edited, or -1 for "add new"

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)
        v.addWidget(_subtitle(
            "Tie two cameras that share a crossing (e.g. a doorway). The same "
            "movement seen on both becomes ONE named event instead of two "
            "camera alerts. We match the motion (time + which frame edge + "
            "direction), not the person.", self))

        self._list = QListWidget(self)
        self._list.setMinimumHeight(96)
        self._list.itemSelectionChanged.connect(self._on_select)
        v.addWidget(self._list)

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

        v.addWidget(_subtitle(
            "Edge = the frame side the person crosses through: where they leave "
            "Camera A and where they appear on Camera B.", self))

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
        v.addStretch(1)

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


class _SystemPage(QWidget):
    """Watchdog toggle + the (PIN-gated) Wipe Data entry point."""

    def __init__(self, settings: Settings, watchdog_on: bool, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)

        self._wd_chk = QCheckBox("Auto-restart watchdog", self)
        self._wd_chk.setChecked(watchdog_on)
        v.addWidget(self._wd_chk)
        v.addWidget(_subtitle(
            "Relaunch Watchhouse automatically if it dies or hangs, and ping "
            "Telegram. Turn off to manage it yourself.", self))

        sep = QFrame(self)
        sep.setObjectName("DialogSeparator")
        sep.setFixedHeight(1)
        v.addWidget(sep)

        wipe_row = QHBoxLayout()
        wipe_btn = QPushButton("Wipe Data…", self)
        wipe_btn.setObjectName("ToolbarAction")
        wipe_btn.setMinimumHeight(30)
        wipe_btn.clicked.connect(self._open_wipe)
        wipe_row.addWidget(wipe_btn)
        wipe_row.addStretch(1)
        v.addLayout(wipe_row)
        v.addWidget(_subtitle(
            "Delete recordings, caches and stored data. PIN-gated "
            "(set WIPE_PIN in .env).", self))
        v.addStretch(1)

    def _open_wipe(self) -> None:
        dlg = WipeDialog(self._settings, expected_pin=self._settings.wipe_pin,
                         parent=self)
        dlg.exec()

    def watchdog_enabled(self) -> bool:
        return self._wd_chk.isChecked()


class SettingsDialog(QDialog):
    """All settings in one place. Read the page getters after Accepted."""

    def __init__(self, cameras, settings: Settings, *,
                 names: dict[int, str], floors: dict[int, float],
                 links, cam_labels: dict[int, str],
                 watchdog_on: bool, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WipeDialog")  # reuse the shared dialog styling
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(620)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(14)

        title = QLabel("SETTINGS", self)
        title.setObjectName("DialogTitle")
        v.addWidget(title)

        tabs = QTabWidget(self)
        tabs.setStyleSheet(_TAB_CSS)
        self.cameras_page = _CamerasPage(cameras, names, self)
        self.detection_page = _DetectionPage(cameras, floors, cam_labels, self)
        self.telegram_page = _TelegramPage(
            settings.telegram_bot_token, settings.telegram_chat_id,
            settings.telegram_commands, settings.telegram_lang, self)
        self.links_page = _LinksPage(cameras, links, cam_labels, self)
        self.system_page = _SystemPage(settings, watchdog_on, self)
        tabs.addTab(self.cameras_page, "Cameras")
        tabs.addTab(self.detection_page, "Detection")
        tabs.addTab(self.telegram_page, "Telegram")
        tabs.addTab(self.links_page, "Links")
        tabs.addTab(self.system_page, "System")
        v.addWidget(tabs, 1)

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
