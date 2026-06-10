"""Backend pipeline: the always-on capture / detect / notify machinery.

MainWindow used to construct and cross-wire the recorder, the segment
analyzer, the live detector, the collision matcher and the Telegram
notifier inline, which buried the backend's lifecycle in UI code.
Pipeline is that composition seam: it owns the backend objects, runs
their plumbing (segment_closed -> analyzer, event -> collision ->
notifier, live alert -> notifier), and exposes only Qt signals plus small
setters to the UI — so the window stays pure presentation and the backend
can be driven headless (tests, a future CLI/service mode).

Lifecycle: construct -> start() once the window is visible (so the live
tiles win the race for the DVR's connection cap) -> shutdown() from
closeEvent, AFTER the camera tiles stopped so no more frames arrive.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from app.core import watchdog
from app.core.analyzer import SegmentAnalyzer
from app.core.collision import CollisionMatcher
from app.core.config import Settings
from app.core.events import EventConfig
from app.core.live_detector import LiveDetector
from app.core.log import bus
from app.core.notifier import TelegramNotifier
from app.core.recorder import RecorderSupervisor


class Pipeline(QObject):
    """Owns recorder -> analyzer -> events -> collision -> Telegram, plus the
    live alert tier and the watchdog heartbeat. UI talks to it through the
    signals below and the set_* methods; nothing here touches widgets."""

    recorder_stats = Signal(int, int, int)  # segments, total bytes, active workers
    ai_totals = Signal(int, int)            # cumulative person/vehicle segments
    ai_model_missing = Signal()             # detection enabled but model absent
    event_extracted = Signal(object)        # every EventClip, before fusion

    def __init__(self, settings: Settings, cameras, armed: set[int],
                 person_floors: dict[int, float], regions: dict,
                 links, cam_labels: dict[int, str],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cameras = tuple(cameras)
        self._armed = set(armed)
        self._person_floors = dict(person_floors)
        self._regions = dict(regions)
        self._cam_labels = dict(cam_labels)
        self._recorder: RecorderSupervisor | None = None
        self._analyzer: SegmentAnalyzer | None = None

        # Quick clips live in their OWN dir (NOT events/), so the Events
        # gallery stays pure segment-tier. Telegram-only, reply-fetchable.
        self._live_clips_dir = settings.recording_dir / "live_clips"

        # Off-device push alerts; no-op unless TELEGRAM_* is configured.
        self._notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            min_interval_s=settings.telegram_min_interval_s,
            notify_ongoing=settings.event_notify_ongoing,
            commands_enabled=settings.telegram_commands,
            state_dir=settings.env_path,
            events_dir=settings.events_dir,
            recording_dir=settings.recording_dir,
            cam_ids=[c.index for c in self._cameras],
            pre_roll_s=settings.live_pre_roll_s,
            post_roll_s=settings.live_post_roll_s,
            map_cap=settings.telegram_map_cap,
            live_clips_dir=self._live_clips_dir,
            lang=settings.telegram_lang,
            parent=self,
        )
        self._notifier.set_cam_labels(self._cam_labels)

        # Real-time alert tier on its own QThread so frame conversion + the
        # pre-roll JPEG buffering never run on the UI thread. Public attribute:
        # the camera tiles' frame_tapped connects straight to its submit slot.
        self.live_detector = LiveDetector(
            settings.detection_model,
            set(self._armed),
            conf=settings.live_conf,
            cooldown_s=settings.live_cooldown_s,
            clips_dir=self._live_clips_dir,
            quick_clip_enabled=settings.live_quick_clip,
            pre_roll_s=settings.live_pre_roll_s,
            post_roll_s=settings.live_post_roll_s,
            max_clip_s=settings.live_max_clip_s,
            clip_retention=settings.telegram_map_cap,
            person_conf=settings.detection_person_conf,
            min_box_frac=settings.detection_min_box_frac,
            person_conf_by_cam=self._person_floors,
        )
        self._live_thread = QThread(self)
        self.live_detector.moveToThread(self._live_thread)
        self._live_thread.start()
        self.live_detector.live_alert.connect(self._on_live_alert)
        self.live_detector.quick_clip_ready.connect(self._on_quick_clip_ready)
        self.live_detector.set_regions(self._regions)

        # Same-movement cross-camera fusion: one crossing = one event. The
        # matcher buffers freshly extracted events and fuses pairs that match
        # a taught camera link, else releases them to the normal notify path.
        self._collision = CollisionMatcher(
            links,
            on_collision=self._on_collision,
            on_single=self._notify_single,
        )
        self._collision_timer = QTimer(self)
        self._collision_timer.timeout.connect(self._collision.sweep)
        self._collision_timer.start(2000)

        # Watchdog heartbeat: prove we're alive to the sibling process.
        self._hb_timer = QTimer(self)
        self._hb_timer.timeout.connect(self._heartbeat_tick)

    # Lifecycle

    def start(self) -> None:
        """Begin background work (call once the main window has painted)."""
        rec_dir = self._settings.recording_dir
        watchdog.seed_default(rec_dir, self._settings.watchdog_enabled)
        watchdog.touch_heartbeat(rec_dir)
        self._hb_timer.start(15_000)
        watchdog.spawn_if_enabled(self._settings)
        # Start the recorder a moment after the live streams so they don't
        # race the DVR's connection cap.
        if self._settings.recording_enabled:
            QTimer.singleShot(1500, self._start_recorder)

    def shutdown(self) -> None:
        """Stop everything in dependency order. Call AFTER the camera tiles
        are down so no more live frames arrive."""
        self._hb_timer.stop()
        self._collision_timer.stop()
        if self._analyzer is not None:
            # Finalize held cross-segment events while the recorder's segments
            # still exist on disk, then stop the worker and the recorder.
            self._analyzer.flush_all()
            self._analyzer.request_stop()
            self._analyzer.wait(3000)
        # Release any event still waiting for a cross-camera partner so its
        # notification isn't lost (best-effort, before the notifier shuts down).
        self._collision.flush_all()
        if self._recorder is not None:
            self._recorder.stop(wait_ms=5000)
        self._live_thread.quit()
        self._live_thread.wait(2000)
        self._notifier.shutdown()  # stop the Telegram reply listener thread

    # Live-updatable preferences (called by the UI's dialogs/toggles)

    def set_cam_labels(self, labels: dict[int, str]) -> None:
        self._cam_labels = dict(labels)
        self._notifier.set_cam_labels(self._cam_labels)

    def set_armed(self, armed: set[int]) -> None:
        self._armed = set(armed)
        if self._analyzer is not None:
            self._analyzer.set_armed(set(self._armed))
        self.live_detector.set_armed(set(self._armed))

    def set_regions(self, regions: dict) -> None:
        self._regions = dict(regions)
        self.live_detector.set_regions(self._regions)
        if self._analyzer is not None:
            self._analyzer.set_regions(self._regions)

    def set_person_floors(self, floors: dict[int, float]) -> None:
        self._person_floors = dict(floors)
        self.live_detector.set_person_floor_by_cam(self._person_floors)
        if self._analyzer is not None:
            self._analyzer.set_person_floor_by_cam(self._person_floors)

    def set_links(self, links) -> None:
        self._collision.set_links(links)

    def configure_telegram(self, token: str, chat_id: str, *,
                           commands_enabled: bool, lang: str) -> None:
        self._notifier.configure(token, chat_id,
                                 commands_enabled=commands_enabled, lang=lang)

    # Internal wiring

    @Slot()
    def _heartbeat_tick(self) -> None:
        watchdog.touch_heartbeat(self._settings.recording_dir)
        # Keep the sibling alive: re-spawn if it crashed while we're enabled.
        watchdog.spawn_if_enabled(self._settings)

    def _start_recorder(self) -> None:
        if self._recorder is not None:
            return
        self._recorder = RecorderSupervisor(self._settings, self._cameras,
                                            parent=self)
        self._recorder.stats_changed.connect(self.recorder_stats)
        if self._settings.detection_enabled:
            self._start_analyzer()
            if self._analyzer is not None:
                self._recorder.segment_closed.connect(self._analyzer.enqueue)
        self._recorder.start()

    def _start_analyzer(self) -> None:
        if self._analyzer is not None:
            return
        model = self._settings.detection_model
        if not model.is_file():
            bus.warn("AI", f"model missing ({model}); detection disabled this session")
            self.ai_model_missing.emit()
            return
        event_cfg = EventConfig(
            enabled=self._settings.event_extraction_enabled,
            pre_roll_s=self._settings.event_pre_roll_s,
            post_roll_s=self._settings.event_post_roll_s,
            merge_gap_s=self._settings.event_merge_gap_s,
            min_hits=self._settings.event_min_hits,
        )
        self._analyzer = SegmentAnalyzer(
            model_path=model,
            conf=self._settings.detection_conf,
            person_conf=self._settings.detection_person_conf,
            person_conf_by_cam=self._person_floors,
            min_box_frac=self._settings.detection_min_box_frac,
            sample_seconds=self._settings.detection_sample_seconds,
            event_cfg=event_cfg,
            events_dir=self._settings.events_dir,
            recording_dir=self._settings.recording_dir,
            cam_ids=[cam.index for cam in self._cameras],
            armed=set(self._armed),
            max_duration_s=self._settings.event_max_duration_s,
            hold_timeout_s=self._settings.event_hold_timeout_s,
            parent=self,
        )
        self._analyzer.set_regions(self._regions)
        self._analyzer.totals_changed.connect(self.ai_totals)
        self._analyzer.event_extracted.connect(self._on_event_extracted)
        self._analyzer.start()
        self.ai_totals.emit(0, 0)

    def _on_event_extracted(self, clip) -> None:
        cams = "+".join(f"cam{c}" for c in clip.cams_captured) or "none"
        bus.info(
            "EVT",
            f"event saved: cam{clip.cam_id} triggered {clip.start_at:%H:%M:%S} "
            f"{clip.label}  ({cams})  -> {clip.folder}",
        )
        # Surface to the UI first (gallery refresh must never wait on, or be
        # broken by, the notification path), then route through the collision
        # matcher: a crossing seen by two cameras fuses into one notification,
        # everything else falls through to the single-camera push.
        self.event_extracted.emit(clip)
        self._collision.feed(clip)

    def _notify_single(self, clip) -> None:
        """Normal per-camera push (matcher release / no link involved)."""
        self._notifier.notify(clip, self._cam_labels.get(clip.cam_id))

    def _on_collision(self, link, direction: str, from_clip, to_clip) -> None:
        """One fused same-movement event: a single named album, two angles."""
        self._notifier.notify_collision(link.name, direction, from_clip, to_clip)

    def _on_live_alert(self, cam_id: int, title: str, thumb_path: str) -> None:
        label = self._cam_labels.get(cam_id, f"camera {cam_id}")
        self._notifier.notify_live(cam_id, label, title, thumb_path)

    def _on_quick_clip_ready(self, cam_id: int, folder: str) -> None:
        # The ~30s quick clip finished encoding. It is NOT added to the Events
        # gallery (that stays pure segment-tier). Telegram-only: reply to the
        # alert photo to fetch it; optionally auto-pushed (off by default).
        if self._settings.live_autosend_clip:
            label = self._cam_labels.get(cam_id, f"camera {cam_id}")
            self._notifier.send_quick_clip(cam_id, label, folder)
