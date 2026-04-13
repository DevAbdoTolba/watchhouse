"""``python -m home_cctv`` entry point.

Plan 00-02 wires the FrameSource, ShutdownSupervisor, and DisplaySink into a
minimal end-to-end capture loop so that ``--mp4 <path>`` is a true regression
test for the live pipeline (ING-06). The ``--phase0`` harness body itself
ships in Plan 00-03.
"""
from __future__ import annotations

import argparse
import sys

# Importing the package runs the pre-cv2 env setup.
import home_cctv  # noqa: F401


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="home_cctv")
    p.add_argument(
        "--phase0", action="store_true", help="Run Phase 0 measurement harness"
    )
    p.add_argument(
        "--mp4",
        type=str,
        default=None,
        help="Path to offline MP4 for regression testing",
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
    from home_cctv.ingest.supervisor import (
        ShutdownSupervisor,
        install_signal_handlers,
    )
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

    supervisor = ShutdownSupervisor()
    install_signal_handlers(supervisor)

    if args.phase0:
        from home_cctv.phase0.sanity import PHASE0_DURATION_SEC, run_phase0

        duration_sec = 5 if args.mp4 else PHASE0_DURATION_SEC
        report = run_phase0(
            settings,
            mp4_override=args.mp4,
            duration_sec=duration_sec,
            show=args.show,
            skip_model_bundle=bool(args.mp4),
            skip_network_probe=bool(args.mp4),
            report_path=None,
        )
        return 0 if report.phase0_verdict == "pass" else 1

    if args.mp4 is None:
        logger.info("no_work_to_do hint='pass --mp4 PATH or --phase0'")
        return 0

    return _run_mp4_capture_loop(args, settings, logger, supervisor)


def _run_mp4_capture_loop(
    args: argparse.Namespace,
    settings,  # type: ignore[no-untyped-def]
    logger,  # type: ignore[no-untyped-def]
    supervisor,  # type: ignore[no-untyped-def]
) -> int:
    from home_cctv.ingest.capture import open_frame_source
    from home_cctv.ingest.display import DisplaySink

    source_id = "cam0:mp4"
    fs = open_frame_source(args.mp4, camera_id=source_id)
    supervisor.register(fs)

    sink = DisplaySink.open(
        source_id=source_id,
        out_dir=settings.EVENT_IMAGE_DIR,
        show=args.show,
    )

    try:
        fs.open()
    except FileNotFoundError as exc:
        logger.error("[%s] mp4_missing err=%r", source_id, exc)
        return 2
    except RuntimeError as exc:
        logger.error("[%s] open_failed err=%r", source_id, exc)
        return 1

    logger.info(
        "[%s] capture_started target=%s is_file_source=%s",
        source_id,
        args.mp4,
        fs.is_file_source,
    )

    try:
        while not supervisor.stop_event.is_set():
            ok, frame = fs.read()
            if not ok:
                # EOF heuristic applies ONLY to file sources. Live RTSP must
                # NEVER exit on a single early decode error — Cam 3/4 produce
                # NAL-unit-0 errors early by design (CONTEXT.md §"New Facts" #2).
                if (
                    fs.is_file_source
                    and fs.stats.decode_errors > 0
                    and fs.stats.frames_decoded > 0
                ):
                    logger.info(
                        "[%s] eof frames_decoded=%d measured_fps=%.2f",
                        source_id,
                        fs.stats.frames_decoded,
                        fs.stats.measured_fps,
                    )
                    break
                continue
            sink.write(frame)
            if fs.stats.frames_decoded % 25 == 0:
                logger.info(
                    "[%s] frame_ok size=%dx%d fps=%.2f frame_idx=%d",
                    source_id,
                    frame.shape[1],
                    frame.shape[0],
                    fs.stats.measured_fps,
                    fs.stats.frames_decoded,
                )
    except Exception as exc:  # pragma: no cover — defensive
        logger.error("[%s] capture_error err=%r", source_id, exc)
        return 1
    finally:
        try:
            sink.close()
        except Exception:
            pass
        supervisor.shutdown()

    logger.info(
        "[%s] done frames=%d corrupted=%d errors=%d fps=%.2f",
        source_id,
        fs.stats.frames_decoded,
        fs.stats.frames_corrupted,
        fs.stats.decode_errors,
        fs.stats.measured_fps,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
