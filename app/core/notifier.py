"""Telegram push notifications for extracted events.

When the SegmentAnalyzer extracts an event, MainWindow hands the EventClip
here; if a bot token + chat id are configured we POST the peak-frame
thumbnail (with a short caption) to a Telegram chat so the family gets an
off-device alert. Everything is best-effort and fully optional:

- No token/chat configured  -> silent no-op (logged once at startup).
- Network/API failure       -> logged as a warning, never raised; the UI
  thread is never blocked because the HTTP call runs on the global
  QThreadPool, not the GUI thread.
- Debounced per trigger camera so a lingering person can't spam the chat
  (the segment-tier presence merge already collapses most of this, this is
  a second guard).

Uses only the standard library (urllib) so the single-file build gains no
new dependency. The bot token is masked in every log line.
"""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool

from app.core.log import bus

_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT_S = 20


def _mask(token: str) -> str:
    """123456789:AAB...xyz -> 1234...xyz so logs never leak the full token."""
    if not token:
        return "(unset)"
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def _encode_multipart(fields: dict[str, str], file_field: str | None,
                      file_path: Path | None) -> tuple[bytes, str]:
    """Build a multipart/form-data body (urllib has no native helper)."""
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    out = bytearray()
    for name, value in fields.items():
        out += b"--" + boundary.encode() + crlf
        out += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        out += str(value).encode("utf-8") + crlf
    if file_field and file_path is not None:
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        data = file_path.read_bytes()
        out += b"--" + boundary.encode() + crlf
        out += (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"'
        ).encode() + crlf
        out += f"Content-Type: {ctype}".encode() + crlf + crlf
        out += data + crlf
    out += b"--" + boundary.encode() + b"--" + crlf
    return bytes(out), boundary


class _SendTask(QRunnable):
    """One fire-and-forget Telegram API call on the global thread pool."""

    def __init__(self, token: str, chat_id: str, caption: str,
                 thumb: Path | None) -> None:
        super().__init__()
        self._token = token
        self._chat_id = chat_id
        self._caption = caption
        self._thumb = thumb

    def run(self) -> None:
        try:
            if self._thumb is not None and self._thumb.is_file():
                self._send_photo()
            else:
                self._send_message()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
            bus.warn("TG", f"send failed: HTTP {e.code} {body}")
        except Exception as e:  # never propagate into the pool
            bus.warn("TG", f"send failed: {e!s}")

    def _post(self, method: str, body: bytes, content_type: str) -> dict:
        url = _API.format(token=self._token, method=method)
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _send_photo(self) -> None:
        body, boundary = _encode_multipart(
            {"chat_id": self._chat_id, "caption": self._caption},
            "photo", self._thumb,
        )
        ct = f"multipart/form-data; boundary={boundary}"
        result = self._post("sendPhoto", body, ct)
        if result.get("ok"):
            bus.info("TG", "alert sent (photo)")
        else:
            bus.warn("TG", f"sendPhoto rejected: {result.get('description', '?')}")

    def _send_message(self) -> None:
        payload = json.dumps({"chat_id": self._chat_id, "text": self._caption})
        result = self._post("sendMessage", payload.encode("utf-8"), "application/json")
        if result.get("ok"):
            bus.info("TG", "alert sent (text)")
        else:
            bus.warn("TG", f"sendMessage rejected: {result.get('description', '?')}")


class TelegramNotifier(QObject):
    """Dispatches event alerts to Telegram. Construct once; call notify()."""

    def __init__(self, token: str, chat_id: str, min_interval_s: float = 20.0,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._token = (token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._min_interval = max(0.0, min_interval_s)
        self._last_sent: dict[int, float] = {}
        self._pool = QThreadPool.globalInstance()
        self.enabled = bool(self._token and self._chat_id)
        if self.enabled:
            bus.info("TG", f"Telegram alerts enabled (bot {_mask(self._token)}, chat {self._chat_id})")
        else:
            bus.info("TG", "Telegram alerts off (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)")

    def notify(self, clip, cam_label: str | None = None) -> None:
        """Queue an alert for an extracted event. Best-effort, non-blocking."""
        if not self.enabled:
            return
        cam_id = getattr(clip, "cam_id", 0)
        now = time.monotonic()
        last = self._last_sent.get(cam_id, 0.0)
        if now - last < self._min_interval:
            return  # debounce: too soon after the previous alert on this camera
        self._last_sent[cam_id] = now

        caption = self._caption(clip, cam_label)
        thumb = getattr(clip, "thumb_path", None)
        thumb = thumb if isinstance(thumb, Path) else (Path(thumb) if thumb else None)
        self._pool.start(_SendTask(self._token, self._chat_id, caption, thumb))

    @staticmethod
    def _caption(clip, cam_label: str | None) -> str:
        where = cam_label or f"camera {getattr(clip, 'cam_id', '?')}"
        when = getattr(clip, "start_at", None)
        when_s = when.strftime("%H:%M:%S") if when is not None else ""
        parts: list[str] = []
        pp = getattr(clip, "peak_person", 0)
        pv = getattr(clip, "peak_vehicle", 0)
        if pp:
            parts.append(f"{pp} person" + ("s" if pp != 1 else ""))
        if pv:
            parts.append(f"{pv} vehicle" + ("s" if pv != 1 else ""))
        what = ", ".join(parts) if parts else "activity"
        return f"\U0001F6A8 Watchhouse: {what} at {where} · {when_s}"
