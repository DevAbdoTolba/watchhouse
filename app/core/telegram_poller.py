"""Telegram reply / button listener: one long-poll QThread.

Owned and lifecycled by TelegramNotifier (started when commands are enabled,
torn down on reconfigure/shutdown). Everything here runs on the poller's own
thread; sends are direct blocking API calls because replies are interactive
and already off the UI thread.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QThread

from app.core import dvr_time
from app.core import telegram_text as tg
from app.core.log import bus
from app.core.telegram_api import (
    API_URL,
    TIMEOUT_S,
    api_answer_callback,
    api_send_media_group,
    api_send_message,
    api_send_photo,
    api_send_video,
    message_id,
)

_POLL_TIMEOUT_S = 10  # getUpdates long-poll; bounds shutdown latency


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
        url = API_URL.format(token=self._token, method=method)
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
            result = self._api("getUpdates", {"timeout": 0}, TIMEOUT_S)
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
                self._map.record(message_id(r), fld, cam, "video")
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
                    mid = message_id({"result": m})
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
                self._map.record(message_id(r), str(folder), cam, "video")
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
                    mid = message_id({"result": m})
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
                self._map.record(message_id(result), str(ev.folder), cam, "thumb")
                return
        api_send_message(self._token, self._chat_id, caption)
