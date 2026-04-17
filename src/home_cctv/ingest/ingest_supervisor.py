"""Top-level ingest composition — Phase 1 Plan 01-03 Task 2.

``IngestSupervisor`` composes everything Plans 01-01 and 01-02 delivered
into a single ``start()`` / ``shutdown()`` lifecycle:

* 4 ``StreamReader`` daemon threads (one per camera)
* 4 ``ReadWatchdog`` daemon threads (one per reader)
* 4 ``CameraHeartbeat`` daemon threads (one per reader)
* 1 ``MainStreamGrabber`` (reentrant, no owned thread)

All of them share the single ``ShutdownSupervisor.stop_event`` installed by
``home_cctv.__main__`` so SIGINT drains the whole pipeline within 2 s.

Start-degraded policy (CONTEXT §G-02): an initial per-camera probe sanity-
checks reachability before boot. If ≥1 camera is reachable, the supervisor
boots with the failing ones in their normal reconnect loop from t=0 (Plan 02
behavior). Only when ALL 4 probes fail does ``start()`` raise
``RuntimeError("no cameras reachable ...")`` — and it does so BEFORE any
reader/watchdog/heartbeat thread is spawned so no threads leak.

Path-traversal guard (T-03-03): every ``cam.sub_path`` AND every ``cam.main_path``
from ``cameras.yaml`` is validated before any thread is spawned. A malformed
path raises ``ValueError(f"malformed sub_path: {…!r}")`` or
``ValueError(f"malformed main_path: {…!r}")`` with zero side effects. This
keeps a tampered cameras.yaml from producing RTSP URLs that target
unexpected resources on the DVR.

MP4 mode (``mp4_mode=True``): the initial probe short-circuits — no
``cv2.VideoCapture`` is ever constructed. The injected
``frame_source_factory`` returns ``Mp4FrameSource`` instances which the
reader threads exercise normally. This is the regression path used by
``python -m home_cctv --live --mp4-mode PATH`` and by Tests 1–6 of the
supervisor test module.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2

from home_cctv.config.cameras import (
    CameraConfig,
    CamerasFile,
    build_rtsp_url,
    load_cameras,
)
from home_cctv.config.env import Settings
from home_cctv.ingest.capture import FrameSource, open_frame_source
from home_cctv.ingest.main_stream_grabber import MainStreamGrabber
from home_cctv.ingest.stream_reader import FrameQueue, StreamReader
from home_cctv.ingest.supervisor import ShutdownSupervisor
from home_cctv.ingest.watchdog import ReadWatchdog
from home_cctv.obs.heartbeat import CameraHeartbeat

_LOG = logging.getLogger("home_cctv.ingest.ingest_supervisor")

# --------------------------------------------------------------------- constants

#: Number of probe attempts per camera before declaring it unreachable (CONTEXT §G-02).
INITIAL_PROBE_ATTEMPTS: int = 3

#: Wall-clock cap per probe attempt — ``cv2.VideoCapture`` has no native
#: timeout kwarg (stimeout=5 s in the env string is FFmpeg-level, not Python-
#: level), so each attempt is wrapped in a side-thread joined with this timeout.
INITIAL_PROBE_TIMEOUT_S: float = 5.0

#: All 4 parallel probes must finish within this envelope. Worst case with
#: everything down: 4 cameras × 3 attempts × ~5 s ≈ 60 s if serial, but with
#: the ThreadPoolExecutor(max_workers=4) it's ≤15 s.
INITIAL_PROBE_OVERALL_DEADLINE_S: float = 15.0

#: Per-thread join grace during shutdown. Matches ``ShutdownSupervisor`` budget.
SHUTDOWN_GRACE_S: float = 2.0


# --------------------------------------------------------------------- dataclass


@dataclass
class CameraRuntime:
    """Bundle of live runtime objects for one camera."""

    camera_id: int
    name: str
    reader: StreamReader
    watchdog: ReadWatchdog
    heartbeat: CameraHeartbeat
    initial_probe_ok: bool


# --------------------------------------------------------------------- guard


def _validate_path(path: str, field_name: str) -> None:
    """Raise ``ValueError`` unless ``path`` is a safe RTSP URL tail.

    Requirements (T-03-03):

    * must start with ``/``
    * must NOT contain ``..``
    * must NOT contain control characters (``ord(c) < 32``)
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"malformed {field_name}: {path!r}")
    if ".." in path:
        raise ValueError(f"malformed {field_name}: {path!r}")
    if any(ord(c) < 32 for c in path):
        raise ValueError(f"malformed {field_name}: {path!r}")


# --------------------------------------------------------------------- supervisor


class IngestSupervisor:
    """Composes all Phase 1 ingest workers under one lifecycle."""

    def __init__(
        self,
        *,
        settings: Settings,
        cameras_yaml_path: Path,
        shutdown: Optional[ShutdownSupervisor] = None,
        frame_source_factory: Optional[Callable[[str, str], FrameSource]] = None,
        main_capture_factory: Optional[Callable[[str], "cv2.VideoCapture"]] = None,
        mp4_mode: bool = False,
    ) -> None:
        self.settings = settings
        self.cameras_file: CamerasFile = load_cameras(cameras_yaml_path)
        self.shutdown_sup: ShutdownSupervisor = shutdown or ShutdownSupervisor()
        # Default factory — every reader gets a real FrameSource resolved
        # from its target URL (RTSP or MP4 path). Tests inject a factory
        # that returns a Mp4FrameSource unconditionally.
        self._frame_source_factory = (
            frame_source_factory
            or (lambda target, camera_id: open_frame_source(target, camera_id=camera_id))
        )
        self._main_capture_factory = main_capture_factory
        self._mp4_mode: bool = bool(mp4_mode)
        self.runtimes: Dict[int, CameraRuntime] = {}
        self.grabber: Optional[MainStreamGrabber] = None
        self._started: bool = False
        self._start_t0: float = 0.0

    # ------------------------------------------------------------ probe: one
    def _probe_single_camera(self, cam: CameraConfig) -> Tuple[bool, str]:
        """Probe one camera with a side-thread timeout per attempt.

        Uses the G-01 pattern — ``cv2.VideoCapture`` has no native timeout so
        every open is wrapped in a watcher thread that we join with
        ``INITIAL_PROBE_TIMEOUT_S``. On timeout we cross-thread-release the
        capture (the worker thread is daemon, so even if it never returns it
        cannot block process exit).
        """
        url = build_rtsp_url(self.settings, cam, stream="sub")
        last_reason = "probe_unknown"
        for attempt in range(INITIAL_PROBE_ATTEMPTS):
            cap_holder: List[Optional["cv2.VideoCapture"]] = [None]

            def _open() -> None:
                try:
                    cap_holder[0] = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                except Exception:  # pragma: no cover — defensive
                    cap_holder[0] = None

            worker = threading.Thread(target=_open, daemon=True)
            worker.start()
            worker.join(timeout=INITIAL_PROBE_TIMEOUT_S)
            if worker.is_alive():
                # Probe timed out — cross-thread release per G-01.
                if cap_holder[0] is not None:
                    try:
                        cap_holder[0].release()
                    except Exception:  # pragma: no cover — defensive
                        pass
                last_reason = "probe_timeout"
            else:
                cap = cap_holder[0]
                if cap is not None and cap.isOpened():
                    try:
                        cap.release()
                    except Exception:  # pragma: no cover — defensive
                        pass
                    _LOG.info(
                        "probe_ok camera_id=%d attempt=%d", cam.id, attempt
                    )
                    return (True, "ok")
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:  # pragma: no cover — defensive
                        pass
                last_reason = "probe_closed"
            # Between attempts: interruptible back-off, honouring stop_event.
            if self.shutdown_sup.stop_event.wait(0.5 * (attempt + 1)):
                return (False, "stop_requested")
        return (False, last_reason)

    # ------------------------------------------------------------ probe: all
    def _initial_probe(self) -> Dict[int, Tuple[bool, str]]:
        """Run one probe per camera; parallel in live mode, short-circuit in mp4."""
        if self._mp4_mode:
            # MP4-mode: do not touch cv2.VideoCapture at all. Reader threads
            # validate fixture existence inside Mp4FrameSource.open().
            return {
                cam.id: (True, "mp4_mode") for cam in self.cameras_file.cameras
            }
        results: Dict[int, Tuple[bool, str]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_id: Dict[concurrent.futures.Future, int] = {
                executor.submit(self._probe_single_camera, cam): cam.id
                for cam in self.cameras_file.cameras
            }
            done, not_done = concurrent.futures.wait(
                future_to_id.keys(),
                timeout=INITIAL_PROBE_OVERALL_DEADLINE_S,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            for fut in done:
                cam_id = future_to_id[fut]
                try:
                    results[cam_id] = fut.result()
                except Exception as exc:  # pragma: no cover — defensive
                    results[cam_id] = (False, f"probe_exception: {exc!r}")
            for fut in not_done:
                cam_id = future_to_id[fut]
                results[cam_id] = (False, "overall_deadline")
        return results

    # ---------------------------------------------------------- start
    def start(self) -> None:
        """Boot all 4 reader/watchdog/heartbeat trios + the main-stream grabber.

        Performs path-traversal validation first, then the initial probe,
        then thread construction. No threads are spawned until every
        cameras.yaml entry has been accepted and at least one probe passes.
        """
        if self._started:
            raise RuntimeError("IngestSupervisor.start() called twice")
        self._start_t0 = time.monotonic()

        # --- Path-traversal guard (T-03-03) - BEFORE any thread spawn ---
        for cam in self.cameras_file.cameras:
            _validate_path(cam.sub_path, "sub_path")
            _validate_path(cam.main_path, "main_path")

        _LOG.info(
            "ingest_starting masked_rtsp=%s mp4_mode=%s cameras=%d",
            self.settings.masked_rtsp_base(),
            self._mp4_mode,
            len(self.cameras_file.cameras),
        )

        probe_results = self._initial_probe()
        degraded = [
            cid for cid, (ok, _reason) in probe_results.items() if not ok
        ]
        if all(not ok for (ok, _reason) in probe_results.values()):
            raise RuntimeError(
                "no cameras reachable after initial probe — check DVR + network + credentials"
            )

        # --- Spawn one reader / watchdog / heartbeat trio per camera ---
        for cam in self.cameras_file.cameras:
            source_id = f"cam{cam.id}:{cam.name}"
            sub_url = build_rtsp_url(self.settings, cam, stream="sub")
            # Inject factory so tests can pass the MP4 path instead of the
            # real credentialed URL.
            fs = self._frame_source_factory(sub_url, source_id)
            # Register BEFORE starting threads so SIGINT during boot still
            # releases the source even if the reader hasn't yet started.
            try:
                self.shutdown_sup.register(fs)
            except Exception as exc:  # pragma: no cover — defensive
                _LOG.warning(
                    "register_failed camera_id=%d err=%r", cam.id, exc
                )

            queue = FrameQueue(maxlen=2)
            reader = StreamReader(
                camera_id=source_id,
                frame_source=fs,
                stop_event=self.shutdown_sup.stop_event,
                queue=queue,
            )
            watchdog = ReadWatchdog(
                camera_id=source_id,
                frame_source=fs,
                stats=fs.stats,
                stop_event=self.shutdown_sup.stop_event,
            )
            heartbeat = CameraHeartbeat(
                camera_id=source_id,
                stats=fs.stats,
                queue=queue,
                expected_fps=float(cam.advertised_sub_fps),
                stop_event=self.shutdown_sup.stop_event,
            )
            reader.start()
            watchdog.start()
            heartbeat.start()

            self.runtimes[cam.id] = CameraRuntime(
                camera_id=cam.id,
                name=cam.name,
                reader=reader,
                watchdog=watchdog,
                heartbeat=heartbeat,
                initial_probe_ok=probe_results[cam.id][0],
            )

        # --- Build the main-stream grabber ---
        cam_by_id: Dict[int, CameraConfig] = {
            c.id: c for c in self.cameras_file.cameras
        }

        def _url_resolver(camera_id: int) -> str:
            cam = cam_by_id[camera_id]
            return build_rtsp_url(self.settings, cam, stream="main")

        self.grabber = MainStreamGrabber(
            url_resolver=_url_resolver,
            stop_event=self.shutdown_sup.stop_event,
            capture_factory=self._main_capture_factory,
        )

        self._started = True
        _LOG.info(
            "ingest_started cameras_booted=%d degraded_cameras=%s",
            len(self.runtimes),
            degraded,
        )

    # ---------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        """Drain all ingest workers within ~2 s. Idempotent."""
        if not self._started:
            return
        self._started = False
        # Phase 0 ShutdownSupervisor: first flip stop_event, then force-release
        # every registered FrameSource cross-thread (the G-01 unblock path).
        self.shutdown_sup.request_stop()
        self.shutdown_sup.shutdown()
        # Join each trio with a per-thread timeout. The daemon=True flag
        # means any thread that can't exit in time won't block process exit.
        for rt in self.runtimes.values():
            try:
                rt.reader.join(timeout=SHUTDOWN_GRACE_S)
            except Exception as exc:  # pragma: no cover — defensive
                _LOG.warning(
                    "reader_join_failed camera_id=%d err=%r", rt.camera_id, exc
                )
            try:
                rt.watchdog.join(timeout=SHUTDOWN_GRACE_S)
            except Exception as exc:  # pragma: no cover — defensive
                _LOG.warning(
                    "watchdog_join_failed camera_id=%d err=%r",
                    rt.camera_id,
                    exc,
                )
            try:
                rt.heartbeat.join(timeout=SHUTDOWN_GRACE_S)
            except Exception as exc:  # pragma: no cover — defensive
                _LOG.warning(
                    "heartbeat_join_failed camera_id=%d err=%r",
                    rt.camera_id,
                    exc,
                )
        if self.grabber is not None:
            self.grabber.shutdown()
        runtime_s = time.monotonic() - self._start_t0
        _LOG.info("ingest_stopped runtime_s=%.2f", runtime_s)


__all__ = [
    "IngestSupervisor",
    "CameraRuntime",
    "INITIAL_PROBE_ATTEMPTS",
    "INITIAL_PROBE_TIMEOUT_S",
    "INITIAL_PROBE_OVERALL_DEADLINE_S",
    "SHUTDOWN_GRACE_S",
]
