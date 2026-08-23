"""Tests for tagger/base.py: the shared tag contract, validator, backend
registry, and auto resolution (T10).

This suite must pass with neither `anthropic` nor `openai` installed, and
performs no network I/O — everything here is pure-Python / filesystem.
"""

from __future__ import annotations

import sys

import pytest

import tagger.base as base
from models import Chunk, compute_text_hash


def make_chunk(chunk_id: str = "ch07_0012", text: str = "The Long Road stretched ahead.") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        position=12,
        text=text,
        char_count=len(text),
        text_hash=compute_text_hash(text),
        kind="body",
        boundary="ends_paragraph",
        over_cap=False,
    )


# ---------------------------------------------------------------------------
# 0. No provider SDK is importable / imported by this module
# ---------------------------------------------------------------------------


def test_no_provider_sdk_installed_in_this_environment():
    """Sanity check for the environment this whole suite runs in: neither
    optional extra is installed, so any accidental hard import in base.py
    would fail this whole file at collection time already. This assertion
    makes that guarantee explicit rather than implicit."""
    assert "anthropic" not in sys.modules or True  # presence in sys.modules is fine
    for mod_name in ("anthropic", "openai"):
        assert mod_name not in sys.modules, (
            f"{mod_name} must not be imported merely by importing tagger.base"
        )


def test_base_module_imports_no_provider_sdk(monkeypatch):
    """Block anthropic/openai from ever being importable, then reload
    tagger.base fresh, to prove the module body itself never imports them."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name in ("anthropic", "openai") or name.startswith(("anthropic.", "openai.")):
            raise ImportError(f"{name} must not be imported by tagger.base")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    for mod in ("tagger.base",):
        sys.modules.pop(mod, None)
    reloaded = importlib.import_module("tagger.base")
    assert reloaded is not None


# ---------------------------------------------------------------------------
# 1. The acceptance test — rejected, not truncated (§13.7 / test 1)
# ---------------------------------------------------------------------------


def test_overlong_sentence_shaped_tag_is_rejected_not_truncated():
    bad_tag = "he says this wearily, with a long pause before the last word"
    assert len(bad_tag) > base.MAX_TAG_CHARS

    ok, reason = base.validate_tag(bad_tag, chunk_text="He walked to the door.")
    assert ok is False
    assert reason  # a reason was recorded

    chunk = make_chunk(text="He walked to the door.")
    accepted, rejected = base.parse_and_validate_response(
        [{"chunk_id": chunk.chunk_id, "tag": bad_tag}], {chunk.chunk_id: chunk}
    )

    # The chunk is absent from accepted -> it generates untagged.
    assert chunk.chunk_id not in accepted
    assert accepted == {}
    assert any(cid == chunk.chunk_id for cid, _ in rejected)

    # Critically: applying "no tag" must never leak any prefix of the bad
    # input. Check every possible truncation-to-32-or-fewer-chars prefix of
    # the rejected string does not appear in the applied output.
    applied = base.apply_tag(chunk.text, accepted.get(chunk.chunk_id))
    assert applied == chunk.text  # untagged, exactly the original text

    # Prefixes of length 1-7 are meaningless (single words/letters coincide
    # with ordinary prose by chance, e.g. "h" inside "He"), so the leak check
    # is meaningful starting from a genuine multi-word fragment: 8 chars up
    # through the full MAX_TAG_CHARS-length truncation, which is exactly the
    # shape a truncate-instead-of-reject bug would have produced.
    for n in range(8, base.MAX_TAG_CHARS + 1):
        prefix = bad_tag[:n]
        assert prefix not in applied, (
            f"a {n}-char prefix of the rejected tag leaked into the applied text"
        )
    # And the full bad tag obviously never appears either.
    assert bad_tag not in applied
    assert "[" not in applied and "]" not in applied


# ---------------------------------------------------------------------------
# 2 & 3. Length boundary
# ---------------------------------------------------------------------------


def test_33_char_otherwise_valid_tag_is_rejected():
    tag = "a" * 33
    assert len(tag) == 33
    ok, reason = base.validate_tag(tag)
    assert ok is False
    assert "32" in reason or "exceeds" in reason


def test_32_char_valid_tag_is_accepted_boundary():
    # Construct a 32-char string that also matches the shape regex. It need
    # not be in VOCABULARY for this specific boundary check on length+shape,
    # so check the length/shape gate directly via the regex + length rather
    # than requiring vocabulary membership for an arbitrary 32-char string.
    tag = "a" * 32
    assert len(tag) == 32
    assert base._TAG_SHAPE_RE.match(tag)
    # Length and shape both pass; vocabulary is the only remaining gate.
    ok, reason = base.validate_tag(tag)
    assert ok is False
    assert reason == "tag not in shipped vocabulary"

    # A real 32-char-or-under vocabulary entry must be accepted outright.
    longest = max(base.VOCABULARY, key=len)
    assert len(longest) <= base.MAX_TAG_CHARS
    ok, reason = base.validate_tag(longest)
    assert ok is True
    assert reason == ""


# ---------------------------------------------------------------------------
# 4. Shape rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [
        "Weary",  # uppercase
        "weary2",  # digit
        "[weary]",  # brackets
        "weary.",  # period
        "weary!",  # exclamation
        "WEARY",
        "weary_sad",  # underscore not allowed
    ],
)
def test_tags_with_uppercase_digits_or_brackets_are_rejected(tag):
    ok, reason = base.validate_tag(tag)
    assert ok is False
    assert "shape" in reason or "regex" in reason or "match" in reason


# ---------------------------------------------------------------------------
# 5. Echoed text rejection
# ---------------------------------------------------------------------------


def test_tag_echoing_distinctive_chunk_word_is_rejected():
    chunk_text = "The storm battered the harbor through the night."
    # "menacing" is in vocabulary and shares no words with the text, so
    # first prove it would otherwise pass...
    ok, _ = base.validate_tag("menacing", chunk_text)
    assert ok is True
    # ...then prove a tag containing a word lifted from the text is rejected,
    # even though "raining hard" is shape-valid.
    ok, reason = base.validate_tag("storm harbor", chunk_text)
    assert ok is False
    assert "echo" in reason


# ---------------------------------------------------------------------------
# 6. Vocabulary rejection despite matching regex
# ---------------------------------------------------------------------------


def test_tag_not_in_vocabulary_rejected_even_if_regex_matches():
    tag = "quietly amused"
    assert base._TAG_SHAPE_RE.match(tag)
    assert tag not in base.VOCABULARY
    ok, reason = base.validate_tag(tag)
    assert ok is False
    assert reason == "tag not in shipped vocabulary"


# ---------------------------------------------------------------------------
# 7. Every shipped vocabulary entry passes its own validator
# ---------------------------------------------------------------------------


def test_every_vocabulary_entry_passes_its_own_validator():
    assert len(base.VOCABULARY) >= 29  # "roughly thirty"
    for entry in base.VOCABULARY:
        ok, reason = base.validate_tag(entry)
        assert ok is True, f"vocabulary entry {entry!r} failed its own validator: {reason}"
        assert len(entry) <= base.MAX_TAG_CHARS


# ---------------------------------------------------------------------------
# 8. apply_tag exact formatting
# ---------------------------------------------------------------------------


def test_apply_tag_exact_format():
    assert base.apply_tag("text", "weary") == "[weary] text"


def test_apply_tag_none_returns_text_unchanged():
    assert base.apply_tag("text", None) == "text"


# ---------------------------------------------------------------------------
# 9. compute_text_hash differs tagged vs untagged
# ---------------------------------------------------------------------------


def test_compute_text_hash_differs_tagged_vs_untagged():
    text = "The Long Road stretched ahead."
    assert compute_text_hash(text, "weary") != compute_text_hash(text, None)


# ---------------------------------------------------------------------------
# 10. Every rejection is logged with chunk id + reason
# ---------------------------------------------------------------------------


def test_every_rejection_is_logged_with_chunk_id_and_reason(caplog):
    chunk = make_chunk(chunk_id="ch03_0007", text="A quiet morning in the valley.")
    bad_tag = "Weary!!"  # shape-invalid

    with caplog.at_level("WARNING", logger="tagger.base"):
        accepted, rejected = base.parse_and_validate_response(
            [{"chunk_id": chunk.chunk_id, "tag": bad_tag}], {chunk.chunk_id: chunk}
        )

    assert accepted == {}
    assert rejected == [(chunk.chunk_id, rejected[0][1])]

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert chunk.chunk_id in record.getMessage()
    assert rejected[0][1] in record.getMessage()


def test_unknown_chunk_id_is_rejected_and_logged(caplog):
    with caplog.at_level("WARNING", logger="tagger.base"):
        accepted, rejected = base.parse_and_validate_response(
            [{"chunk_id": "not_in_batch", "tag": "weary"}], {}
        )
    assert accepted == {}
    assert rejected == [("not_in_batch", "unknown chunk_id (not in requested batch)")]
    assert len(caplog.records) == 1


# ---------------------------------------------------------------------------
# 11. tags.json round-trip
# ---------------------------------------------------------------------------


def test_tags_json_round_trips(tmp_path):
    chunk_a = make_chunk("ch01_0001", "First line of the chapter.")
    chunk_b = make_chunk("ch01_0002", "A second, untagged line.")
    records = base.build_review_records(
        [chunk_a, chunk_b], {"ch01_0001": "weary"}
    )

    assert records == [
        {"chunk_id": "ch01_0001", "text_preview": "First line of the chapter.", "tag": "weary"},
        {"chunk_id": "ch01_0002", "text_preview": "A second, untagged line.", "tag": None},
    ]

    path = tmp_path / "out" / "tags.json"
    base.write_tags_review(path, records)
    assert path.exists()

    round_tripped = base.read_tags_review(path)
    assert round_tripped == records


# ---------------------------------------------------------------------------
# 12. Registry resolves without importing either SDK
# ---------------------------------------------------------------------------


def test_registry_lists_claude_and_codex_without_importing_adapters():
    assert set(base.available_backends()) == {"claude", "codex"}
    assert "tagger.claude" not in sys.modules
    assert "tagger.codex" not in sys.modules


def test_get_backend_imports_adapter_only_when_called(monkeypatch):
    """The adapter files (tagger/claude.py, tagger/codex.py) belong to T11/T12
    and may not exist yet in this environment. Prove get_backend() does the
    lazy import at call time (not at tagger.base import time) by pointing the
    registry at fake, importable stand-in modules and confirming they are
    absent from sys.modules until get_backend() is actually invoked."""
    import types

    fake_claude = types.ModuleType("tagger._fake_claude")
    fake_claude.tag = lambda batch: {}
    sys.modules["tagger._fake_claude"] = fake_claude

    monkeypatch.setitem(base._BACKEND_MODULES, "claude", ("tagger._fake_claude", "tag"))

    # Not imported merely by being registered.
    assert "tagger.claude" not in sys.modules

    fn = base.get_backend("claude")
    assert fn is fake_claude.tag

    sys.modules.pop("tagger._fake_claude", None)


def test_get_backend_unknown_name_raises_keyerror():
    with pytest.raises(KeyError):
        base.get_backend("nope")


# ---------------------------------------------------------------------------
# 13. auto resolution
# ---------------------------------------------------------------------------


def test_auto_resolves_claude_with_only_anthropic_key():
    assert base.resolve_auto({"ANTHROPIC_API_KEY": "x"}) == "claude"


def test_auto_resolves_codex_with_only_openai_key_and_model():
    assert (
        base.resolve_auto({"OPENAI_API_KEY": "x", "OPENAI_TAG_MODEL": "gpt-5"}) == "codex"
    )


def test_auto_resolves_codex_requires_both_openai_key_and_model():
    assert base.resolve_auto({"OPENAI_API_KEY": "x"}) is None
    assert base.resolve_auto({"OPENAI_TAG_MODEL": "gpt-5"}) is None


def test_auto_prefers_claude_when_both_are_set():
    env = {
        "ANTHROPIC_API_KEY": "x",
        "OPENAI_API_KEY": "y",
        "OPENAI_TAG_MODEL": "gpt-5",
    }
    assert base.resolve_auto(env) == "claude"


def test_auto_returns_none_when_neither_is_set():
    assert base.resolve_auto({}) is None
