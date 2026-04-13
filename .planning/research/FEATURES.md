# Feature Landscape — Home CCTV AI Pipeline

**Domain:** Self-hosted, solo, CPU-only smart surveillance over legacy IP cameras
**Researched:** 2026-04-13
**Reference platform:** Frigate 0.16+ (the de-facto open-source baseline in 2026), with cross-checks against Scrypted, Shinobi, and commercial smart cameras
**Confidence:** HIGH on category placements; MEDIUM on CPU-cost numbers (depend on host CPU)

---

## How to Read This Document

Every feature is tagged with three things:

- **Category:** Table Stakes / Differentiator / Anti-Feature
- **Complexity:** trivial / moderate / hard *on a CPU-only WSL2 host*
- **CPU budget impact:** none / low / moderate / blocking — relative to the existing 4-stream YOLOv8n + ByteTrack budget
- **Depends on:** other features that must exist first

The dependency graph is at the bottom. CPU budget notes are at the very bottom.

> **Solo-deployment lens:** "Table stakes" here means *the user (single operator, building for himself)* will be annoyed if it is missing — not what a SaaS product manager would put on a marketing page. Anything that only matters for multi-tenant, multi-user, or enterprise deployments has been pushed to anti-features.

---

## 1. Table Stakes

These are the things the user will *immediately* miss in week 1. Skipping any of these turns the system from "smart NVR" into "log file with extra steps." They are the floor, not the ceiling.

### 1.1 Event Review

| Feature | Why Expected | Complexity | CPU Impact | Depends On |
|---|---|---|---|---|
| **Per-event snapshot saved to disk** | Already in spec. The thumbnail is the entire UX. Without it, the SQLite row is useless. | trivial | none (one frame per trigger) | trigger logic |
| **Timezone-correct local timestamps** (not UTC, not naive) | Reviewing yesterday's events in the wrong timezone is the #1 papercut in DIY CCTV builds. Store UTC, render local. | trivial | none | — |
| **Per-camera filtering** | "Show me only the driveway cam" is the most common query. Has to be a first-class index column. | trivial | none | event log |
| **Per-event-type filtering** | "Show me face recognitions only" / "show me plate reads only". Single SQL `WHERE` clause. | trivial | none | event log |
| **Per-zone filtering** | "Anything in `Front_Door` last night?" Already in schema as `zone_name`. | trivial | none | zones |
| **Date / time-range filtering** | Standard. "Show last 24h", "show 02:00–06:00 last week". | trivial | none | event log |
| **Chronological event timeline (newest first)** | Frigate, Scrypted, every commercial system. The default landing view. | trivial | none | event log |
| **Click-through to full-res snapshot** | The thumbnail is bait; the full image is the answer. Open in lightbox / new tab. | trivial | none | snapshot store |
| **"Jump to previous/next event from same entity ID"** | Once you find Person_405, you want to see *every* event for Person_405 across cameras. Trivial query, huge UX win. | trivial | none | ByteTrack IDs in log |

### 1.2 Detection Quality Floor

| Feature | Why Expected | Complexity | CPU Impact | Depends On |
|---|---|---|---|---|
| **AI-class-gated events (no pixel motion)** | Already a key decision. This is the *defining* feature of a 2026 system vs. a 2010 NVR. Frigate, Reolink, Nest, Ring all market this as "no more shadow alerts." | already in spec | none (it's *removal* of work) | YOLO |
| **Per-camera object-class allowlist** (e.g., Cam 3 only cares about cars) | Without this, indoor cameras flood the log with self-detections. Trivial config, big quality lift. | trivial | none (just a filter) | YOLO |
| **Confidence threshold per class, per camera** | YOLOv8n on legacy 480p sub-streams will hallucinate. A per-camera floor (e.g., person ≥ 0.55) cuts noise massively. | trivial | none | YOLO |
| **Minimum bbox area filter** | Small far-away blobs are usually false positives. One-line filter. | trivial | none | YOLO |
| **Minimum-track-age before logging** (e.g., must be tracked ≥ N frames) | Stops the log from filling with 1-frame YOLO blips. Frigate does this. | trivial | none | ByteTrack |
| **Stable object cooldown** (de-dupe a single track into a single `Person_Detected` event) | Without this, a person walking across the driveway logs 200 events. Use tracker ID + zone + N-second cooldown. | moderate | none | ByteTrack |

### 1.3 Reliability & Operations

| Feature | Why Expected | Complexity | CPU Impact | Depends On |
|---|---|---|---|---|
| **Auto-reconnect on stream drop** | Already in spec. DVRs flake. The pipeline must self-heal without a human restart. | moderate | low | RTSP layer |
| **Per-stream health / heartbeat status** ("Cam 2 last frame: 4s ago") | When a camera dies silently, you need to know *before* you go look for footage that doesn't exist. | trivial | none | RTSP layer |
| **Snapshot retention policy** (delete > N days, or > N GB cap) | Disks fill. This *will* bite in week 3. Frigate, Scrypted, every system has this. Skipping it is a known papercut. | trivial | none | snapshot store |
| **DB vacuum / rotation** | SQLite bloats. A nightly `VACUUM` and optional N-day pruning keep the DB queryable. | trivial | none | SQLite |
| **Structured logging to file with rotation** | When the pipeline crashes at 03:00, logs are the only forensic surface. Use stdlib `RotatingFileHandler`. | trivial | none | — |
| **Graceful shutdown** (SIGINT/SIGTERM closes RTSP, flushes DB) | Otherwise restarts leave half-written rows and zombie ffmpeg processes. | trivial | none | — |
| **Single-command start / restart** (`make run`, systemd unit, or equivalent) | If running it requires remembering 4 env vars and a CWD, the user will stop running it. | trivial | none | — |
| **`.env`-based config (no hardcoded secrets)** | Already in spec. Non-negotiable. | trivial | none | — |
| **Offline MP4 mode for dev/testing** | Already in spec. Without it, you can only debug while a person is physically in front of a real camera. | moderate | none | RTSP layer |

### 1.4 Spatial & Temporal Logic

| Feature | Why Expected | Complexity | CPU Impact | Depends On |
|---|---|---|---|---|
| **Virtual polygon zones, per camera** | Already in spec. The *only* meaningful way to define "interesting" without pixel motion. Every modern system has this. | moderate | none (point-in-polygon is free) | YOLO |
| **Zone entry / exit events** | "Person entered Driveway" is the canonical surveillance event. Compute on transition, not per frame. | moderate | none | zones + ByteTrack |
| **Persistent ByteTrack IDs** | Already in spec. Foundation for *all* downstream logic — peak-bbox face cap, velocity gate, vehicle interaction, loitering. No tracker = no smart events. | already in spec | already budgeted | YOLO |

### 1.5 Storage Schema

| Feature | Why Expected | Complexity | CPU Impact | Depends On |
|---|---|---|---|---|
| **Indexed columns on `timestamp`, `camera_id`, `event_type`, `entity_id`, `zone_name`** | Without indexes, the dashboard's first filter query at 50k rows takes seconds. Add indexes day one. | trivial | none | SQLite |
| **`image_path` is a relative path under `EVENT_IMAGE_DIR`** | Hardcoding absolute paths breaks the moment the storage drive changes. Already implied by spec, must be enforced. | trivial | none | snapshot store |
| **Date-partitioned snapshot directories** (`YYYY-MM-DD/`) | Already in spec. Avoids 500k files in one folder, which kills NTFS/ext4 listings. | trivial | none | snapshot store |

---

## 2. Differentiators

These are the features that elevate the system from "log file with thumbnails" to "actually smart." The user has *already specified* all the headline ones — they are exactly what makes this project worth building over Frigate. The differentiators below are split into "in spec" (build them) and "nice to add" (consider if budget allows).

### 2.1 Already Specified (Build These — They're the Whole Point)

| Feature | Value Proposition | Complexity | CPU Impact | Depends On |
|---|---|---|---|---|
| **Trigger & Catch dual-stream architecture** | The single most important architectural decision in the project. Lets one CPU host a system that would otherwise need a GPU. | hard | none (it's *what saves* the budget) | RTSP layer |
| **Face recognition gated on peak bbox area** (DeepFace) | Smart triggering: only run the expensive model on the frame most likely to succeed. Frigate added this in 0.16. The user beat them to the design. | hard | moderate (one event-frame at a time) | ByteTrack + bbox-area history |
| **ALPR gated on zero centroid velocity** (EasyOCR) | Solves motion blur and headlight bloom in one trick. Frigate uses a similar "wait until stationary" pattern in 0.16 LPR. Strong differentiator vs. naïve continuous OCR. | hard | moderate (one event-frame at a time) | ByteTrack + centroid velocity |
| **Vehicle interaction inference** (Entered / Exited via bbox intersection + ID disappearance) | This is the actual differentiator vs. Frigate. Frigate logs "person near car" but does *not* infer the entry/exit transition as a discrete event. Genuinely novel for a self-hosted system. | hard | low (just bbox ops on existing tracks) | ByteTrack + person∩car overlap |
| **Loitering: ByteTrack ID in zone > X seconds** | Modern systems all have this; the user version is per-zone, per-class configurable. Cheap once tracking exists. | moderate | low | ByteTrack + zones + per-zone config |
| **Polygon zones with semantic names** (`Driveway`, `Front_Door`) | Names propagate into the event log, into search, into future automations. The naming is what makes events human-readable. | moderate | none | — |

### 2.2 Strongly Recommended Additions (High value, low CPU)

| Feature | Value Proposition | Complexity | CPU Impact | Depends On |
|---|---|---|---|---|
| **Known-face library with sub-labels** ("Person_405 → 'Alex'") | Frigate 0.16 ships this. Storing a known-face embeddings folder and labelling matches turns the event log from "Person_Detected" into "Alex arrived home at 18:22." Massive perceived intelligence. | moderate | low (only on face-recognized events) | DeepFace |
| **Plate → owner mapping** ("ABC123 → 'Mum's car'") | Same idea for plates. Once it's there the dashboard reads like a story. Trivial extra table. | trivial | none | EasyOCR |
| **Track replay** (sequence of bboxes for a single ByteTrack ID rendered on the snapshot) | One snapshot is OK; *the path the person took* is far more informative. Store a few centroids per track, draw on snapshot. | moderate | low (a few KB per track) | ByteTrack |
| **Per-zone, per-class loitering thresholds** | "Front_Door allows 30s loiter, Driveway allows 5min." Cheap config, big quality difference. | trivial | none | loitering |
| **Cross-camera entity hand-off** (heuristic: person leaves Cam1 zone → appears in Cam2 zone within N seconds → same logical entity) | This is the holy grail for a 4-camera home setup. Even a *crude* heuristic ("within 10s, adjacent camera, same class") creates a unified narrative across cameras. Frigate does *not* do this well. | hard | low | ByteTrack + zones + camera adjacency map |
| **Event "scoring"** (a derived per-event score combining confidence × bbox area × dwell) | Lets the dashboard sort by "most interesting" instead of strictly chronological. One SQL expression. | trivial | none | event log |
| **Daily summary digest** (one row per day: counts of each event type, top entities, busiest zone) | Generated nightly into a `DailySummary` table. Makes the system feel "alive" without ever opening the live view. | moderate | low (offline batch) | event log |

### 2.3 Optional Stretch Features (defer until pipeline is stable)

| Feature | Value Proposition | Complexity | CPU Impact | Depends On |
|---|---|---|---|---|
| **Free-text natural-language search** ("man in blue shirt at front door") via a small CLIP / VLM model | This is the 2026 commercial-camera headline feature. CLIP is workable on CPU if you embed *only event snapshots* (not every frame). | hard | low if event-only; blocking if continuous | event log + snapshot store |
| **Package / parcel detection** (sub-class of "object" left in zone) | Frigate added this. Could be done by detecting "stationary non-person object in Front_Door zone." Genuinely useful. | moderate | low | YOLO + zones |
| **Audio event tags** (glass break, dog bark) via YAMNet | Most legacy DVR streams *do* carry audio. CPU-cheap, very high signal-to-noise. | moderate | low (YAMNet is tiny) | audio extraction |
| **Pre-event ring buffer** (save the 5s of sub-stream frames *before* the trigger as a short clip) | Single most-requested feature in every NVR forum. Hard part is buffering 4 streams in RAM without leaking. | hard | moderate | RTSP layer |
| **Snapshot deduplication / perceptual hashing** (drop a snapshot if it is ≥ 95% identical to the previous one for the same entity) | Saves disk *and* makes the timeline less repetitive. Cheap with `imagehash`. | trivial | low | snapshot store |
| **Zone-painter web UI** (draw polygons on a still frame instead of editing JSON coordinates) | The single biggest annoyance in DIY zone systems is hand-editing pixel coords. Even a one-page HTML canvas tool is a huge UX win. | moderate | none | dashboard |

---

## 3. Anti-Features (Out of Scope — Deliberate Exclusions)

Categorizing *what not to build* matters as much as scoping what you do build. For a solo self-hosted CPU-only system, these are explicit non-goals.

### 3.1 Hard No (Already in Spec — Reaffirm)

| Anti-Feature | Why Avoid | What to Do Instead |
|---|---|---|
| **Pixel-based motion detection** | False positives on shadows, rain, branches, bug-on-lens, headlight sweep, IR cut-filter switches. Already removed. | AI-class + zone gating only. |
| **Cloud upload / VSaaS / off-site storage** | Privacy + latency + bandwidth + monthly cost. Defeats the entire premise. | All snapshots, DB, models stay local. |
| **UDP RTSP transport** | Demonstrably drops on this LAN. | Forced TCP via OpenCV `rtsp_transport;tcp`, mirroring the proven `ffplay` flags in `terminal.txt`. |
| **GPU / Coral TPU / dedicated accelerator** | Budget = $0. Hardware is fixed. *Note: Frigate community is moving away from Coral in 2026 anyway, so this is also future-proof.* | YOLOv8n + Trigger & Catch architecture. |
| **Continuous DeepFace / EasyOCR on every frame** | Will instantly peg the CPU to 100% on 4 streams. | Event-driven single-frame triggers only. |
| **Replacing BitVision for general viewing** | BitVision works fine for live-look. Out of scope. | This project is the *AI overlay*, not an NVR replacement. |
| **Multi-machine / distributed pipeline** | Single host. Coordination cost is not worth it. | One process, one host. |

### 3.2 Soft No (Not in Spec, Worth Naming Explicitly)

| Anti-Feature | Why Avoid for Solo Self-Hosted | What to Do Instead |
|---|---|---|
| **Push notifications to a phone** (Pushover, ntfy, Telegram bots) | A *huge* time sink to do well (delivery guarantees, dedup, snooze, do-not-disturb, image attachments, certificate management). For a solo user reviewing events the next morning, the dashboard is enough. Add later if a *specific* event type proves urgent. | Polling the dashboard. Optional later: a single ntfy webhook for *one* high-signal event type only (e.g., loitering). |
| **Multi-user accounts / RBAC / login pages** | One operator. Auth adds attack surface, password reset flows, session storage, and zero value. | Bind the dashboard to `127.0.0.1` (or LAN-only) and rely on network isolation. |
| **HTTPS / TLS termination on the dashboard** | LAN-only, single user. Self-signed certs cause more pain than they prevent. | LAN-only bind. If remote access is ever wanted, route through an existing reverse proxy / Tailscale, not the app. |
| **Real-time live video in the dashboard** | Streaming H.264 in a browser cheaply is a project of its own (HLS chunking, MSE, transcoding). BitVision already does live view. | Dashboard shows event snapshots only, never live frames. Live view = open BitVision. |
| **Continuous 24/7 video recording** | Out of scope, eats disk, and BitVision DVR already records natively. | Event-only snapshots. The DVR keeps the raw video; this project keeps the *index*. |
| **Two-way audio / siren / smart home control** | Belongs to the camera firmware or Home Assistant, not an event-detection pipeline. | Out of scope. |
| **Web-scale stack** (Postgres, Redis, Kafka, microservices, container orchestration) | One user, four cameras, one host. SQLite + a single Python process is correct. | Stay boring: SQLite + filesystem + one process. |
| **Heavy generative-AI captioning of every event** ("a man in a red jacket walked past the bin") via a 7B+ VLM | CPU-blocking on this hardware. The current event-type vocabulary is already legible. | Defer. If wanted later, run a small CLIP for *search-time* embedding only, not per-event captioning. |
| **Machine-learning-based behavior anomaly detection** ("learns your routine") | High false-positive rate, opaque, needs months of data to train. Solo home use does not justify it. | Hand-tuned per-zone rules (loitering thresholds, allowed classes). |
| **Privacy-mask geofencing of public sidewalks** | Frigate / commercial systems offer this for legal compliance. For a solo home user pointing cameras at his own property, not required. | Not built. Add a privacy-region mask only if a camera ever needs to be re-aimed at a public space. |
| **Configuration hot-reload** | Adds complexity, loses state on bytetrack IDs. | Restart the process. Single-command restart is table stakes; hot-reload is not. |
| **Plugin / extension architecture** | Single-user codebase. YAGNI. | Edit the source. |
| **Internationalization / i18n of the dashboard** | One user, one language. | English only. Hardcode strings. |
| **Mobile-responsive dashboard with touch gestures** | Reviewing events is a desktop task. Mobile is a "nice to have" never. | Desktop-only layout. Don't waste cycles on a hamburger menu. |

---

## 4. Feature Dependency Graph

```
                                 RTSP/TCP ingest layer
                                          |
                            +-------------+-------------+
                            |                           |
                       Sub-stream loop            Main-stream grabber
                       (continuous)               (one-frame on trigger)
                            |                           ^
                            v                           |
                          YOLOv8n                       |
                            |                           |
                            v                           |
                        ByteTrack                       |
                       (IDs + centroid                  |
                        velocity history)               |
                            |                           |
        +-------------+-----+-----+--------------+      |
        |             |           |              |     |
        v             v           v              v     |
   Polygon zones  bbox-area   centroid       person∩car|
        |         history     velocity         overlap |
        |             |           |              |     |
        v             v           v              v     |
   Zone entry/   Peak-bbox     Zero-vel    Entered/Exited
   exit events   detection    detection      Vehicle    |
        |             |           |              |     |
        |             v           v              |     |
        |        Trigger ----> Trigger -----> Trigger -+
        |        face cap     plate cap      (no main-
        v             |           |          frame needed)
   Loitering          v           v              |
   (dwell timer       Main        Main           |
    in zone)         frame       frame           |
        |             |           |              |
        |             v           v              |
        |         DeepFace     EasyOCR           |
        |             |           |              |
        +-----+-------+-----------+--------------+
              |
              v
        EventLog row + snapshot file
              |
              v
        SQLite (indexed)  +  EVENT_IMAGE_DIR/YYYY-MM-DD/
              |
              v
        Vue 3 dashboard (deferred milestone)
        - filter by camera/zone/type/entity/date
        - per-entity drill-down
        - lightbox snapshots
```

**Critical observations:**

1. **ByteTrack is the keystone.** Every single differentiator (face cap, plate cap, vehicle interaction, loitering, cross-camera handoff) collapses if tracking is unreliable. Tracker quality is more important than detector quality.
2. **Zones depend on nothing but YOLO.** They can be implemented and tested in isolation, before any of the heavy event logic.
3. **DeepFace and EasyOCR have no dependencies on each other.** They can be added in either order, in parallel.
4. **The dashboard depends on absolutely nothing in the runtime pipeline** (it only reads SQLite + filesystem). This is a deliberate decoupling — it lets the dashboard slip to a later milestone without blocking anything.
5. **Vehicle interaction** is the only event type that requires *intersection logic between two simultaneous tracks*. It is structurally different from every other event and worth isolating in its own module.

---

## 5. CPU Budget Notes

This system's defining constraint is the CPU. Every feature decision must answer: *what does this cost when 4 streams are running simultaneously?*

### Budget envelope (rough, host-CPU dependent)

Assume a modern desktop CPU (6–8 cores, no AVX-512 guarantees, no GPU). Approximate per-stream costs:

| Workload | Per-stream cost | Notes |
|---|---|---|
| RTSP/TCP decode (sub-stream, ~480p) | ~3–8% of one core | Dominated by ffmpeg/OpenCV |
| YOLOv8n inference @ 10–15 FPS | ~15–25% of one core | The biggest single cost |
| ByteTrack association | ~1–3% of one core | Cheap |
| Zone polygon checks | <1% | Free |
| Velocity / bbox-area bookkeeping | <1% | Free |
| **Subtotal per stream** | **~20–35% of one core** | |
| **× 4 streams** | **~80–140% (≈ 1–2 cores)** | Steady-state baseline |

This leaves the remaining cores for **bursty event work**:

| Event workload | One-shot cost | Acceptable rate |
|---|---|---|
| Main-stream single-frame grab (1080p+) | ~50–200 ms wall time | A few per minute peak |
| DeepFace embedding + match | ~200 ms – 1.5 s on CPU | A few per minute peak |
| EasyOCR plate read | ~300 ms – 1 s on CPU | A few per minute peak |
| SQLite insert + image write | <10 ms | Unlimited |

### Rules that protect the budget

1. **Heavy models never run on continuous loops.** They run inside an `if trigger:` branch. This is the Trigger & Catch invariant. *Violating it is the single most likely way the project fails.*
2. **Main-stream sockets are opened on demand and closed.** Holding 4 main-stream RTSP sessions open just for snapshot-on-demand will silently double the decode cost and starve YOLO. Frigate's mistake to learn from.
3. **No per-frame logging.** Log on *transitions* (enter, exit, peak, stop) not on *states*. Per-frame logs explode the DB and waste IO.
4. **No per-frame disk writes.** Snapshots are written once per event, not per frame.
5. **Cooldowns on every event type.** A 10-second per-(entity_id, event_type) cooldown prevents log floods and keeps DeepFace/EasyOCR from being triggered repeatedly on the same target.
6. **No second neural model running continuously** (no audio model, no segmentation, no pose). If it runs every frame on every camera, it is forbidden by budget. If it runs *only on trigger*, it can be considered.
7. **YOLO input resolution is sub-stream native.** Do *not* upscale before inference. The whole point of using sub-streams is the lower resolution.
8. **One Python process, multiprocessing for streams (not threads).** Python's GIL kills 4-thread inference. Use `multiprocessing.Process` per camera, an SQLite WAL-mode database for shared writes, and a single process for DeepFace/EasyOCR consumers. Threads will *appear* to work and then silently underperform.
9. **Lazy-import heavy dependencies.** `import deepface` and `import easyocr` should not happen at process start — the model load is multi-second. Import inside the trigger handler (after a one-time warm-up) so that startup latency stays low and memory stays bounded if a model is never used in a session.

### Features classified by CPU risk

**Free (no measurable impact):**
- Zone polygons, bbox area, velocity, all per-event filtering, all DB queries, snapshot retention pruning, all dashboard reads, structured logging, daily digest batch, plate→owner mapping, known-face mapping.

**Low (acceptable on this hardware):**
- DeepFace per trigger, EasyOCR per trigger, vehicle interaction logic, loitering timer, perceptual-hash dedup, audio classifier (YAMNet only), CLIP embedding *of event snapshots only*.

**Moderate (acceptable but watch closely):**
- Pre-event ring buffer (memory cost on 4 streams), main-stream grab (must close after grab), cross-camera handoff heuristic.

**Blocking (do not build):**
- Continuous DeepFace, continuous EasyOCR, continuous CLIP / VLM captioning, any per-frame second neural model, any pixel-motion-on-main-stream pass, any 24/7 recording transcode, any browser live-streaming transcode.

---

## 6. MVP Recommendation

Given the spec already names every important feature, the MVP slicing question is *order*, not *what*. Recommended order — each step is shippable and demoable on its own:

1. **RTSP/TCP ingest + YOLOv8n + offline MP4 mode** — proves the pipeline can survive 4 streams.
2. **ByteTrack + per-track centroid velocity + bbox-area history** — the keystone. Nothing downstream works without it.
3. **Polygon zones + per-camera class allowlist + per-class confidence floor + minimum-track-age + cooldown** — turns YOLO blips into clean `Person_Detected` / `Car_Detected` events. *This is the first version that produces a useful event log.*
4. **SQLite schema + indexes + snapshot writer + retention pruner + structured logging** — makes the system actually operable for more than a day.
5. **Loitering + zone entry/exit events** — pure-tracker features, no new models. Big perceived intelligence jump for near-zero CPU.
6. **Trigger & Catch main-stream grabber** — the architectural piece every later model depends on.
7. **DeepFace face recognition gated on peak bbox** — first heavy model. Validate the trigger pattern works.
8. **EasyOCR ALPR gated on zero velocity** — second heavy model, same pattern.
9. **Vehicle interaction (entered / exited) inference** — pure-tracker, but depends on stable two-track overlap. Easier to debug after #5–#8 prove tracking is solid.
10. **Vue 3 dashboard** — read-only consumer of the SQLite DB and snapshot directory. Deferrable; the system is *useful* before this exists (CLI/SQL queries are enough for the operator-developer in early phases).
11. *(Optional)* Known-face library, plate→owner mapping, daily digest, cross-camera handoff, snapshot dedup, zone-painter UI.

**Defer indefinitely:** push notifications, multi-user auth, HTTPS, live video in dashboard, audio events, pre-event clip buffer, CLIP search, package detection. None of these are needed for a working solo deployment, and each is at least one full sprint of work for marginal value.

---

## 7. Sources

- [Frigate NVR official site](https://frigate.video/) — reference platform for 2026 self-hosted NVR features
- [Frigate documentation: License Plate Recognition](https://docs.frigate.video/configuration/license_plate_recognition/) — confirms the "wait until vehicle stationary" pattern as industry-standard
- [Frigate 0.16 release coverage — facial and license plate recognition](https://idtechwire.com/frigate-0-16-adds-facial-and-license-plate-recognition-to-open-source-nvr/) — confirms peak-bbox face capture and stationary-vehicle plate capture as the dominant 2026 design
- [Frigate snapshots configuration](https://docs.frigate.video/configuration/snapshots/) — snapshot retention is a first-class feature
- [Frigate NVR 2026 Guide (Corelab)](https://corelab.tech/setupfrigate/) — reducing false alerts is the most-searched 2026 CCTV topic
- [Home Assistant Frigate setup guide 2026 (HomeShift)](https://joinhomeshift.com/home-assistant-frigate)
- [Frigate vs Shinobi comparison (selfhosting.sh)](https://selfhosting.sh/compare/frigate-vs-shinobi/) — Shinobi pixel-motion baseline, Frigate AI baseline; confirms pixel motion is "legacy"
- [Awesome Self-Hosted: Video Surveillance](https://awesome-selfhosted.net/tags/video-surveillance.html) — full landscape of comparable self-hosted tools
- [Reolink: Smart Surveillance Analytics intro](https://support.reolink.com/hc/en-us/articles/30167512591257-Introduction-to-Smart-Surveillance-Analytics/) — commercial-camera baseline for filter UI (date / device / event type)
- [XDA: You don't need an NVR — you need Frigate](https://www.xda-developers.com/you-dont-need-an-nvr-for-your-home-security-system-you-just-need-frigate/) — confirms event-based recording over continuous as the 2026 default
- [The Gadgeteer Q1 2026 camera roundup](https://the-gadgeteer.com/2026/04/06/5-new-security-cameras-just-changed-what-you-should-expect/) — 4-of-5 new 2026 cameras ship with no subscription and on-device smart detection; sets user expectations
- [DeepCamera (SharpAI) GitHub](https://github.com/SharpAI/DeepCamera) — comparable open-source local-AI camera platform; confirms VLM search is the 2026 stretch feature

**Confidence notes:**
- *Category placements* (table stakes vs differentiator vs anti-feature): **HIGH** — corroborated across Frigate docs, multiple 2026 review sources, and the project's own constraints.
- *CPU budget numbers*: **MEDIUM** — order-of-magnitude estimates based on YOLOv8n / DeepFace / EasyOCR community benchmarks. Actual per-core costs depend heavily on the specific host CPU, AVX support, and ffmpeg build, and should be re-measured during Phase 1 once the host is profiled. Do not treat the percentages as contractual.
- *Cross-camera handoff* and *audio events* viability on this hardware: **LOW** — flagged for phase-specific research before building.
