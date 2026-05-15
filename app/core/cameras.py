"""Camera definitions and RTSP URL construction for the BitVision/Cantonk DVR.

Path scheme: zero-indexed camera number concatenated with stream-quality digit.
  Camera 1 main = /0,  sub = /1
  Camera 2 main = /10, sub = /11
  Camera 3 main = /20, sub = /21
  Camera 4 main = /30, sub = /31
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from app.core.config import Settings


@dataclass(frozen=True)
class Camera:
    index: int           # 1..4 for humans
    label: str           # short tile title, e.g. "CAM 01"
    location: str        # tile subtitle, e.g. "Front facade (right)"
    main_path: str       # "0", "10", "20", "30"
    sub_path: str        # "1", "11", "21", "31"

    def url(self, stream: str, settings: Settings) -> str:
        path = self.main_path if stream == "main" else self.sub_path
        user = quote(settings.dvr_user, safe="")
        password = quote(settings.dvr_pass, safe="")
        return (
            f"rtsp://{user}:{password}@{settings.dvr_ip}:{settings.dvr_port}/{path}"
        )


def default_cameras() -> tuple[Camera, ...]:
    return (
        Camera(1, "CAM 01", "Front facade (right exterior)", "0",  "1"),
        Camera(2, "CAM 02", "Entry threshold (interior)",    "10", "11"),
        Camera(3, "CAM 03", "Front facade (left exterior)",  "20", "21"),
        Camera(4, "CAM 04", "Stair landing (interior)",      "30", "31"),
    )
