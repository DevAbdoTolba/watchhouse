"""Frame helpers shared by the live and playback decode workers.

Both workers scale frames to the destination tile size *on their own thread*
(cv2 resize, GIL-released) so the UI thread only ever blits a ready-sized
image. Smooth-scaling four full-resolution frames per repaint on the GUI
thread was a major source of UI lag.
"""

from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtGui import QImage


def fit_to(frame: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """Aspect-fit a BGR frame inside target_w x target_h.

    Returns the frame unchanged when the target is unknown (0) or already
    matches. INTER_AREA when shrinking (comparable to Qt's smooth scale),
    INTER_LINEAR when growing.
    """
    h, w = frame.shape[:2]
    if target_w <= 0 or target_h <= 0 or w <= 0 or h <= 0:
        return frame
    scale = min(target_w / w, target_h / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    if (nw, nh) == (w, h):
        return frame
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(frame, (nw, nh), interpolation=interp)


def to_qimage(frame: np.ndarray) -> QImage:
    """BGR ndarray -> owned QImage (copied, safe to pass across threads)."""
    h, w = frame.shape[:2]
    return QImage(frame.data, w, h, frame.strides[0],
                  QImage.Format.Format_BGR888).copy()
