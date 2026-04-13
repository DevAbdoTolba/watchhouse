"""home_cctv package init.

CRITICAL ordering: this file sets environment variables that every downstream
module depends on. It is imported first on `python -m home_cctv`, on `import
home_cctv`, and transitively whenever any submodule is imported. Nothing in
this file may import cv2 or tensorflow — those imports would observe missing
env vars.
"""
import os

# TF must see this before `import tensorflow` anywhere in the process.
os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")

# OpenCV/FFmpeg must see this before `import cv2` anywhere in the process.
from home_cctv.ingest.flags import apply_capture_options  # noqa: E402

apply_capture_options()

__version__ = "0.1.0"
