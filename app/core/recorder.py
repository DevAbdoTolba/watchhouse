"""Watchhouse local recorder.

One ffmpeg subprocess per camera, each invoked with `-c copy -f segment`
so the H.264/H.265 bitstream is bit-copied to MP4 files without
re-encoding (~zero CPU). The segment muxer rolls over every
`recording_segment_minutes`. File names use the camera's local wall
clock via `-strftime 1`, producing a flat-per-camera layout:

    <recording_dir>/cam1/2026-05-16T15-00-00.mp4
    <recording_dir>/cam1/2026-05-16T15-30-00.mp4
    ...

A separate retention pruner walks the tree on a 5-minute timer and
deletes files older than `recording_retention_days`.

If ffmpeg dies (network drop, decoder error), the worker waits with
exponential backoff and respawns.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from app.core.cameras import Camera
from app.core.config import Settings
from app.core.log import bus, mask_url


def _ffmpeg_path() -> str:
    """Returns the path to the bundled ffmpeg binary."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        bus.error("REC", f"could not locate ffmpeg binary: {e!s}")
        raise


class RecorderWorker(QThread):
    """One ffmpeg subprocess per camera. Restarts on death with backoff."""

    status_changed = Signal(str)  # "starting" | "recording" | "reconnecting" | "stopped" | "error"
    segment_opened = Signal(str)  # absolute path to a new segment

    INITIAL_BACKOFF_S = 1.0
    MAX_BACKOFF_S = 30.0

    def __init__(self, camera: Camera, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._camera = camera
        self._settings = settings
        self._stop = False
        self._proc: subprocess.Popen | None = None

    def request_stop(self) -> None:
        self._stop = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _cam_dir(self) -> Path:
        return self._settings.recording_dir / f"cam{self._camera.index}"

    def _build_cmd(self) -> list[str]:
        url = self._camera.url(self._settings.recording_stream, self._settings)
        out_pattern = str(self._cam_dir() / "%Y-%m-%dT%H-%M-%S.mp4")
        seg_seconds = max(60, self._settings.recording_segment_minutes * 60)
        return [
            _ffmpeg_path(),
            "-loglevel", "warning",
            "-hide_banner",
            "-nostats",
            # Input behaviour copied from the live stream worker.
            # NOTE: ffmpeg 7 is picky about which timeout flags apply to
            # RTSP. We omit explicit timeouts here and let the protocol
            # default; the supervisor's backoff loop handles dead streams.
            "-rtsp_transport", "tcp",
            "-fflags", "nobuffer+discardcorrupt",
            "-flags", "low_delay",
            "-err_detect", "ignore_err",
            "-i", url,
            # Segment muxer
            "-c", "copy",
            "-map", "0:v:0",
            "-f", "segment",
            "-segment_time", str(seg_seconds),
            "-segment_format", "mp4",
            "-segment_atclocktime", "1",
            "-reset_timestamps", "1",
            "-strftime", "1",
            "-y",
            out_pattern,
        ]

    def run(self) -> None:
        cam_dir = self._cam_dir()
        try:
            cam_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            bus.error("REC", f"cam{self._camera.index}: cannot create {cam_dir}: {e!s}")
            self.status_changed.emit("error")
            return

        bus.info("REC", f"cam{self._camera.index}: recorder started, dir={cam_dir}")
        backoff = self.INITIAL_BACKOFF_S

        while not self._stop:
            cmd = self._build_cmd()
            masked = " ".join(mask_url(c) if c.startswith("rtsp://") else c for c in cmd)
            bus.info("REC", f"cam{self._camera.index}: spawning ffmpeg  {masked}")
            self.status_changed.emit("starting")

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if sys.platform == "win32"
                        else 0
                    ),
                )
            except FileNotFoundError as e:
                bus.error("REC", f"cam{self._camera.index}: ffmpeg not found: {e!s}")
                self.status_changed.emit("error")
                return

            self.status_changed.emit("recording")
            t_start = time.monotonic()

            # Drain stderr to detect "Opening 'foo.mp4' for writing" lines and
            # also to detect when ffmpeg exits.
            assert self._proc.stderr is not None
            for raw in self._proc.stderr:
                if self._stop:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if "Opening '" in line and ".mp4'" in line:
                    try:
                        fname = line.split("Opening '", 1)[1].split("'", 1)[0]
                        bus.info("REC", f"cam{self._camera.index}: segment -> {Path(fname).name}")
                        self.segment_opened.emit(fname)
                    except Exception:
                        pass
                elif "error" in line.lower() or "warning" in line.lower():
                    bus.warn("REC", f"cam{self._camera.index}: ffmpeg: {line}")

            rc = self._proc.wait() if self._proc.poll() is None else self._proc.returncode
            self._proc = None
            uptime = time.monotonic() - t_start

            if self._stop:
                bus.info("REC", f"cam{self._camera.index}: recorder stopped (uptime {uptime:.0f}s)")
                break

            bus.warn(
                "REC",
                f"cam{self._camera.index}: ffmpeg exited rc={rc} after {uptime:.0f}s; "
                f"restarting in {backoff:.1f}s",
            )
            self.status_changed.emit("reconnecting")
            # If ffmpeg ran for a healthy while before crashing, reset the backoff
            if uptime > 30:
                backoff = self.INITIAL_BACKOFF_S
            self._sleep_backoff(backoff)
            backoff = min(backoff * 2.0, self.MAX_BACKOFF_S)

        self.status_changed.emit("stopped")

    def _sleep_backoff(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            self.msleep(100)


class RecorderSupervisor(QObject):
    """Owns N RecorderWorker instances + a retention pruner.

    Counts segments and bytes on disk for surfacing into the UI status bar.
    """

    stats_changed = Signal(int, int, int)  # segments_count, bytes_total, active_workers

    PRUNE_INTERVAL_MS = 5 * 60 * 1000   # every 5 minutes
    STATS_INTERVAL_MS = 10 * 1000       # every 10 seconds

    def __init__(self, settings: Settings, cameras: tuple[Camera, ...], parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cameras = cameras
        self._workers: list[RecorderWorker] = []
        self._active = 0
        self._prune_timer = QTimer(self)
        self._prune_timer.timeout.connect(self._prune_old_segments)
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)

    def start(self) -> None:
        if not self._settings.recording_enabled:
            bus.info("REC", "recording disabled via RECORDING_ENABLED=0")
            return
        try:
            self._settings.recording_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            bus.error("REC", f"cannot create recordings dir {self._settings.recording_dir}: {e!s}")
            return

        for cam in self._cameras:
            w = RecorderWorker(cam, self._settings, parent=self)
            w.status_changed.connect(lambda st, c=cam.index: self._on_worker_status(c, st))
            self._workers.append(w)
            w.start()

        self._prune_timer.start(self.PRUNE_INTERVAL_MS)
        self._stats_timer.start(self.STATS_INTERVAL_MS)
        # First prune + stats immediately
        QTimer.singleShot(2000, self._prune_old_segments)
        QTimer.singleShot(2000, self._refresh_stats)

    def stop(self, wait_ms: int = 5000) -> None:
        self._prune_timer.stop()
        self._stats_timer.stop()
        for w in self._workers:
            w.request_stop()
        for w in self._workers:
            w.wait(wait_ms)
        self._workers.clear()

    def _on_worker_status(self, cam_index: int, status: str) -> None:
        if status == "recording":
            self._active = sum(1 for w in self._workers if w.isRunning())
        elif status in ("stopped", "error"):
            self._active = sum(1 for w in self._workers if w.isRunning())

    def _prune_old_segments(self) -> None:
        base = self._settings.recording_dir
        if not base.is_dir():
            return
        cutoff = time.time() - (self._settings.recording_retention_days * 86400)
        pruned = 0
        freed_bytes = 0
        for f in base.rglob("*.mp4"):
            try:
                stat = f.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                try:
                    freed_bytes += stat.st_size
                    f.unlink()
                    pruned += 1
                except OSError:
                    continue
        if pruned:
            bus.info(
                "REC",
                f"retention: pruned {pruned} segment(s) older than "
                f"{self._settings.recording_retention_days}d, freed {freed_bytes / 1024 / 1024:.1f} MB",
            )

    def _refresh_stats(self) -> None:
        base = self._settings.recording_dir
        segs = 0
        total = 0
        if base.is_dir():
            for f in base.rglob("*.mp4"):
                try:
                    total += f.stat().st_size
                    segs += 1
                except OSError:
                    continue
        active = sum(1 for w in self._workers if w.isRunning())
        self.stats_changed.emit(segs, total, active)

    @staticmethod
    def disk_free_bytes(path: Path) -> int:
        try:
            return shutil.disk_usage(path).free
        except (OSError, ValueError):
            return 0
