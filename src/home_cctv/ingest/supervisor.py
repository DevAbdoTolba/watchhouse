"""Shutdown supervisor.

A single ``threading.Event`` is shared between the capture loop and the signal
handler. Ctrl+C / SIGTERM flip the event; the loop checks it every read and
exits; the supervisor additionally force-releases every registered
``FrameSource`` to unblock stuck reads (PITFALLS §1.1 — releasing from another
thread is the only way to unblock a hung ``cap.read()`` on WSL2).

Design notes:

* Single-process for now (Phase 0). Phase 1+ can register multiple capture
  threads without changing this interface.
* ``shutdown()`` is idempotent — tests and signal storms both call it safely.
* ``install_signal_handlers()`` binds a module-level singleton so the C-level
  signal thunk can reach the supervisor without capturing it in a closure.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from typing import List, Optional, Protocol

_LOG = logging.getLogger("home_cctv.supervisor")


class _Releasable(Protocol):
    source_id: str

    def release(self) -> None: ...


class ShutdownSupervisor:
    """Owns the shared stop event and the registered-sources list."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._sources: List[_Releasable] = []
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._shutdown_called = False

    # ----------------------------------------------------------- registration
    def register(self, source: _Releasable) -> None:
        with self._lock:
            self._sources.append(source)

    # ----------------------------------------------------------- stop signals
    def request_stop(self) -> None:
        """Flip the stop event without releasing sources — safe from anywhere."""
        self.stop_event.set()

    def shutdown(self) -> None:
        """Force-release every registered FrameSource. Idempotent."""
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            self.stop_event.set()
            sources, self._sources = self._sources, []
        for s in sources:
            try:
                _LOG.info("shutdown releasing source_id=%s", s.source_id)
                s.release()
            except Exception as exc:  # pragma: no cover — defensive
                _LOG.warning(
                    "shutdown release_failed source_id=%s err=%r",
                    s.source_id,
                    exc,
                )

    # -------------------------------------------------------------- blocking
    def wait_for_shutdown(self, timeout: float) -> bool:
        """Block until the stop event fires or ``timeout`` elapses.

        Returns ``True`` if the event fired, ``False`` on timeout.
        """
        return self.stop_event.wait(timeout=timeout)


_SINGLETON: Optional[ShutdownSupervisor] = None


def install_signal_handlers(supervisor: ShutdownSupervisor) -> None:
    """Install SIGINT + SIGTERM handlers that invoke ``supervisor.shutdown()``.

    The handler does the minimum safe work in a signal context: it flips the
    stop event (cheap) then calls ``shutdown()`` which is idempotent.
    """
    global _SINGLETON
    _SINGLETON = supervisor

    def _handler(signum: int, _frame: object) -> None:
        _LOG.info("signal received signum=%s initiating shutdown", signum)
        if _SINGLETON is not None:
            _SINGLETON.request_stop()
            _SINGLETON.shutdown()

    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):  # pragma: no cover — not always available
        pass


__all__ = ["ShutdownSupervisor", "install_signal_handlers"]
