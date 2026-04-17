"""Tests for ``home_cctv.ingest.backoff`` — Phase 1 Plan 01-02 Task 1.

Covers the pure ``JitteredBackoff`` helper:

* Initial state (attempt=0, current_ceiling_s=1.0).
* Full-jitter formula: ``next_delay()`` returns ``uniform(0, current_ceiling)``.
* Climb schedule 1 → 2 → 4 → 8 → 16 → 30 (cap).
* Sticky cap at 30 after many calls.
* Determinism under injected ``random.Random(seed)``.
* ``reset_if_healthy`` two-call semantics (first primes, second resets after 60 s).
* Pre-first-frame no-op when ``last_frame_monotonic is None``.

All tests are pure Python — no threads, no wall clock.
"""
from __future__ import annotations

import random

from home_cctv.ingest.backoff import (
    HEALTHY_RESET_S,
    INITIAL_BACKOFF_S,
    MAX_BACKOFF_S,
    JitteredBackoff,
)


# ------------------------------------------------------------------ constants


def test_module_constants_are_correct() -> None:
    assert INITIAL_BACKOFF_S == 1.0
    assert MAX_BACKOFF_S == 30.0
    assert HEALTHY_RESET_S == 60.0


# ----------------------------------------------------------------- Test 1: initial


def test_initial_state_is_1s_and_attempt_zero() -> None:
    b = JitteredBackoff()
    assert b.current_ceiling_s == 1.0
    assert b.attempt == 0


# ------------------------------------------------------ Test 2: first next_delay


def test_next_delay_first_call_is_bounded_by_one_and_doubles_ceiling() -> None:
    b = JitteredBackoff(rng=random.Random(0))
    delay = b.next_delay()
    assert 0.0 <= delay <= 1.0
    assert b.attempt == 1
    assert b.current_ceiling_s == 2.0


# -------------------------------------- Test 3: climb 1 → 2 → 4 → 8 → 16 → 30


def test_ceiling_climbs_through_expected_schedule() -> None:
    b = JitteredBackoff(rng=random.Random(0))
    # After 1 call: 2.0
    b.next_delay()
    assert b.current_ceiling_s == 2.0
    # After 2: 4.0
    b.next_delay()
    assert b.current_ceiling_s == 4.0
    # After 3: 8.0
    b.next_delay()
    assert b.current_ceiling_s == 8.0
    # After 4: 16.0
    b.next_delay()
    assert b.current_ceiling_s == 16.0
    # After 5: min(32, 30) = 30.0
    b.next_delay()
    assert b.current_ceiling_s == 30.0
    # After 6: still 30 (cap)
    b.next_delay()
    assert b.current_ceiling_s == 30.0


# ---------------------------------------------------- Test 4: cap is sticky


def test_cap_is_sticky_after_many_calls() -> None:
    b = JitteredBackoff(rng=random.Random(0))
    for _ in range(10):
        b.next_delay()
    assert b.current_ceiling_s == 30.0
    # Additional calls never exceed cap.
    for _ in range(20):
        d = b.next_delay()
        assert 0.0 <= d <= 30.0
        assert b.current_ceiling_s == 30.0


# ---------------------------------------------- Test 5: determinism via Random


def test_seeded_rng_produces_identical_delay_sequences() -> None:
    a = JitteredBackoff(rng=random.Random(42))
    b = JitteredBackoff(rng=random.Random(42))
    seq_a = [a.next_delay() for _ in range(10)]
    seq_b = [b.next_delay() for _ in range(10)]
    assert seq_a == seq_b


# --- Test 6: two-call reset_if_healthy semantics — first primes, second resets


def test_reset_if_healthy_primes_then_resets_after_60s() -> None:
    b = JitteredBackoff(rng=random.Random(0))
    # Advance state to current_ceiling_s=4.0, attempt=2
    b.next_delay()
    b.next_delay()
    assert b.current_ceiling_s == 4.0
    assert b.attempt == 2

    T0 = 100.0
    # First call: primes _healthy_since; returns False; state unchanged.
    result1 = b.reset_if_healthy(last_frame_monotonic=T0, now=T0)
    assert result1 is False
    assert b.current_ceiling_s == 4.0
    assert b.attempt == 2

    # Second call at T0+60: returns True; state reset.
    result2 = b.reset_if_healthy(
        last_frame_monotonic=T0 + 60.0, now=T0 + 60.0
    )
    assert result2 is True
    assert b.current_ceiling_s == 1.0
    assert b.attempt == 0


# --------- Test 7: two-call reset_if_healthy — still inside 60-s window


def test_reset_if_healthy_no_reset_inside_60s_window() -> None:
    b = JitteredBackoff(rng=random.Random(0))
    b.next_delay()
    b.next_delay()
    prev_ceiling = b.current_ceiling_s
    prev_attempt = b.attempt

    T0 = 100.0
    result1 = b.reset_if_healthy(last_frame_monotonic=T0, now=T0)
    assert result1 is False
    # 59.9 s later — still inside the window.
    result2 = b.reset_if_healthy(
        last_frame_monotonic=T0 + 59.9, now=T0 + 59.9
    )
    assert result2 is False
    # State unchanged.
    assert b.current_ceiling_s == prev_ceiling
    assert b.attempt == prev_attempt


# ---- Test 8: pre-first-frame no-op when last_frame_monotonic is None


def test_reset_if_healthy_is_noop_when_last_frame_is_none() -> None:
    b = JitteredBackoff(rng=random.Random(0))
    b.next_delay()
    assert b.attempt == 1

    result = b.reset_if_healthy(last_frame_monotonic=None, now=100.0)
    assert result is False
    # State unchanged.
    assert b.attempt == 1
    # Must NOT have primed _healthy_since — a subsequent first real call
    # should behave like the true first call, priming fresh.
    result2 = b.reset_if_healthy(last_frame_monotonic=200.0, now=200.0)
    assert result2 is False  # still just primed
    result3 = b.reset_if_healthy(
        last_frame_monotonic=200.0 + 60.0, now=200.0 + 60.0
    )
    assert result3 is True
