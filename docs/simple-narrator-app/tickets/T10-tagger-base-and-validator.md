# T10 — Tagger base contract + validator

**Depends on:** T01. **Blocks:** T11, T12.
**Read:** `BUILD-PROMPT.md` §6 intro, §6.1, §6.4, §6.5 (lines 526–559 and 643–693).
Skip §6.2 and §6.3 — those are the adapter tickets. **Nothing else.**

## Why this ticket exists

The tag feature is the only expressive lever in a narrator-only pipeline — and it is also
the feature most able to corrupt the output, because a bad tag doesn't fail, it gets
**read aloud**. The validator is the entire safety mechanism. It lives here, once, shared
by both adapters, so a future third backend cannot bypass it.

**This ticket is on the critical path.** Tagging is **on by default**, so on a normal run
model-authored text reaches the TTS input. This validator is the only thing standing
between that and the narrator reading stage directions aloud. Build it first, build it
strictly, and do not soften it for convenience later.

## Files you own

`tagger/base.py`.

## Files you must NOT touch

`tagger/claude.py` and `tagger/codex.py` (T11, T12). Do not write any API client here —
this module must import **no** provider SDK and must be testable with no network and no
optional dependency installed.

## Task

**1. The shared contract.** Both backends implement exactly this:

```python
def tag(batch: list[Chunk]) -> dict[str, str]:
    """Returns {chunk_id: tag_string} for chunks the backend successfully tagged.
    Chunks it failed or declined to tag are simply absent from the returned dict."""
```

Both receive the same system guide, and both are asked for the same JSON shape:

```json
{"items": [{"chunk_id": "ch07_0012", "tag": "weary"}]}
```

Define the system guide, the vocabulary, and the JSON schema **here**, so both adapters
consume identical inputs. Everything backend-specific (client, auth, model id, retries,
typed exceptions) stays in the adapter. **A user who only has Codex must lose zero
functionality** — that is a requirement, not an aspiration, and it is why the shared parts
live in this file.

**2. The validator — this is the ticket.** Every tag from every backend passes through one
shared function before it is accepted:

- **≤ 32 characters.** Deliberately well under the ~64-char threshold where speech-leaking
  becomes likely. Short tags are what the model absorbs as *direction* rather than *text*.
- Must match `^[a-z][a-z ,-]*$` — lowercase words only, spaces/commas/hyphens as
  separators, no other punctuation, never a sentence.
- **Rejected if it contains words lifted from the chunk's own text.** This catches the
  model paraphrasing the line instead of directing its delivery (tagging a rainy scene
  `"raining hard"` because "rain" appears in the source).
- Checked against a shipped vocabulary of ~30 entries. Ship at minimum: `weary`, `urgent`,
  `whispered`, `bitter amusement`, `cold`, `grieving`, `wry`, `awed`, `flat`, `mocking`,
  `tender`, `resigned`, `furious`, `pleading`, `hushed`, `defiant`, `bewildered`,
  `wistful`, `stern`, `playful`, `hesitant`, `triumphant`, `bleak`, `warm`, `sardonic`,
  `anxious`, `reverent`, `exhausted`, `menacing`, `gentle`. Reject anything not on the
  list.
- **A failing tag is dropped and logged; the chunk generates untagged.** **Never truncate
  a bad tag into something that merely looks valid** — truncation turns a rejected
  36-character phrase into a 32-character prefix that passes the length check while still
  being nonsense, or still being a fragment of leaked direction. Reject outright; do not
  repair.

**3. Wiring helpers.** A validated tag is applied at generate time as exactly
`f"[{tag}] {text}"` and nothing more. Never expand a tag into a persona description, never
build a sentence of direction around it. The bracketed word is the entire instruction.

Because a tag changes what is sent to the API, it must change the chunk's identity:
`text_hash = compute_text_hash(text, tag)` using T01's helper. Re-tagging then regenerates
**exactly** the chunks whose tag changed and nothing else.

**4. The backend registry — build it here so the adapters never share a file.** Expose a
name→loader registry in this module (`{"claude": ..., "codex": ...}`) that imports the
adapter **lazily, by name, at call time**. T11 and T12 run in the same wave; if either had
to edit a shared registration file, they would collide. With the registry here, each
adapter is a standalone file that nothing else has to be edited to add.

Also expose the `auto` resolution helper (T09 calls it): `ANTHROPIC_API_KEY` → `"claude"`;
else `OPENAI_API_KEY` + `OPENAI_TAG_MODEL` → `"codex"`; else `None`. Keeping this beside
the registry means one place decides which backend runs.

**5. The review file.** The `tag` stage writes `tags.json` — an array of
`{chunk_id, text_preview, tag}` — for hand editing before any TTS spend. Provide the
read/write helpers here. `--tags-review` (wired in T09) stops the pipeline after this file
is written.

## Invariants at risk in this ticket

- **#3** — markers ≤ 32 chars, validated, dropped not truncated. **This ticket is
  invariant 3.**
- **#4** — the tag must reach `compute_text_hash`.
- **#10** — this module imports no provider SDK at all, so it must work with neither
  `anthropic` nor `openai` installed.

## Definition of done

```bash
pytest tests/test_tagger_base.py -v
```

No network, and the suite must pass in an environment where **neither** `anthropic` nor
`openai` is installed. Assert that explicitly if you can.

Ship at least these tests:

1. **The acceptance test from §13.7**: an over-long, sentence-shaped tag (e.g. `"he says
   this wearily, with a long pause before the last word"`) is **rejected, not truncated**.
   Assert the returned tag is absent, the chunk generates untagged, and — critically —
   that no 32-char prefix of the input appears anywhere in the output.
2. A 33-character otherwise-valid tag is rejected.
3. A 32-character valid tag is accepted (boundary).
4. Tags with uppercase, digits, or `[`/`]`/`.`/`!` are rejected.
5. A tag echoing a distinctive word from the chunk text is rejected.
6. A tag not in the vocabulary is rejected even if it matches the regex.
7. Every one of the ~30 shipped vocabulary entries passes its own validator — a shipped
   list that fails its own rules is a real and easy bug.
8. `apply_tag(text, "weary") == "[weary] text"` exactly — no extra spacing, no wrapper.
9. `compute_text_hash` differs between tagged and untagged for the same text.
10. Every rejection is logged with the chunk id and the reason.
11. `tags.json` round-trips.
12. The registry resolves `"claude"` and `"codex"` **without importing either SDK** until
    the loader is actually called.
13. `auto` resolution returns `"claude"` with only an Anthropic key, `"codex"` with only
    OpenAI key + model, `"claude"` when both are set, and `None` when neither is.

## Report back

- The validator implementation, verbatim.
- The final vocabulary list.
- Confirmation of test 1's stronger assertion — that no truncated prefix leaks — since
  "rejected" and "not truncated" are two different properties and only the second one
  protects the audio.
- Confirmation the module imports no provider SDK.
