---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: unknown
last_updated: "2026-04-17T19:57:07.578Z"
progress:
  total_phases: 5
  completed_phases: 1
  total_plans: 6
  completed_plans: 5
  percent: 83
---

# STATE — Home CCTV AI Pipeline

*Single source of truth for "where are we right now?". Updated on every phase/plan transition.*

---

## Project Reference

- **Project**: Home CCTV AI Pipeline
- **Milestone**: v1 — Local-only AI event pipeline over 4 RTSP streams
- **Core value**: Turn dumb legacy cameras into a smart event log — no new hardware, no cloud, no melted CPU
- **Source of truth**: `.planning/PROJECT.md`
- **Requirements**: `.planning/REQUIREMENTS.md` (59 v1 requirements across ENV/ING/TRK/ZON/TRG/FAC/ALP/STO/OPS)
- **Roadmap**: `.planning/ROADMAP.md` (5 phases: 0, 1, 2, 3, 4)
- **Research**: `.planning/research/SUMMARY.md`
- **Config**: `.planning/config.json` (granularity=coarse, mode=interactive, parallelization=true)

---

## Current Position

Phase: 1 (Multi-Stream Ingest & Reconnect) — EXECUTING
Plan: 2 of 3 (01-01 complete; 01-02 next)

- **current_phase**: 1
- **current_phase_name**: Multi-Stream Ingest & Reconnect
- **current_plan**: 2
- **status**: executing
- **progress**: 20% (1 / 5 phases complete) — phase 1 is 1/3 plans done

```
[████                ] 1 / 5 phases complete
```

**Next action**: Execute `01-02-PLAN.md` (watchdog + reconnect). The StreamReader, FrameQueue, and CameraHeartbeat surfaces from 01-01 are now locked in and every downstream plan in Phase 1 builds on top of them.

---

## Phase Overview

| # | Phase | Status | Plans |
|---|---|---|---|
| 0 | Environment & Sanity | ✅ Complete (2026-04-17) | 3/3 + patch |
| 1 | Multi-Stream Ingest & Reconnect | In Progress | 1/3 |
| 2 | Detection, Tracking & Zoned Events | Not started | 0/0 |
| 3 | Trigger & Catch Event Models | Not started | 0/0 |
| 4 | Hardening & Operations | Not started | 0/0 |

---

## Performance Metrics

*Populated as phases complete. Tracks plan/phase duration, rework rate, node-repair events, verifier bounces.*

- **phases_completed**: 1 / 5
- **plans_completed**: 4 (+ 1 patch) — Phase 0 × 3 plans + 1 patch, Phase 1 × 1 plan (01-01)
- **avg_plan_duration**: —
- **node_repairs**: 0
- **verifier_bounces**: 0

| Phase | Plan | Duration | Tasks | Files | Commits | Notes |
|-------|------|----------|-------|-------|---------|-------|
| 01    | 01   | 12 min   | 2     | 5     | 4       | TDD. All acceptance greps + 107/107 tests green. |

---

## Accumulated Context

### Decisions Log

*Cross-phase decisions that influence downstream work. Initial set from PROJECT.md / SUMMARY.md.*

- **2026-04-13** — Tracker: Ultralytics built-in `tracker="bytetrack.yaml"`, NOT the standalone `ifzhang/ByteTrack` repo (SUMMARY.md §2, §8). Resolved at roadmap time.
- **2026-04-13** — YOLO weights exported to OpenVINO IR mandatory for CPU budget (~2.5-3× speedup). Fallback `onnxruntime`. Model: `yolo26n.pt` preferred, `yolov8n.pt` 1-line fallback.
- **2026-04-13** — Version pins locked: `numpy==1.26.4` (NOT 2.x), `tensorflow-cpu==2.16.2`, `tf-keras==2.16.0`, `TF_USE_LEGACY_KERAS=1`, `deepface==0.0.99`, `easyocr==1.7.2` with `gpu=False` explicit.
- **2026-04-13** — Concurrency model: 4 stream threads + 1 inference thread (NOT 4) + 1 main-stream grabber + 1 EventWriter + DeepFace subprocess + EasyOCR subprocess. No asyncio.
- **2026-04-13** — SQLite + `EVENT_IMAGE_DIR` MUST live on WSL2 ext4, NEVER `/mnt/c/...` (DrvFs kills WAL).
- **2026-04-13** — React dashboard deferred to a later milestone. v1 is operator-usable via SQLite + file browser.
- **2026-04-16** — **Stack pivoted from CPU-only to hybrid GPU+CPU.** User confirmed RTX 3060 Laptop (6 GB VRAM) is exposed to WSL2 via CUDA 12 passthrough (nvidia-smi inside WSL2 shows driver 561.09, CUDA 12.6). First pivot attempt used `tensorflow[and-cuda]==2.16.2` + `torch==2.5.1+cu121` — failed: both pull `nvidia-cublas-cu12` at incompatible versions (12.1.3.1 vs 12.3.4.1). Final decision: **hybrid split.** `torch==2.5.1+cu121` for YOLO + EasyOCR (continuous hot path, GPU-critical). `tensorflow-cpu==2.16.2` for DeepFace (trigger-only, ~10-50/day, CPU latency acceptable). OpenVINO and onnxruntime dropped from deps. All Phase 0 scaffolding code unchanged. `.venv` rebuilt via `rm -rf .venv && uv sync`.
- **2026-04-17** — **Phase 0 complete.** Live 4-camera 2-hour sweep passed with clean decode (99.88% cam1, 99.95% cam2/3, 99.99% cam4). Phase 0 patch fixed 4 harness bugs (DeepFace RetinaFace API, DeepFace ArcFace cache verify path, `env_vars_loaded` reporting, `wsl_version` UTF-16 decode) + recalibrated exit criterion against real sub-stream rates (15/6/6/15 not main-stream 25/12/12/25). 86/86 tests passing. Real measured `cold_start_ms`: yolo_openvino=5951, deepface=3218, easyocr=775. `cameras.yaml` committed with sub-stream rates; Phase 1+ calibrates against those. **Known pending action:** User to switch WSL2 from NAT → mirrored networking (current 3415 ms RTSP handshake latency will strain Phase 1 reconnect watchdog). Doc at `D:\Projects\obsidian\claude\notes\WSL2-Mirrored-Networking-Setup.md`.
- **2026-04-17** — **Phase 1 Plan 01-01 complete.** Delivered `StreamReader` (daemon threading.Thread wrapping any FrameSource), `FrameQueue` (collections.deque(maxlen=2) with observable drop_count/push_count), and `CameraHeartbeat` (30-s cadence structured INFO emitter with 4-state deterministic classification: starting / healthy / degraded / stalled). Live sources never exit on decode_errors alone (D-12); file sources exit on `is_file_source && decode_errors>0 && frames_decoded>0`. Heartbeat uses `stop_event.wait(cadence)` so shutdown is within one cadence tick; caller owns the stop_event (Plan 03 IngestSupervisor). `compute_sample` is a pure function so Phase 4 `metrics.json` writer reuses it verbatim. 107/107 tests passing (86 Phase 0 + 21 new). 4 atomic commits (TDD: RED + GREEN per task). Built on top of Phase 0 `_BaseCvCapture` without rewriting it — post-open 5-frame drop and green-frame guard stay where they already live. Hand-off to Plan 01-02: extend `StreamReader.run()` with exponential-backoff reconnect around the existing `fs.open()` branch + watchdog that releases stuck captures via `stats.last_frame_monotonic` polling.

### Active TODOs

*Cross-phase work items that don't fit a single plan. None at roadmap time.*

_(none)_

### Blockers

*Anything preventing forward progress. Phase 0 exists specifically to answer empirical blockers Q1-Q6 from SUMMARY.md §6 before Phase 1 can begin.*

_(none — Phase 0 will burn down the empirical blockers)_

---

## Session Continuity

**Last session**: 2026-04-17 — Phase 1 Plan 01-01 completed (StreamReader + FrameQueue + CameraHeartbeat)
**Stopped at**: Completed Phase 1 Plan 01-01 (Multi-stream reader + heartbeat foundation)
**Next session entry point**: Execute `01-02-PLAN.md` (watchdog + reconnect)

Resume instructions for a fresh Claude context:

1. Read `.planning/PROJECT.md` for core value and constraints (amended 2026-04-16 for GPU pivot; 2026-04-13 for Vue→React)
2. Read `.planning/ROADMAP.md` for phase structure (5 phases, Phase 0 complete, Phase 1 in progress 1/3)
3. Read `.planning/REQUIREMENTS.md` → Traceability table for REQ→phase mapping (63 REQs; ENV-01..05, ING-06 complete from Phase 0; ING-01, ING-04, ING-05, OPS-01 complete from Phase 1 Plan 01-01)
4. Read `.planning/phases/00-environment-sanity/PHASE0-REPORT.json` for real-host measurements (DVR latency, CPU cores, RAM, cold-start times per model)
5. Read `.planning/phases/01-multi-stream-ingest-reconnect/01-01-SUMMARY.md` for the Plan 01-01 deliverables and the concrete hand-off notes to Plans 01-02 / 01-03 / Phase 2 / Phase 4
6. Read `.planning/phases/01-multi-stream-ingest-reconnect/01-CONTEXT.md` for locked decisions D-01..D-13 and gray-area defaults G-01..G-04 (all still authoritative for Plans 01-02 / 01-03)
7. Read `src/home_cctv/ingest/stream_reader.py` + `src/home_cctv/obs/heartbeat.py` — these are now locked public surfaces; do not rewrite, extend
8. Current phase is 1 (Multi-Stream Ingest & Reconnect); 1/3 plans done; user may still need to flip WSL2 networking to mirrored before the Plan 01-02 reconnect watchdog is exercised against the live DVR

---
*Last updated: 2026-04-17 after Phase 1 Plan 01-01 completion*
