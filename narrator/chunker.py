"""Sentence-aware chunk packing.

Owned by T02 (sentence splitting + greedy packing) and extended by T06
(packable-unit segmentation, paragraph handling, the small-chunk merge, and
`boundary` classification) and T07 (chapter detection, title chunks). See
BUILD-PROMPT.md §3 for the full spec. This ticket implements only §3.1
(sentence boundary detection), §3.2 (the greedy packing loop), and the
over-cap single-sentence handling described in the T02 ticket's task item 3
(a preview of the `over_cap` field defined in full by §3.6 / T06).

Public surface, and what's a hook for later tickets:

- `split_sentences(text)` -- pure sentence splitter, no packing. T06 will
  call this once per "packable unit" (the text between hard boundaries such
  as scene breaks) rather than once per whole chapter -- this function itself
  does not know about units, and shouldn't have to.
- `pack(sentences, ...)` -- pure greedy packer, transcribed literally from the
  spec, with no knowledge of paragraph/section boundaries. T06 layers the
  "prefer to close at a paragraph boundary once >= min_chars" rule *on top*
  of this rather than inside it, so this function stays exactly as specified.
- `split_long_sentence(sentence, ...)` -- clause-punctuation split for the
  rare over-`hard_split_chars` sentence. Kept separate from `pack` so T06/T07
  can reuse it verbatim if a chapter-title or merged fragment ever needs it.
- `pack_with_flags(sentences, ...)` -- convenience wrapper tying the above
  together into `(chunk_text, over_cap)` pairs, the minimal extra structure
  needed to carry `over_cap` out of this module. T06 is expected to replace
  this entry point with one that also derives `boundary` and `kind` and
  builds full `Chunk` objects (via `models.Chunk`); nothing here assumes it
  won't be superseded.
"""

from __future__ import annotations

import re

from models import Chunk, compute_text_hash

# Size bounds, in characters (BUILD-PROMPT.md §3). All later tickets that
# touch chunker.py must keep reading these names, not re-declare their own.
TARGET_CHARS = 200  # soft target -- a chunk closes as soon as it reaches this
MAX_CHARS = 300  # hard cap -- a chunk must never exceed this by packing
MIN_CHARS = 60  # below this, a chunk is a merge candidate (T06, §3.4)
HARD_SPLIT_CHARS = 600  # only past this does a single sentence get split at all

_SENT_END = re.compile(r"[.!?]+[\"'”’\)\]]*")
_OPENERS = "\"'“‘([“‘"
_CLAUSE_PUNCT = ",;:"  # nearest-to-midpoint split points for over-cap sentences

# Personal-title / common-abbreviation guard (orchestrator-authorised scope
# extension to T02, added after landing: "Dr. Smith arrived." has the exact
# same punctuation + whitespace + capital shape as a real sentence boundary,
# so the base §3.1 walk alone treats "Dr." as ending a sentence). Deliberately
# a flat lookup table, not general abbreviation detection -- see
# `_preceding_token` and its use in `split_sentences` below. Comparison is
# case-sensitive and exact, so e.g. "st" in "1st." never matches "St".
_ABBREVIATIONS = frozenset(
    {
        "Mr", "Mrs", "Ms", "Miss", "Dr", "Prof", "Rev", "Fr", "Sr", "Jr",
        "St", "Lt", "Capt", "Col", "Gen", "Sgt", "Maj", "Adm", "Gov", "Sen",
        "Rep", "Hon", "Messrs", "Mmes",
        "vs", "etc", "e.g", "i.e", "cf", "al",
        "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept",
        "Oct", "Nov", "Dec", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
    }
)
# Leading punctuation to strip off a token before comparing it against
# _ABBREVIATIONS, so a quoted '"Dr. Smith spoke."' still matches "Dr".
_TOKEN_STRIP_LEADING = "\"'“‘(["


def _preceding_token(text: str, pos: int) -> str:
    """The whitespace-delimited token immediately before `pos` (with common
    leading quote/bracket punctuation stripped), or "" if there is none."""
    tokens = text[:pos].split()
    if not tokens:
        return ""
    return tokens[-1].lstrip(_TOKEN_STRIP_LEADING)


def split_sentences(text: str) -> list[str]:
    """Split `text` into sentences without ever dropping characters.

    Transcribed literally from BUILD-PROMPT.md §3.1, plus an abbreviation
    guard. `re.split(r'[.!?]')` is the classic wrong answer -- it shatters
    "U.S." and "3.5" into fragments, and loses the fact that
    `"Stop!" she cried.` is one sentence, not two. On its own, though, the
    §3.1 walk still mis-splits "Dr. Smith arrived." -- "Dr." has the exact
    same punctuation + whitespace + capital shape as a genuine sentence end.
    The guard below checks the word immediately before the punctuation
    against a fixed abbreviation list before accepting a boundary.
    """
    sentences: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        rest = text[end:]
        if rest and not rest[0].isspace():
            # e.g. "U.S." or "3.5" -- punctuation immediately followed by a
            # non-space character is not a sentence boundary.
            continue
        nxt = rest.lstrip()
        if nxt == "" or nxt[0].isupper() or nxt[0] in _OPENERS:
            if _preceding_token(text, m.start()) in _ABBREVIATIONS:
                # Looks like a boundary (punctuation + whitespace + capital)
                # but the preceding word is a known title/abbreviation, e.g.
                # "Dr." or "etc." -- keep accumulating instead.
                continue
            # End of text, or the next real character starts a new sentence
            # (capital letter or an opening quote/bracket).
            sentences.append(text[start:end].strip())
            start = end
        # else: not a boundary -- e.g. mid-sentence "Mr. Smith" where the
        # next word is lowercase; keep accumulating.
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def pack(
    sentences: list[str],
    target_chars: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
) -> list[str]:
    """Greedily pack `sentences` into chunks joined by a single space.

    Transcribed literally from BUILD-PROMPT.md §3.2. The hard-cap check
    flushes *before* appending, so a chunk is never pushed over `max_chars`;
    the soft-target check flushes *after* appending, so a chunk lands
    anywhere between roughly `target_chars` and `max_chars` and stops as soon
    as it's "big enough" -- this is what clusters sizes near `target_chars`
    instead of every chunk crawling to `max_chars`.
    """
    chunks: list[str] = []
    buf: list[str] = []
    bc = 0  # buffered char count for the chunk under construction

    def flush():
        nonlocal buf, bc
        if buf:
            chunks.append(" ".join(buf))
        buf = []
        bc = 0

    for sent in sentences:
        add_chars = len(sent) + (1 if buf else 0)  # +1 for the joining space
        if buf and (bc + add_chars > max_chars):
            # HARD cap would be exceeded -- flush what we have FIRST, then
            # start a fresh chunk with this sentence.
            flush()
            add_chars = len(sent)
        buf.append(sent)
        bc += add_chars
        if bc >= target_chars:
            # SOFT target reached -- close now rather than waiting for the
            # hard cap, so chunks cluster near target_chars instead of
            # piling up at max_chars.
            flush()
    flush()
    return chunks


def split_long_sentence(
    sentence: str,
    hard_split_chars: int = HARD_SPLIT_CHARS,
) -> list[str]:
    """Split an over-`hard_split_chars` sentence at the nearest clause
    punctuation (comma, semicolon, or colon) to its midpoint.

    Sentences at or under `hard_split_chars` are returned unchanged as a
    single-element list -- they become their own (possibly over-cap) chunk
    in `pack`, they are not split. If no clause punctuation exists at all,
    the sentence is likewise left whole rather than cut at an arbitrary
    offset, per the ticket's "never at an arbitrary character offset" rule.
    """
    if len(sentence) <= hard_split_chars:
        return [sentence]

    midpoint = len(sentence) / 2
    candidates = [i for i, ch in enumerate(sentence) if ch in _CLAUSE_PUNCT]
    if not candidates:
        return [sentence]

    split_at = min(candidates, key=lambda i: abs(i - midpoint))
    head = sentence[: split_at + 1].strip()
    tail = sentence[split_at + 1 :].strip()
    if not head or not tail:
        return [sentence]
    return [head, tail]


def pack_with_flags(
    sentences: list[str],
    target_chars: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
    hard_split_chars: int = HARD_SPLIT_CHARS,
) -> list[tuple[str, bool]]:
    """Pack `sentences`, returning `(chunk_text, over_cap)` pairs.

    A chunk is `over_cap` exactly when it exceeds `max_chars` -- which, given
    `pack`'s hard-cap check always flushes *before* appending to a non-empty
    buffer, can only happen when a single sentence alone (post clause-split)
    is longer than `max_chars`. Such a sentence is never itself split further
    here -- only `split_long_sentence` (applied first, above
    `hard_split_chars`) ever divides a sentence.
    """
    prepared: list[str] = []
    for sent in sentences:
        prepared.extend(split_long_sentence(sent, hard_split_chars))
    chunks = pack(prepared, target_chars, max_chars)
    return [(chunk, len(chunk) > max_chars) for chunk in chunks]


# ---------------------------------------------------------------------------
# T06: packable-unit segmentation, paragraph-aware packing, small-chunk
# merge, and the `build_chunks` entry point that assembles real
# `models.Chunk` records. See BUILD-PROMPT.md §3.3-3.6.
# ---------------------------------------------------------------------------

# A line that is *only* a section-break marker (three-or-more `*`, `-`, or
# em-dash characters, optionally padded with whitespace) -- BUILD-PROMPT.md
# §3.3's `^\s*[\*\-—]{3,}\s*$`.
_SECTION_BREAK_RE = re.compile(r"^\s*[\*\-—]{3,}\s*$")
# A Markdown heading line (`#` through `######` followed by whitespace).
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")


def segment_packable_units(text: str) -> list[list[str]]:
    """Segment raw chapter text into **packable units** at every hard
    boundary: a section-break marker line, a Markdown heading line, or a run
    of 2+ consecutive blank lines (BUILD-PROMPT.md §3.3). Chapter boundaries
    are out of scope here -- T07 calls this once per already-isolated
    chapter, so this function never sees more than one chapter's text.

    Returns a list of units; each unit is itself a list of paragraph strings
    (a paragraph is text separated from its neighbours by exactly one blank
    line -- an ordinary, *soft* break that packing is allowed to cross, per
    §3.3's "ordinary paragraph breaks are different"). The boundary marker
    lines themselves (dividers, headings) are consumed and never appear in
    any paragraph's text -- they are not meant to be spoken.
    """
    lines = text.split("\n")
    units: list[list[str]] = []
    cur_unit: list[str] = []
    cur_para: list[str] = []

    def close_para() -> None:
        nonlocal cur_para
        if cur_para:
            para_text = " ".join(line.strip() for line in cur_para if line.strip())
            if para_text:
                cur_unit.append(para_text)
        cur_para = []

    def close_unit() -> None:
        nonlocal cur_unit
        close_para()
        if cur_unit:
            units.append(cur_unit)
        cur_unit = []

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _SECTION_BREAK_RE.match(line) or _HEADING_RE.match(line):
            close_unit()
            i += 1
            continue
        if line.strip() == "":
            j = i
            while j < n and lines[j].strip() == "":
                j += 1
            blank_run = j - i
            if blank_run >= 2:
                # Soft section break -- same hard-boundary treatment as a
                # marker line.
                close_unit()
            else:
                # A single blank line is an ordinary paragraph break --
                # packing may still cross it later.
                close_para()
            i = j
            continue
        cur_para.append(line)
        i += 1
    close_unit()
    return [u for u in units if u]


def pack_paragraphs(
    paragraphs: list[str],
    target_chars: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
    min_chars: int = MIN_CHARS,
    hard_split_chars: int = HARD_SPLIT_CHARS,
) -> list[dict]:
    """Pack the paragraphs of a single packable unit into chunk records.

    Behaves like `pack()` (same target/hard-cap flushing) but is paragraph
    aware: once a chunk under construction has reached `min_chars`, closing
    at the *next* paragraph boundary is preferred over packing further
    (BUILD-PROMPT.md §3.3's "ordinary paragraph breaks" rule) -- but packing
    is still allowed to continue across a paragraph break when the buffer
    hasn't reached `min_chars` yet, which is what keeps short one-line-per-
    paragraph dialogue from shattering into a flood of tiny chunks.

    Returns a list of dicts with keys `text`, `over_cap`, `boundary`
    (`"ends_paragraph"` or `"mid_paragraph"` -- never `"ends_section"`; the
    caller, which alone knows where a *unit* ends, is responsible for
    promoting the last record of a unit to `"ends_section"`) and
    `kind` (always `"body"` here; T07 adds `"title"` records separately).
    """
    results: list[dict] = []
    buf: list[str] = []
    bc = 0

    def flush(boundary: str) -> None:
        nonlocal buf, bc
        if buf:
            text = " ".join(buf)
            results.append(
                {
                    "text": text,
                    "over_cap": len(text) > max_chars,
                    "boundary": boundary,
                    "kind": "body",
                }
            )
        buf = []
        bc = 0

    for para in paragraphs:
        prepared: list[str] = []
        for sent in split_sentences(para):
            prepared.extend(split_long_sentence(sent, hard_split_chars))
        last_idx = len(prepared) - 1
        for idx, sent in enumerate(prepared):
            add_chars = len(sent) + (1 if buf else 0)
            if buf and (bc + add_chars > max_chars):
                # HARD cap would be exceeded -- flush first. This can only
                # happen mid-paragraph (a paragraph-boundary close already
                # fires, below, whenever the buffer clears min_chars at a
                # paragraph's last sentence).
                flush("mid_paragraph")
                add_chars = len(sent)
            buf.append(sent)
            bc += add_chars

            if idx == last_idx and bc >= min_chars:
                # End of this paragraph, and the chunk is already big
                # enough to stand alone -- prefer to close here rather than
                # pack on into the next paragraph.
                flush("ends_paragraph")
                continue

            if bc >= target_chars:
                # SOFT target reached before the paragraph ended -- this is
                # necessarily a mid-paragraph close (the last-sentence case
                # above already handled the "ends at paragraph" case).
                flush("mid_paragraph")
        # else: paragraph exhausted without closing (buffer under
        # min_chars) -- fall through and keep packing into the next
        # paragraph, per §3.3.

    # Any leftover buffer belongs to the tail of the unit's last paragraph.
    flush("ends_paragraph")
    return results


def merge_small_chunks(
    items: list[dict],
    min_chars: int = MIN_CHARS,
    max_chars: int = MAX_CHARS,
) -> list[dict]:
    """Fold chunks under `min_chars` into an adjacent chunk, run to a fixed
    point (BUILD-PROMPT.md §3.4).

    `items` is a list of dicts with `text`, `boundary`, `over_cap`, `kind`
    keys (the same shape `pack_paragraphs` / `build_chunks` use). Returns a
    new list; the input is not mutated.

    Rules enforced here:
    - merge into whichever neighbour is smaller, among neighbours the merge
      would still fit under `max_chars`;
    - never merge across a `boundary == "ends_section"` edge -- that field
      is exactly the record of a hard packable-unit boundary, so treating it
      as crossable here would silently reintroduce invariant #1's
      never-pack-across-a-hard-boundary violation at the merge stage;
    - never merge a `kind == "title"` chunk into a body chunk, nor a body
      chunk into a title chunk;
    - re-scan from the top after every single merge until a full pass makes
      no more merges (a fixed point, not one pass) -- merging chunk 5 into
      chunk 4 can make chunk 4 itself newly eligible to merge into chunk 3.
    """

    def fits(a: dict, b: dict) -> bool:
        return len(a["text"]) + 1 + len(b["text"]) <= max_chars

    out = [dict(item) for item in items]
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i, item in enumerate(out):
            if item["kind"] == "title" or len(item["text"]) >= min_chars:
                continue

            left = out[i - 1] if i > 0 else None
            if left is not None and left["kind"] == "title":
                left = None
            right = out[i + 1] if i + 1 < len(out) else None
            if right is not None and right["kind"] == "title":
                right = None

            candidates: list[tuple[str, dict]] = []
            # A record whose own `boundary` is "ends_section" is the last
            # chunk of its packable unit -- merging it with what follows
            # would cross that hard boundary, so the right-merge is only
            # offered when `item` does not itself end a section. Symmetrically,
            # merging with `left` is only offered when `left` does not end a
            # section (i.e. `left` and `item` are still in the same unit).
            if left is not None and left["boundary"] != "ends_section" and fits(left, item):
                candidates.append(("left", left))
            if right is not None and item["boundary"] != "ends_section" and fits(item, right):
                candidates.append(("right", right))

            if not candidates:
                continue

            side, neighbour = min(candidates, key=lambda c: len(c[1]["text"]))
            if side == "left":
                merged_text = f'{left["text"]} {item["text"]}'
                merged = {
                    "text": merged_text,
                    "over_cap": len(merged_text) > max_chars,
                    "boundary": item["boundary"],
                    "kind": "body",
                }
                out[i - 1 : i + 1] = [merged]
            else:
                merged_text = f'{item["text"]} {right["text"]}'
                merged = {
                    "text": merged_text,
                    "over_cap": len(merged_text) > max_chars,
                    "boundary": right["boundary"],
                    "kind": "body",
                }
                out[i : i + 2] = [merged]
            changed = True
            break  # rescan from the top -- this is what makes it a fixed point
    return out


def build_chunks(
    text: str,
    chapter_id: str = "ch00",
    start_position: int = 0,
    target_chars: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
    min_chars: int = MIN_CHARS,
    hard_split_chars: int = HARD_SPLIT_CHARS,
) -> list[Chunk]:
    """The public T06 entry point: raw chapter text in, `models.Chunk`
    records out.

    Pipeline: segment into packable units (§3.3) -> pack each unit's
    paragraphs, marking the last chunk of each unit `"ends_section"` (only
    the caller, working across units, knows where a unit ends) -> fixed-
    point small-chunk merge (§3.4) -> assign `chunk_id`/`position` in final
    order and hash each chunk's text (§3.6).

    `chapter_id` and `start_position` exist for T07: it detects chapters and
    the one `"title"` chunk per chapter itself, then calls this once per
    chapter body with that chapter's id and a `start_position` of 1 (leaving
    position 0 for the title chunk it prepends).
    """
    raw: list[dict] = []
    for unit in segment_packable_units(text):
        packed = pack_paragraphs(unit, target_chars, max_chars, min_chars, hard_split_chars)
        if not packed:
            continue
        packed[-1] = {**packed[-1], "boundary": "ends_section"}
        raw.extend(packed)

    merged = merge_small_chunks(raw, min_chars, max_chars)

    chunks: list[Chunk] = []
    for offset, record in enumerate(merged):
        position = start_position + offset
        chunk_text = record["text"]
        chunks.append(
            Chunk(
                chunk_id=f"{chapter_id}_{position:04d}",
                position=position,
                text=chunk_text,
                char_count=len(chunk_text),
                text_hash=compute_text_hash(chunk_text),
                kind=record["kind"],
                boundary=record["boundary"],
                over_cap=record["over_cap"],
            )
        )
    return chunks
