# T11 — Claude tagger adapter

**Depends on:** T10. **Blocks:** T09.
**Read:** `BUILD-PROMPT.md` §6.2 (lines 600–656). **Nothing else.**

## Why this ticket exists

This adapter has a high density of details that a training prior gets **actively wrong** —
not vague, wrong. Sampling parameters that used to be universal now return HTTP 400.
Refusals arrive as HTTP 200. If you write this from recall instead of from the spec, it
will fail at runtime in ways that look like network problems.

**This adapter is on the default path.** Tagging ships **on** (`--tagger auto`), and a
user with an `ANTHROPIC_API_KEY` set gets this adapter without asking for it. Correctness
and cost-transparency here are not optional polish.

## Files you own

`tagger/claude.py`.

## Files you must NOT touch

`tagger/base.py` (T10 owns the contract, the vocabulary, and **the validator** — reuse
them, never reimplement) and `tagger/codex.py` (T12).

## Task

Implement the T10 contract: `tag(batch: list[Chunk]) -> dict[str, str]`.

**1. Client + model.** `import anthropic` **inside the function, never at module scope** —
a user who installed neither optional SDK must still be able to run the whole pipeline.
`anthropic.Anthropic()` reads `ANTHROPIC_API_KEY` from the environment on its own; do not
thread the key through by hand.

Model id **`claude-opus-5`**. Expose `--tag-model` to override. `claude-haiku-4-5` is the
cheap alternative — name it in `--help` and the README, but leave the choice to the
**user**; do not pick a cheaper default unasked.

**2. Do not send sampling parameters.** `temperature`, `top_p`, and `budget_tokens` were
**removed** on this model generation and **each returns HTTP 400**. Do not set them, do not
expose CLI flags for them, do not "restore" them if you see them in similar code elsewhere.
This is the single most likely mistake to carry in from older code or training data.

Effort control instead: `output_config={"effort": "low"}`. This is an annotation task —
short output, cheap, no deep reasoning needed. If tags come back generic or repetitive in
testing, raise to `"medium"`. Keep it configurable; ship `"low"`.

**3. Structured output, not prose scraping.**
`client.messages.parse(..., output_format=TagBatch)`, reading `response.parsed_output`.

```python
from pydantic import BaseModel

class TagItem(BaseModel):
    chunk_id: str
    tag: str

class TagBatch(BaseModel):
    items: list[TagItem]
```

Do not attempt to pull JSON out of free-text content. `parsed_output` is the contract.

**4. Prompt caching.** The style guide, tag vocabulary, and book bible are stable for the
whole run. Mark that system block `cache_control={"type": "ephemeral"}` and keep the
per-batch chunks in the **user** turn, *after* the breakpoint.

- Caching needs **≥ 1024 tokens** to engage at all — a short system prompt below that floor
  will not cache no matter how it is marked. Make sure the shared guide clears it.
- Verify it works: check `usage.cache_read_input_tokens` across consecutive batches. If it
  stays **zero**, the diagnosis is that something **volatile leaked into the cached
  prefix** — an embedded timestamp, an unsorted dict whose key order varies, a per-call
  random id. Find and remove the volatile element rather than accepting the miss.

**5. Refusals are HTTP 200, not exceptions.** Check `response.stop_reason == "refusal"`
**before** touching `parsed_output` or any content. A novel with battle or violence can
legitimately trip a policy decline. A refused batch behaves like a failed batch: empty
dict, logged, chunks proceed **untagged**. Do not let it crash on an unexpected content
shape.

**6. Server-side fallback**, so a transient overload doesn't hard-fail a batch:
`betas=["server-side-fallback-2026-07-01"]` and `fallbacks="default"` on the
`client.beta.messages.*` path.

**7. Typed exceptions, most-specific first:** `RateLimitError`, then `APIStatusError`, then
`APIConnectionError`, then a broad `except Exception` last. Retry per T10's policy before
giving up on the batch.

**8. Failure contract.** On any failure the batch yields an **empty tag dict**, the failure
is logged, and generation proceeds untagged. **Tagging is enhancement and must never block
the pipeline.**

**9. Every returned tag goes through T10's validator.** No exceptions, no adapter-local
validation, no "this one looks fine".

## Invariants at risk in this ticket

- **#3** — validation is T10's, and this adapter must not bypass or duplicate it.
- **#9** — the API key must never reach a log, an error message, or an NDJSON event.
- **#10** — `import anthropic` is lazy, inside the function.

## Definition of done

```bash
pytest tests/test_tagger_claude.py -v
```

Fake at the HTTP/SDK layer. **No test may spend credit or hit the network.**

Ship at least these tests:

1. The request payload contains **no** `temperature`, **no** `top_p`, **no**
   `budget_tokens`. Assert their absence explicitly — this is the regression that matters
   most.
2. The request carries `output_config={"effort": "low"}`.
3. A response with `stop_reason == "refusal"` returns an empty dict, logs, and does **not**
   raise or read content.
4. A malformed/unparseable response returns an empty dict and the pipeline continues.
5. `RateLimitError` is retried per policy, then yields an empty dict.
6. Tags returned by the fake are passed through T10's validator — feed it an invalid tag
   and assert it is dropped, proving the adapter does not bypass validation.
7. `import anthropic` does **not** happen at module import time — assert
   `import tagger.claude` succeeds with `anthropic` absent from `sys.modules` (or
   monkeypatched to raise on import at module scope).
8. The API key never appears in any logged line or exception message.
9. The system block carries `cache_control` and the per-batch chunks are in the user turn.

## Report back

- The exact request kwargs dict passed to the SDK, verbatim — the orchestrator is checking
  for absent sampling params.
- Confirmation that test 1 asserts **absence**, not just correctness of other fields.
- Confirmation that test 7 genuinely proves the lazy import.
- Whether `usage.cache_read_input_tokens` was exercised, and how you faked it.
