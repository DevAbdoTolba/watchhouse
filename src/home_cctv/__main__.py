"""`python -m home_cctv` entry point."""
from __future__ import annotations

import argparse
import sys

# Importing the package runs the pre-cv2 env setup.
import home_cctv  # noqa: F401


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="home_cctv")
    p.add_argument("--phase0", action="store_true", help="Run Phase 0 measurement harness")
    p.add_argument(
        "--mp4", type=str, default=None, help="Path to offline MP4 for regression testing"
    )
    p.add_argument(
        "--show",
        action="store_true",
        help="Open cv2.imshow preview window (falls back if no display)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from home_cctv.config.env import load_settings, validate_runtime_paths
    from home_cctv.obs.logging_setup import configure_logging

    try:
        settings = load_settings()
        validate_runtime_paths(settings)
    except RuntimeError as exc:
        print(f"STARTUP ERROR: {exc}", file=sys.stderr)
        return 2

    logger = configure_logging(settings.LOG_DIR)
    logger.info(
        "booted version=%s phase0=%s mp4=%s show=%s dvr=%s event_dir=%s",
        home_cctv.__version__,
        args.phase0,
        args.mp4,
        args.show,
        settings.masked_rtsp_base(),
        settings.EVENT_IMAGE_DIR,
    )
    # Plan 01 stub; Plan 03 replaces with Phase 0 harness call.
    return 0


if __name__ == "__main__":
    sys.exit(main())
