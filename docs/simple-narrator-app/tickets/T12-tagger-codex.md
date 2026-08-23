# T12 — Codex / OpenAI tagger adapter

**Depends on:** T10. **Blocks:** T09.
**Read:** `BUILD-PROMPT.md` §6.3 (lines 617–642). **Nothing else.**

## Why this ticket exists

Full parity for a user who has an OpenAI account and no Anthropic one. This is not a
fallback or a stub — a Codex-only user must lose **zero** functionality.

**This adapter is on the default path.** Tagging ships **on** (`--tagger auto`); a user
with `OPENAI_API_KEY` + `OPENAI_TAG_MODEL` set and no Anthropic key gets this adapter
without asking for it.

This ticket also has an unusual instruction: one part of it is **deliberately not
specified**, and you are expected to go look it up rather than recall it.

## Files you own

`tagger/codex.py`.

## Files you must NOT touch

`tagger/base.py` (T10 owns the contract, vocabulary, and **validator** — reuse them, never
reimplement) and `tagger/claude.py` (T11).

## Task

Implement the T10 contract: `tag(batch: list[Chunk]) -> dict[str, str]`.

**1. Client.** `import openai` **inside the function, never at module scope**.
`openai.OpenAI()` reads `OPENAI_API_KEY` from the environment on its own.

**2. The model id is a required config value with NO default: `OPENAI_TAG_MODEL`.**

Do not invent a hardcoded fallback, **even a plausible-looking one**. The reasoning belongs
in the code comment and the README, because it is not obvious: OpenAI model ids churn, and
a stale hardcoded default does **not** fail loudly at build time. It fails months later, at
runtime, as a confusing HTTP 404 that looks like an auth or network problem rather than
what it actually is — a decommissioned model id.

If `OPENAI_TAG_MODEL` is unset, **fail immediately and clearly**: name the exact
environment variable in the error message, and point the user at `client.models.list()` as
the way to discover valid current ids for their account.

**3. ⚠️ Verify the call shape against live documentation before writing it.**

The exact structured-output / JSON-schema parameter name and call shape for the OpenAI SDK
were **deliberately not verified** for this project. Before writing this adapter, confirm
the current parameter name and call shape against OpenAI's live, current documentation. Do
not trust recall or training data for this one call.

This is the same discipline applied to the Fish `model` header, with the opposite
conclusion: that one was hand-verified and is safe to transcribe literally; this one was
not, and must be independently re-checked before it ships.

Request the shared JSON shape from T10:

```json
{"items": [{"chunk_id": "ch07_0012", "tag": "weary"}]}
```

**4. Mirror the Claude adapter's behaviour**: retries per T10's policy, typed error
handling most-specific-first with a broad fallback last, and the same failure contract —
**on any failure the batch yields an empty tag dict, the failure is logged, and generation
proceeds untagged.** Tagging is enhancement, never a blocker.

**5. Every returned tag goes through T10's validator.** Same rule as T11: no adapter-local
validation.

**6. Parity is a requirement, not a goal.** Every tag-stage feature — validation, the
`tags.json` review file, `--tags-review`, resumability via `text_hash` — must behave
identically regardless of which adapter produced the tags.

## Invariants at risk in this ticket

- **#3** — validation is T10's; do not bypass or duplicate.
- **#9** — the API key must never reach a log, error message, or NDJSON event.
- **#10** — `import openai` is lazy, inside the function.

## Definition of done

```bash
pytest tests/test_tagger_codex.py -v
```

Fake at the HTTP/SDK layer. **No test may spend credit or hit the network.**

Ship at least these tests:

1. **The acceptance test from §13.8 (parity)**: with the same fixture chapter and both
   adapters faked to return the same tags, `--tagger claude` and `--tagger codex` produce
   an identical `tags.json` and identical downstream behaviour (same `text_hash` values,
   same chunks marked for regeneration).
2. `OPENAI_TAG_MODEL` unset → fails immediately, and the error message contains the literal
   string `OPENAI_TAG_MODEL` and mentions `models.list`.
3. `OPENAI_TAG_MODEL` set → that exact id is used in the request; assert no other id
   appears anywhere in the module (grep your own source for a hardcoded `gpt-` string and
   assert none exists).
4. A malformed response returns an empty dict and the pipeline continues untagged.
5. A rate-limit error is retried per policy, then yields an empty dict.
6. An invalid tag from the fake is dropped by T10's validator, proving no bypass.
7. `import openai` does not happen at module import time.
8. The API key never appears in any logged line or exception message.

## Report back

- **What you verified against OpenAI's live docs, and the URL** — the orchestrator is
  specifically checking that this lookup happened rather than being recalled. State the
  parameter name and call shape you found.
- The exact request kwargs, verbatim.
- Confirmation that no hardcoded model id exists anywhere in the file (test 3).
- Confirmation that the parity test compares real downstream artifacts (`tags.json` and
  hashes), not just that both returned a dict.
