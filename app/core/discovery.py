"""LAN auto-discovery of the DVR.

Strategy:

1. Detect the host's primary IPv4 address. Treat the matching /24 as the
   search space (e.g. 192.168.1.0/24 if the host is 192.168.1.42).
2. Parallel TCP-connect to <prefix>.1..254 on the configured RTSP port
   (default 554) with a short per-host timeout.
3. For every host that accepts, send `OPTIONS * RTSP/1.0` and look for an
   `RTSP/` reply. First match wins.

The whole pass takes ~3-6 s on a /24 with 40 worker threads, all the work
happens off the GUI thread, and every meaningful event lands in the log
console so the user can see what's going on.
"""

from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from app.core.log import bus


TCP_PROBE_TIMEOUT = 0.4   # seconds per host
RTSP_PROBE_TIMEOUT = 1.5
SCAN_WORKERS = 40


@dataclass(frozen=True)
class DiscoveryResult:
    found_ip: str | None
    candidates_probed: int
    rtsp_hosts: tuple[str, ...]
    elapsed_ms: float


def detect_local_prefix() -> str | None:
    """Return the host's /24 prefix (e.g. '192.168.1'), or None if no
    routable IPv4 interface is reachable.

    Uses the standard UDP-socket trick: opening a datagram socket toward
    a public address (no traffic actually sent) makes the OS pick the
    primary interface, whose `getsockname()` then reveals our LAN IP.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 53))
        local_ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()

    if not local_ip or local_ip.startswith("127."):
        return None
    parts = local_ip.split(".")
    if len(parts) != 4:
        return None
    return ".".join(parts[:3])


def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _speaks_rtsp(ip: str, port: int, timeout: float = RTSP_PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            req = (
                f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\n"
                f"CSeq: 1\r\n"
                f"User-Agent: cctv-console\r\n"
                f"\r\n"
            ).encode("ascii")
            s.sendall(req)
            data = s.recv(2048)
            return b"RTSP/" in data
    except OSError:
        return False


class DiscoveryWorker(QThread):
    completed = Signal(DiscoveryResult)

    def __init__(self, port: int, priority_ips: tuple[str, ...] = (), parent=None) -> None:
        super().__init__(parent)
        self._port = port
        self._priority = tuple(dict.fromkeys(priority_ips))  # de-dupe, keep order

    def run(self) -> None:
        t0 = time.monotonic()

        # Fast path: parallel-probe last-known-good IPs (MRU first). If one
        # of them still answers RTSP, return immediately, no /24 scan.
        if self._priority:
            bus.info("DISC", f"checking {len(self._priority)} cached IP(s) first: {', '.join(self._priority)}")
            hit = self._race_priority()
            if hit is not None:
                elapsed_ms = (time.monotonic() - t0) * 1000
                bus.info("DISC", f"cached IP {hit} still valid (resolved in {elapsed_ms:.0f} ms)")
                self.completed.emit(
                    DiscoveryResult(
                        found_ip=hit,
                        candidates_probed=len(self._priority),
                        rtsp_hosts=(hit,),
                        elapsed_ms=elapsed_ms,
                    )
                )
                return
            bus.info("DISC", "no cached IP responded; falling back to /24 scan")

        prefix = detect_local_prefix()
        if prefix is None:
            bus.error("DISC", "could not detect local subnet (no LAN connection?)")
            self.completed.emit(DiscoveryResult(None, 0, (), 0.0))
            return

        bus.info("DISC", f"scanning {prefix}.0/24 on port {self._port}")
        ips = [f"{prefix}.{i}" for i in range(1, 255)]

        open_hosts: list[str] = []
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            futures = {ex.submit(_tcp_open, ip, self._port, TCP_PROBE_TIMEOUT): ip for ip in ips}
            for fut in as_completed(futures):
                ip = futures[fut]
                try:
                    if fut.result():
                        open_hosts.append(ip)
                except Exception:
                    pass

        scan_ms = (time.monotonic() - t0) * 1000
        bus.info(
            "DISC",
            f"port {self._port} open on {len(open_hosts)} host(s) after {scan_ms:.0f} ms",
        )
        if open_hosts:
            bus.info("DISC", f"candidates: {', '.join(sorted(open_hosts))}")

        rtsp_hosts: list[str] = []
        chosen: str | None = None
        for ip in sorted(open_hosts):
            if _speaks_rtsp(ip, self._port):
                rtsp_hosts.append(ip)
                bus.info("DISC", f"{ip} answered RTSP OPTIONS")
                if chosen is None:
                    chosen = ip
            else:
                bus.debug("DISC", f"{ip} open on {self._port} but did not speak RTSP")

        elapsed_ms = (time.monotonic() - t0) * 1000
        if chosen is None:
            bus.error("DISC", f"no RTSP server found on {prefix}.0/24 ({elapsed_ms:.0f} ms total)")
        else:
            bus.info("DISC", f"DVR at {chosen}  (discovered in {elapsed_ms:.0f} ms)")

        self.completed.emit(
            DiscoveryResult(
                found_ip=chosen,
                candidates_probed=len(ips),
                rtsp_hosts=tuple(rtsp_hosts),
                elapsed_ms=elapsed_ms,
            )
        )

    def _race_priority(self) -> str | None:
        """Probe priority IPs in parallel; return the first that speaks RTSP."""
        with ThreadPoolExecutor(max_workers=max(1, len(self._priority))) as ex:
            futures = {ex.submit(self._priority_check, ip): ip for ip in self._priority}
            for fut in as_completed(futures):
                ip = futures[fut]
                try:
                    if fut.result():
                        return ip
                except Exception:
                    pass
        return None

    def _priority_check(self, ip: str) -> bool:
        # Slightly more generous timeouts than a /24 scan; the cached IP
        # really should respond, so wait long enough to be sure.
        if not _tcp_open(ip, self._port, timeout=1.0):
            return False
        return _speaks_rtsp(ip, self._port, timeout=1.5)
