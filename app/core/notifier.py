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
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool

from app.core.log import bus
from app.core.telegram_map import TelegramMap

_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT_S = 20
_UPLOAD_TIMEOUT_S = 120          # clip uploads are larger / slower than a photo
_MAX_UPLOAD_BYTES = 49 * 1024 * 1024  # Telegram bot send cap is 50 MB; stay under
_POLL_TIMEOUT_S = 10             # getUpdates long-poll; bounds shutdown latency

_HELP_TEXT = (
    "🏠 Watchhouse bot\n"
    "• Reply to an alert photo → get the detecting camera's clip.\n"
    "• Reply again → the next camera angles (until all are sent).\n"
    "• /last → the newest event.\n"
    "• /help → this message."
)


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


def api_send_photo(token: str, chat_id: str, caption: str, path: Path) -> dict:
    return _post_multipart(token, "sendPhoto",
                           {"chat_id": chat_id.strip(), "caption": caption},
                           "photo", path)


def api_send_video(token: str, chat_id: str, caption: str, path: Path) -> dict:
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
    try:
        r = _post_multipart(token, "sendVideo",
                            {"chat_id": chat_id.strip(), "caption": caption,
                             "supports_streaming": "true"}, "video", path)
        if r.get("ok"):
            return r
    except urllib.error.HTTPError:
        pass  # fall through to document delivery
    return _post_multipart(token, "sendDocument",
                           {"chat_id": chat_id.strip(), "caption": caption},
                           "document", path)


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
                 cam: int = 0, kind: str = "thumb", t: float = 0.0) -> None:
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

    def run(self) -> None:
        try:
            if self._thumb is not None and self._thumb.is_file():
                result = api_send_photo(self._token, self._chat_id,
                                        self._caption, self._thumb)
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
                 folder: str, tmap) -> None:
        super().__init__()
        self._token = token
        self._chat_id = chat_id
        self._cam_id = cam_id
        self._label = cam_label
        self._folder = folder
        self._map = tmap

    def run(self) -> None:
        try:
            path = Path(self._folder) / f"cam{self._cam_id}.mp4"
            if not path.is_file():
                return
            caption = f"\U0001F3A5 {self._label} — clip ready"
            result = api_send_video(self._token, self._chat_id, caption, path)
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
                 state_dir=None, events_dir=None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._token = (token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._min_interval = max(0.0, min_interval_s)
        self._notify_ongoing = notify_ongoing
        self._commands = commands_enabled
        self._events_dir = events_dir
        self._cam_labels: dict[int, str] = {}
        self._last_sent: dict[int, float] = {}
        self._pool = QThreadPool.globalInstance()
        self._map = TelegramMap(state_dir)
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
                  commands_enabled: bool | None = None) -> None:
        """Apply new credentials/settings to the running notifier without a
        restart (called by the Telegram setup dialog after it saves to .env)."""
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
        self._pool.start(_SendTask(self._token, self._chat_id, caption, thumb,
                                   tmap=tmap, folder=folder_s, cam=cam_id))

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
        caption = f"\U0001F534 NOW · {cam_label}: {title}  {when}"
        thumb = Path(thumb_path) if thumb_path else None
        tmap = self._map if self._commands else None
        self._pool.start(_SendTask(self._token, self._chat_id, caption, thumb,
                                   tmap=tmap, cam=cam_id, kind="live", t=time.time()))

    def send_quick_clip(self, cam_id: int, cam_label: str, folder: str) -> None:
        """Auto-push the instant quick clip (the ~30s pre-event-buffer video)
        the moment it's encoded, so the family gets the video in seconds. The
        message is recorded so a reply can pull other angles later."""
        if not self.enabled:
            return
        self._pool.start(_QuickClipTask(self._token, self._chat_id, cam_id,
                                        cam_label, folder,
                                        self._map if self._commands else None))

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

    @classmethod
    def _caption(cls, clip, cam_label: str | None) -> str:
        where = cam_label or f"camera {getattr(clip, 'cam_id', '?')}"
        when = getattr(clip, "start_at", None)
        when_s = when.strftime("%H:%M:%S") if when is not None else ""
        state = getattr(clip, "presence_state", "single")
        secs = getattr(clip, "presence_seconds", 0.0)
        if state == "ongoing":
            return f"⏱ {where}: still present (~{cls._mins(secs)})  {when_s}"
        if state == "ended":
            return f"✅ {where}: cleared after {cls._mins(secs)}  {when_s}"
        # started / single -> a normal arrival alert.
        return f"\U0001F6A8 {where}: {cls._what(clip)} detected  {when_s}"


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
                 events_dir) -> None:
        super().__init__()
        self._token = token.strip()
        self._chat_id = str(chat_id).strip()
        self._map = tmap
        self._labels = dict(cam_labels or {})
        self._events_dir = events_dir
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
                    {"offset": offset, "timeout": _POLL_TIMEOUT_S},
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

    def _dispatch_reply(self, entry: dict) -> None:
        """Progressive angle delivery. First reply to an event -> its detecting
        camera(s). Next reply -> the remaining angles. Once every available
        angle has been sent -> 'no more angles'. State is per event folder, so
        replying to the thumbnail OR to any sent clip advances the same event.

        A reply to a LIVE alert (kind 'live') has no folder yet: resolve it to
        the matching event clip if the segment tier has since extracted it, else
        tell the user it's still being prepared so they can reply again."""
        if entry.get("k") == "live" and not entry.get("f"):
            cam = int(entry.get("c", 0))
            t = float(entry.get("t", 0.0))
            resolved = self._resolve_live_event(cam, t)
            if resolved is None:
                self._add_pending(cam, t)
                api_send_message(
                    self._token, self._chat_id,
                    "⏳ Clip isn't ready yet — I'll send it automatically the "
                    "moment it is. No need to reply again.")
                return
            entry = {"f": str(resolved), "c": cam, "k": "thumb"}

        folder = Path(entry.get("f", ""))
        if not folder.is_dir():
            api_send_message(self._token, self._chat_id,
                             "That event's clips are no longer available.")
            return
        all_cams = self._all_cams(folder)
        if not all_cams:
            api_send_message(self._token, self._chat_id,
                             "No clips available for this event.")
            return

        key = str(folder)
        sent = self._sent_cams.setdefault(key, set())
        if not sent:
            # First reply: the camera(s) that detected the event. Falls back to
            # the entry's own camera (the trigger) when no list is recorded.
            detecting = [c for c in self._detecting_cams(folder, int(entry.get("c", 0)))
                         if c in all_cams]
            to_send = detecting or all_cams[:1]
        else:
            to_send = [c for c in all_cams if c not in sent]

        if not to_send:
            api_send_message(self._token, self._chat_id,
                             "✅ No more angles — all cameras sent for this event.")
            return

        for n in to_send:
            if self._stop:
                break
            if self._send_clip(folder, n):
                sent.add(n)

    def _send_clip(self, folder: Path, cam: int) -> bool:
        """Send one camera's clip. Returns True only when Telegram accepted it,
        so the caller marks exactly the angles that actually went out."""
        path = folder / f"cam{cam}.mp4"
        if not path.is_file():
            api_send_message(self._token, self._chat_id,
                             f"No clip for {self._label(cam)}.")
            return False
        caption = f"\U0001F3A5 {self._label(cam)}"
        try:
            result = api_send_video(self._token, self._chat_id, caption, path)
        except Exception as e:
            api_send_message(self._token, self._chat_id,
                             f"Could not send {self._label(cam)}: {e!s}")
            return False
        if result.get("ok"):
            self._map.record(_message_id(result), str(folder), cam, "video")
            bus.info("TG", f"sent clip cam{cam} ({folder.name})")
            return True
        api_send_message(
            self._token, self._chat_id,
            f"Could not send {self._label(cam)}: {result.get('description', '?')}",
        )
        return False

    def _all_cams(self, folder: Path) -> list[int]:
        """Sorted camera numbers that have a clip in this event folder."""
        out: list[int] = []
        for mp4 in sorted(folder.glob("cam*.mp4")):
            m = re.match(r"cam(\d+)\.mp4", mp4.name, re.IGNORECASE)
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    _LIVE_MATCH_WINDOW_S = 180.0  # a live alert and its event share a wall-clock

    def _add_pending(self, cam: int, t: float) -> None:
        """Remember a reply that's waiting for a not-yet-ready clip, so the poll
        loop can auto-deliver it. De-duped per (cam, alert-time)."""
        for p in self._pending:
            if p["c"] == cam and abs(p["t"] - t) < 1.0:
                return
        self._pending.append({"c": cam, "t": t, "since": time.monotonic()})
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
            self._dispatch_reply({"f": str(folder), "c": p["c"], "k": "thumb"})
        self._pending = still

    def _resolve_live_event(self, cam: int, t_epoch: float):
        """Find the event folder for a live alert: an event from `cam` whose
        wall-clock start is within a few minutes of the alert time `t_epoch`.
        Returns the folder Path, or None if the segment tier hasn't extracted it
        yet (the user is told to reply again). Picks the nearest in time."""
        if not self._events_dir or not t_epoch:
            return None
        try:
            from app.core.event_library import scan_events
            events = scan_events(Path(self._events_dir))
        except Exception as e:
            bus.warn("TG", f"live-resolve scan failed: {e!s}")
            return None
        best = None
        best_dt = self._LIVE_MATCH_WINDOW_S
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
            api_send_message(self._token, self._chat_id, _HELP_TEXT)
        elif cmd == "last":
            self._send_last()
        else:
            api_send_message(self._token, self._chat_id, "Unknown command. Send /help.")

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
            api_send_message(self._token, self._chat_id, "No events recorded yet.")
            return
        ev = events[0]
        cam = getattr(ev, "trigger_cam", 0)
        thumb = getattr(ev, "thumb", None)
        caption = (f"\U0001F551 Latest: {self._label(cam)} — {ev.pretty}  "
                   f"{ev.start_at:%H:%M:%S}")
        if thumb and Path(thumb).is_file():
            result = api_send_photo(self._token, self._chat_id, caption, Path(thumb))
            if result.get("ok"):
                self._map.record(_message_id(result), str(ev.folder), cam, "thumb")
                return
        api_send_message(self._token, self._chat_id, caption)
