"""CollisionMatcher — fuse the SAME MOVEMENT seen by two cameras into one event.

Per-camera events are extracted independently, so one person crossing a doorway
fires on both the inside and outside camera. This matcher buffers freshly
extracted events for a short grace window and, using the hand-taught
`camera_links`, decides when two of them are really ONE crossing:

  same movement  ⇔  the events sit on a link's two cameras
                    AND one EXITS the link edge while the other ENTERS its edge
                    AND the second starts within the link's transit window.

No person identity (no Re-ID) — purely time + which frame edge + direction.

A match calls `on_collision(link, direction_label, from_clip, to_clip)`; an event
that finds no partner before its grace expires is released via `on_single(clip)`
(the normal per-camera notification path). Cameras that take part in no link are
passed straight through with no delay.

Lives on the UI thread (fed from the analyzer's event_extracted signal); a
periodic `sweep()` releases timed-out singles and `flush_all()` drains on close.
"""

from __future__ import annotations

import time

from app.core import camera_links as cl
from app.core.log import bus

# How long to hold a link-eligible event waiting for its partner from the other
# camera. Segments are clock-aligned so siblings are analyzed back-to-back; this
# only delays the (already minutes-late) segment notification, never the live tier.
_GRACE_S = 45.0
# Timing slack around the transit window when matching two events by start time.
_SLACK_BEFORE_S = 3.0   # the 'to' event may start slightly before the 'from'
_MARGIN_AFTER_S = 6.0   # ...or a bit after the nominal transit time


def _start_epoch(clip):
    when = getattr(clip, "start_at", None)
    if when is None:
        return None
    try:
        return when.timestamp()
    except (AttributeError, ValueError, OverflowError, OSError):
        return None


class CollisionMatcher:
    def __init__(self, links, on_collision, on_single, grace_s: float = _GRACE_S):
        self._links = list(links or [])
        self._cams = cl.cams_in_links(self._links)
        self._on_collision = on_collision
        self._on_single = on_single
        self._grace = grace_s
        # buffered, awaiting a partner: list of [clip, deadline_monotonic]
        self._pending: list[list] = []

    def set_links(self, links) -> None:
        self._links = list(links or [])
        self._cams = cl.cams_in_links(self._links)

    def feed(self, clip) -> None:
        """Route one freshly extracted event: fuse with a waiting partner, hold
        it for a partner, or pass it straight through."""
        for i, (other, _deadline) in enumerate(self._pending):
            m = self._match(other, clip)
            if m is not None:
                del self._pending[i]
                link, direction, frm, to = m
                bus.info("LINK", f"collision: {link.name} — {direction} "
                                 f"(cam{frm.cam_id} -> cam{to.cam_id})")
                self._on_collision(link, direction, frm, to)
                return
        cam = getattr(clip, "cam_id", 0)
        if cam in self._cams:
            self._pending.append([clip, time.monotonic() + self._grace])
        else:
            self._on_single(clip)

    def sweep(self) -> None:
        """Release any buffered event whose grace has expired (no partner came)."""
        if not self._pending:
            return
        now = time.monotonic()
        keep: list[list] = []
        for entry in self._pending:
            clip, deadline = entry
            if now >= deadline:
                self._on_single(clip)
            else:
                keep.append(entry)
        self._pending = keep

    def flush_all(self) -> None:
        """Drain every buffered event as a single (called on shutdown)."""
        for clip, _ in self._pending:
            self._on_single(clip)
        self._pending = []

    def _match(self, x, y):
        """If x and y are the same movement across a link, return
        (link, direction_label, from_clip, to_clip); else None."""
        cx, cy = getattr(x, "cam_id", 0), getattr(y, "cam_id", 0)
        if cx == cy:
            return None
        for link in self._links:
            pair = {link.cam_a, link.cam_b}
            if {cx, cy} != pair:
                continue
            by_cam = {cx: x, cy: y}
            a, b = by_cam[link.cam_a], by_cam[link.cam_b]
            # A -> B: a exits its edge, b enters its edge.
            if self._fits(a, b, link.edge_a, link.edge_b, link.transit_s):
                return (link, link.label_ab, a, b)
            # B -> A: b exits its edge, a enters its edge.
            if self._fits(b, a, link.edge_b, link.edge_a, link.transit_s):
                return (link, link.label_ba, b, a)
        return None

    @staticmethod
    def _fits(frm, to, exit_edge, entry_edge, transit_s) -> bool:
        if exit_edge == "none" or entry_edge == "none":
            return False
        if getattr(frm, "exit_edge", "none") != exit_edge:
            return False
        if getattr(to, "entry_edge", "none") != entry_edge:
            return False
        e_from, e_to = _start_epoch(frm), _start_epoch(to)
        if e_from is None or e_to is None:
            return False
        dt = e_to - e_from
        return -_SLACK_BEFORE_S <= dt <= transit_s + _MARGIN_AFTER_S
