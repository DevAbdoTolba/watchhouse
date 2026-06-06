"""YOLOv8n object detection over ONNX Runtime (CPU).

Build-time we export ultralytics yolov8n to ONNX; at runtime only
onnxruntime + numpy + opencv are needed (no torch). Detection runs in
batches over frames sampled from a finalized recording segment, so CPU
speed is plenty - this is not a real-time decode path.

Output decoding follows the YOLOv8 ONNX layout: a single [1, 84, 8400]
tensor (4 box coords + 80 COCO class scores, no separate objectness),
which we transpose, threshold on max class score, map back through the
letterbox transform, and run class-agnostic NMS over.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# COCO class ids we surface. Everything else is ignored.
COCO_NAMES = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}
PERSON_IDS = {0}
VEHICLE_IDS = {1, 2, 3, 5, 7}
KEEP_IDS = PERSON_IDS | VEHICLE_IDS


@dataclass(frozen=True)
class Detection:
    label: str
    class_id: int
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float  # xyxy in original-frame pixel coords

    @property
    def is_person(self) -> bool:
        return self.class_id in PERSON_IDS

    @property
    def is_vehicle(self) -> bool:
        return self.class_id in VEHICLE_IDS


def _letterbox(img: np.ndarray, new_shape: int, color: int = 114):
    """Resize keeping aspect ratio, pad to a square. Returns the padded
    image plus (ratio, dw, dh) so detections can be mapped back."""
    import cv2

    h, w = img.shape[:2]
    r = min(new_shape / h, new_shape / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((new_shape, new_shape, 3), color, dtype=np.uint8)
    dw, dh = (new_shape - nw) // 2, (new_shape - nh) // 2
    canvas[dh:dh + nh, dw:dw + nw] = resized
    return canvas, r, dw, dh


class Detector:
    """Lazy-loaded ONNX yolov8n detector. Reused across frames/segments."""

    def __init__(
        self,
        model_path: Path,
        conf_threshold: float = 0.35,
        iou_threshold: float = 0.5,
        min_person_conf: float = 0.0,
        min_box_frac: float = 0.0,
    ) -> None:
        self._model_path = Path(model_path)
        self._conf = conf_threshold
        self._iou = iou_threshold
        # False-positive guards (0 = disabled): a higher confidence floor JUST
        # for the noisy 'person' class, and a minimum box size (as a fraction of
        # the frame's larger side) to drop tiny specks that yolov8n calls people
        # on a grainy night sub-stream.
        self._min_person_conf = max(0.0, min_person_conf)
        self._min_box_frac = max(0.0, min_box_frac)
        self._session = None
        self._input_name: str | None = None
        self._imgsz = 640

    def _passes_filters(self, d: "Detection", w: float, h: float) -> bool:
        """False-positive guards applied after detection (see __init__)."""
        if d.is_person and d.confidence < self._min_person_conf:
            return False
        if self._min_box_frac > 0.0 and w > 0 and h > 0:
            span = max((d.x2 - d.x1) / w, (d.y2 - d.y1) / h)
            if span < self._min_box_frac:
                return False
        return True

    @property
    def available(self) -> bool:
        return self._model_path.is_file()

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
        so.log_severity_level = 3  # errors only
        self._session = ort.InferenceSession(
            str(self._model_path),
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        if len(inp.shape) == 4 and isinstance(inp.shape[2], int):
            self._imgsz = inp.shape[2]

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        """Run detection on one BGR frame (HxWx3 uint8)."""
        import cv2

        self._ensure_session()
        h0, w0 = frame_bgr.shape[:2]
        padded, r, dw, dh = _letterbox(frame_bgr, self._imgsz)
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        blob = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]

        outputs = self._session.run(None, {self._input_name: blob})
        pred = np.squeeze(outputs[0], 0)
        if pred.shape[0] < pred.shape[1]:
            pred = pred.T  # -> [num_anchors, 84]

        boxes = pred[:, :4]
        scores_all = pred[:, 4:]
        class_ids = np.argmax(scores_all, axis=1)
        confs = scores_all[np.arange(scores_all.shape[0]), class_ids]

        mask = (confs >= self._conf) & np.isin(class_ids, list(KEEP_IDS))
        boxes = boxes[mask]
        class_ids = class_ids[mask]
        confs = confs[mask]
        if boxes.shape[0] == 0:
            return []

        cx, cy, bw, bh = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        x1 = (cx - bw / 2 - dw) / r
        y1 = (cy - bh / 2 - dh) / r
        x2 = (cx + bw / 2 - dw) / r
        y2 = (cy + bh / 2 - dh) / r
        x1 = np.clip(x1, 0, w0)
        x2 = np.clip(x2, 0, w0)
        y1 = np.clip(y1, 0, h0)
        y2 = np.clip(y2, 0, h0)

        boxes_xywh = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1).tolist()
        idxs = cv2.dnn.NMSBoxes(boxes_xywh, confs.tolist(), self._conf, self._iou)

        results: list[Detection] = []
        for i in np.array(idxs).flatten() if len(idxs) > 0 else []:
            cid = int(class_ids[i])
            d = Detection(
                label=COCO_NAMES.get(cid, str(cid)),
                class_id=cid,
                confidence=float(confs[i]),
                x1=float(x1[i]),
                y1=float(y1[i]),
                x2=float(x2[i]),
                y2=float(y2[i]),
            )
            if self._passes_filters(d, w0, h0):
                results.append(d)
        return results
