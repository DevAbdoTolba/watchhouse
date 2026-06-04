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
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool

from app.core.log import bus
from app.core.telegram_map import TelegramMap
from app.core import telegram_text as tg

_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT_S = 20
_UPLOAD_TIMEOUT_S = 120          # clip uploads are larger / slower than a photo
_MAX_UPLOAD_BYTES = 49 * 1024 * 1024  # Telegram bot send cap is 50 MB; stay under
_POLL_TIMEOUT_S = 10             # getUpdates long-poll; bounds shutdown latency


def _call(token: str, method: str, payload: dict) -> dict:
    """Blocking JSON Bot API call. Used by the setup dialog's Detect / Test
    buttons (short, user-initiated) - NOT on the event hot path."""
    url = _API.format(token=token.strip(), method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def detect_chat_id(token: str) -> tuple[str | None, str]:
    """Resolve a chat id from the bot's recent updates. The user messages the
    bot once, then we read getUpdates. Returns (chat_id, human_message)."""
    try:
        result = _call(token, "getUpdates", {"timeout": 0})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, "Invalid bot token (Telegram returned 401)."
        return None, f"Telegram error: HTTP {e.code}"
    except Exception as e:
        return None, f"Could not reach Telegram: {e!s}"
    if not result.get("ok"):
        return None, f"Telegram rejected the request: {result.get('description', '?')}"
    updates = result.get("result", [])
    for upd in reversed(updates):  # newest first
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat", {})
        cid = chat.get("id")
        if cid is not None:
            who = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            return str(cid), f"Found chat {cid}{f' ({who})' if who else ''}."
    return None, ("No recent messages. Open Telegram, send your bot any message "
                  "(or add it to your group and send one), then click Detect again.")


def send_test(token: str, chat_id: str) -> tuple[bool, str]:
    """Send a one-off confirmation message. Returns (ok, human_message)."""
    try:
        result = _call(token, "sendMessage",
                       {"chat_id": chat_id.strip(),
                        "text": "✅ Watchhouse is linked. Alerts will arrive here."})
    except urllib.error.HTTPError as e:
        return False, f"Telegram error: HTTP {e.code}"
    except Exception as e:
        return False, f"Could not reach Telegram: {e!s}"
    if result.get("ok"):
        return True, "Test message sent — check your Telegram."
    return False, f"Telegram rejected it: {result.get('description', '?')}"


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


def _post_multipart(token: str, method: str, fields: dict[str, str],
                    file_field: str, file_path: Path) -> dict:
    body, boundary = _encode_multipart(fields, file_field, file_path)
    url = _API.format(token=token.strip(), method=method)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=_UPLOAD_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def api_send_message(token: str, chat_id: str, text: str) -> dict:
    return _call(token, "sendMessage", {"chat_id": chat_id.strip(), "text": text})


def alert_markup(lang: str, with_capture: bool = True) -> str:
    """Inline keyboard JSON for an alert: a 🎥 Capture button (the detecting
    camera's clip) and a 🎬 All cameras button (every angle). On a clip message
    Capture is dropped (the clip is already here), leaving just All cameras.
    callback_data stays tiny ('cap'/'all'); the event is resolved from the
    message the button is attached to, same as a reply."""
    row = []
    if with_capture:
        row.append({"text": tg.t(lang, "btn_capture"), "callback_data": "cap"})
    row.append({"text": tg.t(lang, "btn_all"), "callback_data": "all"})
    return json.dumps({"inline_keyboard": [row]})


def api_send_photo(token: str, chat_id: str, caption: str, path: Path,
                   reply_markup: str | None = None) -> dict:
    fields = {"chat_id": chat_id.strip(), "caption": caption}
    if reply_markup:
        fields["reply_markup"] = reply_markup
    return _post_multipart(token, "sendPhoto", fields, "photo", path)


def api_send_video(token: str, chat_id: str, caption: str, path: Path,
                   reply_markup: str | None = None) -> dict:
    """Upload an mp4 clip. Falls back to sendDocument when Telegram refuses the
    video (it won't transcode HEVC), so the clip is always at least downloadable.
    Guards the 49 MB bot upload cap. Returns the parsed API result dict."""
    try:
        size = path.stat().st_size
    except OSError:
        return {"ok": False, "description": "clip file missing"}
    if size > _MAX_UPLOAD_BYTES:
        mb = size // (1024 * 1024)
        return {"ok": False, "description": f"clip too large ({mb} MB > 49 MB)"}
    video_fields = {"chat_id": chat_id.strip(), "caption": caption,
                    "supports_streaming": "true"}
    doc_fields = {"chat_id": chat_id.strip(), "caption": caption}
    if reply_markup:
        video_fields["reply_markup"] = reply_markup
        doc_fields["reply_markup"] = reply_markup
    try:
        r = _post_multipart(token, "sendVideo", video_fields, "video", path)
        if r.get("ok"):
            return r
    except urllib.error.HTTPError:
        pass  # fall through to document delivery
    return _post_multipart(token, "sendDocument", doc_fields, "document", path)


def api_answer_callback(token: str, callback_query_id: str, text: str = "") -> dict:
    """Acknowledge a button tap so Telegram stops the client's loading spinner.
    `text` shows as a brief toast. Best-effort; never raises into the caller."""
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return _call(token, "answerCallbackQuery", payload)
    except Exception:
        return {"ok": False}


def api_edit_message(token: str, chat_id: str, message_id: int, text: str) -> dict:
    return _call(token, "editMessageText",
                 {"chat_id": chat_id.strip(), "message_id": message_id, "text": text})


def api_delete_message(token: str, chat_id: str, message_id: int) -> dict:
    try:
        return _call(token, "deleteMessage",
                     {"chat_id": chat_id.strip(), "message_id": message_id})
    except Exception:
        return {"ok": False}


def _message_id(result: dict) -> int | None:
    try:
        return int(result.get("result", {}).get("message_id"))
    except (TypeError, ValueError):
        return None


class _SendTask(QRunnable):
    """One fire-and-forget alert send on the global thread pool. When a map +
    event folder are supplied, the sent photo's message_id is recorded so a
    later reply can drill into the clip(s)."""

    def __init__(self, token: str, chat_id: str, caption: str,
                 thumb: Path | None, tmap=None, folder: str = "",
                 cam: int = 0, kind: str = "thumb", t: float = 0.0,
                 markup: str | None = None) -> None:
        super().__init__()
        self._token = token
        self._chat_id = chat_id
        self._caption = caption
        self._thumb = thumb
        self._map = tmap
        self._folder = folder
        self._cam = cam
        self._kind = kind
        self._t = t
        self._markup = markup

    def run(self) -> None:
        try:
            if self._thumb is not None and self._thumb.is_file():
                result = api_send_photo(self._token, self._chat_id,
                                        self._caption, self._thumb,
                                        reply_markup=self._markup)
                if result.get("ok"):
                    bus.info("TG", "alert sent (photo)")
                    # Record so a reply can drill in. Segment thumbs carry a
                    # folder; live alerts carry only cam + timestamp (kind
                    # "live") and resolve to an event clip at reply time.
                    if self._map is not None and (self._folder or self._kind == "live"):
                        self._map.record(_message_id(result), self._folder,
                                         self._cam, self._kind, self._t)
                else:
                    bus.warn("TG", f"sendPhoto rejected: {result.get('description', '?')}")
            else:
                result = api_send_message(self._token, self._chat_id, self._caption)
                if result.get("ok"):
                    bus.info("TG", "alert sent (text)")
                else:
                    bus.warn("TG", f"sendMessage rejected: {result.get('description', '?')}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
            bus.warn("TG", f"send failed: HTTP {e.code} {body}")
        except Exception as e:  # never propagate into the pool
            bus.warn("TG", f"send failed: {e!s}")


class _QuickClipTask(QRunnable):
    """Upload the just-encoded quick clip (the detecting camera's mp4) and
    record it so a reply can fetch the other angles. Off the UI thread."""

    def __init__(self, token: str, chat_id: str, cam_id: int, cam_label: str,
                 folder: str, tmap, lang: str = "en") -> None:
        super().__init__()
        self._token = token
        self._chat_id = chat_id
        self._cam_id = cam_id
        self._label = cam_label
        self._folder = folder
        self._map = tmap
        self._lang = lang

    def run(self) -> None:
        try:
            path = Path(self._folder) / f"cam{self._cam_id}.mp4"
            if not path.is_file():
                return
            caption = tg.t(self._lang, "clip", where=self._label)
            markup = alert_markup(self._lang, with_capture=False) if self._map else None
            result = api_send_video(self._token, self._chat_id, caption, path,
                                    reply_markup=markup)
            if result.get("ok"):
                bus.info("TG", f"quick clip sent (cam{self._cam_id})")
                if self._map is not None:
                    self._map.record(_message_id(result), self._folder,
                                     self._cam_id, "video")
            else:
                bus.warn("TG", f"quick clip rejected: {result.get('description', '?')}")
        except urllib.error.HTTPError as e:
            bus.warn("TG", f"quick clip send failed: HTTP {e.code}")
        except Exception as e:
            bus.warn("TG", f"quick clip send failed: {e!s}")


class TelegramNotifier(QObject):
    """Dispatches event alerts to Telegram. Construct once; call notify()."""

    def __init__(self, token: str, chat_id: str, min_interval_s: float = 20.0,
                 notify_ongoing: bool = True, commands_enabled: bool = False,
                 state_dir=None, events_dir=None, recording_dir=None,
                 cam_ids=None, pre_roll_s: float = 10.0, post_roll_s: float = 20.0,
                 map_cap: int = 5000, live_clips_dir=None, lang: str = "en",
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lang = tg.norm(lang)
        self._token = (token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._min_interval = max(0.0, min_interval_s)
        self._notify_ongoing = notify_ongoing
        self._commands = commands_enabled
        self._events_dir = events_dir
        self._live_clips_dir = live_clips_dir
        self._recording_dir = recording_dir
        self._cam_ids = list(cam_ids) if cam_ids else []
        self._pre_roll_s = pre_roll_s
        self._post_roll_s = post_roll_s
        self._cam_labels: dict[int, str] = {}
        self._last_sent: dict[int, float] = {}
        self._pool = QThreadPool.globalInstance()
        self._map = TelegramMap(state_dir, cap=map_cap)
        self._poller: "TelegramPoller | None" = None
        self.enabled = bool(self._token and self._chat_id)
        if self.enabled:
            bus.info("TG", f"Telegram alerts enabled (bot {_mask(self._token)}, chat {self._chat_id})")
        else:
            bus.info("TG", "Telegram alerts off (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)")
        self._sync_poller()

    def set_cam_labels(self, labels: dict[int, str]) -> None:
        """Friendly camera names for clip captions; refreshed on rename."""
        self._cam_labels = dict(labels)
        if self._poller is not None:
            self._poller.set_labels(self._cam_labels)

    def configure(self, token: str, chat_id: str,
                  min_interval_s: float | None = None,
                  notify_ongoing: bool | None = None,
                  commands_enabled: bool | None = None,
                  lang: str | None = None) -> None:
        """Apply new credentials/settings to the running notifier without a
        restart (called by the Telegram setup dialog after it saves to .env)."""
        if lang is not None:
            self._lang = tg.norm(lang)
        self._token = (token or "").strip()
        self._chat_id = (chat_id or "").strip()
        if min_interval_s is not None:
            self._min_interval = max(0.0, min_interval_s)
        if notify_ongoing is not None:
            self._notify_ongoing = notify_ongoing
        if commands_enabled is not None:
            self._commands = commands_enabled
        self._last_sent.clear()  # fresh creds -> don't carry old debounce state
        self.enabled = bool(self._token and self._chat_id)
        if self.enabled:
            bus.info("TG", f"Telegram reconfigured (bot {_mask(self._token)}, chat {self._chat_id})")
        else:
            bus.info("TG", "Telegram alerts disabled (token/chat cleared)")
        self._sync_poller()

    def _sync_poller(self) -> None:
        """Start/stop the reply listener to match the current enabled/commands
        state. Always tears down the old one first so creds never go stale."""
        self._stop_poller()
        if self.enabled and self._commands:
            self._poller = TelegramPoller(
                self._token, self._chat_id, self._map,
                dict(self._cam_labels), self._events_dir,
                recording_dir=self._recording_dir, cam_ids=self._cam_ids,
                pre_roll_s=self._pre_roll_s, post_roll_s=self._post_roll_s,
                live_clips_dir=self._live_clips_dir, lang=self._lang,
            )
            self._poller.start()
            bus.info("TG", "reply commands listening (replies + /help, /last)")

    def _stop_poller(self) -> None:
        if self._poller is not None:
            self._poller.stop()
            self._poller.wait(12_000)
            self._poller = None

    def shutdown(self) -> None:
        """Stop the reply listener; call from the window's closeEvent."""
        self._stop_poller()

    def notify(self, clip, cam_label: str | None = None) -> None:
        """Queue an alert for an extracted event. Best-effort, non-blocking.

        Debounce policy: an arrival ("started"/"single") and a departure
        ("ended") must never be dropped, so they bypass the per-camera
        debounce. Only the repeating "still present" pings ("ongoing") respect
        self._min_interval - and they can be silenced entirely via config."""
        if not self.enabled:
            return
        state = getattr(clip, "presence_state", "single")
        if state == "ongoing" and not self._notify_ongoing:
            return  # user opted out of the recurring still-present pings
        cam_id = getattr(clip, "cam_id", 0)
        now = time.monotonic()
        if state == "ongoing":
            last = self._last_sent.get(cam_id, 0.0)
            if now - last < self._min_interval:
                return  # debounce: too soon after the previous ongoing ping
        # started / single / ended always send; record the time so a following
        # ongoing ping is still debounced relative to the most recent send.
        self._last_sent[cam_id] = now

        caption = self._caption(clip, cam_label)
        thumb = getattr(clip, "thumb_path", None)
        thumb = thumb if isinstance(thumb, Path) else (Path(thumb) if thumb else None)
        folder = getattr(clip, "folder", None)
        folder_s = str(folder) if folder is not None else ""
        # Pass the map only when commands are on, so replies can drill into the
        # clip; otherwise this is a plain fire-and-forget push.
        tmap = self._map if self._commands else None
        markup = alert_markup(self._lang) if tmap is not None else None
        self._pool.start(_SendTask(self._token, self._chat_id, caption, thumb,
                                   tmap=tmap, folder=folder_s, cam=cam_id,
                                   markup=markup))

    def notify_live(self, cam_id: int, cam_label: str, title: str,
                    thumb_path: str) -> None:
        """Instant real-time alert (the live tier): photo + title, sent now.

        The evidence clip doesn't exist yet (segment tier is minutes behind), so
        we record this photo in the map keyed by camera + timestamp (kind
        'live'). When the user replies, _dispatch_reply resolves it to the
        matching event clip if it has since been extracted, or tells them it's
        still being prepared so they can reply again shortly."""
        if not self.enabled:
            return
        when = time.strftime("%H:%M:%S")
        caption = tg.t(self._lang, "alert_live", where=cam_label, when=when)
        thumb = Path(thumb_path) if thumb_path else None
        tmap = self._map if self._commands else None
        markup = alert_markup(self._lang) if tmap is not None else None
        self._pool.start(_SendTask(self._token, self._chat_id, caption, thumb,
                                   tmap=tmap, cam=cam_id, kind="live",
                                   t=time.time(), markup=markup))

    def send_quick_clip(self, cam_id: int, cam_label: str, folder: str) -> None:
        """Auto-push the instant quick clip (the ~30s pre-event-buffer video)
        the moment it's encoded, so the family gets the video in seconds. The
        message is recorded so a reply can pull other angles later."""
        if not self.enabled:
            return
        self._pool.start(_QuickClipTask(self._token, self._chat_id, cam_id,
                                        cam_label, folder,
                                        self._map if self._commands else None,
                                        lang=self._lang))

    @staticmethod
    def _what(clip) -> str:
        parts: list[str] = []
        pp = getattr(clip, "peak_person", 0)
        pv = getattr(clip, "peak_vehicle", 0)
        if pp:
            parts.append(f"{pp} person" + ("s" if pp != 1 else ""))
        if pv:
            parts.append(f"{pv} vehicle" + ("s" if pv != 1 else ""))
        return ", ".join(parts) if parts else "activity"

    @staticmethod
    def _mins(seconds: float) -> str:
        """Human duration for captions: seconds under a minute, else rounded m."""
        s = max(0.0, float(seconds))
        if s < 60.0:
            return f"{int(round(s))}s"
        return f"{int(round(s / 60.0))}m"

    def _caption(self, clip, cam_label: str | None) -> str:
        where = cam_label or f"camera {getattr(clip, 'cam_id', '?')}"
        when = getattr(clip, "start_at", None)
        when_s = when.strftime("%H:%M:%S") if when is not None else ""
        state = getattr(clip, "presence_state", "single")
        secs = getattr(clip, "presence_seconds", 0.0)
        if state == "ongoing":
            return tg.t(self._lang, "ongoing", where=where,
                        dur=tg.dur(self._lang, secs), when=when_s)
        if state == "ended":
            return tg.t(self._lang, "ended", where=where,
                        dur=tg.dur(self._lang, secs), when=when_s)
        # started / single -> a normal arrival alert.
        who = tg.who(self._lang, getattr(clip, "peak_person", 0),
                     getattr(clip, "peak_vehicle", 0))
        return tg.t(self._lang, "alert", where=where, who=who, when=when_s)


class TelegramPoller(QThread):
    """Long-polls getUpdates and answers replies from the configured chat ONLY.

    Security: every update is dropped unless its chat id matches the linked
    chat, so a stranger who finds the bot cannot pull the home's footage.

    Reply semantics mirror the user's mental model:
      reply to an alert photo  -> send that camera's clip
      reply to a clip          -> send the other camera angles of the event
    Plus /help and /last text commands.
    """

    def __init__(self, token: str, chat_id: str, tmap, cam_labels: dict,
                 events_dir, recording_dir=None, cam_ids=None,
                 pre_roll_s: float = 10.0, post_roll_s: float = 20.0,
                 live_clips_dir=None, lang: str = "en") -> None:
        super().__init__()
        self._lang = tg.norm(lang)
        self._token = token.strip()
        self._chat_id = str(chat_id).strip()
        self._map = tmap
        self._labels = dict(cam_labels or {})
        self._events_dir = events_dir
        self._live_clips_dir = live_clips_dir
        self._recording_dir = Path(recording_dir) if recording_dir else None
        self._cam_ids = list(cam_ids) if cam_ids else []
        self._pre_roll_s = pre_roll_s
        self._post_roll_s = post_roll_s
        self._stop = False
        # Per-event progressive angle delivery: folder -> set of cams already
        # sent. First reply sends the detecting camera(s); the next sends the
        # remaining angles; once all are sent we say "no more angles". In-memory
        # (session) state - fine, since replies happen interactively.
        self._sent_cams: dict[str, set[int]] = {}
        # Pending live-reply requests: the user replied to a live alert whose
        # clip wasn't encoded yet. We hold (cam, alert-time) and auto-deliver the
        # moment the clip exists - no second reply needed, no wasted data.
        self._pending: list[dict] = []

    _PENDING_TTL_S = 1200.0  # stop waiting on a pending request after 20 min

    def set_labels(self, labels: dict) -> None:
        self._labels = dict(labels)

    def stop(self) -> None:
        self._stop = True

    def _api(self, method: str, payload: dict, timeout: float) -> dict:
        url = _API.format(token=self._token, method=method)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def run(self) -> None:
        offset = self._drain()  # ignore messages from while we were offline
        bus.info("TG", "reply listener started")
        while not self._stop:
            try:
                result = self._api(
                    "getUpdates",
                    {"offset": offset, "timeout": _POLL_TIMEOUT_S,
                     "allowed_updates": ["message", "channel_post",
                                         "callback_query"]},
                    _POLL_TIMEOUT_S + 10,
                )
            except Exception as e:
                if not self._stop:
                    bus.warn("TG", f"getUpdates failed: {e!s}")
                    self.msleep(3000)
                continue
            for upd in result.get("result", []):
                offset = max(offset, int(upd.get("update_id", 0)) + 1)
                if self._stop:
                    break
                try:
                    self._handle(upd)
                except Exception as e:
                    bus.warn("TG", f"reply handling failed: {e!s}")
            # After each poll cycle, see if any awaited clip has become ready.
            try:
                self._check_pending()
            except Exception as e:
                bus.warn("TG", f"pending check failed: {e!s}")
        bus.info("TG", "reply listener stopped")

    def _drain(self) -> int:
        try:
            result = self._api("getUpdates", {"timeout": 0}, _TIMEOUT_S)
        except Exception:
            return 0
        offset = 0
        for upd in result.get("result", []):
            offset = max(offset, int(upd.get("update_id", 0)) + 1)
        return offset

    def _handle(self, upd: dict) -> None:
        cq = upd.get("callback_query")
        if cq:
            self._handle_callback(cq)
            return
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            return
        chat = msg.get("chat", {})
        if str(chat.get("id")) != self._chat_id:
            return  # SECURITY: only the linked chat may command the bot
        reply = msg.get("reply_to_message")
        if reply:
            entry = self._map.lookup(int(reply.get("message_id", 0)))
            if entry:
                self._dispatch_reply(entry)
                return
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            self._handle_command(text)

    def _answer_callback(self, cq_id: str, text: str = "") -> None:
        """Stop the client's button spinner with a brief toast. Best-effort."""
        if cq_id:
            api_answer_callback(self._token, cq_id, text)

    def _handle_callback(self, cq: dict) -> None:
        """A tap on an alert's inline button. The button is attached to the alert
        message, which is already in the map (just like a reply target), so we
        resolve the event from that message id - no extra state. 'cap' = the
        detecting camera's clip (progressive), 'all' = every remaining angle.

        We answer the callback FIRST (fast, stops the spinner) and only then do
        the slow clip work, so the family sees instant feedback on the tap."""
        msg = cq.get("message") or {}
        chat = msg.get("chat", {})
        if str(chat.get("id")) != self._chat_id:
            return  # SECURITY: only the linked chat may command the bot
        cq_id = str(cq.get("id", ""))
        data = (cq.get("data") or "").strip()
        entry = self._map.lookup(int(msg.get("message_id", 0)))
        if not entry:
            self._answer_callback(cq_id, tg.t(self._lang, "cb_expired"))
            return
        self._answer_callback(cq_id, tg.t(self._lang, "cb_wait"))
        self._dispatch_reply(entry, mode="all" if data == "all" else "capture")

    def _dispatch_reply(self, entry: dict, mode: str = "capture") -> None:
        """Deliver an event's angles. mode 'capture' = progressive (first call ->
        the detecting camera(s); each later call -> the next remaining angles).
        mode 'all' = every angle not yet sent at once. State is per event folder,
        so the thumbnail's buttons OR a sent clip's button advance the same event.

        A LIVE alert (kind 'live') has no folder yet: resolve it to the matching
        event clip if the segment tier has since extracted it, else queue it and
        auto-send the moment it's ready (no second tap needed)."""
        if entry.get("k") == "live" and not entry.get("f"):
            cam = int(entry.get("c", 0))
            t = float(entry.get("t", 0.0))
            resolved = self._resolve_live_event(cam, t)
            if resolved is None:
                self._add_pending(cam, t, mode)
                api_send_message(self._token, self._chat_id,
                                 tg.t(self._lang, "preparing"))
                return
            entry = {"f": str(resolved), "c": cam, "k": "thumb"}

        folder = Path(entry.get("f", ""))
        if not folder.is_dir():
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "expired"))
            return
        all_cams = self._all_cams(folder)
        if not all_cams:
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "no_clips"))
            return

        key = str(folder)
        sent = self._sent_cams.setdefault(key, set())
        if mode == "all":
            # Every angle still owed - if nothing's gone out yet that's all four.
            to_send = [c for c in all_cams if c not in sent]
        elif not sent:
            # First capture: the camera(s) that detected the event. Falls back to
            # the entry's own camera (the trigger) when no list is recorded.
            detecting = [c for c in self._detecting_cams(folder, int(entry.get("c", 0)))
                         if c in all_cams]
            to_send = detecting or all_cams[:1]
        else:
            to_send = [c for c in all_cams if c not in sent]

        if not to_send:
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "no_more"))
            return

        # On a Capture tap there may still be other angles to fetch, so each
        # sent clip carries an "All cameras" button. On an All tap everything is
        # already going out, so no button (a tap would just say "no more").
        more = [c for c in all_cams if c not in sent and c not in to_send]
        markup = alert_markup(self._lang, with_capture=False) if (
            mode == "capture" and more) else None
        for n in to_send:
            if self._stop:
                break
            if self._send_clip(folder, n, markup=markup):
                sent.add(n)

    # A replied video is capped at this many seconds: the family gets the real
    # event clip if it's already short (dynamic length), or a tight 30s excerpt
    # when the event chunk runs long - never a multi-minute blob. The full-length
    # clip always stays in the gallery; only the Telegram upload is capped.
    _CLIP_MAX_S = 30.0

    def _clip_duration_s(self, folder: Path) -> float:
        """The event's real clip length from its sidecar, or 0 if unknown."""
        try:
            meta = json.loads((folder / "event.json").read_text(encoding="utf-8"))
            return float(meta.get("duration_s", 0.0))
        except (OSError, ValueError, KeyError, TypeError):
            return 0.0

    def _clip_for_send(self, folder: Path, cam: int, path: Path) -> Path:
        """The video to upload for `cam`. A clip already within the cap is sent
        as-is (the real, dynamic event length - a 9s walk-through stays 9s); a
        longer chunk is trimmed to the first _CLIP_MAX_S seconds (arrival +
        activity) and cached so repeat replies reuse it."""
        dur = self._clip_duration_s(folder)
        if dur <= 0.0 or dur <= self._CLIP_MAX_S + 0.5:
            return path
        short = folder / f"cam{cam}_{int(self._CLIP_MAX_S)}s.mp4"
        if short.is_file() and short.stat().st_size > 0:
            return short
        from app.core.events import _cut_clip
        if _cut_clip(path, short, 0.0, self._CLIP_MAX_S):
            bus.info("TG", f"capped cam{cam} clip to {int(self._CLIP_MAX_S)}s ({folder.name})")
            return short
        return path  # trim failed -> fall back to the full clip

    def _send_clip(self, folder: Path, cam: int, markup: str | None = None) -> bool:
        """Send one camera's clip. Returns True only when Telegram accepted it,
        so the caller marks exactly the angles that actually went out."""
        path = folder / f"cam{cam}.mp4"
        if not path.is_file():
            # Other angles exist in the recordings - cut them on demand for the
            # SAME wall-clock window so a reply still delivers all four angles.
            if not self._cut_sibling_clip(folder, cam):
                api_send_message(self._token, self._chat_id,
                                 tg.t(self._lang, "no_footage", where=self._label(cam)))
                return False
        send_path = self._clip_for_send(folder, cam, path)
        caption = tg.t(self._lang, "clip", where=self._label(cam))
        try:
            result = api_send_video(self._token, self._chat_id, caption, send_path,
                                    reply_markup=markup)
        except Exception as e:
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "send_failed",
                                  where=self._label(cam), err=str(e)))
            return False
        if result.get("ok"):
            self._map.record(_message_id(result), str(folder), cam, "video")
            bus.info("TG", f"sent clip cam{cam} ({folder.name})")
            return True
        api_send_message(
            self._token, self._chat_id,
            tg.t(self._lang, "send_failed", where=self._label(cam),
                 err=result.get("description", "?")),
        )
        return False

    def _all_cams(self, folder: Path) -> list[int]:
        """Every camera angle available for this event. Includes clips already
        in the folder PLUS all configured cameras (their angles can be cut on
        demand from the recordings) - so a reply to a live event, whose folder
        holds only the detecting camera, still offers all four angles."""
        present: set[int] = set()
        for mp4 in folder.glob("cam*.mp4"):
            m = re.match(r"cam(\d+)\.mp4", mp4.name, re.IGNORECASE)
            if m:
                present.add(int(m.group(1)))
        # Other angles are only producible when we know where recordings live.
        extra = set(self._cam_ids) if self._recording_dir else set()
        return sorted(present | extra)

    def _cut_sibling_clip(self, folder: Path, cam: int) -> bool:
        """Cut camera `cam`'s clip from the recordings over the event's window,
        writing it into the event folder so it's cached for future replies.
        Returns True if a non-empty clip was produced."""
        if not self._recording_dir:
            return False
        meta_path = folder / "event.json"
        if not meta_path.is_file():
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            start_at = datetime.fromisoformat(meta["start_at"])
            dur = float(meta.get("duration_s", 0.0)) or (
                self._pre_roll_s + self._post_roll_s)
        except (ValueError, OSError, KeyError, TypeError) as e:
            bus.warn("TG", f"sibling cut: bad event.json ({e!s})")
            return False

        from app.core.events import _cut_clip, _segment_covering

        # The live clip's footage starts ~pre_roll before the detection moment;
        # mirror that window on the sibling so the angles line up.
        window_start = start_at - timedelta(seconds=self._pre_roll_s)
        cam_dir = Path(self._recording_dir) / f"cam{cam}"
        found = _segment_covering(cam_dir, window_start)
        if found is None:
            bus.warn("TG", f"sibling cut: no segment covering {window_start:%H:%M:%S} for cam{cam}")
            return False
        seg_start, src = found
        offset = (window_start - seg_start).total_seconds()
        if offset < 0 or offset > 17 * 60:
            bus.warn("TG", f"sibling cut: offset {offset:.0f}s out of range for cam{cam}")
            return False
        out = folder / f"cam{cam}.mp4"
        ok = _cut_clip(src, out, offset, dur)
        if ok:
            bus.info("TG", f"sibling cut: cam{cam} {dur:.1f}s @ {window_start:%H:%M:%S}")
        return ok

    _LIVE_MATCH_WINDOW_S = 180.0  # a live alert and its event share a wall-clock

    def _add_pending(self, cam: int, t: float, mode: str = "capture") -> None:
        """Remember a request waiting for a not-yet-ready clip, so the poll loop
        can auto-deliver it. De-duped per (cam, alert-time); a later 'all' tap
        upgrades an existing 'capture' request so every angle still goes out."""
        for p in self._pending:
            if p["c"] == cam and abs(p["t"] - t) < 1.0:
                if mode == "all":
                    p["m"] = "all"
                return
        self._pending.append({"c": cam, "t": t, "m": mode,
                              "since": time.monotonic()})
        bus.info("TG", f"queued auto-send for cam{cam} clip when ready")

    def _check_pending(self) -> None:
        """Each poll cycle: deliver any pending request whose clip now exists;
        drop ones older than the TTL."""
        if not self._pending:
            return
        still: list[dict] = []
        for p in self._pending:
            if time.monotonic() - p["since"] > self._PENDING_TTL_S:
                bus.info("TG", f"pending cam{p['c']} clip timed out; dropping")
                continue
            folder = self._resolve_live_event(p["c"], p["t"])
            if folder is None:
                still.append(p)
                continue
            bus.info("TG", f"pending cam{p['c']} clip ready; auto-sending")
            self._dispatch_reply({"f": str(folder), "c": p["c"], "k": "thumb"},
                                 mode=p.get("m", "capture"))
        self._pending = still

    def _resolve_live_event(self, cam: int, t_epoch: float):
        """Find the clip folder for a live alert: a clip from `cam` whose
        wall-clock start is within a few minutes of the alert time `t_epoch`.
        Scans BOTH the live-clips dir (the fast ~30s quick clip) and the events
        dir (the later full segment event), picking the nearest in time. Returns
        the folder Path, or None if neither exists yet."""
        if not t_epoch:
            return None
        from app.core.event_library import scan_events

        best = None
        best_dt = self._LIVE_MATCH_WINDOW_S
        for base in (self._live_clips_dir, self._events_dir):
            if not base:
                continue
            try:
                events = scan_events(Path(base))
            except Exception as e:
                bus.warn("TG", f"live-resolve scan failed for {base}: {e!s}")
                continue
            for ev in events:
                if getattr(ev, "trigger_cam", 0) != cam:
                    continue
                start = getattr(ev, "start_at", None)
                if start is None:
                    continue
                dt = abs(start.timestamp() - t_epoch)
                if dt <= best_dt:
                    best_dt = dt
                    best = ev.folder
        return best

    def _detecting_cams(self, folder: Path, fallback_cam: int) -> list[int]:
        """The camera(s) that actually detected the event - sent first on the
        initial reply. Today that's the single trigger camera; once all-camera
        bundling records multiple detecting cams in event.json (key
        'detecting_cams'), they all come through here. Falls back to the
        recorded trigger_cam, then to the message's own camera."""
        meta_path = folder / "event.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                det = meta.get("detecting_cams")
                if isinstance(det, list) and det:
                    return [int(c) for c in det]
                trig = meta.get("trigger_cam")
                if trig is not None:
                    return [int(trig)]
            except (ValueError, OSError, TypeError):
                pass
        return [fallback_cam] if fallback_cam else []

    def _label(self, cam: int) -> str:
        return self._labels.get(cam) or f"camera {cam}"

    def _handle_command(self, text: str) -> None:
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        if cmd in ("start", "help"):
            api_send_message(self._token, self._chat_id, tg.t(self._lang, "help"))
        elif cmd == "last":
            self._send_last()
        else:
            api_send_message(self._token, self._chat_id, tg.t(self._lang, "unknown_cmd"))

    def _send_last(self) -> None:
        if not self._events_dir:
            api_send_message(self._token, self._chat_id, "No events directory configured.")
            return
        try:
            from app.core.event_library import scan_events
            events = scan_events(Path(self._events_dir))
        except Exception as e:
            api_send_message(self._token, self._chat_id, f"Could not read events: {e!s}")
            return
        if not events:
            api_send_message(self._token, self._chat_id, tg.t(self._lang, "no_events"))
            return
        ev = events[0]
        cam = getattr(ev, "trigger_cam", 0)
        thumb = getattr(ev, "thumb", None)
        caption = tg.t(self._lang, "latest", where=self._label(cam),
                       when=f"{ev.start_at:%H:%M:%S}")
        if thumb and Path(thumb).is_file():
            result = api_send_photo(self._token, self._chat_id, caption, Path(thumb))
            if result.get("ok"):
                self._map.record(_message_id(result), str(ev.folder), cam, "thumb")
                return
        api_send_message(self._token, self._chat_id, caption)
