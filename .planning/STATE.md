---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: unknown
last_updated: "2026-04-13T16:48:12.733Z"
progress:
  total_phases: 5
  completed_phases: 0
  total_plans: 3
  completed_plans: 0
  percent: 0
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

Phase: 0 (Environment & Sanity) — EXECUTING
Plan: 1 of 3

- **current_phase**: 0
- **current_phase_name**: Environment & Sanity
- **current_plan**: none
- **status**: ready
- **progress**: 0%

```
[                    ] 0 / 5 phases complete
```

**Next action**: `/gsd-plan-phase 0` to decompose Phase 0 (Environment & Sanity) into executable plans.

---

## Phase Overview

| # | Phase | Status | Plans |
|---|---|---|---|
| 0 | Environment & Sanity | Not started | 0/0 |
| 1 | Multi-Stream Ingest & Reconnect | Not started | 0/0 |
| 2 | Detection, Tracking & Zoned Events | Not started | 0/0 |
| 3 | Trigger & Catch Event Models | Not started | 0/0 |
| 4 | Hardening & Operations | Not started | 0/0 |

---

## Performance Metrics

*Populated as phases complete. Tracks plan/phase duration, rework rate, node-repair events, verifier bounces.*

- **phases_completed**: 0 / 5
- **plans_completed**: 0
- **avg_plan_duration**: —
- **node_repairs**: 0
- **verifier_bounces**: 0

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
- **2026-04-16** — **Stack pivoted from CPU-only to GPU-primary.** User confirmed RTX 3060 Laptop (6 GB VRAM) is exposed to WSL2 via CUDA 12 passthrough (nvidia-smi inside WSL2 shows driver 561.09, CUDA 12.6). `torch` pinned to `2.5.1+cu121`, `tensorflow-cpu==2.16.2` → `tensorflow[and-cuda]==2.16.2`. OpenVINO and onnxruntime dropped from deps (CPU-only optimizations). All Phase 0 scaffolding code is unchanged — the pivot is purely at the dep-pin layer. `.venv` must be rebuilt (`rm -rf .venv && uv sync`) before the Phase 0 live sweep.

### Active TODOs

*Cross-phase work items that don't fit a single plan. None at roadmap time.*

_(none)_

### Blockers

*Anything preventing forward progress. Phase 0 exists specifically to answer empirical blockers Q1-Q6 from SUMMARY.md §6 before Phase 1 can begin.*

_(none — Phase 0 will burn down the empirical blockers)_

---

## Session Continuity

**Last session**: 2026-04-13 — roadmap created
**Next session entry point**: `/gsd-plan-phase 0`

Resume instructions for a fresh Claude context:

1. Read `.planning/PROJECT.md` for core value and constraints
2. Read `.planning/ROADMAP.md` for phase structure
3. Read `.planning/REQUIREMENTS.md` → Traceability table for REQ→phase mapping
4. Read `.planning/research/SUMMARY.md` §6 (empirical blockers) and §7 (build order) before touching Phase 0
5. Current phase is 0; status is `ready`; no plan has been drafted yet

---
*Last updated: 2026-04-13 by gsd-roadmapper*
