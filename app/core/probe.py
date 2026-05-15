"""DVR reachability probe. Runs in a worker thread, logs results."""

from __future__ import annotations

import socket
import time
import uuid

from PySide6.QtCore import QThread, Signal

from app.core.config import Settings
from app.core.log import bus


class ProbeWorker(QThread):
    finished_with = Signal(bool, str)  # ok, summary

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings

    def run(self) -> None:
        s = self._settings
        bus.info("PROBE", f"Probing DVR at {s.dvr_ip}:{s.dvr_port}")
        t0 = time.monotonic()
        try:
            with socket.create_connection((s.dvr_ip, s.dvr_port), timeout=4.0) as sock:
                elapsed = (time.monotonic() - t0) * 1000
                bus.info("PROBE", f"TCP connect OK in {elapsed:.0f} ms")
                # Speak RTSP DESCRIBE to see if anything answers
                req_id = str(uuid.uuid4())[:8]
                describe = (
                    f"OPTIONS rtsp://{s.dvr_ip}:{s.dvr_port}/ RTSP/1.0\r\n"
                    f"CSeq: 1\r\n"
                    f"User-Agent: cctv-console/{__import__('app').__version__}\r\n"
                    f"\r\n"
                ).encode()
                sock.settimeout(3.0)
                sock.sendall(describe)
                t1 = time.monotonic()
                data = sock.recv(2048)
                rtt = (time.monotonic() - t1) * 1000
                first_line = data.split(b"\r\n", 1)[0].decode("ascii", errors="replace") if data else "(no data)"
                bus.info("PROBE", f"RTSP OPTIONS reply in {rtt:.0f} ms: {first_line}")
                self.finished_with.emit(True, first_line)
        except socket.timeout:
            bus.error("PROBE", f"TCP connect to {s.dvr_ip}:{s.dvr_port} TIMED OUT (4s). DVR unreachable on this network.")
            self.finished_with.emit(False, "timeout")
        except OSError as e:
            bus.error("PROBE", f"TCP connect failed: {e!s}")
            self.finished_with.emit(False, str(e))
        except Exception as e:
            bus.error("PROBE", f"Unexpected probe error: {e!r}")
            self.finished_with.emit(False, repr(e))
