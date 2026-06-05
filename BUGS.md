# Watchhouse — Bug Tracker

A running log of caught bugs — fixed or not, cause known or not. The point is a
clear shared view of what's flaky so nothing gets silently forgotten. When a bug
is understood and fixed, move it to **Fixed** (keep the entry + the fix/commit).

Each entry: symptom → status → suspected cause(s) → next step. Suspicions are
**hypotheses, not confirmed** until verified.

---

## Open

### BUG-002 — "Other angles" never sent, no matter how long I wait
- **Symptom:** reply to a clip to get the other camera angles → they never
  arrive, even after waiting well past the segment-close delay.
- **Status:** OPEN — cause unconfirmed.
- **Suspected causes (hypotheses):**
  1. The sibling cut fails and the failure isn't retried/visible: `_send_clip` →
     `_cut_sibling_clip` cuts the other angles on demand from `recordings/camN`
     via `_segment_covering` + `_cut_clip`. If the covering segment can't be
     found, or the computed `offset` is out of range (the `> 17*60` guard), it
     returns False and the angle is never marked sent — but the user may not see
     a clear "couldn't cut" message.
  2. Timestamp mismatch for LIVE events: a live quick-clip event's `event.json`
     `start_at` (wall clock at detection) vs the recording segment boundaries —
     if the window_start lands just outside a segment, `_segment_covering`
     returns the wrong/no segment.
  3. Progressive-delivery state (`_sent_cams`) marking an angle as sent even
     though the cut failed → it's skipped on later replies ("no more angles").
     (Need to confirm: `_send_clip` returns False on failure and the caller only
     marks on True — but double-check the live-resolve path.)
- **Next step:** log each `_cut_sibling_clip` attempt (cam, window_start,
  segment found?, offset, ffmpeg rc) so we can see exactly where it bails.

---

## Fixed

### BUG-001 — Telegram alert sent twice (≈300 pushes/day for ~150 events)
- **Symptom:** every event produced two pushes — an instant one with the correct
  time, then a second ~2–3 min later showing the *same old time* and a detection
  *outside* the per-camera detect box. ~2× the notifications for the real event
  count.
- **Status:** FIXED v0.4.61 (commit on `main`). Hypothesis #1 was correct, and
  it was systematic (every event), not intermittent.
- **Root cause:** two independent producers notified for one moment — the live
  tier (`notify_live`, instant, box-filtered) **and** the segment/evidence tier
  (`notify` via `_on_event_extracted`, ~segment-length late, reusing the event's
  original `start_at` = the "same old time"). The segment tier *never applied the
  detect box*, so it also pushed the very detections the box was meant to exclude
  (the out-of-box image). Dedupe alone couldn't catch those — they have no live
  alert to dedupe against (the live tier already filtered them out), so the box
  filter had to extend to the segment tier too.
- **Fix (two parts):**
  1. **Box now gates notifications on both tiers.** The analyzer tags each event
     with `in_region` (any sampled detection overlapping the camera's box; True
     when no box). Recording stays full-frame — `in_region` only gates the push.
     The notifier skips out-of-box clips (`events.py`, `analyzer.py`,
     `main_window` wires `set_regions` to the analyzer).
  2. **Smart cross-tier dedupe.** `notify_live` records the alert time per camera;
     `notify` skips an arrival (`started`/`single`) when a live alert fired within
     60 s of the event's start. An arrival the live tier *missed* still sends as a
     safety net. `ongoing`/`ended` pings are never deduped. Window kept under the
     2-min leave/return threshold so a genuine re-entry isn't swallowed.
- **Verified:** 11/11 offscreen checks through real `notify`/`notify_live`
  (box gate, dedupe window edges, safety-net, ongoing/ended exemption) + event
  dataclass flag-threading checks.
