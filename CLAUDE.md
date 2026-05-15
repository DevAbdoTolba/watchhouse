# Project — CCTV Console

A Windows-native desktop client for a 4-camera BitVision/Cantonk DVR over RTSP.
Versioned via git tags; each tag is a single shippable feature increment.

## Constraints
- Windows-native Python app (no WSL2 for this version).
- Single-file `.exe` build via PyInstaller.
- Credentials live in `.env`, never committed.
- RTSP forced over TCP with the working ffplay flags (TCP, nobuffer,
  discardcorrupt, ignore_err) since UDP drops on this LAN.
- Dark, professional, restrained UI. No AI-slop tropes (no gradient text, no
  glassmorphism, no side-stripe borders, no identical card grids).

## Layout
- `app/` — the application package (`python -m app` runs it).
- `app/core/` — RTSP, config, camera definitions.
- `app/ui/` — PySide6 windows, widgets, theme.
- `build_exe.py` — PyInstaller wrapper that produces `dist/CCTV-Console.exe`.
- `.old/` — frozen archive of the previous AI-pipeline experiment; reference
  only, do not edit.
- `.agents/skills/impeccable/` — design skill used to keep UI work honest.

## Versions (git tags)
- `v0.1.0` — Four-camera live grid.
- `v0.2.0` — Recorded-clip playback from the DVR (planned).
- `v0.3.0` — TBD.

Each version lives on its own `release/vX.Y.Z` branch, gets merged to `main`
with `--no-ff`, and is annotated-tagged. `main` is always the latest shipped
state.

## Conventions
- Two-space PEP-8-ish formatting, isort default order, no trailing whitespace.
- Avoid comments that restate the code. Comment only the why.
- Qt slots use `@Slot` decorators.
- Long-running work runs in a `QThread`; UI thread never blocks on I/O.
- One module per concern. No mega-files.
