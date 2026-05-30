# Watchhouse - Roadmap / Future Work

Living backlog of planned features. Shipped increments live in git tags;
this file is only what's *not* done yet. Newest ideas at the top of each
section.

## Next up

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
