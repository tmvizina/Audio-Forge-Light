# T14 — Acceptance gate

**Depends on:** T09, T13. **Blocks:** T15.
**Read:** `BUILD-PROMPT.md` §13 (lines 1099–1163) and §14 (lines 1164–1178). Also read the
**Before you report done** checklist (lines 1179–1185). **Nothing else.**

## Why this ticket exists

Every prior ticket shipped its own unit tests. This ticket verifies that the **assembled
system** satisfies the eight acceptance tests from the spec, end to end, and that nothing
was quietly dropped at an integration seam.

This is the real gate. A build that reaches here with a failing test is not done, and the
correct response is to send the owning ticket back — **not** to patch around it here.

## Files you own

`tests/test_acceptance.py` (integration-level only), and `README.md`'s troubleshooting
table.

## Files you must NOT touch

Any implementation module. If an acceptance test fails, **report which ticket owns the
defect**. Fixing it here would hide the drift the ticket structure exists to surface.

## Task

**1. Verify all eight acceptance tests exist and pass at the integration level.** Several
were written as unit tests by their owning ticket; your job is to confirm each holds
against the **assembled pipeline**, not just its module in isolation.

| # | Test | Owning ticket |
|---|---|---|
| 1 | Chapter-boundary fixture → `ch00`, `ch01`, `ch02`, `ch07_5` with title chunks | T07 |
| 2 | 250-word fixture at 200 chars → **no sentence split** (incl. `U.S.`, `"Stop!" she cried.`) | T02 |
| 3 | Pure-punctuation chunk → silence, **no API call** | T03 |
| 4 | Forced 429 → concurrency drops to 1, run still finishes | T05 |
| 5 | Stitched duration = `Σ chunks + gaps ± 100 ms` | T04 |
| 6 | Resumed run regenerates **nothing** | T09 |
| 7 | Over-long sentence-shaped tag → **rejected, not truncated** | T10 |
| 8 | Tagger parity: claude and codex → valid `tags.json`, identical downstream | T12 |

**2. Add the integration tests no single ticket owns:**

9. **Full pipeline, no server**: `python narrate.py run` on a two-chapter fixture, against
   a faked TTS client, produces two playable mp3s with correct chapter naming.
10. **Full pipeline, server only**: the same job driven entirely through the Node UI
    produces a byte-identical result. Neither path is privileged.
11. **The gap lever is free**: re-stitch the same generated chunks at 700, 900, and
    1200 ms; assert three different durations and **zero** TTS calls across all three.
    This is the property the whole architecture is arranged around — verify it explicitly.
12. **Key hygiene**: grep the entire NDJSON stream, every file under `out/`, and all
    committed files for the test API key values. Zero hits.
13. **`.gitignore` check**: `.env` is ignored, and `git log --all -- .env` is empty — it
    never entered history.
14. **Cold install**: in a clean venv with only `requirements.txt` installed (**no**
    `anthropic`, **no** `openai`) **and no LLM keys set**, `chunk`, `generate`, `stitch`,
    and a bare `run` all work — `auto` resolves to untagged and the run completes. This
    proves both the lazy-import rule and the degrade-never-fail rule.

15. **Tagging default behaviour** (§13.9–13.11): `auto` resolves correctly in all four key
    environments; the recommendation lands on **stderr** and never on stdout; `--tagger
    none` silences it; an explicit `--tagger claude`/`codex` with a missing key **fails**
    rather than degrading.

16. **Cost disclosure**: a default run with an LLM key present states on stderr which
    backend it resolved to and that it bills that account. Tagging on by default must never
    be silent about spending money.

**3. Verify the "Before you report done" checklist** from the spec, item by item, and
report the result of each.

**4. Write the §14 troubleshooting table into `README.md`** — symptom | cause | fix —
covering at minimum: HTTP 401 / 402 / 429; empty response body; `ffmpeg not found`; robotic
or wrong voice (reference too long / noisy / transcript mismatch); "the narrator read a
stage direction aloud" (a tag escaped validation); `OPENAI_TAG_MODEL` unset (deliberate, not
a bug); "command line is too long" (someone reverted the concat demuxer); a resumed run
regenerating everything (the tag changed, so the hash changed — correct behaviour).

## Test discipline

**No test may spend API credit or hit the network.** Every backend is faked at the HTTP
layer. Real ffmpeg is used and is a declared dependency. The suite should run in well under
a minute — use a fake clock for backoffs.

## Definition of done

```bash
pytest tests/ -v
```

Everything green, including every prior ticket's unit tests. Plus a manual confirmation
that the browser path (test 10) works.

## Report back

A table of all 16 tests with pass/fail and, for each of the original eight, whether it was
verified at unit level, integration level, or both.

Then, explicitly:

- **Any test that fails, and which ticket owns the defect.** Do not fix it here.
- The measured duration error from test 5.
- The three durations from test 11 and confirmation of zero TTS calls.
- The result of the cold-install test 14, since that is the one most likely to have quietly
  broken.
- The "Before you report done" checklist, item by item.
- Total suite runtime.
