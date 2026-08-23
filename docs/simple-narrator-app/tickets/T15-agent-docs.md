# T15 — Agent docs

**Depends on:** T14. **Blocks:** nothing.
**Read:** the reference `AGENTS.md` and `CLAUDE.md` shipped alongside `BUILD-PROMPT.md`,
plus your own repo as it actually exists now.

## Why this ticket exists

`AGENTS.md` is what every future agent reads on clone, before touching anything. It must
describe **the repo that actually shipped**, not the repo the build prompt imagined. That is
why it is written last, after the acceptance gate — anything that changed during the build
has to be reflected here or the next agent will be working from fiction.

## Files you own

`AGENTS.md`, `CLAUDE.md`, and any corrections to `README.md`.

## Files you must NOT touch

Any implementation module. If you find a discrepancy between the docs and the code,
**document the code as it is** and report the discrepancy — do not change code to match a
document.

## Task

**1. `AGENTS.md`.** A reference version ships with this doc set. Start from it, then
**verify every claim against the real repo** and correct anything that drifted. It must
contain:

- **Repo map** — path | what it owns. Every file that actually exists.
- **Commands** — install, run everything, run one stage, run a chapter range, force a redo,
  re-stitch at a different gap, run the tests, start the UI. All literal and
  copy-pasteable, all verified to actually work.
- **The invariants** — the twelve listed below, each with the rule, one line of *why*, and
  what breaks if violated.
- **The Claude sampling-parameter warning** as its own block.
- **The Python↔Node NDJSON contract** — the frozen event table from T01, exactly as
  implemented.
- **Extension points** — how to add a TTS backend behind `fish_client`'s interface, and how
  to add a tagger backend behind `tagger/base.py`'s contract (reusing the shared validator,
  never writing a new one).
- **Running the tests** — the one command, and the rule that no test may spend API credit.
  List the acceptance tests by one-line name so an agent knows what coverage exists.
- **Common request → file to touch** — a table so an agent doesn't re-derive the layout
  every session.
- **Gotchas** — Windows paths via `pathlib`; explicit interpreter spawn from Node;
  unbuffered Python stdout or the SSE log stalls; ffprobe is separate from ffmpeg; the
  silence cache is keyed by gap length, so a stale `gap_*.wav` from a changed sample rate
  must be deleted.

**2. The twelve invariants**, verified against the shipped code:

1. Never split a sentence in the chunker.
2. Never call the TTS on a chunk with no alphanumerics.
3. Markers stay ≤ 32 chars and go through the validator — dropped, never truncated.
4. `text_hash` covers the applied tag.
5. Concat **demuxer** with a list file, never the filter (Windows 8191-char limit).
6. Gaps are applied only at stitch time.
7. The Fish model id goes in the `model` **header**, not the msgpack body.
8. Reference audio ships as raw bytes inside the msgpack map.
9. Keys never reach stdout, logs, or the NDJSON stream.
10. `anthropic` / `openai` are lazy imports inside their adapters.
11. No numpy, torch, or soundfile.
12. A failed chunk never kills the run.

**3. `CLAUDE.md`** — a short pointer to `AGENTS.md` and nothing more. One canonical agent
doc; the pointer exists so Claude Code reaches it and the two never drift apart.

**4. `README.md` corrections.** The README shipped with this doc set was written before the
build. Correct any command, flag, or path that changed. Everything in it must be literally
runnable against the shipped app.

**5. Verify the pacing claim appears in all three docs**: default 900 ms inter-chunk,
3000 ms title, 1200 ms upper end, 700 ms lower — and that **re-tuning costs one ffmpeg pass
and zero API calls**. T14's test 11 proves this is true of the code; make sure all three
docs say so.

## Definition of done

- Every command in `AGENTS.md` and `README.md` has been **executed** and works. This is a
  documentation ticket, so "it looks right" is not the bar — run them.
- Every invariant has been checked against the actual implementation, not assumed.
- `CLAUDE.md` points at `AGENTS.md` and contains no duplicated content.
- A fresh reader can go from clone to a generated chapter using `README.md` alone.

## Report back

- Every discrepancy you found between the pre-written docs and the shipped code, and how
  you resolved it. **This list is the most valuable output of this ticket** — it is the
  measured drift between what was specified and what was built.
- Confirmation that every documented command was actually run.
- Any invariant that the shipped code does **not** satisfy — report it, do not paper over
  it in prose.
