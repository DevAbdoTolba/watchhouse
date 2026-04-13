import logging
from pathlib import Path

from home_cctv.obs.logging_setup import CredentialMaskFilter, configure_logging


def _reset_logger():
    # Ensure a clean root logger between tests so handler dedup logic doesn't hide bugs
    lg = logging.getLogger("home_cctv")
    for h in list(lg.handlers):
        lg.removeHandler(h)


def test_rtsp_password_masked_in_record():
    f = CredentialMaskFilter()
    rec = logging.LogRecord(
        "x",
        logging.INFO,
        "p",
        1,
        "connecting to rtsp://admin:REDACTED@192.168.1.10:554/1",
        (),
        None,
    )
    f.filter(rec)
    assert "REDACTED" not in rec.msg
    assert "rtsp://admin:***@192.168.1.10:554/1" in rec.msg


def test_http_basic_auth_masked():
    f = CredentialMaskFilter()
    rec = logging.LogRecord(
        "x", logging.INFO, "p", 1, "GET http://user:secret@host/x", (), None
    )
    f.filter(rec)
    assert "secret" not in rec.msg
    assert "http://user:***@host/x" in rec.msg


def test_handlers_get_filter(tmp_path):
    _reset_logger()
    adapter = configure_logging(tmp_path)
    handlers = adapter.logger.handlers
    assert handlers, "expected at least one handler"
    for h in handlers:
        assert any(isinstance(fl, CredentialMaskFilter) for fl in h.filters), (
            f"handler {h} missing CredentialMaskFilter — credentials could leak"
        )


def test_rotating_file_handler_sized_10mb_x5(tmp_path):
    _reset_logger()
    from logging.handlers import RotatingFileHandler

    adapter = configure_logging(tmp_path)
    rfhs = [h for h in adapter.logger.handlers if isinstance(h, RotatingFileHandler)]
    assert rfhs, "expected a RotatingFileHandler"
    h = rfhs[0]
    assert h.maxBytes == 10 * 1024 * 1024
    assert h.backupCount == 5


def test_integration_password_never_written_to_file(tmp_path):
    _reset_logger()
    adapter = configure_logging(tmp_path)
    adapter.info("dialing rtsp://admin:REDACTED@192.168.1.10:554/11 now")
    for h in adapter.logger.handlers:
        h.flush()
    log_file = Path(tmp_path).resolve() / "home_cctv.log"
    text = log_file.read_text(encoding="utf-8")
    assert "REDACTED" not in text
    assert "***" in text


def test_bracketed_camera_prefix_rendered(tmp_path):
    """CONTEXT.md §Logging Format locks `[cam1:exterior_red]` bracketed prefix."""
    _reset_logger()
    adapter = configure_logging(tmp_path, camera_id="cam1:exterior_red")
    adapter.info("frame_ok size=1280x720 fps=24.1 frame_idx=1032")
    for h in adapter.logger.handlers:
        h.flush()
    log_file = Path(tmp_path).resolve() / "home_cctv.log"
    text = log_file.read_text(encoding="utf-8")
    assert "[cam1:exterior_red]" in text, f"missing bracketed prefix in: {text!r}"
    assert "frame_ok" in text


def test_default_main_prefix_when_no_camera_id(tmp_path):
    """With no camera_id, the formatter falls back to `[main]`."""
    _reset_logger()
    adapter = configure_logging(tmp_path)
    adapter.info("booted")
    for h in adapter.logger.handlers:
        h.flush()
    text = (Path(tmp_path).resolve() / "home_cctv.log").read_text(encoding="utf-8")
    assert "[main]" in text
