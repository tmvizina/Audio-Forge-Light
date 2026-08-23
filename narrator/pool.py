"""Adaptive concurrency pool. Owned by T05.

Transport-agnostic: this module knows nothing about HTTP, Fish TTS, or
`Chunk` objects. It drives an async `job_fn(index, payload) -> Any`
coroutine function against a list of arbitrary payloads with a fixed worker
pool whose *effective* concurrency can shrink without cancelling in-flight
work (the "parking pattern"), and that adapts concurrency up/down based on
latency and a small set of retryable error types.

Contract for `job_fn`
----------------------
`job_fn` may simply return a value on success. To signal a condition this
pool should treat specially, raise one of the exceptions below:

- `RateLimitError`  -- HTTP 429 (or equivalent). Degrades concurrency
  immediately. May carry `retry_after` (seconds) to honour a `Retry-After`
  header; when present it overrides the computed backoff for the next try.
- `ServerError`      -- HTTP 5xx. Degrades concurrency immediately.
- Any other exception is treated as a plain failure: it is retried up to
  `max_attempts` like the above, but does NOT force a concurrency degrade.
  (A per-request timeout is handled internally via `asyncio.wait_for` and
  is reported to the degrade logic as if `CallTimeoutError` were raised --
  `job_fn` never needs to raise it itself.)

Callers that only have a status-code-in-message exception (e.g. this
project's `fish_client.synthesize`, which raises a plain `RuntimeError`
with the status code embedded in its text) must translate that into
`RateLimitError` / `ServerError` at the call site before handing the
coroutine to this pool -- pool.py deliberately does not parse error
strings, since it has no idea what transport produced them.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from events import emit_concurrency_changed, log

# ---------------------------------------------------------------------------
# Exceptions job_fn may raise to signal a retryable / degrade-worthy call.
# ---------------------------------------------------------------------------


class RetryableError(Exception):
    """Base class for conditions this pool retries. Subclasses that are also
    degrade-worthy (rate limit, server error, timeout) are recognised by
    `degrade_reason()` below; a bare `RetryableError` is retried but does
    not by itself force a concurrency degrade."""

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitError(RetryableError):
    """HTTP 429. Degrades concurrency immediately, regardless of latency."""


class ServerError(RetryableError):
    """HTTP 5xx. Degrades concurrency immediately, regardless of latency."""


class CallTimeoutError(RetryableError):
    """The per-request timeout elapsed. Raised internally by the pool (via
    `asyncio.wait_for`) -- job_fn does not need to raise this itself.
    Degrades concurrency immediately."""


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Backoff schedule, one entry per retry (0-based: index 0 is the wait before
# the 2nd try, index 1 before the 3rd, index 2 before a 4th if ever
# configured). At the default max_attempts=3 only indices 0 and 1 are ever
# consumed by a real run; the full published schedule (2/4/8s) is still
# addressable directly via `backoff_seconds()` for testing / future reuse.
BACKOFF_SCHEDULE_S: tuple[float, ...] = (2.0, 4.0, 8.0)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_COURTESY_DELAY_S = 0.3
DEGRADE_LATENCY_FACTOR = 1.75
RAMP_UP_STREAK = 10
BASELINE_SAMPLE_SIZE = 5


def backoff_seconds(attempt_index: int, retry_after: float | None = None) -> float:
    """Delay before the retry following a failed attempt.

    `attempt_index` is 0-based (0 = wait before the 2nd try). A `retry_after`
    value (from a `Retry-After` header) always wins over the computed
    schedule.
    """
    if retry_after is not None:
        return retry_after
    idx = min(attempt_index, len(BACKOFF_SCHEDULE_S) - 1)
    return BACKOFF_SCHEDULE_S[idx]


def degrade_reason(
    *,
    error: BaseException | None,
    median_recent: float | None,
    baseline: float | None,
) -> str | None:
    """The single degrade-decision function: implements the ticket's
    two-condition OR rule explicitly, so it can never collapse into one
    check. Returns the human-readable reason to log/emit if this outcome
    should degrade concurrency by exactly one step, else None.

    Degrade triggers on EITHER:
      - error is a RateLimitError / ServerError / CallTimeoutError
        (immediate, regardless of latency), OR
      - (error is None and) the median of the last 5 successful call
        latencies exceeds baseline * DEGRADE_LATENCY_FACTOR.
    """
    if isinstance(error, RateLimitError):
        return "429 received"
    if isinstance(error, ServerError):
        return "5xx received"
    if isinstance(error, CallTimeoutError):
        return "timeout"
    if error is None and median_recent is not None and baseline is not None:
        if median_recent > baseline * DEGRADE_LATENCY_FACTOR:
            return f"median {median_recent:.1f}s vs baseline {baseline:.1f}s"
    return None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class JobOutcome:
    """Reported to an optional `on_job_done` callback after each job settles
    (success, or final failure after exhausting retries). Carries enough for
    a caller that knows the domain (e.g. narrate.py, which knows chunk_id)
    to emit chunk_done / chunk_failed events itself."""

    index: int
    success: bool
    value: Any = None
    error: str | None = None
    attempts: int = 0
    latency_s: float = 0.0
    concurrency: int = 1


@dataclass
class RunResult:
    """Outcome of an `AdaptivePool.run()` call."""

    results: dict[int, Any] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)
    final_concurrency: int = 1

    def ordered(self) -> list[Any]:
        """Results in chunk-index order, 0..max seen. A failed index is
        represented as None -- callers that need the failure reason should
        use `.failed` directly."""
        indices = list(self.results) + list(self.failed)
        if not indices:
            return []
        n = max(indices) + 1
        return [self.results.get(i) for i in range(n)]

    def summary_lines(self) -> list[str]:
        """Human-readable end-of-run failure summary, sorted by index."""
        return [f"chunk {i} failed: {msg}" for i, msg in sorted(self.failed.items())]


@dataclass
class _JobRecord:
    index: int
    payload: Any


@dataclass
class _AdaptiveState:
    ramp_up_enabled: bool
    ceiling: int
    baseline: float | None = None
    first_successes: list = field(default_factory=list)
    recent_latencies: list = field(default_factory=list)
    fast_streak: int = 0

    def record_success(self, latency_s: float) -> None:
        if self.baseline is None:
            self.first_successes.append(latency_s)
        self.recent_latencies.append(latency_s)


class _CourtesyGate:
    """Paces the *start* of consecutive calls across the whole pool (not
    per-worker), so a free-tier key is never hammered with a burst even at
    full concurrency. A no-op when `delay_s <= 0`."""

    def __init__(
        self,
        delay_s: float,
        sleep: Callable[[float], Awaitable[None]],
        now: Callable[[], float],
    ):
        self.delay_s = delay_s
        self._sleep = sleep
        self._now = now
        self._lock = asyncio.Lock()
        self._last_start: float | None = None

    async def wait_turn(self) -> None:
        if self.delay_s <= 0:
            return
        async with self._lock:
            current = self._now()
            if self._last_start is not None:
                remaining = self.delay_s - (current - self._last_start)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_start = self._now()


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


class AdaptivePool:
    """Fixed-size worker pool with adaptive *effective* concurrency.

    `max_workers` is the ceiling and the pool's physical size (in this
    project it always equals `target`, e.g. 3 -- you never need more live
    workers than the starting concurrency). `target` is how many of those
    workers are currently allowed to pull work; shrinking it never cancels a
    worker mid-request -- see `_sync_events`.
    """

    def __init__(self, max_workers: int, target: int = 3):
        if target > max_workers:
            raise ValueError("target cannot exceed max_workers")
        self.max_workers = max_workers
        self.start_target = target
        self.target = target
        self.events = [asyncio.Event() for _ in range(max_workers)]
        self._sync_events()

    def _sync_events(self) -> None:
        for i, ev in enumerate(self.events):
            if i < self.target:
                ev.set()
            else:
                ev.clear()

    def set_target(self, n: int, reason: str = "") -> None:
        """Change effective concurrency. Floor is 1; ceiling is
        `start_target` (ramp-up never exceeds the run's starting
        concurrency). A no-op (no event, no log line) if `n` doesn't
        actually change the clamped target -- e.g. repeated failures at the
        floor stay at 1 silently rather than spamming identical log lines.
        """
        new_target = max(1, min(n, self.start_target))
        if new_target == self.target:
            return
        old = self.target
        self.target = new_target
        self._sync_events()
        if reason:
            emit_concurrency_changed(old, new_target, reason)
            log(f"concurrency {old} → {new_target} ({reason})")

    async def run(
        self,
        jobs: list[Any],
        job_fn: Callable[[int, Any], Awaitable[Any]],
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        courtesy_delay_s: float = DEFAULT_COURTESY_DELAY_S,
        ramp_up: bool = False,
        on_job_done: Callable[[JobOutcome], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> RunResult:
        """Run `job_fn(index, payload)` for every item in `jobs`, respecting
        (and adapting) this pool's effective concurrency.

        Results land in the returned `RunResult` keyed by index, regardless
        of completion order. A chunk that exhausts `max_attempts` is
        recorded in `.failed` and the run continues to the rest.

        `sleep`/`now` are injectable so tests can skip real backoff waits
        (2/4/8s) without monkeypatching global asyncio state -- pass a fake
        that records calls and returns immediately.
        """
        queue: "asyncio.Queue[_JobRecord]" = asyncio.Queue()
        for i, payload in enumerate(jobs):
            queue.put_nowait(_JobRecord(i, payload))

        result = RunResult(final_concurrency=self.target)
        state = _AdaptiveState(ramp_up_enabled=ramp_up, ceiling=self.start_target)
        lock = asyncio.Lock()  # guards state + result mutation across workers
        gate = _CourtesyGate(courtesy_delay_s, sleep, now)

        async def run_one(record: _JobRecord) -> None:
            last_error: BaseException | None = None
            elapsed = 0.0
            for attempt in range(max_attempts):
                if attempt > 0:
                    retry_after = getattr(last_error, "retry_after", None)
                    await sleep(backoff_seconds(attempt - 1, retry_after))
                await gate.wait_turn()

                start = now()
                try:
                    value = await asyncio.wait_for(
                        job_fn(record.index, record.payload), timeout=timeout_s
                    )
                except asyncio.TimeoutError:
                    elapsed = now() - start
                    last_error = CallTimeoutError(f"timed out after {timeout_s}s")
                    async with lock:
                        self._on_call_failure(state, last_error)
                    continue
                except Exception as exc:  # noqa: BLE001 - job_fn's contract
                    elapsed = now() - start
                    last_error = exc
                    async with lock:
                        self._on_call_failure(state, last_error)
                    continue

                elapsed = now() - start
                async with lock:
                    result.results[record.index] = value
                    self._on_call_success(state, elapsed)
                if on_job_done:
                    on_job_done(
                        JobOutcome(
                            record.index,
                            True,
                            value=value,
                            attempts=attempt + 1,
                            latency_s=elapsed,
                            concurrency=self.target,
                        )
                    )
                return

            # exhausted every attempt -- record failed, run continues.
            message = str(last_error) if last_error else "unknown error"
            async with lock:
                result.failed[record.index] = message
            if on_job_done:
                on_job_done(
                    JobOutcome(
                        record.index,
                        False,
                        error=message,
                        attempts=max_attempts,
                        latency_s=elapsed,
                        concurrency=self.target,
                    )
                )

        async def worker(idx: int) -> None:
            """One worker slot. Parks on its event whenever idx >= target;
            never re-checks target mid-job, so a job already running always
            finishes -- this is the parking pattern (BUILD-PROMPT.md §8)."""
            while True:
                await self.events[idx].wait()  # park here if idx >= target
                try:
                    record = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await run_one(record)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker(i)) for i in range(self.max_workers)]
        await queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        result.final_concurrency = self.target
        return result

    # -- adaptive decision points, factored out so they're each independently
    # testable and so the OR rule lives in exactly one place (degrade_reason).

    def _on_call_success(self, state: _AdaptiveState, latency_s: float) -> None:
        state.record_success(latency_s)
        if state.baseline is None and len(state.first_successes) >= BASELINE_SAMPLE_SIZE:
            state.baseline = statistics.median(state.first_successes[:BASELINE_SAMPLE_SIZE])
            log(f"baseline latency frozen at {state.baseline:.2f}s (median of first 5 successes)")

        median_recent = None
        if state.baseline is not None and len(state.recent_latencies) >= BASELINE_SAMPLE_SIZE:
            median_recent = statistics.median(state.recent_latencies[-BASELINE_SAMPLE_SIZE:])

        reason = degrade_reason(error=None, median_recent=median_recent, baseline=state.baseline)
        if reason:
            self.set_target(self.target - 1, reason=reason)
            state.fast_streak = 0
        elif state.baseline is not None and latency_s <= state.baseline:
            state.fast_streak += 1
        else:
            state.fast_streak = 0

        if state.ramp_up_enabled and state.fast_streak >= RAMP_UP_STREAK:
            if self.target < state.ceiling:
                self.set_target(
                    self.target + 1,
                    reason=f"{RAMP_UP_STREAK} consecutive fast successes",
                )
            state.fast_streak = 0

    def _on_call_failure(self, state: _AdaptiveState, exc: BaseException) -> None:
        state.fast_streak = 0
        reason = degrade_reason(error=exc, median_recent=None, baseline=state.baseline)
        if reason:
            self.set_target(self.target - 1, reason=reason)
