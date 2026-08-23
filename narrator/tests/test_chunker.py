"""Tests for chunker.py's sentence splitting and greedy packing (T02).

Covers BUILD-PROMPT.md §3.1 (split_sentences), §3.2 (pack), and the T02
ticket's task item 3 (over-cap single-sentence handling via
split_long_sentence / pack_with_flags). Section/paragraph boundary handling,
the small-chunk merge, and chapter detection belong to T06/T07 and are
deliberately out of scope here.
"""

from __future__ import annotations

import statistics

from chunker import (
    HARD_SPLIT_CHARS,
    MAX_CHARS,
    MIN_CHARS,
    TARGET_CHARS,
    build_chunks,
    merge_small_chunks,
    pack,
    pack_with_flags,
    segment_packable_units,
    split_long_sentence,
    split_sentences,
)

# ~250-word acceptance fixture (BUILD-PROMPT.md §13.2). Deliberately contains
# an abbreviation ("U.S.") and a quote-then-lowercase-continuation sentence
# ('"Stop!" she cried.') -- the two classic cases a naive `re.split` shatters.
ACCEPTANCE_FIXTURE = (
    'The council convened in the old hall long before dawn, its stone walls '
    'slick with autumn mist and the murmur of two dozen anxious voices. '
    'Ilyra Calder had crossed half the continent to reach this room, and she '
    'was not about to let the delegates from the U.S. stall another vote. '
    'The chairman cleared his throat and reminded everyone that the treaty '
    'had waited three years already, that patience among the border towns '
    'was thinner than paper, and that a further delay might not survive the '
    'winter. Ilyra rose before he finished. "Stop!" she cried, and her chair '
    'clattered backward against the flagstones loud enough that the '
    'youngest scribe jumped in his seat. Every head in the hall turned '
    'toward her. She steadied her breath, smoothed the front of her '
    'travel-worn coat, and began again more quietly. The numbers, she said, '
    'were not abstractions; they were grain stores, or the lack of them, in '
    'villages she had walked through herself. A vote deferred was a harvest '
    'deferred, and nobody in this hall would starve because of a scheduling '
    'preference. The room fell silent for a long moment, and then, one by '
    'one, the delegates began to nod. By the time the sun cleared the '
    'eastern windows, the treaty had its votes, all fourteen of them, and '
    'Ilyra sat back down feeling, for the first time in months, that the '
    'long journey might actually have been worth it.'
)


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def test_1_acceptance_fixture_no_sentence_split_across_chunks():
    """250-word fixture, target_chars=200: reassembling reproduces the exact
    sentence list, and no sentence is split across two chunks."""
    sentences = split_sentences(ACCEPTANCE_FIXTURE)

    # The two trap phrases must each survive as their own intact sentence.
    assert any("U.S." in s for s in sentences)
    assert any('"Stop!" she cried' in s for s in sentences)

    chunks = pack(sentences, target_chars=200, max_chars=300)

    # Reassembling every chunk with the same single-space joiner used inside
    # pack() must exactly reproduce joining the original sentence list --
    # this only holds if no sentence was altered, dropped, or duplicated.
    assert " ".join(chunks) == " ".join(sentences)

    # Every sentence appears whole inside exactly one chunk.
    for sent in sentences:
        containing = [c for c in chunks if sent in c]
        assert len(containing) == 1, f"sentence not intact in exactly one chunk: {sent!r}"


def test_2_decimal_does_not_split():
    sentences = split_sentences(
        "The reading was 3.5 percent higher than last year's average."
    )
    assert len(sentences) == 1
    assert "3.5" in sentences[0]


def test_3_abbreviation_mid_sentence_does_not_split():
    sentences = split_sentences(
        "The delegates agreed that the U.S. government said the plan would work."
    )
    assert len(sentences) == 1
    assert "U.S. government" in sentences[0]


def test_4_lowercase_continuation_after_quote_is_one_sentence():
    sentences = split_sentences('"Stop!" she cried.')
    assert sentences == ['"Stop!" she cried.']


def test_5_no_chunk_exceeds_max_chars_for_ordinary_sentences():
    sentences = split_sentences(ACCEPTANCE_FIXTURE)
    chunks = pack(sentences, target_chars=TARGET_CHARS, max_chars=MAX_CHARS)
    assert all(len(c) <= MAX_CHARS for c in chunks)


def test_6_chunk_sizes_cluster_near_target_not_max():
    # A longer text with many short-to-medium sentences so the distribution
    # is meaningful (a single chunk's median is not informative).
    long_text = " ".join([ACCEPTANCE_FIXTURE] * 4)
    sentences = split_sentences(long_text)
    chunks = pack(sentences, target_chars=TARGET_CHARS, max_chars=MAX_CHARS)

    assert len(chunks) >= 5
    median = statistics.median(len(c) for c in chunks)
    # Proves the soft target is actually firing: median closer to 200 than 300.
    assert abs(median - TARGET_CHARS) < abs(median - MAX_CHARS)


def test_7_over_cap_single_sentence_becomes_its_own_flagged_chunk():
    long_sentence = "A " + ("very " * 74) + "long sentence indeed."
    assert MAX_CHARS < len(long_sentence) <= HARD_SPLIT_CHARS

    sentences = split_sentences(long_sentence)
    assert len(sentences) == 1  # sanity: it's genuinely one sentence

    results = pack_with_flags(sentences)
    assert len(results) == 1
    chunk_text, over_cap = results[0]
    assert over_cap is True
    assert chunk_text == long_sentence  # not split, not altered


def test_8_hard_split_over_600_splits_at_clause_punctuation():
    head = "A " + ("very " * 100) + "long clause"
    tail = "and an equally long second clause that follows after it" + (" indeed" * 15) + "."
    long_sentence = f"{head}, {tail}"
    assert len(long_sentence) > HARD_SPLIT_CHARS

    pieces = split_long_sentence(long_sentence)
    assert len(pieces) == 2

    # Split at the comma, not mid-word: each piece is a clean substring of
    # the original (after stripping), and no word was truncated.
    assert long_sentence.startswith(pieces[0][:-1]) or pieces[0][:-1] in long_sentence
    assert pieces[0].endswith(",")
    assert long_sentence.strip().endswith(pieces[1])
    for piece in pieces:
        assert not piece[0].isspace() and not piece[-1].isspace()
        # No word was cut in half: every piece's first/last "tokens" are
        # whole words found in the original text.
        first_word = piece.split()[0]
        last_word = piece.rstrip(",;:").split()[-1]
        assert first_word in long_sentence
        assert last_word in long_sentence


def test_9_split_sentences_drops_no_characters():
    sentences = split_sentences(ACCEPTANCE_FIXTURE)
    assert _normalize_ws(" ".join(sentences)) == _normalize_ws(ACCEPTANCE_FIXTURE)


# --- Abbreviation / personal-title guard -----------------------------------
# Orchestrator-authorised scope extension: "Dr. Smith arrived." has the exact
# same punctuation + whitespace + capital shape as a real sentence boundary,
# so the base §3.1 walk alone mis-splits titles and other common
# abbreviations. See chunker._ABBREVIATIONS / chunker._preceding_token.


def test_10_title_abbreviation_dr_does_not_split():
    sentences = split_sentences("Dr. Smith arrived.")
    assert sentences == ["Dr. Smith arrived."]


def test_11_multiple_title_abbreviations_in_one_sentence():
    sentences = split_sentences("Mrs. Jones and Mr. Vale left.")
    assert sentences == ["Mrs. Jones and Mr. Vale left."]


def test_12_st_abbreviation_then_genuine_boundary_is_two_sentences():
    sentences = split_sentences("He went to St. Petersburg. It was cold.")
    assert sentences == ["He went to St. Petersburg.", "It was cold."]


def test_13_etc_mid_sentence_does_not_split():
    sentences = split_sentences("Bring pens, paper, etc. Then leave the room.")
    assert sentences == ["Bring pens, paper, etc. Then leave the room."]


def test_14_lowercase_1st_is_not_caught_by_st_abbreviation():
    # "st." in "1st." must NOT be treated as the "St" abbreviation --
    # comparison is case-sensitive and exact, not a suffix match.
    sentences = split_sentences("He finished 1st. Then he celebrated loudly.")
    assert sentences == ["He finished 1st.", "Then he celebrated loudly."]


def test_15_genuine_boundary_after_word_merely_resembling_abbreviation():
    # "walk" is not in the abbreviation list, so this must still split
    # normally -- the guard must not over-trigger on ordinary words.
    sentences = split_sentences("She saw him walk. Then she left.")
    assert sentences == ["She saw him walk.", "Then she left."]


def test_16_abbreviation_guard_does_not_break_acceptance_fixture():
    # Regression: the guard must not interfere with the ordinary sentences
    # in the T02 acceptance fixture (none of which contain a guarded title).
    sentences = split_sentences(ACCEPTANCE_FIXTURE)
    assert _normalize_ws(" ".join(sentences)) == _normalize_ws(ACCEPTANCE_FIXTURE)
    assert any("U.S." in s for s in sentences)
    assert any('"Stop!" she cried' in s for s in sentences)


# --- T06: packable-unit boundaries, paragraph packing, small-chunk merge ---
# Covers BUILD-PROMPT.md §3.3-3.6: hard boundaries (section dividers,
# headings, blank-line runs) are never packed across, ordinary paragraph
# breaks may be, the small-chunk merge runs to a fixed point, and the
# `boundary`/`kind`/`over_cap` manifest fields are populated correctly.


def test_17_asterisk_divider_never_packed_across():
    text = (
        "Alpha content stays on its own side of the divider right here.\n\n"
        "***\n\n"
        "Beta content stays on its own side of the divider right here too."
    )
    chunks = build_chunks(text)
    assert not any("Alpha" in c.text and "Beta" in c.text for c in chunks)
    assert not any("***" in c.text for c in chunks)


def test_18_dash_divider_and_heading_are_hard_boundaries_too():
    dash_text = (
        "Alpha content stays on its own side of the divider right here.\n\n"
        "---\n\n"
        "Beta content stays on its own side of the divider right here too."
    )
    dash_chunks = build_chunks(dash_text)
    assert not any("Alpha" in c.text and "Beta" in c.text for c in dash_chunks)
    assert not any("---" in c.text for c in dash_chunks)

    heading_text = (
        "Alpha content stays on its own side of the heading right here.\n\n"
        "## Scene Two\n\n"
        "Beta content stays on its own side of the heading right here too."
    )
    heading_chunks = build_chunks(heading_text)
    assert not any("Alpha" in c.text and "Beta" in c.text for c in heading_chunks)
    assert not any("Scene Two" in c.text for c in heading_chunks)


def test_19_two_blank_lines_are_hard_a_single_blank_line_is_not():
    two_blank = "Alpha paragraph one.\n\n\nBeta paragraph two."
    units_two = segment_packable_units(two_blank)
    assert len(units_two) == 2

    one_blank = "Alpha paragraph one.\n\nBeta paragraph two."
    units_one = segment_packable_units(one_blank)
    assert len(units_one) == 1
    # Short enough that packing crosses the single paragraph break.
    chunks_one = build_chunks(one_blank)
    assert any("Alpha" in c.text and "Beta" in c.text for c in chunks_one)


def test_20_short_paragraph_dialogue_packs_into_few_chunks_not_six():
    lines = [
        '"Yes," she said.',
        '"No," he said.',
        '"Wait," they said.',
        '"Really?" she asked.',
        '"Truly," he replied.',
        '"Enough," the elder said.',
    ]
    text = "\n\n".join(lines)
    chunks = build_chunks(text)
    # This is the assertion that catches over-strict paragraph handling:
    # six tiny one-line paragraphs must not become six separate chunks.
    assert len(chunks) < 6


def test_21_closes_at_paragraph_boundary_once_min_chars_reached():
    # Both paragraphs individually clear min_chars, so the merge pass has no
    # reason to fold them back together -- this isolates the pack-time
    # "prefer to close at the paragraph boundary" behaviour from the
    # separate, later small-chunk merge (which would legitimately undo it
    # if para2 were left under min_chars).
    para1 = (
        "This first paragraph is deliberately long enough on its own to "
        "clear the minimum size threshold for closing early."
    )
    assert MIN_CHARS <= len(para1) < TARGET_CHARS
    para2 = (
        "A second paragraph that is also long enough on its own to clear "
        "the minimum size threshold, so the merge pass leaves it alone."
    )
    assert MIN_CHARS <= len(para2) < TARGET_CHARS
    text = f"{para1}\n\n{para2}"

    chunks = build_chunks(text)
    assert len(chunks) == 2
    first = chunks[0]
    assert para1 in first.text
    assert para2 not in first.text
    assert first.boundary == "ends_paragraph"


def test_22_fixed_point_merge_chains_across_multiple_passes():
    # B can't clear min_chars alone; merging it into C still leaves the
    # result under min_chars, so a correct fixed-point merge must then fold
    # that combined chunk into D as well. A single, non-restarting pass
    # would stop after the first merge and leave an under-sized chunk.
    items = [
        {"text": "A" * 150, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "B" * 20, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "C" * 25, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "D" * 20, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "E" * 150, "over_cap": False, "boundary": "ends_section", "kind": "body"},
    ]
    result = merge_small_chunks(items, min_chars=MIN_CHARS, max_chars=MAX_CHARS)

    assert len(result) == 3
    assert result[0]["text"] == "A" * 150
    assert result[2]["text"] == "E" * 150
    middle = result[1]["text"]
    assert middle.count("B") == 20
    assert middle.count("C") == 25
    assert middle.count("D") == 20
    assert len(middle) >= MIN_CHARS
    # No chunk left under min_chars that could legally have merged further.
    assert all(len(r["text"]) >= MIN_CHARS for r in result)


def test_23_merge_picks_the_smaller_neighbour():
    items = [
        {"text": "L" * 200, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "S" * 30, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "R" * 40, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
    ]
    result = merge_small_chunks(items)
    assert len(result) == 2
    assert result[0]["text"] == "L" * 200
    # R (40) is smaller than L (200), so S merges right into R, not left into L.
    assert result[1]["text"] == ("S" * 30) + " " + ("R" * 40)


def test_24_merge_exceeding_max_chars_does_not_happen():
    items = [
        {"text": "L" * 280, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "S" * 30, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "R" * 280, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
    ]
    result = merge_small_chunks(items, min_chars=MIN_CHARS, max_chars=MAX_CHARS)
    # Neither neighbour fits (280 + 1 + 30 = 311 > 300), so S is left alone.
    assert len(result) == 3
    assert result[1]["text"] == "S" * 30


def test_25_boundary_ends_section_and_ends_paragraph():
    text = (
        "Short lead-in paragraph that stays under the target on its own "
        "merits here now.\n\n"
        "Second short paragraph continues right after it without "
        "triggering an early close yet.\n\n"
        "***\n\n"
        "New section paragraph begins after the divider and stands "
        "entirely alone over here."
    )
    chunks = build_chunks(text)
    assert chunks[-1].boundary == "ends_section"
    assert any(c.boundary == "ends_paragraph" for c in chunks[:-1])


def test_26_boundary_mid_paragraph_when_split_within_a_single_paragraph():
    # One paragraph, no blank lines, long enough to force more than one
    # chunk -- the first chunk must close mid-paragraph, not at a paragraph
    # boundary (there isn't one until the very end).
    long_para = ("Short sentence here. " * 20).strip()
    chunks = build_chunks(long_para)
    assert len(chunks) >= 2
    assert chunks[0].boundary == "mid_paragraph"


def test_27_over_cap_survives_the_merge_pass():
    items = [
        {"text": "S" * 30, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
        {"text": "O" * 350, "over_cap": True, "boundary": "mid_paragraph", "kind": "body"},
    ]
    result = merge_small_chunks(items)
    # S (30) can't merge into O: 30 + 1 + 350 > max_chars, so both records
    # survive untouched and O's over_cap flag is preserved.
    assert len(result) == 2
    over_cap_records = [r for r in result if r["over_cap"]]
    assert len(over_cap_records) == 1
    assert over_cap_records[0]["text"] == "O" * 350


def test_27b_over_cap_survives_end_to_end_through_build_chunks():
    long_sentence = "A " + ("very " * 74) + "long sentence indeed."
    assert MAX_CHARS < len(long_sentence) <= HARD_SPLIT_CHARS
    text = f"Hi.\n\n{long_sentence}"

    chunks = build_chunks(text)
    over_cap_chunks = [c for c in chunks if c.over_cap]
    assert len(over_cap_chunks) == 1
    assert long_sentence in over_cap_chunks[0].text


def test_28_title_chunk_is_never_merged_into_a_body_chunk():
    items = [
        {"text": "T" * 10, "over_cap": False, "boundary": "ends_paragraph", "kind": "title"},
        {"text": "B" * 20, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
    ]
    result = merge_small_chunks(items)
    assert len(result) == 2
    assert result[0]["kind"] == "title"
    assert result[0]["text"] == "T" * 10
    assert result[1]["text"] == "B" * 20


def test_29_merge_never_crosses_an_ends_section_boundary():
    # Both records are individually small and would fit together within
    # max_chars, but the left one ends a section -- merging across that
    # boundary must never happen, even though the character-count check
    # alone would allow it.
    items = [
        {"text": "A" * 20, "over_cap": False, "boundary": "ends_section", "kind": "body"},
        {"text": "B" * 20, "over_cap": False, "boundary": "mid_paragraph", "kind": "body"},
    ]
    result = merge_small_chunks(items)
    assert len(result) == 2
    assert result[0]["text"] == "A" * 20
    assert result[1]["text"] == "B" * 20


def test_30_build_chunks_populates_all_manifest_fields():
    text = "A short chapter body with just one plain paragraph in it here."
    chunks = build_chunks(text, chapter_id="ch07", start_position=1)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.chunk_id == "ch07_0001"
    assert chunk.position == 1
    assert chunk.char_count == len(chunk.text)
    assert chunk.kind == "body"
    assert chunk.boundary == "ends_section"
    assert chunk.over_cap is False
    assert len(chunk.text_hash) == 64  # sha256 hex digest
