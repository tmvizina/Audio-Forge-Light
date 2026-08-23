"""Tests for the adaptive concurrency pool (T05 contract).

Every test uses a fake, in-memory job coroutine -- no network, no TTS, no
GPU, no credit spent. Real 2s/4s/8s backoff waits are skipped by injecting a
fake `sleep` into `AdaptivePool.run()` (see `fake_sleep()` below); job
"latencies" are real but tiny (milliseconds) asyncio.sleep calls, so the
whole suite runs in a couple of seconds, not minutes.

Tests 4-9 exercise the adaptive decision points (`_on_call_success` /
`_on_call_failure`) directly against a hand-built `_AdaptiveState`, which is
both faster and more precise than driving the same scenario through a full
`run()` -- there is no public/private distinction that blocks this within
the same package, and the ticket explicitly asks for the exact
degrade-decision function to be exercised.
"""

from __future__ import annotations

import asyncio

import pytest

import pool as pool_module
from pool import (
    AdaptivePool,
    CallTimeoutError,
    RateLimitError,
    RetryableError,
    RunResult,
    ServerError,
    _AdaptiveState,
    backoff_seconds,
    degrade_reason,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fake_sleep():
    """A `sleep` replacement for AdaptivePool.run() that records every
    requested delay but never actually waits -- this is what keeps the
    suite out of real 2s/4s/8s backoffs."""
    calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        calls.append(seconds)

    return _sleep, calls


def run(coro):
    """Run a coroutine to completion. No pytest-asyncio plugin is declared
    as a project dependency, so tests drive the event loop directly."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Acceptance test (ticket item 1 / BUILD-PROMPT.md §13.4): a forced 429
#    drops concurrency to 1 and the run still finishes with every chunk
#    accounted for.
# ---------------------------------------------------------------------------


def test_forced_429_drops_to_floor_and_run_still_finishes_every_chunk():
    """Simulates a genuinely rate-limited free tier: the first 5 call
    attempts of the run (however they land across the 3 initially-concurrent
    chunks and their retries) all come back 429. That is enough to cascade
    the degrade 3 -> 2 -> 1 and hold at the floor (further 429s inside that
    same opening wave are silent no-ops once at 1) -- and the run still
    finishes with every chunk accounted for, none lost, none stuck retrying
    forever."""

    n = 10
    forced_429_budget = {"n": 5}
    seen_before = set()

    async def job_fn(index: int, payload) -> str:
        # 429 only a chunk's very first attempt, and only for the opening
        # wave -- guarantees no single chunk is ever hit twice, so every
        # chunk succeeds well within its 3-attempt retry budget regardless
        # of exactly how the 3 workers happen to interleave.
        if index not in seen_before and forced_429_budget["n"] > 0:
            seen_before.add(index)
            forced_429_budget["n"] -= 1
            raise RateLimitError("rate limited", retry_after=0.0)
        return f"audio-{index}"

    sleep, _ = fake_sleep()

    async def main():
        p = AdaptivePool(max_workers=3, target=3)
        result = await p.run(
            list(range(n)),
            job_fn,
            courtesy_delay_s=0,
            sleep=sleep,
        )
        return p, result

    p, result = run(main())

    assert p.target == 1
    assert result.final_concurrency == 1
    # every chunk accounted for: either succeeded or recorded failed, none
    # missing, none silently dropped -- and with only 5 forced 429s spread
    # across up to 3 attempts each on 10 chunks, none should have exhausted
    # its retry budget.
    accounted = set(result.results) | set(result.failed)
    assert accounted == set(range(n))
    assert result.failed == {}
    assert len(result.results) == n


# ---------------------------------------------------------------------------
# 2. Results come back in chunk-index order even when jobs complete out of
#    order (job 12 slower than job 40).
# ---------------------------------------------------------------------------


def test_results_keyed_by_index_not_completion_order():
    n = 50
    completion_order: list[int] = []

    async def job_fn(index: int, payload) -> str:
        if index == 12:
            await asyncio.sleep(0.15)  # still real, but tiny
        return f"r{index}"

    sleep, _ = fake_sleep()

    async def main():
        p = AdaptivePool(max_workers=3, target=3)

        def on_done(outcome: pool_module.JobOutcome) -> None:
            completion_order.append(outcome.index)

        result = await p.run(
            list(range(n)),
            job_fn,
            courtesy_delay_s=0,
            sleep=sleep,
            on_job_done=on_done,
        )
        return result

    result = run(main())

    # job 40 must have completed before job 12 in wall-clock/arrival order...
    assert completion_order.index(40) < completion_order.index(12)
    # ...yet both land at their own chunk-index position in the results.
    assert result.results[12] == "r12"
    assert result.results[40] == "r40"
    ordered = result.ordered()
    assert ordered[12] == "r12"
    assert ordered[40] == "r40"
    assert ordered[0] == "r0"


# ---------------------------------------------------------------------------
# 3. Lowering the target does not cancel an in-flight job.
# ---------------------------------------------------------------------------


def test_shrinking_target_does_not_cancel_in_flight_job():
    async def main():
        p = AdaptivePool(max_workers=3, target=3)
        started = asyncio.Event()
        release = asyncio.Event()

        async def job_fn(index: int, payload):
            if index == 0:
                started.set()
                await release.wait()
                return "r0"
            return f"r{index}"

        sleep, _ = fake_sleep()
        run_task = asyncio.create_task(
            p.run([0, 1, 2], job_fn, courtesy_delay_s=0, sleep=sleep)
        )

        # Wait until job 0's worker is genuinely mid-request (past the pull,
        # inside job_fn) before shrinking the pool.
        await started.wait()
        p.set_target(1)  # shrink while job 0 is in flight
        assert p.target == 1

        # Job 0 must NOT have been cancelled by the shrink -- release it now
        # and confirm it still completes normally.
        release.set()
        result = await run_task
        return result

    result = run(main())
    assert result.results[0] == "r0"
    assert result.results[1] == "r1"
    assert result.results[2] == "r2"
    assert result.failed == {}


# ---------------------------------------------------------------------------
# 4. Baseline is frozen: after 5 successes, later slow calls don't move it.
# ---------------------------------------------------------------------------


def test_baseline_freezes_after_first_five_successes():
    p = AdaptivePool(max_workers=3, target=3)
    state = _AdaptiveState(ramp_up_enabled=False, ceiling=3)

    for _ in range(5):
        p._on_call_success(state, 1.0)
    assert state.baseline == 1.0

    # Much slower calls afterwards must not move the frozen baseline.
    for _ in range(10):
        p._on_call_success(state, 50.0)
    assert state.baseline == 1.0


# ---------------------------------------------------------------------------
# 5. Latency median > baseline * 1.75 degrades exactly one step.
# ---------------------------------------------------------------------------


def test_latency_median_above_threshold_degrades_one_step_not_to_floor(monkeypatch):
    changes: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        pool_module, "emit_concurrency_changed", lambda f, t, r: changes.append((f, t, r))
    )

    p = AdaptivePool(max_workers=3, target=3)
    state = _AdaptiveState(ramp_up_enabled=False, ceiling=3)

    for _ in range(5):
        p._on_call_success(state, 1.0)  # baseline = 1.0
    assert p.target == 3

    # 1.75x baseline = 1.75; feed latencies that push the rolling median of
    # the last 5 completed calls over that line.
    p._on_call_success(state, 2.0)  # window: 1,1,1,1,2 -> median 1.0, no trip
    p._on_call_success(state, 2.0)  # window: 1,1,1,2,2 -> median 1.0, no trip
    p._on_call_success(state, 2.0)  # window: 1,1,2,2,2 -> median 2.0 > 1.75 -> degrade

    assert p.target == 2  # exactly one step down from 3, not to the floor
    assert len(changes) == 1
    assert changes[0][0] == 3 and changes[0][1] == 2
    assert "median" in changes[0][2] and "baseline" in changes[0][2]


# ---------------------------------------------------------------------------
# 6. A 429 degrades immediately even when latencies are fast.
# ---------------------------------------------------------------------------


def test_429_degrades_immediately_even_with_fast_latencies(monkeypatch):
    changes: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        pool_module, "emit_concurrency_changed", lambda f, t, r: changes.append((f, t, r))
    )

    p = AdaptivePool(max_workers=3, target=3)
    state = _AdaptiveState(ramp_up_enabled=False, ceiling=3)

    for _ in range(5):
        p._on_call_success(state, 0.05)  # baseline = 0.05, all fast

    assert p.target == 3

    p._on_call_failure(state, RateLimitError("nope"))

    assert p.target == 2
    assert changes == [(3, 2, "429 received")]


# ---------------------------------------------------------------------------
# 7. The floor holds at 1 -- repeated failures never reach 0.
# ---------------------------------------------------------------------------


def test_floor_holds_at_one_under_repeated_failures():
    p = AdaptivePool(max_workers=3, target=3)
    state = _AdaptiveState(ramp_up_enabled=False, ceiling=3)

    for _ in range(10):
        p._on_call_failure(state, ServerError("boom"))

    assert p.target == 1  # never 0, never negative


# ---------------------------------------------------------------------------
# 8. --ramp-up off: a degraded run stays degraded after 20 fast successes.
# ---------------------------------------------------------------------------


def test_ramp_up_off_stays_degraded_after_many_fast_successes():
    p = AdaptivePool(max_workers=3, target=1)  # already degraded
    state = _AdaptiveState(ramp_up_enabled=False, ceiling=3)

    for _ in range(5):
        p._on_call_success(state, 0.02)  # baseline = 0.02
    for _ in range(20):
        p._on_call_success(state, 0.02)  # 20 more fast successes

    assert p.target == 1  # ramp-up disabled: never steps back up


# ---------------------------------------------------------------------------
# 9. --ramp-up on: steps back up after 10 consecutive fast successes, capped
#    at the original starting value (3).
# ---------------------------------------------------------------------------


def test_ramp_up_on_steps_back_up_after_ten_fast_successes_capped_at_three():
    p = AdaptivePool(max_workers=3, target=3)
    state = _AdaptiveState(ramp_up_enabled=True, ceiling=3)
    p.set_target(1, reason="test setup: simulate a degraded run")
    assert p.target == 1

    for _ in range(30):
        p._on_call_success(state, 0.02)  # all fast, all consecutive

    assert p.target == 3  # ramped back up, never above the starting ceiling


def test_ramp_up_needs_a_full_streak_of_ten_before_stepping():
    p = AdaptivePool(max_workers=3, target=3)
    state = _AdaptiveState(ramp_up_enabled=True, ceiling=3)
    p.set_target(1, reason="test setup")

    for _ in range(5):
        p._on_call_success(state, 0.02)  # freeze baseline, streak starts at 1
    for _ in range(8):
        p._on_call_success(state, 0.02)  # streak: 2..9, not yet 10

    assert p.target == 1  # not ramped up yet

    p._on_call_success(state, 0.02)  # 10th consecutive fast success

    assert p.target == 2  # exactly one step, not straight to the ceiling


# ---------------------------------------------------------------------------
# 10. Backoff is 2/4/8s, and Retry-After overrides it.
# ---------------------------------------------------------------------------


def test_backoff_schedule_is_2_4_8_and_retry_after_overrides():
    assert backoff_seconds(0) == 2.0
    assert backoff_seconds(1) == 4.0
    assert backoff_seconds(2) == 8.0
    # beyond the published schedule, clamp to the last entry.
    assert backoff_seconds(99) == 8.0
    # Retry-After always wins, at any attempt index.
    assert backoff_seconds(0, retry_after=30.0) == 30.0
    assert backoff_seconds(2, retry_after=30.0) == 30.0


def test_run_honours_backoff_schedule_and_retry_after_header():
    attempts = {"n": 0}

    async def job_fn(index: int, payload):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RetryableError("transient")  # -> backoff_seconds(0) == 2.0
        if attempts["n"] == 2:
            raise RateLimitError("slow down", retry_after=30.0)  # overrides 4.0
        return "ok"

    sleep, calls = fake_sleep()

    async def main():
        p = AdaptivePool(max_workers=1, target=1)
        return await p.run([0], job_fn, courtesy_delay_s=0, sleep=sleep)

    result = run(main())

    assert result.results[0] == "ok"
    assert calls == [2.0, 30.0]


# ---------------------------------------------------------------------------
# 11. A chunk that exhausts 3 attempts is recorded failed, the remaining
#     chunks still run, and the failure appears in the end-of-run summary.
# ---------------------------------------------------------------------------


def test_chunk_exhausting_retries_is_recorded_failed_and_run_continues():
    async def job_fn(index: int, payload) -> str:
        if index == 2:
            raise ServerError("still down")
        return f"ok-{index}"

    sleep, _ = fake_sleep()

    async def main():
        p = AdaptivePool(max_workers=3, target=3)
        return await p.run(
            list(range(5)), job_fn, courtesy_delay_s=0, sleep=sleep, max_attempts=3
        )

    result = run(main())

    assert result.failed == {2: "still down"}
    assert result.results == {0: "ok-0", 1: "ok-1", 3: "ok-3", 4: "ok-4"}
    # the rest of the book didn't die because of chunk 2.
    accounted = set(result.results) | set(result.failed)
    assert accounted == {0, 1, 2, 3, 4}
    assert result.summary_lines() == ["chunk 2 failed: still down"]


# ---------------------------------------------------------------------------
# Bonus: the degrade-decision function in isolation (pure, no pool/state).
# ---------------------------------------------------------------------------


def test_degrade_reason_pure_function_covers_every_branch():
    assert degrade_reason(error=RateLimitError("x"), median_recent=None, baseline=None) == "429 received"
    assert degrade_reason(error=ServerError("x"), median_recent=None, baseline=None) == "5xx received"
    assert degrade_reason(error=CallTimeoutError("x"), median_recent=None, baseline=None) == "timeout"
    # a plain retryable error is retried but is not, by itself, degrade-worthy.
    assert degrade_reason(error=RetryableError("x"), median_recent=None, baseline=None) is None
    # latency path only evaluated when there is no error.
    assert degrade_reason(error=None, median_recent=10.0, baseline=1.0) == "median 10.0s vs baseline 1.0s"
    assert degrade_reason(error=None, median_recent=1.7, baseline=1.0) is None  # under 1.75x
    assert degrade_reason(error=None, median_recent=None, baseline=1.0) is None  # no window yet
