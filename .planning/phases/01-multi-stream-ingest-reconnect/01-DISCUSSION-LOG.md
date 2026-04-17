# Phase 1: Multi-Stream Ingest & Reconnect — Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in `01-CONTEXT.md`. This log records the interaction pace chosen
> by the user and the pre-analyzed gray areas that were *not* explicitly answered.

**Date:** 2026-04-17
**Phase:** 01-multi-stream-ingest-reconnect
**Areas discussed:** (none — user skipped discussion)

---

## Meta question asked

| Option | Description | Selected |
|--------|-------------|----------|
| I'll pick defaults, you review | Auto-select recommended answers, write CONTEXT.md, user reviews | |
| Walk me through each area | One question per area, 4 short interactions | |
| Just skip discussion, go straight to planning | Minimal context, planner handles the details | ✓ |

**User's choice:** Skip discussion — route straight to `/gsd-plan-phase 1`.
**Notes:** The user initially entered the full discuss flow but opted out of answering gray-area questions. CONTEXT.md was still written so downstream agents have the carry-forward decisions + canonical refs to work from. Gray areas marked "Planner's discretion" with recommended defaults inline.

## Gray areas that were identified but not explicitly discussed

Captured in `01-CONTEXT.md` under "Planner's discretion" with recommended defaults:

- **G-01** Watchdog mechanism — side-thread close + `stimeout=5s` (recommended); alternatives: executor timeout, process-per-camera (latter requires re-approving D-01).
- **G-02** Startup failure policy — start-degraded with per-camera reconnect loop from t=0; refuse to start only if all 4 unreachable after brief probe.
- **G-03** Main-stream grab API — synchronous `grab_main_frame(camera_id) -> bytes | None`, serialized via a global `Semaphore(1)` + 2s TTL cache, owned by a single MainStreamGrabber thread.
- **G-04** Health heartbeat — every 30 s per camera, one structured log line to the rotating handler. No `metrics.json` yet (Phase 4 / OPS-02).

## Claude's Discretion

All four gray areas above.

## Deferred Ideas

See `01-CONTEXT.md` <deferred> block.
