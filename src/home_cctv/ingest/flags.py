"""OPENCV_FFMPEG_CAPTURE_OPTIONS canonical builder.

This module MUST be imported before any `import cv2` anywhere in the codebase.
The string below is the ground-truth translation of terminal.txt ffplay flags +
stimeout (per SUMMARY.md §2 and PITFALLS.md §1.1 — the only knob that actually
unblocks a stalled cap.read() on WSL2).
"""
import os

# Exact string from CONTEXT.md — do not reformat, do not reorder.
OPENCV_FFMPEG_OPTIONS_STRING: str = (
    "rtsp_transport;tcp"
    "|fflags;nobuffer+discardcorrupt"
    "|flags;low_delay"
    "|err_detect;ignore_err"
    "|stimeout;5000000"
    "|reconnect;1"
    "|reconnect_streamed;1"
    "|reconnect_delay_max;2"
    "|analyzeduration;1000000"
    "|probesize;2000000"
)

_ENV_KEY = "OPENCV_FFMPEG_CAPTURE_OPTIONS"


def apply_capture_options() -> None:
    """Set OPENCV_FFMPEG_CAPTURE_OPTIONS in os.environ. Idempotent."""
    os.environ[_ENV_KEY] = OPENCV_FFMPEG_OPTIONS_STRING


def assert_capture_options_active() -> None:
    """Runtime assert: env var matches canonical string (catches re-ordering bugs)."""
    actual = os.environ.get(_ENV_KEY, "")
    if actual != OPENCV_FFMPEG_OPTIONS_STRING:
        raise RuntimeError(
            f"OPENCV_FFMPEG_CAPTURE_OPTIONS mismatch.\n"
            f"  expected: {OPENCV_FFMPEG_OPTIONS_STRING}\n"
            f"  actual:   {actual}"
        )
