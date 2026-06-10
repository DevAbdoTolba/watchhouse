"""Telegram link setup: configure bot token + chat id from the GUI.

Replaces hand-editing .env. The user pastes the @BotFather token, messages
the bot once, and clicks Detect to auto-fill the chat id (via getUpdates);
Send Test confirms the link before saving. Saving writes TELEGRAM_* into the
gitignored .env and reconfigures the live notifier (no restart). The token is
shown masked by default and never logged in full.

Network calls (Detect / Test) run on the global QThreadPool so the modal never
freezes; results return to the GUI thread via a queued signal.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core import notifier


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
            cid, msg = notifier.detect_chat_id(self._token)
            self._signals.done.emit("detect", cid is not None, cid or "", msg)
        else:
            ok, msg = notifier.send_test(self._token, self._chat_id)
            self._signals.done.emit("test", ok, self._chat_id, msg)


class TelegramDialog(QDialog):
    """Edit Telegram credentials. Call values() after exec() == Accepted."""

    def __init__(self, token: str, chat_id: str,
                 commands_enabled: bool = True, lang: str = "en",
                 parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WipeDialog")  # reuse shared dialog styling
        self.setWindowTitle("Telegram Alerts")
        self.setModal(True)
        self.setMinimumWidth(500)
        self._pool = QThreadPool.globalInstance()
        self._signals = _NetSignals()
        self._signals.done.connect(self._on_net_done)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(14)

        title = QLabel("TELEGRAM ALERTS", self)
        title.setObjectName("DialogTitle")
        sub = QLabel(
            "Get event alerts (photo + caption) pushed to Telegram. Create a "
            "bot with @BotFather, paste its token below, send your bot any "
            "message, then click Detect to fill the chat ID.",
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

        # Status line (Detect/Test feedback).
        self._status = QLabel("", self)
        self._status.setObjectName("DialogSubtitle")
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        # Buttons: Test (left) | Cancel, Save (right).
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._test_btn = QPushButton("Send Test", self)
        self._test_btn.setObjectName("ToolbarAction")
        self._test_btn.setMinimumHeight(30)
        self._test_btn.clicked.connect(self._on_test)
        btn_row.addWidget(self._test_btn)
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
