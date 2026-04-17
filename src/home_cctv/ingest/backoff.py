"""Jittered exponential backoff helper — Phase 1 Plan 01-02 Task 1.

Pure Python, no threads, no I/O. Injected RNG for deterministic tests.

Schedule (CONTEXT §D-07):

    attempt        1     2     3     4      5      6+
    ceiling_s      1     2     4     8     16     30 (cap)
    delay          uniform(0, ceiling_s)   ← full-jitter (ARCHITECTURE.md §189)

After ``HEALTHY_RESET_S`` seconds of sustained healthy frame reads, the counter
resets to 1. This is tracked by the two-call ``reset_if_healthy`` pattern:

    1. Caller invokes ``reset_if_healthy`` immediately after every frame the
       reader successfully decodes. By construction ``last_frame_monotonic ≈ now``.
    2. The first such call primes ``_healthy_since``; subsequent calls where
       ``now - _healthy_since >= HEALTHY_RESET_S`` call ``reset()`` and return True.

No cross-call staleness gate is needed — the freshness of
``last_frame_monotonic`` is an *input contract*, not something this method
re-validates. A staleness gate would contradict the invariant above and make
the two-call test impossible for callers that legitimately have
``last_frame_monotonic == now`` to the microsecond.
"""
from __future__ import annotations

import random
from typing import Optional

#: Initial backoff ceiling on first reconnect attempt (CONTEXT §D-07).
INITIAL_BACKOFF_S: float = 1.0

#: Sticky cap — backoff ceiling never exceeds this value (CONTEXT §D-07).
MAX_BACKOFF_S: float = 30.0

#: Sustained-healthy window — after this many seconds of continuous frames,
#: the backoff resets to 1 s so a subsequent outage starts fast again.
HEALTHY_RESET_S: float = 60.0


class JitteredBackoff:
    """Full-jitter exponential backoff with sustained-health reset.

    Deterministic under an injected ``random.Random``. No threads, no I/O.
    Instances are NOT thread-safe — each caller (per-camera reader) owns
    its own instance.
    """

    def __init__(self, *, rng: Optional[random.Random] = None) -> None:
        self._rng = rng if rng is not None else random.Random()
        self.current_ceiling_s: float = INITIAL_BACKOFF_S
        self.attempt: int = 0
        self._healthy_since: Optional[float] = None

    def next_delay(self) -> float:
        """Return a full-jitter delay in ``[0, current_ceiling_s]``.

        Advances ``attempt`` and doubles ``current_ceiling_s`` (capped at
        ``MAX_BACKOFF_S``) as a side effect.
        """
        delay = self._rng.uniform(0.0, self.current_ceiling_s)
        self.attempt += 1
        self.current_ceiling_s = min(self.current_ceiling_s * 2.0, MAX_BACKOFF_S)
        return delay

    def reset(self) -> None:
        """Reset to initial state (ceiling=1.0, attempt=0, unhealthy)."""
        self.current_ceiling_s = INITIAL_BACKOFF_S
        self.attempt = 0
        self._healthy_since = None

    def reset_if_healthy(
        self,
        *,
        last_frame_monotonic: Optional[float],
        now: float,
    ) -> bool:
        """Opportunistically reset after ``HEALTHY_RESET_S`` of continuous health.

        Semantic invariant: the caller invokes this immediately after every
        frame the reader successfully decodes, so ``last_frame_monotonic ≈ now``.

        Returns ``True`` exactly on the call that triggers a reset; ``False``
        otherwise (including the first priming call and pre-first-frame calls).
        """
        if last_frame_monotonic is None:
            # Pre-first-frame: do NOT prime (no sustained-health window yet).
            return False
        if self._healthy_since is None:
            # First post-reset successful read — start the 60-s window.
            self._healthy_since = last_frame_monotonic
            return False
        if last_frame_monotonic < self._healthy_since:
            # Clock restart or out-of-order call — re-prime with the newer value.
            self._healthy_since = last_frame_monotonic
            return False
        if now - self._healthy_since >= HEALTHY_RESET_S:
            self.reset()
            return True
        return False


__all__ = [
    "JitteredBackoff",
    "INITIAL_BACKOFF_S",
    "MAX_BACKOFF_S",
    "HEALTHY_RESET_S",
]
