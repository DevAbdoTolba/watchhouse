"""Model bundle downloader, warm-up, and verifier.

Phase 0 pre-downloads the full inference stack — YOLO26n (+ YOLOv8n fallback),
the OpenVINO IR export of YOLO, DeepFace ArcFace + RetinaFace, and EasyOCR
English — so that Phase 1+ starts offline. Every startup re-verifies file
sizes against ``weights.lock.json`` (committed at repo root); a corrupt cache
triggers a redownload.

CONTEXT.md §"Model Pre-Download" + §"Phase 0 Measurement Harness Format"
locks the ``cold_start_ms`` sub-schema with exactly three keys:
``yolo_openvino``, ``deepface``, ``easyocr``. Every call into
``verify_model_bundle`` and every write of ``weights.lock.json`` MUST preserve
those three keys so downstream consumers never KeyError.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict

import numpy as np

_LOG = logging.getLogger("home_cctv.phase0.model_bundle")

# Anchored to repo root: src/home_cctv/phase0/model_bundle.py → parents[3]
# reaches the repo root, independent of cwd (pytest / systemd / direct run).
WEIGHTS_LOCK_PATH: Path = (
    Path(__file__).resolve().parents[3] / "weights.lock.json"
)

# Canonical weight entries. Kept here so the schema is the single source of
# truth for both ``persist_weights_lock`` and ``verify_model_bundle``.
_WEIGHT_ENTRIES: tuple[str, ...] = (
    "yolo26n.pt",
    "yolov8n.pt",
    "yolo26n_openvino_model",
    "deepface_arcface",
    "deepface_retinaface",
    "easyocr_english",
)

_DIR_ENTRIES: set[str] = {"yolo26n_openvino_model", "easyocr_english"}

_COLD_START_KEYS: tuple[str, ...] = ("yolo_openvino", "deepface", "easyocr")


def _zero_cold_start() -> Dict[str, int]:
    return {k: 0 for k in _COLD_START_KEYS}


def _dir_size_bytes(p: Path) -> int:
    total = 0
    for sub in p.rglob("*"):
        try:
            if sub.is_file():
                total += sub.stat().st_size
        except OSError:
            continue
    return total


def _entry_size(cache_dir: Path, name: str) -> int:
    path = cache_dir / name
    if not path.exists():
        return 0
    if name in _DIR_ENTRIES and path.is_dir():
        return _dir_size_bytes(path)
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _load_lock() -> Dict[str, Any]:
    if not WEIGHTS_LOCK_PATH.exists():
        return {"weights": {}, "cold_start_ms": _zero_cold_start()}
    try:
        return json.loads(WEIGHTS_LOCK_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        _LOG.warning("weights.lock.json unreadable: %r", exc)
        return {"weights": {}, "cold_start_ms": _zero_cold_start()}


def persist_weights_lock(bundle: Dict[str, Any]) -> Path:
    """Write ``bundle`` to ``WEIGHTS_LOCK_PATH`` with ``verified: true``.

    ``bundle`` is expected to contain ``weights`` (dict keyed by name →
    size_bytes + is_directory) and ``cold_start_ms`` (dict with the three
    canonical keys). Missing keys are filled in with zeros so the on-disk
    schema is always complete.
    """
    data: Dict[str, Any] = {
        "_note": (
            "Weight bundle manifest. Populated by "
            "phase0.model_bundle.download_model_bundle() -> "
            "persist_weights_lock() on first successful run."
        ),
        "cache_root": "$HOME/.cache/home_cctv/models",
        "weights": {},
        "cold_start_ms": _zero_cold_start(),
    }
    weights_in = bundle.get("weights", {})
    for name in _WEIGHT_ENTRIES:
        entry = weights_in.get(name, {})
        size = int(entry.get("size_bytes", 0))
        out: Dict[str, Any] = {
            "size_bytes": size,
            "verified": size > 0,
        }
        if name in _DIR_ENTRIES:
            out["is_directory"] = True
        data["weights"][name] = out
    cold = bundle.get("cold_start_ms") or {}
    for k in _COLD_START_KEYS:
        data["cold_start_ms"][k] = int(cold.get(k, 0))
    WEIGHTS_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_LOCK_PATH.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )
    return WEIGHTS_LOCK_PATH


def verify_model_bundle(cache_dir: Path) -> Dict[str, Any]:
    """Check the cache against ``weights.lock.json``.

    Returns a dict matching CONTEXT.md §model_bundle keys:
    ``yolov8n_pt_cached``, ``yolo26n_pt_cached``, ``yolo_openvino_export_cached``,
    ``deepface_arcface_cached``, ``deepface_retinaface_cached``,
    ``easyocr_english_cached``, ``total_weights_size_mb``, ``cold_start_ms``.

    Never raises — corrupt or missing entries are reported as ``False``.
    """
    cache_dir = Path(cache_dir)
    lock = _load_lock()
    lock_weights = lock.get("weights", {}) or {}

    def _cached(name: str) -> bool:
        actual = _entry_size(cache_dir, name)
        if actual <= 0:
            return False
        lock_entry = lock_weights.get(name, {}) or {}
        if not lock_entry.get("verified", False):
            # Lock not yet populated — accept any non-zero size at this stage.
            return actual > 0
        expected = int(lock_entry.get("size_bytes", 0))
        if expected <= 0:
            return actual > 0
        return actual == expected

    total_bytes = sum(_entry_size(cache_dir, n) for n in _WEIGHT_ENTRIES)
    cold = lock.get("cold_start_ms") or {}
    cold_out = _zero_cold_start()
    for k in _COLD_START_KEYS:
        cold_out[k] = int(cold.get(k, 0))

    return {
        "yolo26n_pt_cached": _cached("yolo26n.pt"),
        "yolov8n_pt_cached": _cached("yolov8n.pt"),
        "yolo_openvino_export_cached": _cached("yolo26n_openvino_model"),
        "deepface_arcface_cached": _cached("deepface_arcface"),
        "deepface_retinaface_cached": _cached("deepface_retinaface"),
        "easyocr_english_cached": _cached("easyocr_english"),
        "total_weights_size_mb": round(total_bytes / (1024 * 1024), 2),
        "cold_start_ms": cold_out,
    }


def warmup_model_bundle(cache_dir: Path) -> Dict[str, int]:
    """Run one warm-up call per model, return integer milliseconds.

    Each delta wraps ONLY the inference call (``model(...)`` /
    ``represent(...)`` / ``readtext(...)``), not the load time. Failures are
    logged-and-continued with a zero so the caller can decide what to do.
    """
    cache_dir = Path(cache_dir)
    out: Dict[str, int] = _zero_cold_start()

    # YOLO OpenVINO inference
    try:
        from ultralytics import YOLO  # type: ignore
        ir_dir = cache_dir / "yolo26n_openvino_model"
        model_ref: Any
        if ir_dir.exists():
            model_ref = YOLO(str(ir_dir))
        else:
            fallback = cache_dir / "yolo26n.pt"
            model_ref = YOLO(str(fallback if fallback.exists() else "yolo26n.pt"))
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        t0 = time.perf_counter()
        model_ref(dummy, verbose=False)
        out["yolo_openvino"] = max(1, int(round((time.perf_counter() - t0) * 1000)))
    except Exception as exc:
        _LOG.warning("yolo warmup failed: %r", exc)

    # DeepFace ArcFace represent
    try:
        from deepface import DeepFace  # type: ignore
        dummy = np.zeros((224, 224, 3), dtype=np.uint8)
        t0 = time.perf_counter()
        DeepFace.represent(
            img_path=dummy,
            model_name="ArcFace",
            detector_backend="skip",
            enforce_detection=False,
        )
        out["deepface"] = max(1, int(round((time.perf_counter() - t0) * 1000)))
    except Exception as exc:
        _LOG.warning("deepface warmup failed: %r", exc)

    # EasyOCR readtext
    try:
        import easyocr  # type: ignore
        reader = easyocr.Reader(
            ["en"],
            gpu=False,
            model_storage_directory=str(cache_dir / "easyocr_english"),
            verbose=False,
        )
        dummy = np.zeros((100, 300, 3), dtype=np.uint8)
        t0 = time.perf_counter()
        reader.readtext(dummy)
        out["easyocr"] = max(1, int(round((time.perf_counter() - t0) * 1000)))
    except Exception as exc:
        _LOG.warning("easyocr warmup failed: %r", exc)

    return out


def _move_into_cache(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def download_model_bundle(cache_dir: Path) -> Dict[str, Any]:
    """Download every weight + warm up + persist ``weights.lock.json``.

    Auto-calls ``warmup_model_bundle`` and ``persist_weights_lock`` on
    success — no manual shell snippet required (W6). On partial failure
    the best-effort bundle is still persisted with ``verified: true`` on
    whatever weights actually landed.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # --- YOLO26n + YOLOv8n ---
    try:
        from ultralytics import YOLO  # type: ignore

        for weight in ("yolo26n.pt", "yolov8n.pt"):
            dest = cache_dir / weight
            if dest.exists() and dest.stat().st_size > 0:
                _LOG.info("yolo weight already cached: %s", dest)
                continue
            _LOG.info("downloading %s (ultralytics auto-cache)", weight)
            try:
                m = YOLO(weight)
                src_path = Path(getattr(m, "ckpt_path", "")) if getattr(m, "ckpt_path", None) else Path(weight)
                if src_path.exists() and src_path.resolve() != dest.resolve():
                    shutil.copy2(src_path, dest)
                elif Path(weight).exists():
                    shutil.copy2(Path(weight), dest)
            except Exception as exc:
                _LOG.warning("failed to download %s: %r", weight, exc)

        # OpenVINO IR export from yolo26n
        ir_dir = cache_dir / "yolo26n_openvino_model"
        if not ir_dir.exists() or _dir_size_bytes(ir_dir) == 0:
            _LOG.info("exporting yolo26n to OpenVINO IR")
            try:
                src_pt = cache_dir / "yolo26n.pt"
                if src_pt.exists():
                    m = YOLO(str(src_pt))
                else:
                    m = YOLO("yolo26n.pt")
                export_path = m.export(
                    format="openvino", half=False, dynamic=False, imgsz=640
                )
                ep = Path(export_path) if isinstance(export_path, (str, Path)) else None
                if ep is not None and ep.exists():
                    target = ep if ep.is_dir() else ep.parent
                    _move_into_cache(target, ir_dir)
            except Exception as exc:
                _LOG.warning("OpenVINO export failed: %r", exc)
    except Exception as exc:
        _LOG.warning("ultralytics unavailable: %r", exc)

    # --- DeepFace: ArcFace + RetinaFace ---
    deepface_dir = Path.home() / ".deepface" / "weights"
    try:
        from deepface import DeepFace  # type: ignore

        _LOG.info("building deepface models (auto-download to ~/.deepface/weights)")
        DeepFace.build_model("ArcFace")
        DeepFace.build_model("retinaface")
        for marker_name, pattern in (
            ("deepface_arcface", "arcface_weights.h5"),
            ("deepface_retinaface", "retinaface.h5"),
        ):
            candidates = list(deepface_dir.glob(pattern)) if deepface_dir.exists() else []
            dest = cache_dir / marker_name
            if candidates:
                shutil.copy2(candidates[0], dest)
    except Exception as exc:
        _LOG.warning("deepface download failed: %r", exc)

    # --- EasyOCR English ---
    try:
        import easyocr  # type: ignore

        _LOG.info("downloading EasyOCR English reader")
        easy_dir = cache_dir / "easyocr_english"
        easy_dir.mkdir(parents=True, exist_ok=True)
        easyocr.Reader(
            ["en"],
            gpu=False,
            model_storage_directory=str(easy_dir),
            download_enabled=True,
            verbose=False,
        )
    except Exception as exc:
        _LOG.warning("easyocr download failed: %r", exc)

    # --- Warm up + persist ---
    cold_start_ms = warmup_model_bundle(cache_dir)
    bundle: Dict[str, Any] = {
        "weights": {
            name: {
                "size_bytes": _entry_size(cache_dir, name),
                "is_directory": name in _DIR_ENTRIES,
            }
            for name in _WEIGHT_ENTRIES
        },
        "cold_start_ms": cold_start_ms,
    }
    persist_weights_lock(bundle)
    return bundle


__all__ = [
    "WEIGHTS_LOCK_PATH",
    "download_model_bundle",
    "warmup_model_bundle",
    "verify_model_bundle",
    "persist_weights_lock",
]
