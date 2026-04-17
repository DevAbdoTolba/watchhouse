"""Cameras.yaml loader.

Loads the committed ``cameras.yaml`` into a typed pydantic model and exposes
``build_rtsp_url`` for constructing full RTSP URLs from Settings + per-camera
sub/main paths. Consumed by Plan 00-03's Phase 0 sweep and by Phase 1's
multi-stream ingest.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

from home_cctv.config.env import Settings


class CameraConfig(BaseModel):
    id: int
    name: str
    location: str
    coverage: str
    sub_path: str
    main_path: str
    codec: Literal["hevc", "h264"]
    # FPS fields. ``native_fps`` is the legacy key (equivalent to
    # ``main_stream_fps`` — the camera's main-stream hardware rate).
    # ``main_stream_fps`` + ``sub_stream_fps`` are the 2026-04-17 patch that
    # disambiguates the two: sub-stream is the rate we actually ingest at
    # /1, /11, /21, /31, which the DVR halves relative to main-stream. Exit
    # criteria and ByteTracker frame_rate MUST use ``sub_stream_fps``.
    # If only ``native_fps`` is provided, ``main_stream_fps`` defaults to it
    # and ``sub_stream_fps`` defaults to ``max(1, native_fps // 2)``.
    native_fps: int
    main_stream_fps: Optional[int] = None
    sub_stream_fps: Optional[int] = None
    native_width: int
    native_height: int
    sensor_native: Optional[str] = None
    audio_multiplex: bool = False
    nal_unit_0_workaround_required: bool = False
    known_hazard: Optional[str] = None
    notes: str = ""

    @model_validator(mode="after")
    def _fill_stream_fps_defaults(self) -> "CameraConfig":
        # main_stream_fps falls back to legacy native_fps.
        if self.main_stream_fps is None:
            object.__setattr__(self, "main_stream_fps", self.native_fps)
        # sub_stream_fps falls back to main_stream_fps // 2, floor 1.
        if self.sub_stream_fps is None:
            main = self.main_stream_fps or self.native_fps
            object.__setattr__(
                self, "sub_stream_fps", max(1, int(main) // 2)
            )
        return self

    @property
    def advertised_sub_fps(self) -> int:
        """Canonical target FPS for sub-stream exit criteria (Bug fix 2026-04-17)."""
        return int(self.sub_stream_fps or max(1, self.native_fps // 2))


class DvrConfig(BaseModel):
    host_env: str = "DVR_IP"
    port_env: str = "DVR_PORT"
    user_env: str = "DVR_USER"
    pass_env: str = "DVR_PASS"
    vendor: Optional[str] = None


class CamerasFile(BaseModel):
    dvr: DvrConfig
    cameras: List[CameraConfig] = Field(default_factory=list)


def load_cameras(path: Path | str) -> CamerasFile:
    """Load cameras.yaml from disk and return a typed CamerasFile."""
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return CamerasFile.model_validate(data)


def build_rtsp_url(
    settings: Settings,
    cam: CameraConfig,
    *,
    stream: Literal["sub", "main"],
) -> str:
    """Format ``rtsp://user:pass@host:port/<path>`` from Settings + CameraConfig.

    Leading slash on the path is normalized (``/1`` or ``1`` both yield the
    correct URL).
    """
    path = cam.sub_path if stream == "sub" else cam.main_path
    if not path.startswith("/"):
        path = "/" + path
    return (
        f"rtsp://{settings.DVR_USER}:{settings.DVR_PASS}"
        f"@{settings.DVR_IP}:{settings.DVR_PORT}{path}"
    )


__all__ = [
    "CameraConfig",
    "CamerasFile",
    "DvrConfig",
    "load_cameras",
    "build_rtsp_url",
]
