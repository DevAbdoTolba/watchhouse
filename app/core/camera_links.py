"""Camera links — "same movement across two cameras is ONE event".

A link is a hand-taught, directional relationship between two cameras: when a
person leaves cam A's frame at a given edge and appears at cam B's matching edge
within a transit window, it's the *same movement* (e.g. crossing a doorway), not
two events. We don't identify the *person* (no Re-ID) — we match the MOTION:
time + which edge they exit/enter + direction of travel.

Each link names the crossing and both directions, so a fused event is reported
as e.g. "Front door — went out" instead of two separate "camera 1" / "camera 2"
alerts. Stored in <env_dir>/.cctv-camera-links.json (gitignored).

The geometry ("align/skew"): we don't warp the images — we just declare which
edge of cam A hands off to which edge of cam B. Edges are "left"/"right"/"top"/
"bottom" (the frame side a person crosses through).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_FILE = ".cctv-camera-links.json"
EDGES = ("left", "right", "top", "bottom")
DEFAULT_TRANSIT_S = 5.0


@dataclass(frozen=True)
class Link:
    name: str            # human label for the crossing, e.g. "Front door"
    cam_a: int           # first camera (the "inside" perspective, your call)
    edge_a: str          # the frame edge a person crosses on cam A
    cam_b: int           # second camera (the "outside" perspective)
    edge_b: str          # the matching frame edge on cam B
    transit_s: float = DEFAULT_TRANSIT_S  # rough A->B / B->A travel time
    label_ab: str = "went out"   # shown when motion goes A -> B
    label_ba: str = "came in"    # shown when motion goes B -> A

    def to_dict(self) -> dict:
        return {
            "name": self.name, "cam_a": self.cam_a, "edge_a": self.edge_a,
            "cam_b": self.cam_b, "edge_b": self.edge_b,
            "transit_s": self.transit_s,
            "label_ab": self.label_ab, "label_ba": self.label_ba,
        }


def _norm_edge(e) -> str:
    e = str(e).strip().lower()
    return e if e in EDGES else "none"


def _from_dict(d: dict) -> "Link | None":
    try:
        cam_a = int(d["cam_a"])
        cam_b = int(d["cam_b"])
        if cam_a == cam_b:
            return None
        return Link(
            name=str(d.get("name") or f"cam{cam_a}-cam{cam_b}"),
            cam_a=cam_a, edge_a=_norm_edge(d.get("edge_a")),
            cam_b=cam_b, edge_b=_norm_edge(d.get("edge_b")),
            transit_s=max(0.0, float(d.get("transit_s", DEFAULT_TRANSIT_S))),
            label_ab=str(d.get("label_ab") or "went out"),
            label_ba=str(d.get("label_ba") or "came in"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _path(env_path) -> Path:
    base = Path(env_path).parent if env_path else Path.cwd()
    return base / _FILE


def load(env_path) -> list[Link]:
    try:
        data = json.loads(_path(env_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [lk for lk in (_from_dict(d) for d in data if isinstance(d, dict)) if lk]


def save(env_path, links: list[Link]) -> None:
    try:
        _path(env_path).write_text(
            json.dumps([lk.to_dict() for lk in links], indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def cams_in_links(links: list[Link]) -> set[int]:
    """Every camera that participates in at least one link (so the matcher only
    holds those events back; cameras with no link notify immediately)."""
    cams: set[int] = set()
    for lk in links:
        cams.add(lk.cam_a)
        cams.add(lk.cam_b)
    return cams
