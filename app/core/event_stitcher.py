"""Cross-segment event stitching (carry-state).

The recorder rolls a new MP4 every ~15 minutes, so a presence that straddles
a boundary (a car pulling in at 14:14:58, still there at 14:15:03) lands half
in one segment and half in the next. Analysing each segment in isolation would
either lose the tail or emit two unrelated events. This module holds the open
event at a segment's trailing edge and continues it when the next *contiguous*
segment's leading edge also shows presence - so one real moment is one event,
spanning however many segments it touches.

The analyzer feeds finished per-segment scans in; the stitcher decides whether
each scan continues a held chain, flushes held chains, or stands alone, and
calls an injected `writer` to materialize each finalized chain into evidence.
Keeping the policy here keeps the analyzer thread small and makes the logic
unit-testable without Qt or a real detector.

Decisions are driven by wall-clock, never arrival order: segments can be
re-scanned, arrive late, or out of order, and a real coverage gap (recorder
restart, dropped file) must break the chain. State is keyed by camera index.

v0.4.20 layers a presence lifecycle on top: a long continuous presence is
force-split into several chains by the duration cap, but it is ONE presence to
the user - it started once, may ping several times, and ends once. The stitcher
tracks that lifecycle per camera (independent of the per-chain bookkeeping) and
tags each emitted clip started / ongoing / ended / single so the notifier can
say arrived vs still-present vs left.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from app.core import events as evt
from app.core.log import bus


@dataclass
class SegmentScan:
    """The analyzer's findings for one finalized segment.

    `events` are the presence intervals detected within this segment, with
    segment-local offsets. They are pre-merge-gap-grouped by the analyzer but
    NOT yet noise-filtered - a 1-hit tail that continues into the next segment
    must survive to be stitched; `min_hits` is applied to the merged total.
    """

    cam_id: int
    path: "object"               # pathlib.Path
    seg_start: datetime
    duration_s: float
    events: list                 # list[evt.PendingEvent]

    @property
    def end_wall(self) -> datetime:
        return self.seg_start + timedelta(seconds=self.duration_s)


@dataclass
class _Chain:
    cam_id: int
    parts: list = field(default_factory=list)   # list[evt.ChainPart]
    deadline: datetime = datetime.min           # flush if now passes this

    @property
    def last(self):
        return self.parts[-1]

    @property
    def end_wall(self) -> datetime:
        p = self.last
        return p.seg_start + timedelta(seconds=p.ev.t_end)

    @property
    def seg_end_wall(self) -> datetime:
        p = self.last
        return p.seg_start + timedelta(seconds=p.duration_s)

    @property
    def start_wall(self) -> datetime:
        p = self.parts[0]
        return p.seg_start + timedelta(seconds=p.ev.t_start)

    def total_span_s(self) -> float:
        return (self.end_wall - self.start_wall).total_seconds()


@dataclass
class _Presence:
    """A continuous presence on one camera, surviving cap-split re-seeds.

    `start_wall` is the wall-clock the presence first began; `last_end_wall` is
    the wall-clock of the most recent activity seen for it. `emitted` counts how
    many clips this presence has already produced, so the first one can be
    tagged "started" and a final-without-any-prior-emit one "single"."""

    start_wall: datetime
    last_end_wall: datetime
    emitted: int = 0

    def span_s(self) -> float:
        return max(0.0, (self.last_end_wall - self.start_wall).total_seconds())


# Writer signature: (parts, presence_state, presence_seconds) -> EventClip|None.
# The analyzer binds the events_dir / cfg / recording_dir / cam_ids; the stitcher
# supplies the parts plus the presence lifecycle tag for that clip.
Writer = Callable[[list, str, float], object]
Emit = Callable[[object], None]


class EventStitcher:
    """Holds open events across segment boundaries and flushes finished ones."""

    def __init__(
        self,
        cfg: "evt.EventConfig",
        writer: Writer,
        emit: Emit | None = None,
        *,
        max_duration_s: float = 120.0,
        hold_timeout_s: float = 1800.0,
        contig_tolerance_s: float = 0.0,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._cfg = cfg
        self._writer = writer
        self._emit = emit or (lambda _c: None)
        self._max_duration_s = max(1.0, max_duration_s)
        self._hold_timeout_s = max(60.0, hold_timeout_s)
        # How far apart two segments' (prev.end_wall -> next.seg_start) may be
        # and still count as continuous coverage. Cameras are ~1-2s clock-skewed
        # and segments are atclocktime, so the real gap hovers near zero; allow
        # merge_gap plus a few seconds of recorder slack.
        self._contig_tol = cfg.merge_gap_s + 3.0 + max(0.0, contig_tolerance_s)
        self._now = now_fn
        self._held: dict[int, _Chain] = {}
        # Per-camera presence lifecycle, tracked across cap-split re-seeds so a
        # long continuous presence is one started -> ongoing... -> ended story.
        self._presence: dict[int, _Presence] = {}

    # -- public API ---------------------------------------------------------

    def feed(self, scan: SegmentScan) -> list:
        """Process one segment. Returns the list of EventClips finalized as a
        result (may be empty; a continuing presence emits nothing yet)."""
        clips: list = []
        # 1) Time-based flush of any *other* cameras' stale chains.
        clips += self._flush_expired()

        cam = scan.cam_id
        held = self._held.get(cam)

        # 2) If we hold a chain for this camera, decide continue vs. flush.
        if held is not None:
            if self._is_contiguous(held, scan):
                clips += self._continue_chain(held, scan)
                return clips
            # Coverage gap or no leading presence -> the held event is done.
            clips += self._flush_chain(cam, "ended")

        # 3) No (longer any) held chain: turn this scan's events into output,
        #    holding the trailing one if it is still open at the segment edge.
        clips += self._ingest_fresh(scan)
        return clips

    def flush_all(self) -> list:
        """Flush every held chain - call on analyzer stop / app close, before
        the recorder shuts down, so the final tail still becomes an event."""
        clips: list = []
        for cam in list(self._held.keys()):
            clips += self._flush_chain(cam, "ended")
        return clips

    # -- internals ----------------------------------------------------------

    def _new_part(self, scan: SegmentScan, ev) -> "evt.ChainPart":
        return evt.ChainPart(
            path=scan.path, seg_start=scan.seg_start,
            duration_s=scan.duration_s, ev=ev,
        )

    def _deadline_for(self, scan: SegmentScan) -> datetime:
        return self._now() + timedelta(seconds=self._hold_timeout_s)

    def _ends_open(self, scan: SegmentScan, ev) -> bool:
        """True if `ev` is still active within merge_gap of the segment end -
        i.e. presence likely continues into the next segment."""
        gap = scan.duration_s - ev.t_end
        return scan.duration_s > 0 and gap <= self._cfg.merge_gap_s

    def _starts_open(self, scan: SegmentScan, ev) -> bool:
        """True if `ev` begins within merge_gap of the segment start - i.e. it
        likely continues a presence from the previous segment."""
        return ev.t_start <= self._cfg.merge_gap_s

    def _is_contiguous(self, held: _Chain, scan: SegmentScan) -> bool:
        gap = (scan.seg_start - held.seg_end_wall).total_seconds()
        if gap < -2.0 or gap > self._contig_tol:
            return False  # overlap (re-scan/dup) or real coverage gap
        # Held tail must reach its segment edge AND the new segment must open
        # with presence near its start, else it's two separate events.
        if not self._ends_open_chain(held):
            return False
        lead = self._leading_event(scan)
        return lead is not None and self._starts_open(scan, lead)

    def _ends_open_chain(self, held: _Chain) -> bool:
        p = held.last
        return self._ends_open(
            SegmentScan(p.ev.cam_id, p.path, p.seg_start, p.duration_s, []),
            p.ev,
        )

    def _leading_event(self, scan: SegmentScan):
        if not scan.events:
            return None
        return min(scan.events, key=lambda e: e.t_start)

    def _begin_presence(self, cam: int, start: datetime, end: datetime) -> None:
        """Open a new presence for a camera if none is active; otherwise extend
        the existing one's tail. The start_wall of an open presence is never
        moved (it survives cap-split re-seeds)."""
        pres = self._presence.get(cam)
        if pres is None:
            self._presence[cam] = _Presence(start_wall=start, last_end_wall=end)
        else:
            pres.last_end_wall = max(pres.last_end_wall, end)

    def _continue_chain(self, held: _Chain, scan: SegmentScan) -> list:
        clips: list = []
        lead = self._leading_event(scan)
        held.parts.append(self._new_part(scan, lead))
        held.deadline = self._deadline_for(scan)

        # The presence's tail advances as the chain grows, even before a flush.
        self._begin_presence(scan.cam_id, held.start_wall, held.end_wall)

        still_open = self._ends_open(scan, lead) and self._leads_to_edge(scan, lead)
        capped = held.total_span_s() >= self._max_duration_s

        # Other presence intervals in this segment are independent events.
        rest = [e for e in scan.events if e is not lead]

        if capped:
            # A continuously-active presence (parked car) is force-split: flush
            # the capped chain, then if it is still active at the segment edge
            # re-seed a fresh chain from the same trailing interval so the next
            # contiguous segment continues the *new* event, not the closed one.
            # The presence itself is NOT over while still_open, so that flush is
            # "ongoing"; if the cap landed exactly at presence end (no re-seed),
            # this is the final clip -> "ended".
            state = "ongoing" if still_open else "ended"
            clips += self._flush_chain(scan.cam_id, state)
            clips += self._emit_singletons(scan, rest)
            if still_open:
                # Re-seed the chain AND keep the presence alive across the split.
                self._held[scan.cam_id] = _Chain(
                    cam_id=scan.cam_id,
                    parts=[self._new_part(scan, lead)],
                    deadline=self._deadline_for(scan),
                )
                lw = scan.seg_start + timedelta(seconds=lead.t_end)
                self._begin_presence(
                    scan.cam_id,
                    scan.seg_start + timedelta(seconds=lead.t_start),
                    lw,
                )
            return clips

        if still_open:
            # Keep holding the chain; emit the independent intervals now.
            clips += self._emit_singletons(scan, rest)
            return clips

        # Presence ended inside this segment: finalize the chain. Any trailing
        # independent interval that itself ends open re-opens a fresh hold.
        clips += self._flush_chain(scan.cam_id, "ended")
        clips += self._ingest_events(scan, rest, hold_trailing=True)
        return clips

    def _leads_to_edge(self, scan: SegmentScan, lead) -> bool:
        # The leading event is the one we just continued; it keeps the chain
        # open only if no *later* interval supersedes it before the edge. With
        # merge-gap grouping the leading interval already extends to t_end, so
        # being the max-t_end interval means it owns the trailing edge.
        return all(lead.t_end >= e.t_end for e in scan.events)

    def _ingest_fresh(self, scan: SegmentScan) -> list:
        return self._ingest_events(scan, list(scan.events), hold_trailing=True)

    def _emit_singletons(self, scan: SegmentScan, evs: list) -> list:
        """Write each event as its own single-part event, holding none."""
        return self._ingest_events(scan, evs, hold_trailing=False)

    def _ingest_events(self, scan: SegmentScan, evs: list, *,
                       hold_trailing: bool = True) -> list:
        """Emit each event in `evs` as its own (single-part) chain, except the
        trailing one which - if it ends open - is held for stitching."""
        clips: list = []
        if not evs:
            return clips

        trailing = None
        if hold_trailing and evs:
            trailing = max(evs, key=lambda e: e.t_end)
            if not self._ends_open(scan, trailing):
                trailing = None

        for ev in evs:
            if ev is trailing:
                continue
            # A non-held standalone interval starts and ends within this emit:
            # a normal short alert. Its span is just the interval length.
            span = max(0.0, ev.t_end - ev.t_start)
            clip = self._writer([self._new_part(scan, ev)], "single", span)
            if clip is not None:
                self._emit(clip)
                clips.append(clip)

        if trailing is not None:
            self._held[scan.cam_id] = _Chain(
                cam_id=scan.cam_id,
                parts=[self._new_part(scan, trailing)],
                deadline=self._deadline_for(scan),
            )
            # A held trailing interval opens a NEW presence on this camera.
            ts = scan.seg_start + timedelta(seconds=trailing.t_start)
            te = scan.seg_start + timedelta(seconds=trailing.t_end)
            self._begin_presence(scan.cam_id, ts, te)
        return clips

    def _flush_chain(self, cam: int, state: str) -> list:
        """Finalize a held chain. `state` is "ongoing" (cap-split; the presence
        continues and a fresh chain is re-seeded) or "ended" (presence over).

        The first clip a presence ever emits is re-tagged "started" so the user
        gets an arrival alert; a presence that both starts and ends in this one
        flush (never pinged before) is "single". The reported `presence_seconds`
        is always the FULL presence span so far, not just this chain's length."""
        held = self._held.pop(cam, None)
        if held is None:
            return []

        pres = self._presence.get(cam)
        if pres is not None:
            pres.last_end_wall = max(pres.last_end_wall, held.end_wall)
            span = pres.span_s()
            first_emit = pres.emitted == 0
        else:
            span = held.total_span_s()
            first_emit = True

        if state == "ended" and first_emit:
            out_state = "single"   # started and ended without any mid-presence ping
        elif first_emit:
            out_state = "started"  # first ping of a presence that keeps going
        else:
            out_state = state      # "ongoing" continuation or final "ended"

        clip = self._writer(list(held.parts), out_state, span)

        if pres is not None:
            if state == "ended":
                self._presence.pop(cam, None)
            else:
                pres.emitted += 1

        if clip is None:
            return []
        self._emit(clip)
        if len(held.parts) > 1:
            bus.info("EVT", f"cam{cam}: stitched {len(held.parts)} segments into one event")
        return [clip]

    def _flush_expired(self) -> list:
        now = self._now()
        clips: list = []
        for cam, held in list(self._held.items()):
            if now >= held.deadline:
                clips += self._flush_chain(cam, "ended")
        return clips
