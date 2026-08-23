# T05 — Adaptive concurrency pool

**Depends on:** T01. **Blocks:** T09.
**Read:** `BUILD-PROMPT.md` §8 (lines 768–865). **Nothing else.**

## Why this ticket exists

The degrade policy is what lets this app run against a rate-limited free tier without
either hammering it or stalling. The parking-worker pattern is the part builders usually
get wrong — the instinct is to cancel tasks to shrink a pool, which kills in-flight work.

## Files you own

`pool.py`.

## Files you must NOT touch

Everything else. You do not own the HTTP call — you accept a coroutine to run per job.
Keep this module transport-agnostic so it can be tested without any TTS at all.

## Task

**1. Ordered results.** N worker coroutines (`asyncio`) pull from one ordered queue. Each
worker writes its result keyed by the job's **chunk index**, never by arrival order.
**Completion order must never affect output order** — a slow chunk 12 finishing after
chunk 40 still lands at position 12.

**2. Shrinking without cancelling — the parking pattern.** Size the pool at the ceiling
(3). Give each worker an index `0..N-1` and an `asyncio.Event`. Before pulling its next
job a worker checks whether its index `< target_concurrency`; if not, it `await`s its park
event. Lowering the target simply stops setting events for retired indices — a worker
already mid-request **finishes that job** and only parks on its next iteration. Raising
the target sets the events for newly-eligible indices.

```python
class AdaptivePool:
    def __init__(self, max_workers: int, target: int = 3):
        self.target = target
        self.events = [asyncio.Event() for _ in range(max_workers)]
        self._sync_events()

    def set_target(self, n: int):
        self.target = max(1, n)   # floor is 1
        self._sync_events()

    async def worker(self, idx: int, queue):
        while True:
            await self.events[idx].wait()      # park here if idx >= target
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await run_job(job)                 # may call set_target() on failure
            queue.task_done()
```

**3. Baseline.** The median latency of the **first 5 successful calls** in the run.
Compute once and **freeze it**. Every later decision compares against this frozen
baseline, not a rolling one.

**4. Degrade** one step (3→2→1) when **either**:
- the median of the last 5 completed latencies exceeds `baseline × 1.75`, **or**
- any call returns 429, a 5xx, or times out.

A **429 degrades immediately**, regardless of the latency median. **The floor is 1** —
never 0. A stuck run is worse than a slow one.

**5. Recovery is opt-in** via `--ramp-up`, **default off**. When enabled: after 10
consecutive fast successes (at or under baseline, no errors), step up one, capped at the
starting value of 3. Without the flag a degraded run **stays** degraded — that is
deliberate, not a missing feature. A run that already hit trouble should stay conservative
rather than oscillate.

**6. Retries.** 3 attempts per chunk, backoff **2 s → 4 s → 8 s**. If the response carries
`Retry-After`, sleep that instead of the computed backoff. Add a short courtesy delay
between calls so a free tier is not hammered. Per-request timeout **180 s**.

**7. A failed chunk never kills the run.** A chunk exhausting all 3 attempts is recorded
as **failed** in the manifest — not written as silence, not aborting the run. The run
continues, and all failures are collected and printed in a summary at the end. A 400-chunk
book must not die because chunk 214 hit a transient 502.

**8. Log every state change** as exactly one line, via the T01 event emitter
(`concurrency_changed` with `from`, `to`, `reason`), plus a human line on stderr:

```
concurrency 3 → 2 (median 8.4s vs baseline 3.9s)
concurrency 3 → 2 (429 received)
```

## Invariants at risk in this ticket

- **#12** — a failed chunk never kills the run.
- **#9** — nothing in a retry log or error path may echo the API key.

## Definition of done

```bash
pytest tests/test_pool.py -v
```

All tests use a **fake job coroutine** with scripted latencies and failures. No network, no
TTS, no credit.

Ship at least these tests:

1. **The acceptance test from §13.4**: a forced 429 drops concurrency to 1 and the run
   **still finishes with every chunk accounted for**.
2. Results come back in **chunk-index order** even when the fake jobs complete out of
   order (make job 12 slower than job 40 and assert position).
3. Lowering the target does **not** cancel an in-flight job — assert the job that was
   running when `set_target` was called still completes.
4. Baseline is frozen: after 5 successes, later slow calls do not move the baseline.
5. A latency median above `baseline × 1.75` degrades exactly one step, not to the floor.
6. A 429 degrades immediately even when latencies are fast.
7. The floor holds at 1 — repeated failures never reach 0.
8. `--ramp-up` off: a degraded run stays degraded after 20 fast successes.
9. `--ramp-up` on: steps back up after 10 consecutive fast successes, capped at 3.
10. Backoff is 2/4/8 s, and a `Retry-After: 30` header overrides it.
11. A chunk that exhausts 3 attempts is recorded failed, the remaining chunks still run,
    and the failure appears in the end-of-run summary.

Use a fake clock or monkeypatched `asyncio.sleep` — the suite must run in seconds, not
sit through real 8-second backoffs.

## Report back

- The `AdaptivePool` implementation, verbatim.
- The exact degrade-decision function, since that is where the two-condition rule usually
  gets collapsed into one.
- Confirmation that test 3 (no cancellation of in-flight work) genuinely asserts
  completion, not just absence of an exception.
- The wall-clock runtime of the suite (should be seconds).
