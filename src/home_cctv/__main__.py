"""`python -m home_cctv` entry point."""
from __future__ import annotations

import argparse
import sys

# Importing the package runs the pre-cv2 env setup.
import home_cctv  # noqa: F401


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="home_cctv")
    p.add_argument("--phase0", action="store_true", help="Run Phase 0 measurement harness")
    p.add_argument("--mp4", type=str, default=None, help="Path to offline MP4 for regression testing")
    p.add_argument(
        "--show",
        action="store_true",
        help="Open cv2.imshow preview window (falls back if no display)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Plan 01 stub: full harness arrives in Plan 03.
    print(
        f"home_cctv v{home_cctv.__version__} booted "
        f"(phase0={args.phase0}, mp4={args.mp4}, show={args.show})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
