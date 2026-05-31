"""Stitch a presence session's cap-split clips into one continuous file.

A long continuous presence is force-split into many ~2-minute events so the
notifier can ping frequently (see event_stitcher). Those chunks share a
`session_id` and are grouped back into an EventSession by event_library. This
module joins one session's per-member clips for a single camera, in order,
into ONE continuous mp4 - ON DEMAND only, when the user clicks "Save full
clip". The result can be hours / gigabytes; that's the user's explicit choice,
which is why it is never auto-written.

The join reuses events._concat_clips (ffmpeg concat demuxer, stream copy, no
re-encode). The per-event folders are never touched: this only writes a new
combined file where the caller asks.
"""

from __future__ import annotations

from pathlib import Path

from app.core.events import _concat_clips
from app.core.log import bus

# Keep a typing reference without a hard import cycle at module load.
try:  # pragma: no cover - import convenience
    from app.core.event_library import EventSession
except Exception:  # pragma: no cover
    EventSession = object  # type: ignore


def export_session(session: "EventSession", cam_id: int, out_path: Path) -> bool:
    """Concatenate `session`'s cam<cam_id> member clips, in session order, into
    one continuous mp4 at `out_path`. Members lacking that camera's clip are
    skipped. Returns False if no member has a clip for that camera, or if the
    concat fails. Does NOT delete the source event folders."""
    parts: list[Path] = []
    for member in session.members:
        clip = member.clips.get(cam_id)
        if clip is not None and clip.is_file():
            parts.append(clip)

    if not parts:
        bus.warn(
            "EVT",
            f"save-full-clip: session {session.session_id} has no cam{cam_id} "
            "clips to export",
        )
        return False

    bus.info(
        "EVT",
        f"save-full-clip: stitching {len(parts)} cam{cam_id} clips of session "
        f"{session.session_id} -> {out_path.name}",
    )

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        bus.warn("EVT", f"save-full-clip: cannot create {out_path.parent}: {e!s}")
        return False

    # _concat_clips DELETES its inputs on success (it owns the temp sub-clips in
    # the event path). Here the inputs are the permanent per-event clips, so copy
    # them into a scratch dir first and concat those throwaway copies.
    import shutil
    import tempfile

    # Scratch on the DESTINATION drive so the final concat/move never crosses
    # volumes (Windows os.replace fails C:->D:); also avoids copying gigabytes
    # onto the system drive. out_path.parent was created just above.
    try:
        scratch = Path(tempfile.mkdtemp(prefix="wh_sess_", dir=str(out_path.parent)))
    except OSError:
        scratch = Path(tempfile.mkdtemp(prefix="wh_sess_"))
    try:
        copies: list[Path] = []
        for i, src in enumerate(parts):
            dst = scratch / f"part{i:05d}.mp4"
            shutil.copy2(src, dst)
            copies.append(dst)
        ok = _concat_clips(copies, out_path)
    except OSError as e:
        bus.warn("EVT", f"save-full-clip: copy/concat failed: {e!s}")
        ok = False
    finally:
        try:
            shutil.rmtree(scratch, ignore_errors=True)
        except OSError:
            pass

    if ok:
        size_mb = out_path.stat().st_size / (1024 * 1024) if out_path.is_file() else 0.0
        bus.info(
            "EVT",
            f"save-full-clip: wrote {out_path} ({size_mb:.1f} MB, "
            f"{len(parts)} clips)",
        )
    else:
        bus.warn("EVT", f"save-full-clip: concat failed for {out_path.name}")
    return ok
