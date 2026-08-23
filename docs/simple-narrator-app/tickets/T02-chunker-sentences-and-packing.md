# T02 — Chunker: sentence splitting + greedy packing

**Depends on:** T01. **Blocks:** T06, T07.
**Read:** `BUILD-PROMPT.md` §3 intro and §3.1–3.2 (lines 110–212). **Nothing else.**

## Why this ticket exists

This is the highest-risk algorithm in the app and the one most likely to be "simplified"
into a bug. A naive splitter looks like it works on a test paragraph and then shatters
`U.S.` and `"Stop!" she cried.` across a whole novel — which is only audible after you've
paid to generate the audio.

Transcribe the algorithm as specified. Do not improvise a cleaner-looking one.

## Files you own

`chunker.py` — sentence splitting and packing only.

## Files you must NOT touch

Everything else. In particular **do not** implement section/paragraph boundary handling,
the small-chunk merge, or chapter detection — those are T06 and T07 and they will edit
this same file after you. Leave the file structured so they can extend it cleanly.

## Task

**1. `split_sentences(text: str) -> list[str]`.** Use exactly these constants:

```python
_SENT_END = re.compile(r"[.!?]+[\"'”’\)\]]*")
_OPENERS = "\"'“‘([“‘"
```

The walk, exactly as specified:

- Iterate `_SENT_END.finditer(text)`. For each match ending at `end`:
- `rest = text[end:]`
- If `rest` is non-empty and `rest[0]` is **not** whitespace → `continue`. This is what
  keeps `U.S.` and `3.5` intact.
- `nxt = rest.lstrip()`
- If `nxt == ""` **or** `nxt[0].isupper()` **or** `nxt[0] in _OPENERS` → this **is** a
  boundary: emit `text[start:end].strip()` and set `start = end`.
- Otherwise it is not a boundary. This is what keeps `"Stop!" she cried.` as one sentence.
- After the loop, emit the remaining tail if non-empty.

**No characters may ever be dropped.** The concatenation of the returned sentences, with
whitespace normalised, must equal the input with whitespace normalised. Assert this in a
test.

**2. `pack(sentences, target_chars=200, max_chars=300) -> list[str]`.** The greedy loop,
exactly as specified:

```python
buf, bc = [], 0
for sent in sentences:
    add_chars = len(sent) + (1 if buf else 0)   # +1 for the joining space
    if buf and (bc + add_chars > max_chars):    # HARD cap: flush FIRST
        flush(); add_chars = len(sent)
    buf.append(sent); bc += add_chars
    if bc >= target_chars:                      # SOFT target: close now
        flush()
flush()
```

Chunks join with a single space. The two-limit structure is the point: the hard cap
flushes *before* appending so a chunk never exceeds `max_chars`, and the soft target
closes as soon as the chunk is big enough, so sizes cluster near 200 rather than piling up
at 300.

**3. Over-cap single sentences.** A lone sentence longer than `max_chars` becomes its own
chunk and is **flagged** (the `over_cap` field from T01), not split. Only past
`hard_split_chars = 600` do you split it at all — and then at the **nearest clause
punctuation** (comma, semicolon, or colon nearest the midpoint), never at an arbitrary
character offset.

## Invariants at risk in this ticket

- **#1 — never split a sentence.** This ticket is invariant 1. Everything else here is
  detail.

## Definition of done

```bash
pytest tests/test_chunker.py -v
```

Ship at least these tests:

1. **The acceptance test from §13.2**: a 250-word fixture packed at `target_chars=200`,
   where reassembling the chunks reproduces the original sentence list exactly — no
   sentence appears split across two chunks. The fixture **must** contain `U.S.` and
   `"Stop!" she cried.`
2. A decimal (`3.5`) does not split.
3. An abbreviation mid-sentence (`the U.S. government said`) does not split.
4. Lowercase continuation after a quote (`"Stop!" she cried.`) is one sentence.
5. No chunk exceeds `max_chars`.
6. Chunk sizes cluster near `target_chars` — assert the median is closer to 200 than 300,
   which is what proves the soft target is actually firing.
7. A 400-char single sentence becomes one chunk with `over_cap=True` and is not split.
8. A 700-char single sentence splits at clause punctuation, not mid-word.
9. Round-trip: no characters dropped by `split_sentences`.

## Report back

- Your `split_sentences` and `pack` implementations, verbatim.
- The measured chunk-size distribution on your 250-word fixture (min / median / max).
- Confirmation that tests 1–4 pass, since those four are the ones that catch the
  classic wrong implementation.
- Anything you left as a hook for T06/T07 and where.
