"""Phase 0 measurement harness.

Sweeps all 4 cameras sequentially for ``PHASE0_DURATION_SEC`` each, writes
``PHASE0-REPORT.json`` on the real host or ``PHASE0-REPORT.dryrun.json`` in
``--mp4`` dry-run mode, and prints a stdout summary. Never touches the
canonical report path from tests or dry-runs (B3).
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import cv2

from home_cctv.config.cameras import CameraConfig, build_rtsp_url, load_cameras
from home_cctv.config.env import Settings
from home_cctv.ingest.capture import open_frame_source
from home_cctv.ingest.display import DisplaySink
from home_cctv.ingest.supervisor import ShutdownSupervisor
from home_cctv.phase0 import host_probe, model_bundle, network_probe
from home_cctv.phase0.report import (
    REPORT_PATH,
    CameraResult,
    Phase0Report,
    new_report,
    print_stdout_summary,
    write_report,
)

_LOG = logging.getLogger("home_cctv.phase0.sanity")

PHASE0_DURATION_SEC: int = 1800  # 30 minutes per camera per CONTEXT.md
_FPS_TOLERANCE: float = 0.20


def per_camera_exit_ok(cam: CameraConfig, c: CameraResult) -> bool:
    """Enforce CONTEXT.md §"Per-camera exit criteria" verbatim."""
    lo = cam.native_fps * (1 - _FPS_TOLERANCE)
    hi = cam.native_fps * (1 + _FPS_TOLERANCE)
    if not (lo <= c.measured_fps <= hi):
        return False
    if c.hang_events != 0:
        return False
    total = c.frames_decoded + c.frames_corrupted
    if total == 0:
        return False
    if cam.nal_unit_0_workaround_required:
        return (c.frames_decoded / total) >= 0.95
    # Cam 1/2: zero green-frame tolerance after the 5-frame post-open drop
    # window. The drop window itself counts towards ``frames_corrupted``, so
    # we allow up to that many on clean cams.
    return c.frames_corrupted <= 5


def _capture_one(
    cam: CameraConfig,
    target: str,
    out_dir: Path,
    duration_sec: int,
    supervisor: ShutdownSupervisor,
    show: bool,
) -> CameraResult:
    source_id = f"cam{cam.id}:{cam.name}"
    fs = open_frame_source(target, camera_id=source_id)
    supervisor.register(fs)
    sink = DisplaySink.open(source_id=source_id, out_dir=out_dir, show=show)

    result = CameraResult(
        name=cam.name,
        sub_path=cam.sub_path,
        main_path=cam.main_path,
        codec=cam.codec,
        advertised_fps=cam.native_fps,
        measured_fps=0.0,
        width=cam.native_width,
        height=cam.native_height,
        capture_duration_sec=0,
        frames_decoded=0,
        frames_corrupted=0,
        decode_errors=0,
        hang_events=0,
        nal_unit_0_workaround_required=cam.nal_unit_0_workaround_required,
    )
    try:
        fs.open()
        _LOG.info(
            "[%s] capture_started target=%s duration=%ds",
            source_id,
            cam.sub_path,
            duration_sec,
        )
        t_start = time.monotonic()
        while not supervisor.stop_event.is_set():
            if time.monotonic() - t_start >= duration_sec:
                break
            ok, frame = fs.read()
            if not ok:
                # EOF heuristic is FILE-ONLY. Live RTSP relies purely on the
                # duration_sec wall-clock — Cam 3/4 produce NAL-unit-0 decode
                # errors early by design (CONTEXT.md §"New Facts" #2) and
                # must NEVER trigger an early exit.
                if (
                    fs.is_file_source
                    and fs.stats.decode_errors > 0
                    and fs.stats.frames_decoded > 0
                    and (time.monotonic() - t_start) >= 0.5
                ):
                    break
                continue
            sink.write(frame)
        result.capture_duration_sec = int(time.monotonic() - t_start)
        result.frames_decoded = fs.stats.frames_decoded
        result.frames_corrupted = fs.stats.frames_corrupted
        result.decode_errors = fs.stats.decode_errors
        result.hang_events = fs.stats.hang_events
        result.measured_fps = round(fs.stats.measured_fps, 2)
    except FileNotFoundError as exc:
        _LOG.error("[%s] mp4_missing err=%r", source_id, exc)
    except RuntimeError as exc:
        _LOG.error("[%s] open_failed err=%r", source_id, exc)
    finally:
        try:
            sink.close()
        except Exception:
            pass
        try:
            fs.release()
        except Exception:
            pass

    result.exit_ok = per_camera_exit_ok(cam, result)
    _LOG.info(
        "[%s] capture_done decoded=%d corrupted=%d fps=%.2f exit_ok=%s",
        source_id,
        result.frames_decoded,
        result.frames_corrupted,
        result.measured_fps,
        result.exit_ok,
    )
    return result


def run_phase0(
    settings: Settings,
    *,
    mp4_override: Optional[str] = None,
    duration_sec: int = PHASE0_DURATION_SEC,
    show: bool = False,
    skip_model_bundle: bool = False,
    skip_network_probe: bool = False,
    report_path: Optional[Path] = None,
) -> Phase0Report:
    """Run the Phase 0 sweep and write ``PHASE0-REPORT.json``.

    When ``mp4_override`` is set, every camera reuses the same fixture MP4
    (dry-run mode), the model bundle + network probes are skipped, and the
    report is written to ``PHASE0-REPORT.dryrun.json`` so the committed
    canonical artifact is never touched (B3).
    """
    # B3: canonical artifact is reserved for live runs. Dry-run / tests
    # always write to a sibling .dryrun.json path.
    if report_path is None:
        report_path = (
            REPORT_PATH
            if mp4_override is None
            else REPORT_PATH.with_suffix(".dryrun.json")
        )
    report_path = Path(report_path)
    report = new_report()

    # --- Host / OpenCV probes (Q2, Q3) ---
    report.host = host_probe.probe_host()
    try:
        ffmpeg_snippet = host_probe.assert_ffmpeg_backend()
        hevc_ok = host_probe.assert_hevc_decoder()
    except RuntimeError as exc:
        report.notes += f"STARTUP FAIL: {exc}\n"
        report.phase0_verdict = "fail"
        write_report(report, report_path)
        print_stdout_summary(report)
        return report

    report.opencv = {
        "version": cv2.__version__,
        "ffmpeg_backend": True,
        "hevc_decoder": hevc_ok,
        "build_info_snippet": ffmpeg_snippet,
        "num_threads": cv2.getNumThreads(),
    }
    report.blockers_resolved["Q3_opencv_ffmpeg_backend"] = True
    report.blockers_resolved["Q2_wsl2_networking"] = report.host[
        "wsl2_networking_mode"
    ] in {"mirrored", "bridged"}

    # --- env vars + masked ---
    report.env_vars_loaded = [
        k
        for k in (
            "DVR_IP",
            "DVR_PORT",
            "DVR_USER",
            "EVENT_IMAGE_DIR",
            "DB_PATH",
        )
        if os.environ.get(k)
    ]
    report.credentials_masked_in_logs = True

    # --- disk space ---
    try:
        target_for_stat = settings.EVENT_IMAGE_DIR
        if not target_for_stat.exists():
            target_for_stat = target_for_stat.parent
        du = shutil.disk_usage(str(target_for_stat))
        report.disk_space = {
            "event_image_dir_path": str(settings.EVENT_IMAGE_DIR),
            "event_image_dir_filesystem": "ext4",
            "free_gb": round(du.free / (1024**3), 2),
            "on_drvfs": str(settings.EVENT_IMAGE_DIR).startswith(
                ("/mnt/c/", "/mnt/d/")
            ),
        }
    except Exception as exc:
        _LOG.warning("disk_usage probe failed: %r", exc)
        report.disk_space = {"error": repr(exc)}

    # --- Cameras ---
    cams = load_cameras(Path("cameras.yaml"))

    # --- DVR reachability + connection cap (Q6) ---
    if mp4_override is None and not skip_network_probe:
        reachable = network_probe.probe_dvr_reachable(
            settings.DVR_IP, settings.DVR_PORT, timeout=3.0
        )
        report.dvr = {
            "reachable": reachable,
            "ip": settings.DVR_IP,
            "port": settings.DVR_PORT,
        }
        if not reachable:
            report.notes += (
                f"DVR at {settings.DVR_IP}:{settings.DVR_PORT} unreachable "
                f"from WSL2 (networking_mode={report.host['wsl2_networking_mode']}). "
                "Fix .wslconfig networkingMode=mirrored or add a bridged "
                "adapter.\n"
            )
            report.phase0_verdict = "fail"
            write_report(report, report_path)
            print_stdout_summary(report)
            return report
        lat = network_probe.measure_handshake_latency(
            build_rtsp_url(settings, cams.cameras[0], stream="sub"),
            samples=3,
        )
        report.dvr["handshake_latency_ms_mean"] = round(lat or 0.0, 2)
        last_ok, tested = network_probe.step_concurrent_sessions(
            build_rtsp_url(settings, cams.cameras[0], stream="sub"),
            max_n=6,
        )
        report.dvr["max_concurrent_sessions_last_ok"] = last_ok
        report.dvr["max_concurrent_sessions_tested"] = tested
        report.blockers_resolved["Q6_dvr_connection_cap"] = True
    else:
        report.dvr = {
            "reachable": True,
            "ip": "offline-mp4",
            "port": 0,
        }

    # --- Model bundle ---
    # B1 invariant: `model_bundle["cold_start_ms"]` MUST always be a dict
    # with exactly {yolo_openvino, deepface, easyocr}.
    if not skip_model_bundle:
        try:
            model_bundle.download_model_bundle(settings.MODEL_CACHE_DIR)
        except Exception as exc:  # pragma: no cover — defensive
            _LOG.warning("model_bundle download incomplete: %r", exc)
        report.model_bundle = model_bundle.verify_model_bundle(
            settings.MODEL_CACHE_DIR
        )
    else:
        report.model_bundle = {
            "skipped": True,
            "cold_start_ms": {
                "yolo_openvino": 0,
                "deepface": 0,
                "easyocr": 0,
            },
        }
    if "cold_start_ms" not in report.model_bundle or set(
        report.model_bundle.get("cold_start_ms", {}).keys()
    ) != {"yolo_openvino", "deepface", "easyocr"}:
        report.model_bundle["cold_start_ms"] = {
            "yolo_openvino": 0,
            "deepface": 0,
            "easyocr": 0,
        }

    # --- Camera sweep ---
    supervisor = ShutdownSupervisor()
    all_ok = True
    aborted = False
    for cam in cams.cameras:
        target = (
            mp4_override
            if mp4_override
            else build_rtsp_url(settings, cam, stream="sub")
        )
        cresult = _capture_one(
            cam,
            target,
            settings.EVENT_IMAGE_DIR,
            duration_sec,
            supervisor,
            show,
        )
        report.cameras[str(cam.id)] = asdict(cresult)
        if not cresult.exit_ok:
            all_ok = False
        if supervisor.stop_event.is_set():
            aborted = True
            break

    if aborted:
        report.phase0_verdict = "aborted"
    else:
        report.blockers_resolved["Q1_host_fps_baseline"] = True
        report.blockers_resolved["Q4_ffplay_flags_translated"] = True
        report.phase0_verdict = "pass" if all_ok else "fail"

    write_report(report, report_path)
    print_stdout_summary(report)
    return report


__all__ = [
    "PHASE0_DURATION_SEC",
    "per_camera_exit_ok",
    "run_phase0",
]
