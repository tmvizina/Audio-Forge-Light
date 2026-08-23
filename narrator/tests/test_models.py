"""Tests for the frozen Chunk dataclass and compute_text_hash (T01 contracts)."""

from __future__ import annotations

from models import Chunk, compute_text_hash


def test_chunk_round_trips_through_to_dict_from_dict():
    chunk = Chunk(
        chunk_id="ch07_0012",
        position=12,
        text="The Long Road stretched ahead.",
        char_count=31,
        text_hash=compute_text_hash("The Long Road stretched ahead."),
        kind="body",
        boundary="ends_paragraph",
        over_cap=False,
    )

    round_tripped = Chunk.from_dict(chunk.to_dict())

    assert round_tripped == chunk
    assert round_tripped.chunk_id == "ch07_0012"
    assert round_tripped.position == 12
    assert round_tripped.text == "The Long Road stretched ahead."
    assert round_tripped.char_count == 31
    assert round_tripped.text_hash == chunk.text_hash
    assert round_tripped.kind == "body"
    assert round_tripped.boundary == "ends_paragraph"
    assert round_tripped.over_cap is False


def test_tag_changes_the_hash():
    assert compute_text_hash("abc", "weary") != compute_text_hash("abc", None)


def test_hash_is_stable_across_calls():
    assert compute_text_hash("abc", None) == compute_text_hash("abc", None)
    assert compute_text_hash("abc", "weary") == compute_text_hash("abc", "weary")
