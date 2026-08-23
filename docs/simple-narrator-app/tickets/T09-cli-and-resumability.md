# T09 — CLI wiring + resumability

**Depends on:** T02–T08 **and T10–T12** (tagging is on by default). **Blocks:** T13, T14.
**Read:** `BUILD-PROMPT.md` §9 (lines 866–893), §12 precedence (lines 1075–1145), the
NDJSON contract in §11 (lines 1030–1074), and §6.0 auto-resolution (lines 544–575). **Nothing else** — the modules you are wiring
already exist and their contracts are frozen.

## Why this ticket exists

This is the integration point: the stage where six independently-built modules become one
pipeline. It is single-threaded by nature and it is where the resumability guarantee lives.

You are **wiring**, not reimplementing. If you find yourself writing a sentence splitter,
an ffmpeg command, or a retry loop, stop — that logic already exists in a module you
should be calling.

## Files you own

`narrate.py` (except the `prep-ref` subcommand, which T08 already added).

## Files you must NOT touch

Every module you are calling. If one has a bug, **report it** — do not patch it from here.
A fix made in `narrate.py` to work around a `chunker.py` bug is exactly the drift these
tickets exist to prevent.

## Task

**1. Subcommands.** `chunk`, `tag`, `generate`, `stitch`, `run` (plus T08's `prep-ref`).
`run` executes the full pipeline: chunk → tag (on by default) → generate → stitch. Each stage is
independently invocable and each reads the previous stage's on-disk artifacts, so a user
can stop and resume anywhere.

**2. Config precedence — state it and honour it:**

```
CLI flag  >  .env  >  config.json  >  built-in default
```

**3. Resumability — the core of this ticket.** `out/<book>/<chNN>/manifest.json` records
per chunk: `chunk_id`, `text_hash`, and the wav path.

Before dispatching a chunk to the pool, check: **does the wav exist on disk AND does the
manifest's recorded `text_hash` match the hash of the chunk's current (possibly re-tagged)
text?** If both hold, **skip it** — no API call, no pool slot consumed. `--force` bypasses
the check entirely.

This is a reliability requirement, not an optimization: a 400-chunk run must never restart
from zero because of one network blip, one rate-limit exhaustion, or the user closing the
terminal.

Because `text_hash` covers the **applied tag** (T01's helper), changing a chunk's tag — or
turning tagging on/off — correctly invalidates that chunk and forces regeneration. A
re-tagged chunk must never silently keep serving audio generated under the old tag. Use
T01's `compute_text_hash`; do not compute a hash locally.

**4. Write the manifest incrementally.** Persist after **each individual chunk** completes
(success or terminal failure), not once at the end of the chapter. A crash, kill, or power
loss partway through must lose at most one chunk of work.

**5. Emit the NDJSON events** from T01's `events.py` at the right points: `run_started`,
`chunked`, `tagged`, `chunk_done` (fresh **or resumed** — the UI needs both), `chunk_failed`,
`concurrency_changed`, `stitched`, `done`, `error`. Human logging goes to **stderr**.

**6. Useful flags:** `--chapters ch01-ch03` (range or list), `--force`, `--normalize`,
`--single-file`, `--ramp-up`, `--gap-ms`, `--tagger`, `--tag-model`, `--tags-review`.

`--tags-review` stops the pipeline after `tags.json` is written, **before any TTS spend**.

**7. Failure summary.** Collect every failed chunk and print a summary at the end. A failed
chunk never kills the run.

**8. Preflight** (T01) runs before any command that will shell out, so a missing ffmpeg
surfaces immediately rather than three steps into a long run.

**9. `--tagger` defaults to `auto` — implement the resolution, not just the flag.**
Tagging is **on by default**. `auto` resolves in this order:

1. `ANTHROPIC_API_KEY` set → claude adapter
2. else `OPENAI_API_KEY` **and** `OPENAI_TAG_MODEL` set → codex adapter
3. else → run **untagged**, and print the recommendation to **stderr**

Three behaviours that are easy to get subtly wrong:

- **`auto` degrades, it never fails.** A user with only a Fish key still gets their
  audiobook. Do not turn a missing LLM key into an error on this path.
- **`--tagger claude` / `--tagger codex` with a missing key FAILS loudly**, naming the
  variable. An explicit choice that cannot be honoured is an error, not a downgrade.
- **`--tagger none` silences the recommendation.** A user who has opted out is not nagged.

The recommendation text goes to **stderr only** — putting it on stdout corrupts the NDJSON
stream. Emit the resolved backend in `run_started`, and state on stderr that tagging bills
a second account.

If you are deferring tickets 10–12, hard-wire `--tagger none` but still route text through
`compute_text_hash` with a `None` tag, so adding tags later doesn't silently serve stale
audio.

## Invariants at risk in this ticket

- **#4** — `text_hash` covers the tag. **This ticket is where invariant 4 is enforced.**
- **#12** — a failed chunk never kills the run.
- **#9** — `run_started` carries `config`; that payload must be the **sanitised** config,
  never `.env` contents.
- **#2** — the unspeakable guard lives in `fish_client` (T03); make sure the skip path
  still records the chunk and its silent wav in the manifest, so the stitch order stays
  intact.

## Definition of done

```bash
pytest tests/test_cli.py -v
python narrate.py run --book tests/fixtures/two_chapters.txt --tagger none
```

The second command must complete end-to-end against a **faked** TTS client with **no Node
server running**. No test may spend credit.

Ship at least these tests:

1. **The acceptance test from §13.6**: run a fixture book twice; the second run makes
   **zero** API calls. Assert on the fake client's call count.
2. Changing one chunk's text regenerates **exactly that chunk** and no others.
3. Changing one chunk's **tag** regenerates exactly that chunk (invariant 4 end-to-end).
4. `--force` regenerates everything.
5. The manifest is written incrementally: kill the run after chunk 3 (raise from the fake
   client), then assert the manifest on disk already records chunks 1–3.
6. A resumed run emits `chunk_done` for skipped chunks too, so the UI progress bar is
   correct.
7. Config precedence: a CLI flag beats `.env` beats `config.json` beats the default —
   assert all three overrides.
8. `--tags-review` stops before any TTS call (fake client call count zero).
9. A failed chunk does not abort the run, and appears in the end-of-run summary.
10. `run_started`'s `config` payload contains **no** key material.
11. Every NDJSON line on stdout parses as JSON; human logs appear only on stderr.
12. `--chapters ch01-ch03` processes exactly those three.
13. **`auto` resolution**, four cases: Anthropic key only → claude; OpenAI key +
    `OPENAI_TAG_MODEL` only → codex; both → claude; neither → untagged, run still
    completes, recommendation on **stderr** and **not** on stdout.
14. `--tagger none` in a key-less environment completes untagged with **no** recommendation.
15. `--tagger claude` with no `ANTHROPIC_API_KEY` raises an error naming the variable —
    it does **not** fall back to untagged. Same for `--tagger codex`.

## Report back

- The resumability check, verbatim — the orchestrator is verifying it tests **both** wav
  existence and hash match, not just existence.
- Confirmation that `compute_text_hash` is imported from T01 and not reimplemented.
- The end-to-end run output for the two-chapter fixture (the NDJSON stream).
- The resolved-backend logic, verbatim, and confirmation that the recommendation goes to
  stderr only.
- Any module bug you found and **reported rather than patched**.
