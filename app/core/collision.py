"""CollisionMatcher — fuse the SAME MOVEMENT seen by two cameras into one event.

Per-camera events are extracted independently, so one person crossing a doorway
fires on both the inside and outside camera. This matcher buffers freshly
extracted events for a short grace window and, using the hand-taught
`camera_links`, decides when two of them are really ONE crossing:

  same movement  ⇔  the events sit on a link's two cameras
                    AND both start within the same-event window (~6s).
                    The exit/entry edges only choose which DIRECTION to show;
                    they never block a fuse (an ambiguous edge still fuses).

No person identity (no Re-ID) — purely time + the link + (for the label) edge.

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
# camera. Segments are clock-aligned so siblings are analyzed back-to-back (a few
# seconds apart), so a short hold suffices; keep it tight to avoid adding latency.
_GRACE_S = 12.0
# Two events on a linked camera pair whose START times fall within this window are
# the SAME crossing. This replaces the old per-link transit time, which dropped
# real crossings whenever the travel-time guess was off — the family thinks in
# terms of "around the same moment", so the matcher does too.
_SAME_EVENT_WINDOW_S = 6.0


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
        """If x and y are the same crossing across a link, return
        (link, direction_label, from_clip, to_clip); else None.

        Fusion is by TIME (the same-event window) + the link's camera pair. The
        exit/entry edges only choose which DIRECTION label to show; ambiguous
        edges still fuse (earliest event first), so a real crossing is never
        dropped just because the edges didn't line up."""
        cx, cy = getattr(x, "cam_id", 0), getattr(y, "cam_id", 0)
        if cx == cy:
            return None
        ex, ey = _start_epoch(x), _start_epoch(y)
        if ex is None or ey is None:
            return None
        if abs(ex - ey) > _SAME_EVENT_WINDOW_S:
            return None
        for link in self._links:
            if {cx, cy} != {link.cam_a, link.cam_b}:
                continue
            by_cam = {cx: x, cy: y}
            a, b = by_cam[link.cam_a], by_cam[link.cam_b]
            # A -> B: a exits its edge, b enters its edge.
            if self._edges_fit(a, b, link.edge_a, link.edge_b):
                return (link, link.label_ab, a, b)
            # B -> A: b exits its edge, a enters its edge.
            if self._edges_fit(b, a, link.edge_b, link.edge_a):
                return (link, link.label_ba, b, a)
            # Same moment on the linked pair, edges ambiguous: still ONE crossing.
            if ex <= ey:
                return (link, link.label_ab, x, y)
            return (link, link.label_ab, y, x)
        return None

    @staticmethod
    def _edges_fit(frm, to, exit_edge, entry_edge) -> bool:
        """True when frm exits exit_edge and to enters entry_edge (for the
        direction label only). 'none' edges never line up."""
        if exit_edge == "none" or entry_edge == "none":
            return False
        return (getattr(frm, "exit_edge", "none") == exit_edge
                and getattr(to, "entry_edge", "none") == entry_edge)
