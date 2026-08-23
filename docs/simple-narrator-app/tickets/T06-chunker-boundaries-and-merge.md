# T06 — Chunker: boundaries, merge, manifest fields

**Depends on:** T02. **Blocks:** T07.
**Read:** `BUILD-PROMPT.md` §3.3–3.6 (lines 213–276). **Nothing else.**

## Why this ticket exists

T02 packs sentences correctly but has no idea what a scene break is. This ticket adds the
structural rules — which boundaries are hard, which are merely preferred — plus the
fixed-point merge that clears orphan fragments, and it populates the manifest fields
everything downstream reads.

**File conflict:** you are editing `chunker.py`, which T02 wrote. Do not run alongside T02
or T07.

## Files you own

`chunker.py` (continuing T02's work).

## Files you must NOT touch

Everything else. Chapter detection is T07 — do not start it.

## Task

**1. Packable units — hard boundaries, never packed across.** Before sentence splitting,
segment the raw chapter text into packable units at every:

- section-break marker line: a line that is only `***`, `---`, or similar
  (`^\s*[\*\-—]{3,}\s*$`)
- Markdown heading line (`^\s*#{1,6}\s`)
- run of 2+ consecutive blank lines (a soft section break, same rule)
- chapter boundary (T07 handles the detection; a chapter's chunks never merge into the
  next chapter's)

A chunk may never span two packable units. **Hard rule, no exceptions** — a narration that
runs a scene break straight into the next scene without a pause reads as a continuity
error, not a stylistic quirk.

**2. Ordinary paragraph breaks are different — packing MAY cross them.** Do not treat every
blank line as a hard boundary. At a 200-char target, refusing to pack across paragraph
breaks would shatter short-paragraph dialogue (one line per paragraph is a common pattern)
into a flood of tiny chunks, each incurring its own gap and its own API call.

The rule: packing continues across a single paragraph break, **but once a chunk has reached
`min_chars`, prefer to close it at the next paragraph boundary** rather than packing
further. A paragraph boundary is a preferred-but-not-mandatory close point once the chunk
is already big enough to stand alone.

**3. Small-chunk merge — fixed point, not one pass.** Sweep for chunks under `min_chars`
(60) and fold each into an adjacent chunk:

- Merge into whichever **neighbour is smaller** — this keeps the resulting sizes more even
  than always taking the left neighbour.
- Only if the result still fits: `fits(a, b) = len(a) + 1 + len(b) <= max_chars`.
- **Run to a fixed point:** `while changed and len(out) > 1:`, rescanning from the top
  after every merge, until a full pass produces no merges. One pass is not enough —
  merging chunk 5 into chunk 4 can make chunk 4 itself newly eligible to merge into 3.
- Never merge a `kind="title"` chunk with an adjacent body chunk (T07 creates those;
  respect the field now).

Why it matters: without this, orphan fragments — a two-word chapter tail, a short
interjection stranded before a long sentence, a solitary "Yes." left dangling by a
section-break split — render as isolated blips of near-silence or click artifacts at the
TTS layer instead of reading naturally.

**4. Populate the manifest fields.** Every chunk record must carry exactly the T01 `Chunk`
fields. Two need real work here:

- `boundary` — `"ends_section"` / `"ends_paragraph"` / `"mid_paragraph"`, recording **why
  this specific chunk ended where it did**. Populate it correctly even though the stitcher
  currently applies one flat gap everywhere. This is the hook a future
  `--mid-paragraph-gap-ms` reads, and discarding it now means re-chunking the whole book
  later to get it back.
- `over_cap` — true only for the flagged single-sentence-over-`max_chars` case from T02.

`kind` is `"body"` for everything this ticket produces; T07 adds the one `"title"` chunk
per chapter.

## Invariants at risk in this ticket

- **#1** — the merge must never join across a hard boundary, which would effectively
  reintroduce a sentence-level error at the structural level.
- **#6** — `boundary` must be populated now, or gap refinement later requires regenerating
  audio, which invariant 6 exists to prevent.

## Definition of done

```bash
pytest tests/test_chunker.py -v
```

T02's tests must still pass — run the whole file, not just yours.

Ship at least these tests:

1. A `***` divider is never packed across: assert no chunk contains text from both sides.
2. A `---` divider and a Markdown `## heading` behave the same way.
3. Two consecutive blank lines act as a hard boundary; a single blank line does **not**.
4. Short-paragraph dialogue (six one-line paragraphs) packs into few chunks, not six —
   this is the test that catches over-strict paragraph handling.
5. A chunk at or above `min_chars` closes at the next paragraph boundary in preference to
   packing on.
6. **Fixed-point merge**: construct input where one merge enables another, and assert the
   final result has no chunk under `min_chars` that could legally have merged. A
   single-pass implementation fails this.
7. Merging picks the **smaller** neighbour — assert against a case where left and right
   differ in size.
8. A merge that would exceed `max_chars` does not happen.
9. `boundary` is correct for a chunk ending at a section break, at a paragraph break, and
   mid-paragraph — one assertion each.
10. `over_cap` survives from T02 through the merge pass.
11. A title chunk is never merged into a body chunk.

## Report back

- The merge loop, verbatim, so the orchestrator can confirm it is a fixed point rather
  than a single pass.
- The boundary-classification logic.
- Confirmation that all T02 tests still pass.
- The chunk-count difference on test 4 (six one-line paragraphs → how many chunks).
