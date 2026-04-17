---
status: partial
phase: 01-multi-stream-ingest-reconnect
source: [01-VERIFICATION.md]
started: 2026-04-17T20:56:13Z
updated: 2026-04-17T20:56:13Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. SC 2 — 30-second DVR cable-pull recovery
expected: Run `uv run python -m home_cctv --live`, wait until all 4 cameras log `reader_connected` + initial `heartbeat state=healthy`. Physically unplug the DVR's network cable (or kill its LAN) for 30 seconds, then reconnect. Within 60 seconds of reconnect, all 4 streams resume (4 × `reconnect_attempt` lines with ceiling climbing 1 → 2 → 4 → 8 → 16 → 30 s visible in logs, followed by 4 × `reader_connected` and fresh `heartbeat` lines). Process does NOT restart. No green/partial frames bleed into `FrameQueue` after reconnect (the 5-frame post-open drop absorbs them inside `_BaseCvCapture`).
result: [pending]

### 2. SC 3 — iptables-blocked port 554 watchdog force-release
expected: With `uv run python -m home_cctv --live` running and all 4 cameras healthy, run `sudo iptables -I OUTPUT -p tcp --dport 554 -j DROP` on the WSL2 host for 60 seconds. Within 10 seconds of the block, every camera logs one `watchdog_release camera_id=… age_s=… threshold_s=10.0 hang_events=N` line, followed by `reader_source_released_externally` and the backoff reconnect sequence. When the iptables rule is removed (`sudo iptables -D OUTPUT -p tcp --dport 554 -j DROP`), all 4 cameras reconnect within the backoff window and resume emitting healthy heartbeats. No process hang, no zombie ffmpeg subprocesses (`ps aux | grep ffmpeg` shows nothing leftover).
result: [pending]

### 3. SC 5 — Live main-stream on-demand grab + semaphore + TTL cache
expected: With `uv run python -m home_cctv --live` running, attach a Python REPL to the process (or write a small helper script using the same `IngestSupervisor`) and invoke `supervisor.grabber.grab_main_frame(camera_id=2)`. Observe exactly one `main_grab_open camera_id=2` log line, one bytes-returned JPEG, one `main_grab_release`. Immediately call the same API 4 more times within 2 seconds → the next 4 calls log `main_grab_cache_hit` (NOT another `_open`). After 2 seconds elapse, the next call logs a fresh `main_grab_open`. During any single call, DVR session count stays at 5 (4 sub-streams + 1 main), verifiable by checking the DVR admin panel's active-sessions view or `ss -tn dst 192.168.1.10` on the WSL2 host. Two concurrent callers targeting the same `camera_id` within 2 s (e.g. threading two grab calls with a Barrier) produce exactly ONE `main_grab_open` — the second caller hits `main_grab_cache_hit_after_wait`.
result: [pending]

## Summary

total: 3
passed: 0
issues: 0
pending: 3
skipped: 0
blocked: 0

## Gaps
