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
- **Symptom (06/06):** the live-tier image arrives ~30 s after the movement,
  not in real time.
- **Status:** OPEN — cause unconfirmed.
- **Suspected causes (hypotheses):**
  1. CPU contention: the live detector and the segment analyzer both run yolov8n
     on CPU. While the analyzer is grinding a closed segment, the live
     `_busy`-gated inference starves and live frames back up → tens of seconds of
     lag. (Live tier shares the machine, not a queue, with the batch tier.)
  2. Send latency / pool backlog (see BUG-005): the alert is *produced* fast but
     the HTTP send waits behind other pooled tasks.
- **Next step:** timestamp the live path end-to-end (frame grab → detect done →
  emit → send) and log the gaps; consider giving the live detector inference
  priority / its own throttle independent of analyzer load.

### BUG-004 — Quick clip cropped before the movement ends (< 30 s)
- **Symptom (06/06):** the clip cuts off while the person is still moving, even
  though it's under the 30 s cap.
- **Status:** OPEN — likely cause known.
- **Suspected cause:** the dynamic clip closes `post_roll` (LIVE_POST_ROLL_SECONDS,
  default **3 s**) after the *last* sighting. A brief gap in detection (person
  pauses, occluded, or a missed frame > 3 s) trips the close before they're
  really gone. The cap (30 s) isn't the limiter — the short grace is.
- **Next step:** raise the default grace (e.g. 3 → 6–8 s) and/or close only after
  N consecutive empty samples rather than a single gap; confirm against a real
  pause-mid-walk clip.

### BUG-005 — Duplicates, latency, and out-of-order Telegram messages
- **Symptom (06/06):** messages duplicated, late, and not in chronological order.
- **Status:** OPEN — cause unconfirmed; possible regression from recent work.
- **Suspected causes (hypotheses):**
  1. **Out-of-order:** every send is a separate `QRunnable` on the *global*
     QThreadPool (parallel). They finish in arbitrary order → messages land out
     of sequence. Fix direction: serialize sends through one ordered worker.
  2. **Latency:** the new collision matcher holds link-eligible events up to
     `grace_s` (45 s) before releasing/fusing — adds delay for linked cameras.
     Also pool backlog under load.
  3. **Duplicates:** re-check the dedupe interplay with the collision path and
     any double `segment_closed` (filesystem-watcher race) re-extracting events.
- **Next step:** add a single ordered send queue (preserve order, dedupe key on
  it); reconsider the 45 s collision grace; log every send with its event key.

### BUG-006 — Message time ≠ recorder time (drift up to ~20 s)
- **Symptom (06/06):** the time in the Telegram message differs from the time
  burned into the recording by up to 20 s.
- **Status:** OPEN — cause unconfirmed (separate from the 1 h DST gap, BUG-007).
- **Suspected causes (hypotheses):**
  1. PC clock vs DVR clock drift (no NTP sync between them).
  2. The message shows the event/processing time, not the exact frame time;
     segment-start + offset rounding could add seconds.
- **Next step:** decide the single source of truth for "when" (frame wall-clock
  from the segment), and measure PC↔DVR drift.

### BUG-007 — DVR ignores DST → times 1 h off the DVR overlay
- **Symptom (06/06):** message says 16:00 but the DVR shows 15:00. The DVR clock
  does not observe summer time; our timestamps (system local time, with DST) are
  1 h ahead of the DVR's burned-in time.
- **Status:** OPEN — fix approach needs a decision.
- **Fix options:** (a) a configurable `DVR_TIME_OFFSET` (minutes/hours) applied to
  every displayed/messaged timestamp so they match the DVR; (b) auto-detect the
  offset from the DVR. (a) is simple + robust; (b) is fragile. Leaning (a).
- **Next step:** confirm offset approach with the user, then apply the offset at
  the point timestamps are formatted for messages/UI (keep filenames as-is or
  shift consistently — decide which clock owns the recordings).

---

## Fixed

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
