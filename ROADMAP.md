# Watchhouse - Roadmap / Future Work

Living backlog of planned features. Shipped increments live in git tags;
this file is only what's *not* done yet. Newest ideas at the top of each
section.

## Next up

### Quick-clip tier + reply-wait machinery (phase 2 of real-time)
The instant photo alert shipped (v0.4.35 live tier). Phase 2 = get a short
video to the phone seconds after the photo, without the 15-min wait.

**Three-tier model (decided):**
1. ⚡ Live tier (DONE v0.4.35): instant photo + title off live frames.
2. 🎬 Quick clip (THIS): a short ~10 s video, seconds after the photo.
3. 🐢 Evidence tier (unchanged): 15-min segments → full event clips w/ margins.

**Why NOT 10-second recording segments** (the rejected approach): 4 cams ×
90-min window ÷ 10 s = ~2,160 files always on disk + the analyzer waking every
10 s per cam (CPU thrash, worse on `main`). And "read the unfinished clip"
does not work with our mp4s — the moov index is written only on close
(`+faststart`), so an open segment isn't readable until it finalizes. So tiny
segments give neither instant video nor a free lunch.

**Better source for the quick clip:** the live tier already holds decoded
frames. Keep a small rolling buffer (e.g. last ~10 s at ~6 fps) per armed
camera; on a live trigger, encode that buffer to mp4 (cv2.VideoWriter) — gives
pre-roll + a real ~10 s clip, no extra files, no open-mp4 problem. (Alternative
if we want true recorded quality: switch the recorder to fragmented mp4
`+frag_keyframe+empty_moov` so the open segment IS readable, then cut from it.
More invasive; revisit only if the buffered-frame clip looks too low-quality.)

**Reply-wait machinery (on top of the quick clip):**
- The instant photo is sent immediately and is replyable.
- If the user replies *before* the clip is ready: reply "⏳ clip is being
  prepared, ~30 s — I'll send it the moment it's done", set a pending flag
  keyed to that event, and auto-send the clip when encoding finishes.
- If they reply *after* it's ready: send immediately (no wait).
- Same pattern for "other angles": if they reply to a clip while the sibling
  angles are still encoding, hold + auto-send when ready.
- Needs a small pending-request table in telegram_map (event -> chat msg
  awaiting a not-yet-ready clip) and a "clip ready" callback from the encoder.

### Notification latency — segment tier (secondary, after the live tier)
The live tier (v0.4.35) handles "someone is here NOW". The segment/evidence
tier is still 0–15 min late by design (a segment is only analyzed after it
closes; default `RECORDING_SEGMENT_MINUTES=3` as of v0.4.47, down from 15, so
the 4-cam evidence event and "other angles" reply land in ~3 min worst-case). But
if we want the *clip* sooner too, options cheapest-first:
- **Shorter segments** (e.g. 3–5 min) — one-line config; more files + passes.
- **Analyze the open segment on a timer** — needs fragmented-mp4 recording to
  read the in-progress file (see quick-clip note above).
Note: the 90-min window and 15-min segment were never tuned scientifically.
15-min = a comfortable buffer; segment length and analysis time are INDEPENDENT
(a short segment does not rush analysis — face/name passes can take as long as
they need on a closed clip). So future face/name work argues for a slow second
pass, not bigger segments.

### Timeline overlay: mark detection spans on the scrub/timeline bar
In playback, paint the regions where bounding boxes exist directly onto the
timeline/scrub bar (e.g. tick marks or a coloured band over the seconds that
have detections), so the user can see at a glance *where in the clip* the
activity is and jump straight to it — instead of scrubbing blindly. Source data
already exists: each event's `tracks` (clip-relative `t` per box set) drives the
overlay today; reuse those timestamps to shade the bar. Pairs with the existing
double-click-to-reset-zoom timeline.

### Telegram bot as a remote control panel
Let the bot configure/operate the system, not just receive alerts. Commands
gated to the linked chat id (same security model as today). Wanted:
- **Arm/disarm live detection per camera** - start or stop event detection on a
  specific camera (or all) from chat (e.g. `/arm cam3`, `/disarm cam1`). Mirrors
  the planned per-camera arming UI; both should write the same `.env`/state.
- **Export a time range and have it delivered** - ask the bot for footage over a
  given window, and a period/schedule for delivery, and it sends the clip(s)
  back. Two shapes: one-shot ("send cam2 07:00-07:10 today") and recurring
  ("send cam2 07:00-07:10 every morning"). Pairs with the playback range cutout
  export and the pinned-recording feature below.

### Permanent (pinned) recording beyond the rolling window
Today everything lives in the ~90-min sliding window and ages out. Want the
ability to mark a chosen time range to be **kept permanently** (not temporary)
and stay accessible. A pinned range is exempt from the retention pruner (like
the existing protected `imported/` and `events/` roots, but user-chosen on the
timeline). Promote/demote flow: highlight a temp clip -> pin it -> it becomes
permanent. (Storage note: pinned ranges grow unbounded - surface their total
size somewhere.)

### Timeline color layering (z-ordered overlays)
Three stacked layers on the timeline, painted back-to-front so the front layer
always wins visually:
1. **Back - orange:** the temporary rolling recording (the normal tape, as it
   is now).
2. **Middle - blue:** ranges chosen to be kept permanently (the pinned feature
   above). Promote an orange temp clip (highlight it) and it flips to blue.
3. **Front - green:** spans where extracted events are available, overlaid on
   top so they're always visible over orange/blue.
Z-order matters: orange is the base, blue sits over orange, green sits on top of
everything.

### Hover peek thumbnail (YouTube-style scrub preview)
On hover over the timeline, show a small peek thumbnail of that moment/event -
like YouTube's seek-bar preview. Needs cheap per-position thumbnails: reuse the
event thumbs where they exist, sample segment frames elsewhere.

### Per-camera event arming (UI) + all-camera event bundling
Today every camera triggers events all the time. Two coupled changes:

- **Arming UI:** a simple, obvious control to pick which cameras *capture
  events* and which don't. Default on startup = all cameras armed (capture all
  movement). Disarming a camera only stops it from *triggering* an event; its
  rolling-buffer recording keeps running so it can still be bundled (below).
- **All-camera bundling:** when any *armed* camera trips, cut a clip from ALL
  4 cameras over the same window - not just the triggering one - applying the
  agreed in/out safe margin to every angle. One moment -> one event folder
  holding four synchronized clips. (The event already stores all cams; this
  makes the *trigger* fan out to every camera's margin explicitly.)

### Detection margin (central-band detection, full-frame evidence)
**Problem:** yolov8n fires on tiny edge slivers — e.g. just the toes poking into
the very bottom of the frame, or a head at the top. That triggers an event whose
detection box is a useless sliver, even though the saved full frame shows the
whole person.

**Idea (the user's):** ignore the top/bottom ~10–20% of the model's view when
deciding whether to trigger — only count detections in the central band — but
keep RECORDING/saving the FULL frame. So a toe that crosses into the central
band still triggers, and the evidence clip/thumbnail shows the whole body with a
comfortable margin above/below.

How (cheap, no new model):
- A per-camera (or global) `DETECTION_MARGIN_PCT` (e.g. 0.15 = ignore top & bottom
  15%). Apply it as a band: drop any detection whose box is entirely inside the
  margin, OR mask the frame fed to the detector so it can't fire there.
- Recording + the extracted clip/thumbnail stay full-frame — the margin is only a
  trigger filter, never a crop of the evidence.
- Simplest form of the polygon-zones feature (below): a horizontal band instead
  of a drawn polygon. Could ship first as a quick win, then generalise to drawn
  zones. Pairs with per-camera Interior/Exterior + the entering/leaving tripwire.

### Per-region detection classes (what to detect, where)
Builds on the shipped per-camera detect zone (v0.4.59, a draggable rectangle).
Next: let EACH region also choose WHAT counts inside it — e.g. people only at the
front door, vehicles only on the driveway, animals in the garden, or "any
movement" for a sensitive corner. So a region becomes (rectangle + class set).

- Per region: a checklist of classes — person / vehicle / animal / any-motion —
  defaulting to person+vehicle (today's behaviour). A live/segment detection
  only fires if its class is enabled for the region it falls in.
- Multiple regions per camera (not just one rect): different rules per area of
  the same view (door = people, driveway = cars).
- "Any movement" = a cheap motion trigger (frame-diff) for classes the model
  doesn't cover, independent of yolo.
- Forward-looking: keep the class set OPEN so new models/classes plug in later
  (faces, packages, license plates, specific animals) without reworking the UI —
  the region just gains more checkboxes as detectors are added.
- Storage extends `.cctv-detect-regions.json`: region = {rect, classes[]}; the
  live filter (and later the segment analyzer) checks class ∈ region.classes.
- Pairs with: detection margin (this is its general form), polygon zones (swap
  the rect for a drawn polygon), Re-ID, faces/ALPR.

### Spatial camera handoff (follow-the-person across cameras)
**Objective:** when a person is detected in a camera's default detect region
(e.g. the interior stairs), proactively *extend capture to the spatially
adjacent cameras* for a short window, so we record where they went next - the
door, then the street - instead of only the movement on the stairs plus a flat
margin. Effectively a longer, *spatial* margin that follows the path of travel.

How (the established pattern - "camera link / topology model", validated
against the MTMC literature and how Frigate users build this on top of zones):
- **Adjacency graph, hand-authored** (4 cameras - no need to learn it): nodes =
  cameras (optionally entry/exit *zones* within a camera, like the doorway edge
  of the stairs view); edges = physical paths with a rough **transit window**
  (e.g. stairs -> entry ~2-6 s, entry -> street ~3-10 s). Stored in config.
- **Exit-zone trigger:** when activity reaches an exit zone of camera A (person
  leaving frame toward a known neighbour), flag neighbour cameras B as "expect
  arrival" for their transit window and **extend/boost their event capture**
  over that window - even if B's own detector hasn't tripped yet.
- **Result:** one event that spans the whole journey across cameras with margins
  that track movement, not fixed seconds; far fewer "lost the person at the
  frame edge" gaps. Naturally feeds the same all-camera bundle.
- **Composes with Re-ID (below):** topology says *where/when* to look; Re-ID
  appearance match confirms the arrival on B is the *same* person, not a new
  one. Spatio-temporal prior x appearance similarity = the match decision.
- Phase it: (1) hand-authored adjacency + zone-triggered capture extension
  first (cheap, deterministic, immediately useful); (2) add Re-ID confirmation;
  (3) optional later: *learn* transit-time distributions from observed
  departures/arrivals instead of hand-tuning them.

### Entering vs leaving (direction of travel)
**Objective:** for each event, say whether the person is coming IN or going OUT
(e.g. "Person entered via the front door" vs "Person left"). Far more useful
than a bare "person detected".

How (the right way, NOT body orientation):
- **Do NOT use face-vs-back.** A person can walk backward / moon-walk, glance
  over a shoulder, or be side-on — body orientation lies. Direction must come
  from where they actually MOVE, not which way they face.
- **Track the trajectory with vanilla CV (cheap, no extra model).** We already
  get a bounding box per sampled frame from yolov8n. Follow the box centroid
  across the frames before+after the trigger; the sign of its displacement is
  the direction of travel. (Plain centroid tracking / optical-flow if needed -
  no new ML, fast on CPU.)
- **Per-camera zones** (hand-drawn): mark a near/inside region and a far/outside
  region, or a single threshold "tripwire" line (the doorway). Direction = which
  zone the centroid crosses FROM and TO. This is the standard line-crossing /
  zone-transition trick used in people counters. Reuses the same zone editor the
  spatial-handoff and polygon-zones features want - build the zone tool once.
- **Per-camera Interior/Exterior dropdown** (in settings/UI): tells the engine
  what "in" and "out" mean for that camera (an interior-stairs cam vs a
  street-facing cam have opposite "inside" directions). Pairs with the zones.
- Output: tag the event ("entering"/"leaving"/"passing") -> show in the events
  list, the Telegram caption, and (later) a simple occupancy count
  (entries - exits = how many people are currently inside).
- Phase it: (1) Interior/Exterior dropdown + one tripwire line per camera +
  centroid-direction = entering/leaving tag; (2) full polygon near/far zones;
  (3) occupancy counter. Shares the zone editor with spatial-handoff + zones.

### Cross-camera same-person de-duplication (Re-ID)
One person walking past multiple cameras currently fires up to 4 separate
events; 3 people => up to 12. Fuse detections of the *same individual* across
cameras (and across the safe-margin window) into a single event, so the count
reflects people, not camera angles.

- Approach: person re-identification - appearance embeddings compared by
  cosine similarity within a short temporal window, tying tracks across cams.
- Candidates to evaluate against the CPU-only single-exe constraint: a small
  ONNX Re-ID embedder (e.g. OSNet exported to ONNX) + simple matcher;
  `torchreid`/FastReID for the model zoo; or a tracker with a Re-ID backbone
  (BoT-SORT / Deep OC-SORT). Prefer lightweight ONNX to keep the .exe lean.
- Pairs with all-camera bundling: the bundle is the natural grouping unit; Re-ID
  decides whether two camera hits inside it are the same person or different.
- Pairs with spatial handoff (above): topology narrows the candidates by
  where/when; Re-ID disambiguates among them by appearance.

### Save / export fixes
- **"Save full clip" - FIXED v0.4.31.** Root cause: the single-part concat path
  did `os.replace()` (via `Path.replace`) to move the scratch copy into place,
  which fails across volumes on Windows (WinError 17) - scratch lived on `%TEMP%`
  (C:) while the user saved onto the data drive (D:), so *every* single-member
  export (168 of 173 sessions) silently wrote nothing. Fix: `shutil.move`
  (cross-drive safe) + scratch dir created on the destination drive + a real
  success/failure dialog (was admin-log-only before, hence "nothing happened").
- **Playback range cutout export:** mark an in/out range on the scrub bar or
  timeline and export just that segment to a file. *(still pending)*

### Live urgent-alert tier (real-time, separate from the segment analyzer)
A fast, instant notification path that runs on the **live runtime frames**
- the ones already decoded for the preview tiles - NOT on the recorded
window segments. Today's `SegmentAnalyzer` is inherently delayed: it only
sees a segment after it finalizes (up to 15 min later), so it's fine for
evidence but useless for "someone is at the door right now".

- **Light + quick model:** much cheaper than the batch yolov8n pass - e.g.
  yolov8n at low res (320) or a tiny person/motion classifier. Goal is
  near-real-time on CPU, glanceable latency.
- **Live source, no extra decode:** tap the frames `StreamWorker` already
  produces (`frame_ready`) and sample at a low rate (~1-2 fps) so it adds
  little load. No re-reading files from disk.
- **Its own thread, fully separate** from the segment analyzer. The two
  tiers coexist: live tier = instant alert; segment tier = recorded
  evidence clip with margins. They don't share a queue or thread.
- **Notification:** keep it simple - an in-app toast/banner (+ optional
  sound / OS notification). Respect the per-camera EVENTS arming.
- Debounce so a person lingering doesn't spam alerts (one alert per
  presence, like the segment-tier presence merge).

### Dual-stream record + two-pass analysis (sub-triggers-main)
Record **both** the sub and the main stream into a **1.5-hour** rolling
buffer (currently only one stream is recorded, sub by default).

- Analyze the **sub** stream continuously - cheap, what makes 4-camera
  always-on detection feasible.
- When the sub detection trips on something, re-run detection on the
  **main** stream for the *same timestamp window*, so the event is
  analyzed twice and we keep the high-res result (better small/distant
  detections, sharper evidence, future face/plate work).
- Net effect: light always-on pass on sub, targeted heavy pass on main
  only where it matters. "Best of both."

Implications to design for:
- Disk: recording both streams ~doubles footprint vs sub-only. Keep the
  90-min (1.5h) window so resident size stays bounded. Re-measure GB/day.
- Recorder needs two ffmpeg segment workers per camera (sub + main) with
  the same clock-aligned boundaries so sub and main segments line up by
  timestamp (same lookup trick already used for cross-camera bundling).
- Event extraction should save the **main**-stream clips as the evidence
  (and thumbnail), with the sub pass only used as the cheap trigger.
- The existing per-camera EVENTS arming and all-camera bundling stay as-is.

## Backlog (from the original feature ladder)

- **Push notification service** - deliver alerts (event captured / live
  urgent alert) off-device to phones. Two candidates, undecided:
  - **ntfy.sh** - dead-simple HTTP POST to a topic, no accounts; subscribers
    install the ntfy app. Easiest to wire (one requests call), can self-host.
  - **Telegram bot** - leaning this way: create a bot, add the whole family
    to a group, and everyone gets alerts (thumbnail/clip) and potentially
    on-demand camera access in one shared place. Friendlier for non-technical
    family members than ntfy.
  Should send the event thumbnail (and optionally the clip), respect
  per-camera arming, and debounce. Pairs naturally with the live urgent-alert
  tier (instant) and the recorded-event path (evidence).
- **Polygon zones** + intrusion / loitering logic (per-camera drawn zones,
  dwell-time rules).
- **Face recognition** (DeepFace) over event clips.
- **ALPR** (EasyOCR) - license plate read on vehicle events.
- **SQLite event log** + searchable dashboard (filter by camera, class,
  time, zone).
- **FTP bridge** - DVR auto-pushes new clips to an embedded FTP server
  (was deprioritized).

## Notes

- AGPL-3.0 applies to the bundled YOLOv8n weights - fine for personal use,
  swap the model before any commercial distribution.
