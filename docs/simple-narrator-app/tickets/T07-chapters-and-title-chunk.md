# T07 — Chapter detection + the title chunk

**Depends on:** T06. **Blocks:** T09.
**Read:** `BUILD-PROMPT.md` §4 (lines 276–358). **Nothing else.**

## Why this ticket exists

Chapters are the unit the user actually receives — one mp3 each. This ticket turns a flat
`.txt` into chapters, and creates the one chunk per chapter that is generated and gapped
differently from every other.

**File conflict:** you are editing `chunker.py` after T06. Do not run alongside T02 or T06.

## Files you own

`chunker.py` (continuing T06's work).

## Files you must NOT touch

Everything else.

## Task

**1. Detect chapters.** Scan line by line for a **standalone** heading line:

```
^\s*Chapter\s+(\d+(?:\.\d+)?)\s*[:\-–—]?\s*(.*)$
```

Group 1 is the number (integer or `N.N`); group 2, if non-empty, is the title text. Note
the separator class covers a colon, hyphen, en-dash and em-dash. Everything before the
first match is **front matter** and becomes its own pseudo-chapter `ch00`. Everything from
one match to (not including) the next is that chapter's body.

**2. Chapter id derivation.**

```python
def chapter_id(num_str: str) -> str:
    if "." in num_str:
        whole, frac = num_str.split(".", 1)
        return f"ch{int(whole):02d}_{frac}"
    return f"ch{int(num_str):02d}"
```

`Chapter 7` → `ch07`. `Chapter 7.5` → `ch07_5`. No heading anywhere, or text before the
first heading → `ch00`.

**3. The title chunk.** The heading is **not** folded into the first body chunk. It becomes
its own chunk with `kind="title"`, spoken with the chapter number **spelled out as words**.

This matters: TTS engines read a lone digit `7` inconsistently — sometimes "seven",
sometimes "seven-period", and in one observed case dropping it silently. So
`"Chapter 7: The Long Road"` is spoken as:

```
Chapter Seven. The Long Road.
```

A fractional chapter reads as `"Chapter Seven Point Five."` — literally the word "Point"
between the spelled-out numbers, mirroring how a person reads a decimal aloud. A heading
with no subtitle (group 2 empty) speaks just `"Chapter Seven."`.

**4. Number-to-words — a local helper, not a dependency.** Chapter numbers in a novel are
small integers (essentially always under 200). A lookup table plus simple composition is a
dozen lines and covers every real case. Do **not** add a number-to-words package for this.

```python
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
          "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty",
         "ninety"]
```

**5. The title gap.** The title chunk is followed by the **3000 ms** title gap, not the
900 ms inter-chunk gap — a deliberately longer pause that reads as a chapter break rather
than a mid-scene beat. You do not implement the gap here; you mark the chunk `kind="title"`
so T04's stitcher applies the right one. Confirm the field is set; that is your side of
the contract.

**6. Never merge across chapters.** A chapter's chunks never merge into the next chapter's,
and the title chunk never merges with a body chunk (T06 already respects `kind`; make sure
chapter segmentation happens **before** packing so the rule holds structurally).

## Invariants at risk in this ticket

- **#6** — the title chunk carries `kind="title"` so the 3000 ms gap is applied at stitch
  time, not baked into the audio.

## Definition of done

```bash
pytest tests/test_chunker.py -v
```

All T02 and T06 tests must still pass — run the whole file.

Ship at least these tests:

1. **The acceptance test from §13.1**: a fixture containing front matter, `Chapter 1`,
   `Chapter 2: Title`, and `Chapter 7.5` produces chapter ids exactly `ch00`, `ch01`,
   `ch02`, `ch07_5`, and each real chapter's first chunk is `kind="title"`.
2. `Chapter 7` → `ch07`; `Chapter 12` → `ch12`; `Chapter 7.5` → `ch07_5`.
3. All four separators work: `Chapter 7: X`, `Chapter 7 - X`, `Chapter 7 – X`,
   `Chapter 7 — X`.
4. A heading with no subtitle speaks `"Chapter Seven."` with no trailing junk.
5. `Chapter 7.5` speaks `"Chapter Seven Point Five."`.
6. Number-to-words is correct for 1, 7, 10, 13, 20, 21, 42, 99, 100, 101, 115.
7. A `Chapter` mention **inside a paragraph** (not on its own line) is **not** detected as
   a heading — this is the false-positive case that would silently split a chapter in two.
8. Text with no chapter headings at all yields a single `ch00`.
9. Front matter before `Chapter 1` lands in `ch00` and is not lost.
10. No chunk spans two chapters.
11. The title chunk is not merged into the following body chunk.

## Report back

- The `int_to_words` implementation and its output for the test 6 values.
- The spoken text produced for `Chapter 7: The Long Road`, `Chapter 7`, and `Chapter 7.5`,
  verbatim.
- Confirmation that test 7 (mid-paragraph "Chapter" false positive) passes.
- Confirmation that all T02 and T06 tests still pass.
