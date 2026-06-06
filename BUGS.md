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

### BUG-003 — Live alert photo delayed ~30 s (should be instant)
- **Symptom (06/06):** the live-tier image arrives ~30 s after the movement.
- **Status:** ADDRESSED v0.4.64 — confirm on the real system.
- **Root cause (most likely):** send contention. Telegram sends ran on the global
  QThreadPool alongside the live detector's `_InferTask`; a slow video upload
  occupied pool threads, so live inference (and the photo send behind it) stalled.
- **Fix:** sends moved OFF the global pool onto a dedicated ordered worker
  (BUG-005), freeing the pool for live inference; and the sender is a *priority*
  queue so an instant photo never waits behind a video upload. If 30 s latency
  persists, the remaining suspect is yolov8n CPU contention with the segment
  analyzer — next step would be giving live inference its own throttle/priority.

### BUG-005 — Duplicates, latency, and out-of-order Telegram messages
- **Symptom (06/06):** messages duplicated, late, and not in chronological order.
- **Status:** PARTIAL v0.4.64 — out-of-order FIXED, latency REDUCED; duplicates
  still under observation.
- **Fixes applied:**
  1. **Out-of-order → fixed:** sends were separate `QRunnable`s on the *global*
     QThreadPool (parallel, arbitrary completion order). Now ONE ordered worker
     thread drains a FIFO queue, so messages go out in the order produced
     (`notifier._send_loop`/`_enqueue`; verified FIFO under a slow-first send).
  2. **Latency → reduced:** collision matcher grace cut 45 s → 15 s (siblings are
     analyzed back-to-back, so the long hold was unnecessary).
- **Still open — duplicates:** not reproduced/confirmed. Candidate sources to
  watch: live cooldown re-firing for a lingering person (≈ every 45 s, partly by
  design) and live pings + the collision album for one crossing reading as
  "duplicate". Next step: log every send with its event key and get one concrete
  duplicate example (two sends, same folder/epoch) to pin the cause.

### BUG-006 — Message time ≠ recorder time (drift up to ~20 s)
- **Symptom (06/06):** the Telegram message time differs from the burned-in
  recording time by up to 20 s (this is on TOP of the 1 h DST gap, BUG-007).
- **Status:** PARTIAL — the 1 h DST component is fixed (BUG-007); the residual
  ~20 s is PC↔DVR clock drift, NOT something the app can correct cleanly.
- **Fix / recommendation:** the displayed time is now the event's true frame
  wall-clock (segment-start + offset), shifted by DVR_TIME_OFFSET. The remaining
  seconds-level gap is the two devices' clocks drifting — point both at NTP, or
  nudge DVR_TIME_OFFSET_MINUTES. No further code change planned.

---

## Fixed

### BUG-007 — DVR ignores DST → all times 1 h off the DVR overlay
- **Symptom (06/06):** message says 16:00 but the DVR shows 15:00; the DVR clock
  doesn't observe summer time, so every app time ran 1 h ahead of the footage.
- **Status:** FIXED v0.4.64.
- **Fix:** new `dvr_time` module + `DVR_TIME_OFFSET_MINUTES` (set it to -60).
  A pure DISPLAY shift applied to EVERY shown time — Telegram captions, the
  events list, the timeline axis, the cursor/status clocks — so the whole app
  reads in DVR time. Internals (recording filenames, segment lookup, pruning,
  timeline positioning, seeking) stay on the raw PC clock, so nothing breaks.
  Wired from Settings at startup. Verified: shift / seconds-of-day / midnight
  wrap / passthrough unit checks.

### BUG-004 — Quick clip cropped before the movement ends (< 30 s)
- **Symptom (06/06):** the clip cuts off while the person is still moving, even
  though it's under the 30 s cap.
- **Status:** FIXED v0.4.64.
- **Root cause:** the dynamic clip closed `post_roll` (LIVE_POST_ROLL_SECONDS,
  default **3 s**) after the *last* sighting. A brief detection gap (pause,
  occlusion, flicker > 3 s) tripped the close early; the 30 s cap was never the
  limiter.
- **Fix:** raised the default grace 3 → 7 s so brief pauses/flicker no longer cut
  the clip (still capped at LIVE_MAX_CLIP_SECONDS). Tunable via
  LIVE_POST_ROLL_SECONDS.

### BUG-008 — False positives: rubbish detected as "person" (URGENT)
- **Symptom (06/06):** ~40 images of noise/clutter flagged as a person, plus a
  fresh false person detection. yolov8n on a grainy night DVR sub-stream invents
  people.
- **Status:** MITIGATED v0.4.63 — two configurable guards added; effectiveness on
  the user's real night footage still to be confirmed (tunable).
- **Root cause:** the only gate was a single confidence threshold (0.35 batch /
  0.40 live) over all classes. The 'person' class is the noisiest, and tiny
  specks pass when confidence is moderate.
- **Fix:** in `detect.py`, two post-detection filters (apply to both tiers):
  `DETECTION_PERSON_CONF` (default 0.55) — a higher confidence floor JUST for
  'person' (vehicles keep DETECTION_CONF); and `DETECTION_MIN_BOX_FRAC` (default
  0.07) — drop boxes smaller than 7% of the frame's larger side. Wired through
  config → analyzer + live_detector. Both default-off in the Detector itself
  (config supplies the active defaults), so it's fully tunable from `.env`.
- **Verified:** 5/5 filter unit checks (low-conf person dropped, high-conf kept,
  tiny speck dropped, vehicle ignores the person floor, defaults-disabled passes).
- **If still noisy:** raise DETECTION_PERSON_CONF toward 0.6–0.7; if real people
  get missed, lower it / lower DETECTION_MIN_BOX_FRAC.

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
