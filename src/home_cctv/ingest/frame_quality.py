"""Frame-quality heuristics.

PITFALLS §1.3: ``discardcorrupt`` can still let partially-decoded frames
through as valid ndarrays where the bottom strip is uniform green or grey.
YOLO would happily hallucinate detections on the dead band. A cheap variance
check gates this before the frame ever leaves the FrameSource.
"""
from __future__ import annotations

import numpy as np

# Per PITFALLS §1.3 — bottom 40 rows, variance < 3.0 is a dead frame.
BOTTOM_STRIP_HEIGHT: int = 40
BOTTOM_STRIP_VARIANCE_THRESHOLD: float = 3.0


def is_green_frame(frame: np.ndarray | None) -> bool:
    """Return ``True`` if ``frame`` should be DROPPED as green / partial / dead.

    Also treats ``None``, empty, or wrong-shape frames as dead. The caller is
    expected to count dead frames as ``frames_corrupted`` rather than
    ``frames_decoded``.
    """
    if frame is None:
        return True
    if not hasattr(frame, "size") or frame.size == 0:
        return True
    if frame.ndim != 3:
        return True
    h = frame.shape[0]
    strip_h = min(BOTTOM_STRIP_HEIGHT, max(h // 4, 1))
    bottom = frame[-strip_h:, :, :]
    # Compute std per channel — a solid-colour or dead strip has near-zero
    # variance in every channel. Using the overall std would pass solid
    # green (`[0,255,0]`) because it mixes very different values across
    # channels; per-channel std correctly flags it as dead.
    per_channel_std = bottom.reshape(-1, bottom.shape[-1]).std(axis=0)
    return float(per_channel_std.max()) < BOTTOM_STRIP_VARIANCE_THRESHOLD
