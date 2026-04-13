"""Phase0Report writer.

Schema mirrors CONTEXT.md §Phase 0 Measurement Harness Format byte-for-byte.
Tests and dry-run mode MUST pass an explicit ``report_path`` — no default —
so the committed ``PHASE0-REPORT.json`` is never overwritten by anything
except the real-host live sweep (B3).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Canonical path for the COMMITTED artifact. Tests and dry-runs MUST NOT
# write to this path — they must pass an explicit report_path (tmp_path in
# tests, or REPORT_PATH.with_suffix(".dryrun.json") in --mp4 dry-run mode).
REPORT_PATH: Path = Path(
    ".planning/phases/00-environment-sanity/PHASE0-REPORT.json"
)


@dataclass
class CameraResult:
    name: str
    sub_path: str
    main_path: str
    codec: str
    advertised_fps: int
    measured_fps: float
    width: int
    height: int
    capture_duration_sec: int
    frames_decoded: int
    frames_corrupted: int
    decode_errors: int
    hang_events: int
    nal_unit_0_workaround_required: bool = False
    exit_ok: bool = False


@dataclass
class Phase0Report:
    timestamp_utc: str
    host: Dict[str, Any] = field(default_factory=dict)
    opencv: Dict[str, Any] = field(default_factory=dict)
    env_vars_loaded: List[str] = field(default_factory=list)
    credentials_masked_in_logs: bool = True
    disk_space: Dict[str, Any] = field(default_factory=dict)
    dvr: Dict[str, Any] = field(default_factory=dict)
    cameras: Dict[str, Any] = field(default_factory=dict)
    model_bundle: Dict[str, Any] = field(default_factory=dict)
    blockers_resolved: Dict[str, Any] = field(default_factory=dict)
    phase0_verdict: str = "aborted"
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, path: Path) -> "Phase0Report":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)


def new_report() -> Phase0Report:
    return Phase0Report(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        host={},
        opencv={},
        env_vars_loaded=[],
        credentials_masked_in_logs=True,
        disk_space={},
        dvr={},
        cameras={},
        model_bundle={},
        blockers_resolved={
            "Q1_host_fps_baseline": False,
            "Q2_wsl2_networking": False,
            "Q3_opencv_ffmpeg_backend": False,
            "Q4_ffplay_flags_translated": False,
            "Q5_reconnect_watchdog_sketch": "designed but not yet implemented",
            "Q6_dvr_connection_cap": False,
        },
        phase0_verdict="aborted",
        notes="",
    )


def write_report(report: Phase0Report, path: Path) -> Path:
    """Write report to an EXPLICIT path. No default — callers must decide."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, default=str)
    return path


def print_stdout_summary(report: Phase0Report) -> None:
    print("=" * 72)
    print(
        f"Phase 0 — {report.timestamp_utc}   verdict={report.phase0_verdict}"
    )
    print("-" * 72)
    for cam_id, c in report.cameras.items():
        mark = "PASS" if c.get("exit_ok") else "FAIL"
        name = c.get("name", "?")
        fps = float(c.get("measured_fps", 0.0))
        adv = int(c.get("advertised_fps", 0))
        dec = int(c.get("frames_decoded", 0))
        corr = int(c.get("frames_corrupted", 0))
        errs = int(c.get("decode_errors", 0))
        hangs = int(c.get("hang_events", 0))
        print(
            f"  [{mark}] cam{cam_id} {name:20s} "
            f"fps={fps:.2f}/{adv}  "
            f"decoded={dec}  corrupted={corr}  "
            f"errors={errs}  hangs={hangs}"
        )
    print("-" * 72)
    print(f"blockers: {report.blockers_resolved}")
    print("=" * 72)


__all__ = [
    "REPORT_PATH",
    "CameraResult",
    "Phase0Report",
    "new_report",
    "write_report",
    "print_stdout_summary",
]
