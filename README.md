# CCTV Console

A native Windows desktop client for a 4-camera BitVision / Cantonk DVR over RTSP.
Single executable, no installer, no telemetry, no cloud.

## v0.1.0 — Live grid

Four-tile live view of the DVR. Per-tile sub/main stream toggle, automatic
reconnect with exponential backoff, status indicators, dark professional
theme. No recording, no analytics yet, that comes in later tags.

## Run from source

```powershell
# 1. create a venv (Python 3.11 or 3.12)
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. install dependencies
pip install -e .

# 3. copy credentials template and edit
copy .env.example .env

# 4. launch
python -m app
```

## Build a single .exe

```powershell
pip install -e .[build]
python build_exe.py
```

Output: `dist\CCTV-Console.exe` (one-file, windowed, no console).

## Configuration

All settings live in `.env` at the working directory next to the exe (or
project root in dev). `.env` is gitignored, `.env.example` is the template.

| Var          | Purpose                            |
|--------------|------------------------------------|
| `DVR_IP`     | DVR LAN address                    |
| `DVR_PORT`   | RTSP port (default `554`)          |
| `DVR_USER`   | DVR admin user                     |
| `DVR_PASS`   | DVR admin password                 |
| `CAMn_DEFAULT` | `sub` or `main` per camera (1-4) |

## Stream paths (BitVision/Cantonk indexed scheme)

| Camera | Sub          | Main         |
|--------|--------------|--------------|
| Cam 1  | `/1`         | `/0`         |
| Cam 2  | `/11`        | `/10`        |
| Cam 3  | `/21`        | `/20`        |
| Cam 4  | `/31`        | `/30`        |

RTSP is forced over TCP. The pipeline tolerates the H.265 NAL-unit-0 anomaly
on cameras 3 and 4 via `discardcorrupt + ignore_err`, matching the working
ffplay configuration.
