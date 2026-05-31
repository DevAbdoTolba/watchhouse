# Watchhouse - Roadmap / Future Work

Living backlog of planned features. Shipped increments live in git tags;
this file is only what's *not* done yet. Newest ideas at the top of each
section.

## Next up

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
