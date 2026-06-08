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
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThread

from app.core.log import bus
from app.core.telegram_map import TelegramMap
from app.core import telegram_text as tg
from app.core import dvr_time

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


def _encode_multipart_multi(fields: dict[str, str],
                            files: dict[str, Path]) -> tuple[bytes, str]:
    """multipart/form-data with several attached files (for sendMediaGroup)."""
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    out = bytearray()
    for name, value in fields.items():
        out += b"--" + boundary.encode() + crlf
        out += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        out += str(value).encode("utf-8") + crlf
    for name, path in files.items():
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        out += b"--" + boundary.encode() + crlf
        out += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{path.name}"'
        ).encode() + crlf
        out += f"Content-Type: {ctype}".encode() + crlf + crlf
        out += path.read_bytes() + crlf
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


def api_send_message(token: str, chat_id: str, text: str,
                     reply_to: int | None = None) -> dict:
    payload = {"chat_id": chat_id.strip(), "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return _call(token, "sendMessage", payload)


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
                   reply_markup: str | None = None,
                   reply_to: int | None = None) -> dict:
    fields = {"chat_id": chat_id.strip(), "caption": caption}
    if reply_markup:
        fields["reply_markup"] = reply_markup
    if reply_to:
        fields["reply_to_message_id"] = str(reply_to)
    return _post_multipart(token, "sendPhoto", fields, "photo", path)


def api_send_video(token: str, chat_id: str, caption: str, path: Path,
                   reply_markup: str | None = None,
                   reply_to: int | None = None) -> dict:
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
    if reply_to:
        video_fields["reply_to_message_id"] = str(reply_to)
        doc_fields["reply_to_message_id"] = str(reply_to)
    try:
        r = _post_multipart(token, "sendVideo", video_fields, "video", path)
        if r.get("ok"):
            return r
    except urllib.error.HTTPError:
        pass  # fall through to document delivery
    return _post_multipart(token, "sendDocument", doc_fields, "document", path)


def api_send_media_group(token: str, chat_id: str, caption: str,
                         paths: list, kind: str = "photo",
                         reply_to: int | None = None) -> dict:
    """Send 2+ photos OR videos as ONE album message (caption on the first item),
    optionally as a reply to `reply_to`. Falls back to a single send for one item.
    Returns the API result; `result` is a LIST of the sent messages."""
    valid = [Path(p) for p in paths if p and Path(p).is_file()][:10]
    if not valid:
        return {"ok": False, "description": "no media"}
    if len(valid) == 1:
        if kind == "video":
            return api_send_video(token, chat_id, caption, valid[0], reply_to=reply_to)
        return api_send_photo(token, chat_id, caption, valid[0], reply_to=reply_to)
    media = []
    files: dict[str, Path] = {}
    for i, p in enumerate(valid):
        key = f"file{i}"
        item = {"type": kind, "media": f"attach://{key}"}
        if i == 0:
            item["caption"] = caption
        media.append(item)
        files[key] = p
    fields = {"chat_id": chat_id.strip(), "media": json.dumps(media)}
    if reply_to:
        fields["reply_to_message_id"] = str(reply_to)
    body, boundary = _encode_multipart_multi(fields, files)
    url = _API.format(token=token.strip(), method="sendMediaGroup")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=_UPLOAD_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


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


def _composite_thumbs(paths: list, out_path: Path) -> "Path | None":
    """Stitch up to two camera thumbs side-by-side into one image, so a collision
    can be sent as a SINGLE photo (which CAN carry inline buttons, unlike an
    album). Returns the written combined path, a lone existing thumb, or None."""
    existing = [Path(p) for p in paths if p and Path(p).is_file()]
    if not existing:
        return None
    if len(existing) == 1:
        return existing[0]
    try:
        import cv2
        import numpy as np

        imgs = [im for im in (cv2.imread(str(p)) for p in existing[:2]) if im is not None]
        if len(imgs) < 2:
            return existing[0]
        h = min(im.shape[0] for im in imgs)
        resized = [cv2.resize(im, (max(1, int(im.shape[1] * (h / im.shape[0]))), h))
                   for im in imgs]
        gap = np.full((h, 6, 3), 20, dtype=np.uint8)  # thin dark separator
        combined = resized[0]
        for im in resized[1:]:
            combined = np.hstack([combined, gap, im])
        cv2.imwrite(str(out_path), combined)
        return out_path
    except Exception as e:
        bus.warn("TG", f"thumb composite failed: {e!s}")
        return existing[0]


class _CollisionTask(QRunnable):
    """Send a fused same-movement event as ONE photo (both camera thumbs stitched
    side-by-side, so it CAN carry inline buttons), named by the link + direction.
    Records it as a 'collision' entry carrying BOTH event folders so its buttons
    serve both clips (Capture) or every union angle (All cameras). Off the UI
    thread."""

    def __init__(self, token: str, chat_id: str, caption: str, thumbs: list,
                 markup: str | None, tmap, from_folder: str, to_folder: str,
                 cam_from: int, cam_to: int) -> None:
        super().__init__()
        self._token = token
        self._chat_id = chat_id
        self._caption = caption
        self._thumbs = thumbs
        self._markup = markup
        self._map = tmap
        self._from_folder = from_folder
        self._to_folder = to_folder
        self._cam_from = cam_from
        self._cam_to = cam_to

    def run(self) -> None:
        try:
            out = (Path(self._from_folder) / "collision_thumb.jpg"
                   if self._from_folder else None)
            photo = _composite_thumbs(self._thumbs, out) if out else None
            if photo is None:
                api_send_message(self._token, self._chat_id, self._caption)
                bus.info("TG", "collision alert sent (text; no thumbs)")
                return
            result = api_send_photo(self._token, self._chat_id, self._caption,
                                    photo, reply_markup=self._markup)
            if not result.get("ok"):
                bus.warn("TG", f"collision photo rejected: {result.get('description', '?')}")
                return
            bus.info("TG", "collision alert sent (photo)")
            if self._map is not None and self._from_folder:
                self._map.record(_message_id(result), self._from_folder,
                                 self._cam_from, "collision",
                                 extra={"f2": self._to_folder, "c2": self._cam_to})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
            bus.warn("TG", f"collision send failed: HTTP {e.code} {body}")
        except Exception as e:
            bus.warn("TG", f"collision send failed: {e!s}")


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
        # Recent live-tier alert times (epoch) per camera, for cross-tier
        # dedupe: when the segment tier later extracts the same arrival, the
        # live alert already covered it, so we skip the duplicate push.
        self._live_alerts: dict[int, list[float]] = {}
        # Ordered priority send queue: ONE worker drains it so messages arrive in
        # order (the global QThreadPool ran sends in parallel → reordered them).
        # Two lanes by priority: photos/text (0) jump ahead of slow video uploads
        # (1) so an instant alert never waits behind a clip; FIFO within a lane
        # via a sequence tiebreak. Each item is (prio, seq, callable).
        self._send_q: "queue.PriorityQueue" = queue.PriorityQueue()
        self._send_seq = 0
        self._send_thread = threading.Thread(
            target=self._send_loop, name="tg-sender", daemon=True)
        self._send_thread.start()
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

    def _send_loop(self) -> None:
        """Drain the priority queue, one send at a time (photos before videos)."""
        while True:
            _prio, _seq, fn = self._send_q.get()
            if fn is None:
                break
            try:
                fn()
            except Exception as e:  # never let one bad send kill the sender
                bus.warn("TG", f"send task failed: {e!s}")

    def _enqueue(self, task, prio: int = 0) -> None:
        """Queue a QRunnable-style send. prio 0 = photo/text (fast lane),
        prio 1 = video upload (slow lane, never blocks a fast-lane alert)."""
        self._send_seq += 1
        self._send_q.put((prio, self._send_seq, task.run))

    def shutdown(self) -> None:
        """Stop the reply listener and the send worker; call from closeEvent."""
        self._stop_poller()
        # Sentinel after any queued sends (prio 9) so they flush first; the join
        # timeout bounds it if an upload is stuck.
        self._send_q.put((9, self._send_seq + 1, None))
        self._send_thread.join(timeout=5.0)

    def notify(self, clip, cam_label: str | None = None) -> None:
        """Queue an alert for an extracted event. Best-effort, non-blocking.

        Debounce policy: an arrival ("started"/"single") and a departure
        ("ended") must never be dropped, so they bypass the per-camera
        debounce. Only the repeating "still present" pings ("ongoing") respect
        self._min_interval - and they can be silenced entirely via config."""
        if not self.enabled:
            return
        cam_id = getattr(clip, "cam_id", 0)
        state = getattr(clip, "presence_state", "single")
        # The detect box gates NOTIFICATIONS, not recording: an event whose
        # activity never entered the box is still saved to the gallery, just
        # not pushed to Telegram (mirrors the live tier's box filter).
        if not getattr(clip, "in_region", True):
            bus.info("TG", f"cam{cam_id}: event outside detect box - not pushed")
            return
        # Cross-tier dedupe: the instant live alert already covered this arrival,
        # so skip the duplicate segment push. An arrival the live tier missed
        # (none recorded near this time) still goes out as a safety net.
        if state in ("started", "single") and self._live_covered(cam_id, clip):
            bus.info("TG", f"cam{cam_id}: arrival already sent live - not re-pushed")
            return
        if state == "ongoing" and not self._notify_ongoing:
            return  # user opted out of the recurring still-present pings
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
        self._enqueue(_SendTask(self._token, self._chat_id, caption, thumb,
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
        self._record_live_alert(cam_id)  # for cross-tier dedupe in notify()
        when = dvr_time.now().strftime("%H:%M:%S")
        caption = tg.t(self._lang, "alert_live", where=cam_label, when=when)
        thumb = Path(thumb_path) if thumb_path else None
        tmap = self._map if self._commands else None
        markup = alert_markup(self._lang) if tmap is not None else None
        self._enqueue(_SendTask(self._token, self._chat_id, caption, thumb,
                                tmap=tmap, cam=cam_id, kind="live",
                                t=time.time(), markup=markup))

    # An extracted arrival within this many seconds of a live alert on the same
    # camera is the SAME arrival - skip the segment push. Kept well under the
    # 2-minute leave/return threshold so a genuine re-entry the live tier missed
    # still gets its own (safety-net) push.
    _LIVE_DEDUP_WINDOW_S = 60.0
    _LIVE_ALERT_TTL_S = 600.0  # forget live-alert times after 10 min

    def _record_live_alert(self, cam_id: int) -> None:
        now = time.time()
        lst = self._live_alerts.setdefault(cam_id, [])
        lst.append(now)
        cutoff = now - self._LIVE_ALERT_TTL_S
        self._live_alerts[cam_id] = [t for t in lst if t >= cutoff]

    def _live_covered(self, cam_id: int, clip) -> bool:
        """True if a live alert fired on this camera close enough to the event's
        start to be the same arrival (so the segment push would be a duplicate)."""
        when = getattr(clip, "start_at", None)
        if when is None:
            return False
        try:
            ev_epoch = when.timestamp()
        except (AttributeError, ValueError, OverflowError, OSError):
            return False
        return any(abs(t - ev_epoch) <= self._LIVE_DEDUP_WINDOW_S
                   for t in self._live_alerts.get(cam_id, ()))

    def send_quick_clip(self, cam_id: int, cam_label: str, folder: str) -> None:
        """Auto-push the instant quick clip (the ~30s pre-event-buffer video)
        the moment it's encoded, so the family gets the video in seconds. The
        message is recorded so a reply can pull other angles later."""
        if not self.enabled:
            return
        self._enqueue(_QuickClipTask(self._token, self._chat_id, cam_id,
                                     cam_label, folder,
                                     self._map if self._commands else None,
                                     lang=self._lang), prio=1)

    def notify_collision(self, name: str, direction: str, from_clip,
                         to_clip) -> None:
        """Fused same-movement event: ONE photo (both angles side-by-side) named
        by the link + direction (e.g. 'Front door — went out') instead of a
        camera. Its buttons serve BOTH detecting clips (Capture) or every angle
        over the union of both events (All cameras). Replaces the two separate
        per-camera segment pushes."""
        if not self.enabled:
            return
        when = dvr_time.shift(getattr(from_clip, "start_at", None))
        when_s = when.strftime("%H:%M:%S") if when is not None else ""
        caption = tg.t(self._lang, "collision", name=name, direction=direction,
                       when=when_s)
        thumbs = [getattr(from_clip, "thumb_path", None),
                  getattr(to_clip, "thumb_path", None)]
        from_folder = str(getattr(from_clip, "folder", "") or "")
        to_folder = str(getattr(to_clip, "folder", "") or "")
        cam_from = getattr(from_clip, "cam_id", 0)
        cam_to = getattr(to_clip, "cam_id", 0)
        tmap = self._map if self._commands else None
        markup = alert_markup(self._lang) if tmap is not None else None
        self._enqueue(_CollisionTask(self._token, self._chat_id, caption, thumbs,
                                     markup, tmap, from_folder, to_folder,
                                     cam_from, cam_to))

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
        when = dvr_time.shift(getattr(clip, "start_at", None))
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
        # Pending angle requests that must wait for not-yet-finalized segments,
        # delivered as ONE album (reply to the alert, attributed to the asker) the
        # moment every angle is ready - so the user never has to tap again.
        self._pending_reqs: list[dict] = []

    _PENDING_TTL_S = 1200.0  # stop waiting on a pending request after 20 min
    # Extra lead added to sibling cuts beyond the live pre-roll, to absorb the
    # ~1s cross-camera segment-start skew + keyframe snapping so the angle always
    # STARTS before the detection moment (never shifted past it).
    _SIBLING_LEAD_S = 3.0

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
            anchor = int(reply.get("message_id", 0))
            entry = self._map.lookup(anchor)
            if entry:
                self._dispatch_reply(entry, anchor=anchor,
                                     requester=self._who(msg.get("from")))
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
        anchor = int(msg.get("message_id", 0))
        entry = self._map.lookup(anchor)
        if not entry:
            self._answer_callback(cq_id, tg.t(self._lang, "cb_expired"))
            return
        self._answer_callback(cq_id, tg.t(self._lang, "cb_wait"))
        self._dispatch_reply(entry, mode="all" if data == "all" else "capture",
                             anchor=anchor, requester=self._who(cq.get("from")))

    def _dispatch_reply(self, entry: dict, mode: str = "capture",
                        anchor: int | None = None, requester: str = "") -> None:
        """Deliver an event's angles as a REPLY to the alert message (`anchor`),
        attributed to `requester`, so overlapping requests never get confused.
        'all' = every angle as ONE album; 'capture' = the detecting camera(s).
        Angles whose recording segment isn't finalized yet make the whole request
        WAIT, then arrive together as one album - no partial dribble, no re-tap.

        A LIVE alert (kind 'live') has no folder yet: resolve it, or queue + retry
        (carrying the anchor + requester) the moment the clip exists."""
        if entry.get("k") == "collision":
            self._dispatch_collision(entry, mode, anchor, requester)
            return
        if entry.get("k") == "live" and not entry.get("f"):
            cam = int(entry.get("c", 0))
            t = float(entry.get("t", 0.0))
            resolved = self._resolve_live_event(cam, t)
            if resolved is None:
                self._add_pending(cam, t, mode, anchor, requester)
                api_send_message(self._token, self._chat_id,
                                 tg.t(self._lang, "preparing"), reply_to=anchor)
                return
            entry = {"f": str(resolved), "c": cam, "k": "thumb"}

        folder = Path(entry.get("f", ""))
        if not folder.is_dir():
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "expired"), reply_to=anchor)
            return
        all_cams = self._all_cams(folder)
        if not all_cams:
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "no_clips"), reply_to=anchor)
            return

        key = str(folder)
        sent = self._sent_cams.setdefault(key, set())
        if mode == "all":
            to_send = [c for c in all_cams if c not in sent]
        elif not sent:
            detecting = [c for c in self._detecting_cams(folder, int(entry.get("c", 0)))
                         if c in all_cams]
            to_send = detecting or all_cams[:1]
        else:
            to_send = [c for c in all_cams if c not in sent]

        if not to_send:
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "no_more"), reply_to=anchor)
            return

        # Ready every requested angle (cut on demand). If any aren't ready yet
        # (segment still being written), queue the WHOLE request and wait so the
        # album arrives as one message - the user already sees "preparing".
        ready, missing = self._ready_angles(folder, to_send)
        if missing:
            self._queue_req(anchor, folder, to_send, requester)
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "preparing"), reply_to=anchor)
            return
        if self._send_angles(folder, ready, anchor, requester):
            for c, _ in ready:
                sent.add(c)

    def _dispatch_collision(self, entry: dict, mode: str, anchor: int | None,
                            requester: str) -> None:
        """A fused-crossing alert's button. Capture = both detecting clips (one
        per event); All cameras = every angle over the UNION window of both events
        (longest clip). The source segments are already finalized (events are in
        the past), so we cut on demand and send ONE album reply to the alert."""
        f = Path(entry.get("f", ""))
        f2 = Path(entry.get("f2", ""))
        cam_from = int(entry.get("c", 0))
        cam_to = int(entry.get("c2", 0))
        if not f.is_dir():
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "expired"), reply_to=anchor)
            return
        if mode == "all":
            items = self._ready_union(f, f2)
            caption = tg.t(self._lang, "angles_album", when=self._union_when(f, f2))
        else:
            items = []
            for fld, cam in ((f, cam_from), (f2, cam_to)):
                if not fld.is_dir():
                    continue
                p = self._ready_clip(fld, cam)
                if p is not None:
                    items.append((cam, p, str(fld)))
            caption = tg.t(self._lang, "pair_clips", when=self._union_when(f, f2))
        if not items:
            api_send_message(self._token, self._chat_id,
                             tg.t(self._lang, "no_clips"), reply_to=anchor)
            return
        self._send_video_album(items, anchor, requester, caption)

    def _send_video_album(self, items: list, anchor: int | None,
                          requester: str, caption: str) -> bool:
        """Send [(cam, path, folder)] as one reply: a single video for one item,
        else ONE album. Records each sent message against ITS OWN folder so
        further replies still resolve. Returns True if anything was accepted."""
        if not items:
            return False
        who = tg.t(self._lang, "requested_by", who=requester) if requester else ""
        full = caption + who
        if len(items) == 1:
            cam, p, fld = items[0]
            try:
                r = api_send_video(self._token, self._chat_id, full, Path(p),
                                   reply_to=anchor)
            except Exception as e:
                bus.warn("TG", f"collision clip send failed: {e!s}")
                return False
            if r.get("ok"):
                self._map.record(_message_id(r), fld, cam, "video")
                bus.info("TG", f"sent collision clip cam{cam}")
                return True
            return False
        paths = [str(p) for _, p, _ in items]
        try:
            r = api_send_media_group(self._token, self._chat_id, full, paths,
                                     kind="video", reply_to=anchor)
        except Exception as e:
            bus.warn("TG", f"collision album send failed: {e!s}")
            return False
        if r.get("ok"):
            msgs = r.get("result")
            if isinstance(msgs, list):
                for (cam, _p, fld), m in zip(items, msgs):
                    mid = _message_id({"result": m})
                    if mid is not None:
                        self._map.record(mid, fld, cam, "video")
            bus.info("TG", f"sent {len(items)}-clip collision album")
            return True
        bus.warn("TG", f"collision album rejected: {r.get('description', '?')}")
        return False

    def _event_start(self, folder: Path):
        """The event's start datetime (raw PC time) from its sidecar, or None."""
        try:
            meta = json.loads((folder / "event.json").read_text(encoding="utf-8"))
            return datetime.fromisoformat(meta["start_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _union_window(self, f: Path, f2: Path):
        """[(earliest start - lead) .. (latest end + post)] spanning BOTH events,
        so 'all angles' clips cover the whole crossing + margins. Returns
        (window_start, dur_seconds) or None if neither event time is readable."""
        s1, s2 = self._event_start(f), self._event_start(f2)
        starts = [s for s in (s1, s2) if s is not None]
        if not starts:
            return None
        ends = []
        if s1 is not None:
            ends.append(s1 + timedelta(seconds=max(0.0, self._clip_duration_s(f))))
        if s2 is not None:
            ends.append(s2 + timedelta(seconds=max(0.0, self._clip_duration_s(f2))))
        start_min = min(starts)
        end_max = max(ends) if ends else start_min
        lead = self._pre_roll_s + self._SIBLING_LEAD_S
        window_start = start_min - timedelta(seconds=lead)
        dur = (end_max - start_min).total_seconds() + lead + self._post_roll_s
        return window_start, max(1.0, dur)

    def _union_when(self, f: Path, f2: Path) -> str:
        """The earliest of the two event starts, in DVR display time."""
        starts = [s for s in (self._event_start(f), self._event_start(f2))
                  if s is not None]
        if not starts:
            return ""
        return dvr_time.shift(min(starts)).strftime("%H:%M:%S")

    def _ready_union(self, f: Path, f2: Path) -> list:
        """Cut every configured camera over the union window into folder f
        (cached as cam{c}_all.mp4). Returns [(cam, path, folder)] produced."""
        win = self._union_window(f, f2)
        if win is None:
            return []
        window_start, dur = win
        cams = self._cam_ids or self._all_cams(f)
        items = []
        for cam in cams:
            out = f / f"cam{cam}_all.mp4"
            if out.is_file() and out.stat().st_size > 0:
                items.append((cam, out, str(f)))
                continue
            if self._cut_at(out, cam, window_start, dur):
                bus.info("TG", f"union cut cam{cam} {dur:.1f}s @ {window_start:%H:%M:%S}")
                items.append((cam, out, str(f)))
        return items

    def _cut_at(self, out: Path, cam: int, window_start, dur: float) -> bool:
        """Cut cam's clip from the recordings over [window_start, +dur]. False if
        the covering segment isn't on disk / not finalized yet."""
        if not self._recording_dir:
            return False
        from app.core.events import _cut_clip, _segment_covering

        cam_dir = Path(self._recording_dir) / f"cam{cam}"
        found = _segment_covering(cam_dir, window_start)
        if found is None:
            bus.warn("TG", f"cut: no segment covering {window_start:%H:%M:%S} for cam{cam}")
            return False
        seg_start, src = found
        offset = (window_start - seg_start).total_seconds()
        if offset < 0 or offset > 17 * 60:
            bus.warn("TG", f"cut: offset {offset:.0f}s out of range for cam{cam}")
            return False
        return _cut_clip(src, out, offset, dur)

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

    @staticmethod
    def _who(frm) -> str:
        """A short name/mention for the person who tapped/replied (for the
        caption's 'for {who}'). @username if set (gives them a notification),
        else first name."""
        if not isinstance(frm, dict):
            return ""
        return ("@" + frm["username"]) if frm.get("username") else (
            frm.get("first_name") or "")

    def _event_when(self, folder: Path) -> str:
        """The event's start time in DVR display time, for the album caption."""
        try:
            meta = json.loads((folder / "event.json").read_text(encoding="utf-8"))
            return dvr_time.shift(datetime.fromisoformat(meta["start_at"])).strftime("%H:%M:%S")
        except (OSError, ValueError, KeyError, TypeError):
            return ""

    def _ready_clip(self, folder: Path, cam: int) -> "Path | None":
        """The send-ready clip path for `cam`: an existing folder clip, or one cut
        on demand from the recordings. None if the covering segment isn't
        finalized yet (still being written) - i.e. try again shortly."""
        path = folder / f"cam{cam}.mp4"
        if not path.is_file():
            if not self._cut_sibling_clip(folder, cam):
                return None
        return self._clip_for_send(folder, cam, path)

    def _ready_angles(self, folder: Path, cams: list):
        """Split `cams` into (ready=[(cam, path)], missing=[cam]) by readiness."""
        ready, missing = [], []
        for c in cams:
            p = self._ready_clip(folder, c)
            if p is not None:
                ready.append((c, p))
            else:
                missing.append(c)
        return ready, missing

    def _send_angles(self, folder: Path, items: list, anchor: int | None,
                     requester: str) -> bool:
        """Send the angle clips for one request as a reply to the alert. One clip
        -> a single video; several -> ONE album (bulk gallery). Records each sent
        message so further replies still resolve to the event. Returns True if
        anything was accepted."""
        if not items:
            return False
        who = tg.t(self._lang, "requested_by", who=requester) if requester else ""
        if len(items) == 1:
            cam, p = items[0]
            cap = tg.t(self._lang, "clip", where=self._label(cam)) + who
            try:
                r = api_send_video(self._token, self._chat_id, cap, Path(p),
                                   reply_to=anchor)
            except Exception as e:
                api_send_message(self._token, self._chat_id,
                                 tg.t(self._lang, "send_failed",
                                      where=self._label(cam), err=str(e)),
                                 reply_to=anchor)
                return False
            if r.get("ok"):
                self._map.record(_message_id(r), str(folder), cam, "video")
                bus.info("TG", f"sent clip cam{cam} ({folder.name})")
                return True
            return False
        cap = tg.t(self._lang, "angles_album", when=self._event_when(folder)) + who
        paths = [str(p) for _, p in items]
        try:
            r = api_send_media_group(self._token, self._chat_id, cap, paths,
                                     kind="video", reply_to=anchor)
        except Exception as e:
            bus.warn("TG", f"angle album send failed: {e!s}")
            return False
        if r.get("ok"):
            msgs = r.get("result")
            if isinstance(msgs, list):
                for (cam, _), m in zip(items, msgs):
                    mid = _message_id({"result": m})
                    if mid is not None:
                        self._map.record(mid, str(folder), cam, "video")
            bus.info("TG", f"sent {len(items)}-angle album ({folder.name})")
            return True
        bus.warn("TG", f"angle album rejected: {r.get('description', '?')}")
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

        # Start the sibling EARLIER than the detection by pre_roll + a safety lead,
        # so cross-camera segment-start skew (~1s) and keyframe snapping can't push
        # the angle past the moment. Extend the duration by the same lead so the
        # tail still reaches the end of the event.
        lead = self._pre_roll_s + self._SIBLING_LEAD_S
        window_start = start_at - timedelta(seconds=lead)
        ok = self._cut_at(folder / f"cam{cam}.mp4", cam, window_start,
                          dur + self._SIBLING_LEAD_S)
        if ok:
            bus.info("TG", f"sibling cut: cam{cam} @ {window_start:%H:%M:%S}")
        return ok

    _LIVE_MATCH_WINDOW_S = 180.0  # a live alert and its event share a wall-clock

    def _add_pending(self, cam: int, t: float, mode: str = "capture",
                     anchor: int | None = None, requester: str = "") -> None:
        """Remember a LIVE-alert request whose event clip doesn't exist yet, so
        the poll loop can auto-deliver it (carrying the anchor + requester). A
        later 'all' tap upgrades an existing 'capture' request."""
        for p in self._pending:
            if p["c"] == cam and abs(p["t"] - t) < 1.0:
                if mode == "all":
                    p["m"] = "all"
                return
        self._pending.append({"c": cam, "t": t, "m": mode, "anchor": anchor,
                              "requester": requester, "since": time.monotonic()})
        bus.info("TG", f"queued auto-send for cam{cam} clip when ready")

    def _queue_req(self, anchor: int | None, folder: Path, cams: list,
                   requester: str) -> None:
        """Queue an angle request that must WAIT for not-yet-finalized segments,
        so it can be delivered as ONE album the moment every angle is ready."""
        key = str(folder)
        for r in self._pending_reqs:
            if r["anchor"] == anchor and r["f"] == key:
                r["cams"] = sorted(set(r["cams"]) | set(cams))
                return
        self._pending_reqs.append({"anchor": anchor, "f": key, "cams": list(cams),
                                   "requester": requester, "since": time.monotonic()})
        bus.info("TG", f"queued angle request ({folder.name}) until segments close")

    def _check_pending(self) -> None:
        """Each poll cycle: resolve live requests, and deliver queued angle
        requests as one album once all their segments finalize (or at TTL)."""
        if not self._pending and not self._pending_reqs:
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
                                 mode=p.get("m", "capture"), anchor=p.get("anchor"),
                                 requester=p.get("requester", ""))
        self._pending = still

        # Angle requests: send as ONE album when every angle is ready, or send
        # whatever's available once the TTL is hit (a camera may be offline).
        still_reqs: list[dict] = []
        for r in self._pending_reqs:
            if self._stop:
                still_reqs.append(r)
                continue
            folder = Path(r["f"])
            sent = self._sent_cams.setdefault(r["f"], set())
            remaining = [c for c in r["cams"] if c not in sent]
            if not remaining:
                continue
            ready, missing = self._ready_angles(folder, remaining)
            expired = time.monotonic() - r["since"] > self._PENDING_TTL_S
            if missing and not expired:
                still_reqs.append(r)
                continue
            if ready and self._send_angles(folder, ready, r["anchor"], r["requester"]):
                for c, _ in ready:
                    sent.add(c)
            if expired and missing:
                api_send_message(self._token, self._chat_id,
                                 tg.t(self._lang, "no_footage",
                                      where=self._label(missing[0])),
                                 reply_to=r["anchor"])
        self._pending_reqs = still_reqs

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
                       when=f"{dvr_time.shift(ev.start_at):%H:%M:%S}")
        if thumb and Path(thumb).is_file():
            result = api_send_photo(self._token, self._chat_id, caption, Path(thumb))
            if result.get("ok"):
                self._map.record(_message_id(result), str(ev.folder), cam, "thumb")
                return
        api_send_message(self._token, self._chat_id, caption)
