"""Thin Telegram Bot API client (stdlib urllib only).

Pure request/response helpers shared by the send tasks, the notifier, the
reply poller, the watchdog's down-alert, and the setup dialog's Detect /
Test buttons. No Qt and no app state: every function takes the token/chat
explicitly and returns the parsed API result dict, so each caller decides
its own threading and error policy.

Uses only the standard library (urllib) so the single-file build gains no
new dependency. Never log a raw token — use mask_token().
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app.core import telegram_text as tg

API_URL = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_S = 20
_UPLOAD_TIMEOUT_S = 120          # clip uploads are larger / slower than a photo
_MAX_UPLOAD_BYTES = 49 * 1024 * 1024  # Telegram bot send cap is 50 MB; stay under


def _call(token: str, method: str, payload: dict) -> dict:
    """Blocking JSON Bot API call. Used by the setup dialog's Detect / Test
    buttons (short, user-initiated) - NOT on the event hot path."""
    url = API_URL.format(token=token.strip(), method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
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


def mask_token(token: str) -> str:
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
    url = API_URL.format(token=token.strip(), method=method)
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
    url = API_URL.format(token=token.strip(), method="sendMediaGroup")
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


def message_id(result: dict) -> int | None:
    """The sent message's id from an API result, or None."""
    try:
        return int(result.get("result", {}).get("message_id"))
    except (TypeError, ValueError):
        return None
