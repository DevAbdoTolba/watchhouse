"""Fire-and-forget Telegram send tasks.

Each task is one alert / quick-clip / collision send. TelegramNotifier
queues them on its ordered priority sender (never the UI thread). When a
TelegramMap + event folder are supplied, the sent message_id is recorded
so a later reply or button tap can drill into the clip(s).
"""

from __future__ import annotations

import urllib.error
from pathlib import Path

from PySide6.QtCore import QRunnable

from app.core import telegram_text as tg
from app.core.log import bus
from app.core.telegram_api import (
    alert_markup,
    api_send_message,
    api_send_photo,
    api_send_video,
    message_id,
)


class _SendTask(QRunnable):
    """One fire-and-forget alert send. When a map + event folder are supplied,
    the sent photo's message_id is recorded so a later reply can drill into
    the clip(s)."""

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
                        self._map.record(message_id(result), self._folder,
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
                    self._map.record(message_id(result), self._folder,
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
                self._map.record(message_id(result), self._from_folder,
                                 self._cam_from, "collision",
                                 extra={"f2": self._to_folder, "c2": self._cam_to})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
            bus.warn("TG", f"collision send failed: HTTP {e.code} {body}")
        except Exception as e:
            bus.warn("TG", f"collision send failed: {e!s}")
