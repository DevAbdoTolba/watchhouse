# Watchhouse — Bug Tracker

A running log of caught bugs — fixed or not, cause known or not. The point is a
clear shared view of what's flaky so nothing gets silently forgotten. When a bug
is understood and fixed, move it to **Fixed** (keep the entry + the fix/commit).

Each entry: symptom → status → suspected cause(s) → next step. Suspicions are
**hypotheses, not confirmed** until verified.

---

## Open

### BUG-001 — Telegram alert occasionally sent twice
- **Symptom:** the same alert/photo sometimes arrives in Telegram twice.
- **Status:** OPEN — cause unconfirmed (intermittent, hard to reproduce).
- **Suspected causes (hypotheses):**
  1. Two notification paths firing for one moment: the instant LIVE photo
     (`notify_live`) **and** the later segment EVENT alert (`notify`) — these are
     by design two different messages, but for a single subject they can *look*
     like a duplicate. Need to confirm whether the two are truly identical or
     just close in time.
  2. A segment processed twice: `RecorderSupervisor._scan_for_closed_segments`
     could emit `segment_closed` for the same file more than once (filesystem
     watcher race), leading the analyzer to extract + notify the same event
     twice.
  3. Poller `getUpdates` offset handling re-delivering an update (would double a
     *reply*, not the initial alert).
- **Next step:** add a debug log tag on every Telegram send (path + event folder
  + timestamp) and watch for two sends with the same folder/epoch. Decide on a
  short dedupe window keyed by (event-folder | cam+epoch) once the path is known.

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

_(move resolved bugs here, keeping the entry + the fix commit)_
