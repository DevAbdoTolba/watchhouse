# Watchhouse — Bug Tracker

A running log of caught bugs — fixed or not, cause known or not. The point is a
clear shared view of what's flaky so nothing gets silently forgotten. When a bug
is understood and fixed, move it to **Fixed** (keep the entry + the fix/commit).

Each entry: symptom → status → suspected cause(s) → next step. Suspicions are
**hypotheses, not confirmed** until verified.

---

## Open

### BUG-009 — Detection silently stopped for ~16 h (app died, ffmpeg orphaned)
- **Symptom (06/07):** "no detection history, nothing detected — threshold too
  high." In fact ZERO events from 06/06 23:30 through 06/07 ~15:46.
- **Status:** GUARDED (v0.4.70 adds an auto-restart watchdog) + VISIBLE (v0.4.66
  detection log). Root cause of the death itself still unconfirmed, but it now
  self-heals + self-reports instead of staying silently dead. Re-confirmed on
  06/08: a 46-min dead window 01:21–02:07 AM (heartbeat would have caught it).
- **What actually happened:** the Watchhouse app process was not running (no
  Watchhouse/large-python process), yet recording segments kept appearing — the
  recorder's **ffmpeg child processes had orphaned** and kept writing after the
  app went down (~23:31, when v0.4.65 was rebuilt/relaunched). With the app dead,
  the analyzer never ran → no events. The threshold was never the cause: the
  v0.4.65 analyzer code runs clean (verified, no crash) and a real person was
  caught at 0.83 the night before. cam2 (which has NO per-camera floor) also had
  zero events — proof the analyzer, not the floor, was the issue.
- **Fix shipped:** persistent **detection log** `recordings/detections.log`
  (app/core/detlog.py) — one line per analyzed segment with the person
  confidences KEPT, dropped by the per-camera floor, and dropped by the global
  guards, plus events written. Now: no lines = app/analyzer down; `kept=[]` with
  `drop_camfloor=[0.7,…]` = threshold too high; all-empty = model saw nothing.
  Restarted the app; log confirmed filling (cam4 kept=[0.81], cam2 empty).
- **Fix shipped (v0.4.70):** an external **watchdog** (`app/core/watchdog.py`),
  the SAME exe run detached as `Watchhouse.exe --watchdog`, spawned by the app at
  startup. The app touches a heartbeat file every 15 s; the watchdog relaunches
  it (from the .env folder, so the right config/language reloads) + pings Telegram
  when the heartbeat goes stale > `WATCHDOG_IDLE_MINUTES` (default 2). It kills
  orphaned ffmpeg before relaunch, has a crash-loop back-off (5×/30 min → pause +
  alert), steps aside on a clean quit (shutdown marker), and is toggleable live
  from System menu → "Auto-restart watchdog" (persisted).
- **Next step:** observe the watchdog.log over a few days to finally catch the
  underlying death cause; consider an "analyzer idle" signal (heartbeat tied to
  segment progress, not just the UI thread) for the hang-but-process-alive case.

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

### BUG-002 — "Other angles" need a manual re-tap + arrive time-shifted
- **Symptom (06/07):** tapped "all cameras" → "لا يوجد فيديو لـ … في ذلك الوقت"
  (no video at that time) for all 3 siblings. Tapped again manually a bit later →
  they arrived, but shifted ~2 s AFTER the detection moment.
- **Status:** FIXED v0.4.67. Two bugs, both confirmed:
  1. **Manual re-tap:** the sibling angle is cut on demand from `recordings/camN`
     over the event window. If that camera's covering segment isn't FINALIZED yet
     (still being written, no moov), `_cut_clip` fails → it errored "no video"
     and gave up. The user had to tap again after the segment closed.
  2. **+2 s shift:** the cut started at only `start_at − pre_roll` (pre_roll = the
     2 s live value). Cross-camera segment-start skew (~1 s) + stream-copy keyframe
     snapping pushed the angle PAST the detection moment.
- **Fix:**
  1. A sibling cut that fails now returns 'retry' (not an error): the request is
     queued (`_pending_cuts`) and auto-retried every poll cycle until the segment
     finalizes, then auto-delivered — one "preparing…" message, no re-tap.
     `_send_clip` returns sent/retry/failed; `_check_pending` drains the queue;
     TTL 20 min then a single "no video" if it truly never appears.
  2. Sibling cuts now start `pre_roll + 3 s` (`_SIBLING_LEAD_S`) BEFORE the
     detection and extend the duration by the same, so the moment is always
     inside the clip with margin — never shifted past it.
- **Verified:** offscreen — detecting cam sent immediately, 3 siblings queued +
  one "preparing", all four auto-delivered after segments finalize, queue
  drained; cut offset/duration use the extra lead.

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
- **Status:** FIXED v0.4.65 — confirmed against real footage. (v0.4.63's global
  guards were insufficient: this bag is a *confident* static FP, not low-conf.)
- **What actually solved it — per-camera 'person' floor:** measured cam4's bin
  bags over 168 events: the bag peaks ~0.76 while real people there hit 0.80+ in
  totally different boxes (IoU~0). A per-camera floor `DETECTION_PERSON_CONF_BY_CAM`
  (set `4:0.78`) applied in BOTH tiers. Replay over the day's real events: **158
  bag detections killed, all 14 real-people events (0.80–0.96) survive.** Other
  cameras keep the normal 0.55 floor so distant people aren't lost.
  Lesson: a confident static false positive can't be beaten by a *global* number
  or box-size — measure ITS ceiling and cap that one camera just above it.
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
