"""CCTV Console application package."""

import os

# Must be set before cv2 is imported anywhere. Mirrors the working ffplay flags
# that handle this DVR's H.265 NAL-unit-0 anomaly on cameras 3 and 4.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp"
    "|fflags;nobuffer"
    "|flags;low_delay"
    "|err_detect;ignore_err"
    "|reorder_queue_size;0"
    "|max_delay;500000"
    "|analyzeduration;1000000"
    "|probesize;2000000"
    # Socket-level timeouts so an unreachable DVR fails over instead of
    # hanging cv2.VideoCapture() forever in the worker thread.
    "|stimeout;5000000"
    "|timeout;5000000",
)

__version__ = "0.3.4"
