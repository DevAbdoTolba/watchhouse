# Project - Watchhouse

A local AI surveillance platform for legacy DVRs. Bridges 4 RTSP IP cameras
on a BitVision / Cantonk DVR and (eventually) layers AI event detection
(people, faces, plates, zones, loitering) on top. Single-file Windows
executable. No cloud, no installer.

Versioned via git tags; each tag is a single shippable feature increment.

## Constraints

- Windows-native Python app.
- Single-file `.exe` build via PyInstaller.
- Credentials live in `.env`, never committed.
- RTSP forced over TCP with the working ffplay flags (`nobuffer`,
  `discardcorrupt`, `ignore_err`) since UDP drops on this LAN.
- Dark, professional, restrained UI. No AI-slop tropes (no gradient text,
  no glassmorphism, no side-stripe borders, no identical card grids).

## Layout

- `app/` - the application package (`python -m app` runs it).
- `app/core/` - RTSP, config, camera definitions, DVR probe, LAN discovery,
  IP cache, log bus.
- `app/ui/` - PySide6 windows, widgets, theme, console panel.
- `build_exe.py` - PyInstaller wrapper that produces `dist/Watchhouse.exe`.
- `.agents/skills/impeccable/` - design skill used to keep UI work honest.

## Versions (git tags so far)

- `v0.1.0`-`v0.1.5` - 4-camera live grid, sub/main toggle, auto-discovery,
  IP cache, admin log console, reconnect.
- `v0.2.0+` - DVR playback, then AI feature ladder (detection, zones, face,
  ALPR, hardening, dashboard).

Each release lives on its own `release/vX.Y.Z` branch where appropriate, gets
merged to `main` with `--no-ff`, and is annotated-tagged. `main` is always
the latest shipped state.

## Conventions

- Two-space PEP-8-ish formatting, isort default order, no trailing whitespace.
- Avoid comments that restate the code. Comment only the why.
- Qt slots use `@Slot` decorators.
- Long-running work runs in a `QThread`; UI thread never blocks on I/O.
- One module per concern. No mega-files.
- Anything that touches a URL must run it through `mask_url()` before logging.
