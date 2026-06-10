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
deletes files older than `recording_retention_minutes`. Default is a
90-minute sliding window so the on-disk footprint stays small; the
v0.4+ AI analyzer is what extracts the keeper clips before segments
age out of the window.

If ffmpeg dies (network drop, decoder error), the worker waits with
exponential backoff and respawns.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, QTimer, Signal

from app.core.cameras import Camera
from app.core.config import Settings
from app.core.log import bus, mask_url
from app.core.pins import Pins


def _parse_seg_start(path: Path) -> datetime | None:
    """Wall-clock start from a segment filename (2026-06-02T08-00-00.mp4)."""
    try:
        return datetime.strptime(path.stem, "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None


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
                # Segment rollovers are detected by the supervisor's filesystem
                # watcher, not from here - ffmpeg doesn't print its "Opening"
                # line at this log level. We only surface real errors/warnings.
                if "error" in line.lower() or "warning" in line.lower():
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


class _FsTask(QRunnable):
    """Run a filesystem-walking callable on the global pool. The retention
    prune and the stats count both rglob the whole recordings tree — far too
    slow for the UI thread on a spinning disk."""

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self._fn()
        except Exception as e:  # never let a scan kill the pool thread
            bus.warn("REC", f"background scan failed: {e!s}")


class RecorderSupervisor(QObject):
    """Owns N RecorderWorker instances + a retention pruner.

    Counts segments and bytes on disk for surfacing into the UI status bar.
    Both disk walks run on the global thread pool; only their timers and the
    resulting Qt signals touch the UI thread.
    """

    stats_changed = Signal(int, int, int)  # segments_count, bytes_total, active_workers
    segment_closed = Signal(str)           # absolute path to a just-finalized segment

    PRUNE_INTERVAL_MS = 5 * 60 * 1000   # every 5 minutes
    STATS_INTERVAL_MS = 10 * 1000       # every 10 seconds
    SEGMENT_SCAN_INTERVAL_MS = 10 * 1000  # watch for rolled-over segments

    def __init__(self, settings: Settings, cameras: tuple[Camera, ...], parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cameras = cameras
        self._workers: list[RecorderWorker] = []
        self._active = 0
        # Per-camera newest segment path seen by the filesystem watcher. When a
        # *newer* file appears the previous newest is finalized -> emit it. We
        # watch the directory rather than parse ffmpeg's stderr because ffmpeg
        # only prints its "Opening '...'" segment line at -loglevel verbose,
        # not at the warning level we run it at.
        self._last_segment: dict[int, str] = {}
        # Reentry guards for the pooled disk walks (set on the UI thread,
        # cleared by the pool thread; GIL-atomic).
        self._prune_busy = False
        self._stats_busy = False
        self._prune_timer = QTimer(self)
        self._prune_timer.timeout.connect(self._prune_old_segments)
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._segment_timer = QTimer(self)
        self._segment_timer.timeout.connect(self._scan_for_closed_segments)

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

        # Startup sweep: drop stale segments left behind by a previous (or
        # crashed) session *before* new recording begins, so the on-disk
        # window is correct from the first second rather than 5 minutes in.
        # imported/ and events/ are protected and never swept.
        self._prune_old_segments(startup=True)

        # Baseline the segment watcher against whatever already exists so we
        # only fire on segments that close from now on (no startup backlog).
        self._scan_for_closed_segments(baseline=True)

        self._prune_timer.start(self.PRUNE_INTERVAL_MS)
        self._stats_timer.start(self.STATS_INTERVAL_MS)
        self._segment_timer.start(self.SEGMENT_SCAN_INTERVAL_MS)
        QTimer.singleShot(2000, self._refresh_stats)

    def stop(self, wait_ms: int = 5000) -> None:
        self._prune_timer.stop()
        self._stats_timer.stop()
        self._segment_timer.stop()
        for w in self._workers:
            w.request_stop()
        for w in self._workers:
            w.wait(wait_ms)
        self._workers.clear()

    def _scan_for_closed_segments(self, baseline: bool = False) -> None:
        """Poll each camera dir for a newer segment file. Segment filenames are
        ISO timestamps, so the lexically-greatest name is the newest (currently
        in-progress) file. When it advances, the file that *was* newest has been
        finalized by ffmpeg (moov atom written) and is emitted for analysis.

        `baseline=True` just records the current newest per camera without
        emitting, so existing files aren't re-analyzed on startup."""
        base = self._settings.recording_dir
        for cam in self._cameras:
            cam_dir = base / f"cam{cam.index}"
            if not cam_dir.is_dir():
                continue
            names = sorted(p.name for p in cam_dir.glob("*.mp4"))
            if not names:
                continue
            newest = str(cam_dir / names[-1])
            prev = self._last_segment.get(cam.index)
            if prev == newest:
                continue
            self._last_segment[cam.index] = newest
            if baseline or prev is None:
                continue
            # `prev` was the newest last time and a later file now exists, so
            # `prev` is finalized.
            if Path(prev).is_file():
                bus.info("REC", f"cam{cam.index}: segment closed -> {Path(prev).name}")
                self.segment_closed.emit(prev)

    def _on_worker_status(self, cam_index: int, status: str) -> None:
        if status == "recording":
            self._active = sum(1 for w in self._workers if w.isRunning())
        elif status in ("stopped", "error"):
            self._active = sum(1 for w in self._workers if w.isRunning())

    def _protected_roots(self) -> list[Path]:
        """Subtrees the pruner must never touch: user-curated imported clips
        and extracted AI event evidence. Both outlive the rolling buffer."""
        base = self._settings.recording_dir
        roots: list[Path] = []
        for p in (base / "imported", self._settings.events_dir):
            try:
                roots.append(p.resolve())
            except OSError:
                continue
        return roots

    def _is_protected(self, f: Path, protected: list[Path]) -> bool:
        try:
            parents = f.resolve().parents
        except OSError:
            return True  # can't resolve -> err on the side of keeping it
        return any(root in parents for root in protected)

    def _prune_old_segments(self, startup: bool = False) -> None:
        if self._prune_busy:
            return
        self._prune_busy = True
        QThreadPool.globalInstance().start(
            _FsTask(lambda: self._prune_blocking(startup)))

    def _prune_blocking(self, startup: bool) -> None:
        try:
            self._do_prune(startup)
        finally:
            self._prune_busy = False

    def _do_prune(self, startup: bool) -> None:
        base = self._settings.recording_dir
        if not base.is_dir():
            return
        cutoff = time.time() - (self._settings.recording_retention_minutes * 60)
        protected = self._protected_roots()
        pins = Pins.load(self._settings.env_path)
        seg_len_min = self._settings.recording_segment_minutes
        pruned = 0
        freed_bytes = 0
        kept_pinned = 0
        for f in base.rglob("*.mp4"):
            if self._is_protected(f, protected):
                continue
            try:
                stat = f.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                # Never delete footage the user pinned (blue layer): if this
                # segment's wall-clock window overlaps a pin / keep-recording
                # range, keep it even though it aged out of the rolling window.
                seg_start = _parse_seg_start(f)
                if seg_start is not None and pins.overlaps(
                        seg_start, seg_start + timedelta(minutes=seg_len_min)):
                    kept_pinned += 1
                    continue
                try:
                    freed_bytes += stat.st_size
                    f.unlink()
                    pruned += 1
                except OSError:
                    continue
        if pruned:
            prefix = "startup sweep" if startup else "retention"
            bus.info(
                "REC",
                f"{prefix}: pruned {pruned} segment(s) older than "
                f"{self._settings.recording_retention_minutes}min, "
                f"freed {freed_bytes / 1024 / 1024:.1f} MB",
            )
        elif startup:
            bus.info("REC", "startup sweep: no stale segments to prune")

    def _refresh_stats(self) -> None:
        if self._stats_busy:
            return
        self._stats_busy = True
        # Snapshot the worker count here — self._workers belongs to the UI
        # thread and stop() clears it.
        active = sum(1 for w in self._workers if w.isRunning())
        QThreadPool.globalInstance().start(
            _FsTask(lambda: self._refresh_stats_blocking(active)))

    def _refresh_stats_blocking(self, active: int) -> None:
        try:
            base = self._settings.recording_dir
            segs = 0
            total = 0
            if base.is_dir():
                protected = self._protected_roots()
                for f in base.rglob("*.mp4"):
                    if self._is_protected(f, protected):
                        continue
                    try:
                        total += f.stat().st_size
                        segs += 1
                    except OSError:
                        continue
            self.stats_changed.emit(segs, total, active)
        finally:
            self._stats_busy = False

    @staticmethod
    def disk_free_bytes(path: Path) -> int:
        try:
            return shutil.disk_usage(path).free
        except (OSError, ValueError):
            return 0
