"""Telegram push notifications for extracted events.

When the SegmentAnalyzer extracts an event, MainWindow hands the EventClip
here; if a bot token + chat id are configured we POST the peak-frame
thumbnail (with a short caption) to a Telegram chat so the family gets an
off-device alert. Everything is best-effort and fully optional:

- No token/chat configured  -> silent no-op (logged once at startup).
- Network/API failure       -> logged as a warning, never raised; the UI
  thread is never blocked because every send runs on a dedicated ordered
  sender thread, not the GUI thread.
- Debounced per trigger camera so a lingering person can't spam the chat
  (the segment-tier presence merge already collapses most of this, this is
  a second guard).

The module family: `telegram_api` (pure Bot API client), `telegram_tasks`
(the QRunnable sends this notifier queues), `telegram_poller` (the reply /
button listener this notifier owns), `telegram_map` (message -> event).
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject

from app.core import dvr_time
from app.core import telegram_text as tg
from app.core.log import bus
from app.core.telegram_api import alert_markup, mask_token
from app.core.telegram_map import TelegramMap
from app.core.telegram_poller import TelegramPoller
from app.core.telegram_tasks import _CollisionTask, _QuickClipTask, _SendTask


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
            bus.info("TG", f"Telegram alerts enabled (bot {mask_token(self._token)}, chat {self._chat_id})")
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
            bus.info("TG", f"Telegram reconfigured (bot {mask_token(self._token)}, chat {self._chat_id})")
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
