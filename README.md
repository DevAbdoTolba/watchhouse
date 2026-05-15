# Watchhouse

A local AI surveillance platform for legacy DVRs. Native Windows desktop app
that bridges 4 RTSP IP cameras hanging off a BitVision / Cantonk DVR and
turns them into a smart event log. No cloud, no installer, no telemetry.

Single self-contained `.exe`.

## What's shipped (v0.1.x)

- 4-camera live grid (2x2) with per-tile sub / main stream toggle
- LAN auto-discovery of the DVR when its DHCP-assigned IP moves
- IP history cache so the next launch resolves the DVR in well under a second
- Toggleable admin log console (Ctrl+L) with masked URLs and per-source filtering
- TCP-only RTSP with the ffplay tolerance flags (`nobuffer`, `discardcorrupt`,
  `ignore_err`) baked into OpenCV's FFmpeg backend
- Reconnect on stream stall with exponential backoff
- Professional dark theme; no AI-slop visual tropes

## On the roadmap

- DVR-side recorded playback (browse clips from the DVR)
- Snapshot to disk + per-tile diagnostics
- Person + vehicle detection (YOLO) with persistent tracking (ByteTrack)
- Polygon zones, zone-entry and loitering events, SQLite event log
- Triggered face recognition (DeepFace + known-faces gallery)
- Triggered license-plate reading (EasyOCR + plate validator)
- Vehicle entered / exited detection
- Retention, worker supervision, multi-day hardening
- Dashboard layer (review past events, filter, drill down)

## Run from source

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env       # edit DVR credentials
python -m app
```

## Build the single .exe

```powershell
pip install -e .[build]
python build_exe.py
```

Output: `dist\Watchhouse.exe` (one file, windowed, no console window).

## Configuration

All settings live in `.env` next to the `.exe` (or at the project root in dev).
`.env` is gitignored, `.env.example` is the template.

| Var | Purpose |
|---|---|
| `DVR_IP` | DVR LAN address (auto-updated by discovery) |
| `DVR_PORT` | RTSP port (default `554`) |
| `DVR_USER` | DVR admin user |
| `DVR_PASS` | DVR admin password |
| `CAMn_DEFAULT` | `sub` or `main` per camera (1-4) |

Watchhouse also writes `.cctv-known-dvrs.json` next to your `.env` — a tiny
local cache of recent working IPs (gitignored).

## Stream paths (BitVision / Cantonk indexed scheme)

| Camera | Sub | Main |
|---|---|---|
| Cam 1 | `/1` | `/0` |
| Cam 2 | `/11` | `/10` |
| Cam 3 | `/21` | `/20` |
| Cam 4 | `/31` | `/30` |

RTSP is forced over TCP. The pipeline tolerates the H.265 NAL-unit-0 anomaly
on cameras 3 and 4 via `discardcorrupt + ignore_err`, matching the working
ffplay configuration.

## License

TBD — pick before AI features ship.
