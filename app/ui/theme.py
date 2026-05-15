"""Centralized color tokens and QSS stylesheet.

Palette is Restrained: a tinted-ink surface family plus one warm amber accent
that surfaces only on active selection. Status colors (teal-green, amber, brick-red)
appear as tiny indicator dots and never as fills.

OKLCH targets are noted next to the hex approximations so future adjustments
stay anchored to the perceptual values, not the sRGB triplet.
"""

# OKLCH(0.18 0.012 250) — deep tinted ink
INK         = "#181c25"
# OKLCH(0.21 0.011 250) — primary surface
SURFACE     = "#1f242e"
# OKLCH(0.23 0.010 250) — toolbar / header band
SURFACE_2   = "#22272f"
# OKLCH(0.17 0.011 250) — video letterbox / behind frames
VIDEO_BG    = "#13161d"
# Borders
BORDER      = "#2a3040"
BORDER_2    = "#363d4d"
# Text
TEXT        = "#ebe7e1"   # OKLCH(0.94 0.005 80)  warm off-white
TEXT_MUTED  = "#8a91a0"   # OKLCH(0.66 0.008 250)
TEXT_DIM    = "#5d6573"   # OKLCH(0.52 0.008 250)
# Accent  warm amber/copper, used sparingly
ACCENT      = "#c69561"   # OKLCH(0.74 0.13 70)
ACCENT_BG   = "#2a2017"
# Status
OK          = "#4ea683"   # OKLCH(0.72 0.14 155)  teal-green
WARN        = "#d0a45c"   # OKLCH(0.78 0.14 78)   amber
ERROR       = "#b25c4e"   # OKLCH(0.66 0.18 25)   brick-red


STYLESHEET = f"""
* {{
    font-family: "Segoe UI Variable", "Segoe UI", system-ui, -apple-system, sans-serif;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: {INK};
}}

QMainWindow, QWidget {{
    background: {INK};
}}

QToolTip {{
    background: {SURFACE_2};
    color: {TEXT};
    border: 1px solid {BORDER_2};
    padding: 4px 8px;
    font-size: 11px;
}}

#Toolbar {{
    background: {SURFACE_2};
    border-bottom: 1px solid {BORDER};
}}

#Brand {{
    color: {TEXT};
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.12em;
}}

#BrandSeparator {{
    color: {TEXT_DIM};
    font-size: 13px;
    font-weight: 300;
}}

#Version {{
    color: {TEXT_MUTED};
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.08em;
}}

#ToolbarAction {{
    background: transparent;
    border: 1px solid {BORDER_2};
    border-radius: 2px;
    color: {TEXT_MUTED};
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.06em;
    padding: 6px 14px;
}}
#ToolbarAction:hover {{
    border-color: {ACCENT};
    color: {TEXT};
}}
#ToolbarAction:pressed {{
    background: {ACCENT_BG};
}}

#Grid {{
    background: {INK};
}}

#CameraTile {{
    background: {SURFACE};
    border: 1px solid {BORDER};
}}

#TileHeader {{
    background: {SURFACE_2};
    border-bottom: 1px solid {BORDER};
}}

#TileName {{
    color: {TEXT};
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.10em;
}}

#TileLocation {{
    color: {TEXT_MUTED};
    font-size: 11px;
    font-weight: 400;
}}

#StreamToggle {{
    background: transparent;
    border: 1px solid {BORDER_2};
    border-radius: 2px;
    color: {TEXT_MUTED};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    padding: 3px 10px;
}}
#StreamToggle:hover {{
    border-color: {TEXT_MUTED};
    color: {TEXT};
}}
#StreamToggle:checked {{
    background: {ACCENT_BG};
    border-color: {ACCENT};
    color: {ACCENT};
}}

#StatusBar {{
    background: {SURFACE_2};
    border-top: 1px solid {BORDER};
}}

#StatusBarText {{
    color: {TEXT_MUTED};
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 11px;
    letter-spacing: 0.04em;
}}
"""
