"""Tests for tagger/claude.py (T11): the Claude delivery-tag adapter.

The `anthropic` package is deliberately NOT installed in this environment —
that is the correct state, and this suite proves the module still imports
and runs cleanly. Every test that needs a client fakes the SDK at the
`sys.modules["anthropic"]` layer; nothing here spends credit or touches the
network.
"""

from __future__ import annotations

import sys
import types

import pytest

from models import Chunk, compute_text_hash

# `tagger.claude` must be importable with `anthropic` absent from
# sys.modules and absent from the environment entirely. Importing it here,
# at module scope, before any fake is installed, is itself a standing proof
# of that — if the module did `import anthropic` at module scope, collecting
# this file would already fail.
#
# Immediately popped from sys.modules again: tagger/base.py's own test suite
# (test_tagger_base.py) asserts "tagger.claude" is absent from sys.modules
# unless something has explicitly imported it, and pytest's collection phase
# imports every test file — this one included — before any test in any file
# actually runs. Leaving the entry behind here would make that unrelated
# suite's assertions depend on file collection order. The `claude_mod` name
# below still refers to the already-executed module object either way; only
# its registry entry is removed.
import tagger.claude as claude_mod

sys.modules.pop("tagger.claude", None)


def make_chunk(
    chunk_id: str = "ch01_0001", text: str = "He walked slowly to the door."
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        position=1,
        text=text,
        char_count=len(text),
        text_hash=compute_text_hash(text),
        kind="body",
        boundary="ends_paragraph",
        over_cap=False,
    )


# ---------------------------------------------------------------------------
# Fake anthropic SDK
# ---------------------------------------------------------------------------


class FakeRateLimitError(Exception):
    pass


class FakeAPIStatusError(Exception):
    def __init__(self, message: str = "status error", status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


class FakeAPIConnectionError(Exception):
    pass


class _FakeUsage:
    def __init__(self, cache_read_input_tokens: int = 0, cache_creation_input_tokens: int = 0):
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class _FakeResponse:
    """A normal (non-refusal) response. `parsed_output` is a plain attribute
    here since access-tracking is only needed on the refusal-guard variant
    below."""

    def __init__(self, parsed_output=None, stop_reason: str = "end_turn", cache_read_input_tokens: int = 0):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.stop_details = None
        self.usage = _FakeUsage(cache_read_input_tokens=cache_read_input_tokens)


class _RefusalGuardResponse:
    """A stop_reason == 'refusal' response whose `parsed_output` raises an
    assertion-recording flag (not an exception the adapter's broad except
    could swallow) if ever read, so the test can prove the adapter checks
    stop_reason before touching content."""

    def __init__(self):
        self.stop_reason = "refusal"
        self.stop_details = types.SimpleNamespace(category="violence", explanation="policy decline")
        self.usage = _FakeUsage()
        self.accessed = False

    @property
    def parsed_output(self):
        self.accessed = True
        return None


class _FakeItem:
    def __init__(self, chunk_id: str, tag: str):
        self.chunk_id = chunk_id
        self.tag = tag

    def model_dump(self) -> dict:
        return {"chunk_id": self.chunk_id, "tag": self.tag}


class _FakeParsed:
    def __init__(self, items: list):
        self.items = items


def _install_fake_anthropic(monkeypatch, parse_impl=None):
    """Install a fake `anthropic` module into sys.modules for the duration of
    one test (monkeypatch reverts it automatically) and return it.

    `parse_impl(kwargs, call_number) -> response` is invoked by the fake
    `client.beta.messages.parse(**kwargs)`; every call's kwargs are also
    appended to `fake.calls` so tests can assert on the exact request shape.
    If `parse_impl` raises, that exception propagates out of `.parse()`
    exactly as the real SDK would raise a typed exception.
    """
    fake = types.ModuleType("anthropic")
    fake.RateLimitError = FakeRateLimitError
    fake.APIStatusError = FakeAPIStatusError
    fake.APIConnectionError = FakeAPIConnectionError

    calls: list[dict] = []
    fake.calls = calls

    def default_parse_impl(kwargs, n):
        return _FakeResponse(parsed_output=_FakeParsed([]))

    impl = parse_impl or default_parse_impl

    class _FakeBetaMessages:
        def parse(self, **kwargs):
            calls.append(kwargs)
            return impl(kwargs, len(calls))

    class _FakeBeta:
        def __init__(self):
            self.messages = _FakeBetaMessages()

    class _FakeAnthropicClient:
        def __init__(self, *args, **kwargs):
            self.beta = _FakeBeta()

    fake.Anthropic = _FakeAnthropicClient

    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return fake


def _no_sleep(monkeypatch):
    """Tests that exercise retries must not actually sleep 2s/4s/8s."""
    monkeypatch.setattr(claude_mod.time, "sleep", lambda seconds: None)


# ---------------------------------------------------------------------------
# 1. No sampling parameters, ever — the regression that matters most
# ---------------------------------------------------------------------------


def test_request_contains_no_sampling_parameters(monkeypatch):
    fake = _install_fake_anthropic(monkeypatch)
    chunk = make_chunk()

    claude_mod.tag([chunk])

    assert len(fake.calls) == 1
    kwargs = fake.calls[0]

    # Explicit absence, not merely "not asserted on" — this is the
    # regression the ticket calls out as most likely to be carried in from
    # older code/training data.
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "budget_tokens" not in kwargs
    # budget_tokens is a thinking sub-param in older APIs; make sure no
    # nested `thinking` block smuggles it in either.
    assert "thinking" not in kwargs


# ---------------------------------------------------------------------------
# 2. Effort control via output_config, not sampling params
# ---------------------------------------------------------------------------


def test_request_carries_low_effort_output_config(monkeypatch):
    fake = _install_fake_anthropic(monkeypatch)
    chunk = make_chunk()

    claude_mod.tag([chunk])

    kwargs = fake.calls[0]
    assert kwargs["output_config"] == {"effort": "low"}


# ---------------------------------------------------------------------------
# 3. Refusals are HTTP 200, not exceptions — check stop_reason first
# ---------------------------------------------------------------------------


def test_refusal_returns_empty_dict_without_reading_content(monkeypatch, caplog):
    responses: list[_RefusalGuardResponse] = []

    def parse_impl(kwargs, n):
        r = _RefusalGuardResponse()
        responses.append(r)
        return r

    _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)
    chunk = make_chunk()

    with caplog.at_level("WARNING", logger="tagger.claude"):
        result = claude_mod.tag([chunk])

    assert result == {}
    assert len(responses) == 1
    assert responses[0].accessed is False, (
        "parsed_output was read despite stop_reason == 'refusal' — "
        "the adapter must check stop_reason before touching content"
    )
    assert any("refus" in r.getMessage().lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Malformed/unparseable response -> empty dict, pipeline continues
# ---------------------------------------------------------------------------


def test_malformed_response_returns_empty_dict_and_does_not_raise(monkeypatch, caplog):
    def parse_impl(kwargs, n):
        # end_turn (not refusal), but the SDK could not produce parsed_output.
        return _FakeResponse(parsed_output=None, stop_reason="end_turn")

    _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)
    chunk = make_chunk()

    with caplog.at_level("WARNING", logger="tagger.claude"):
        result = claude_mod.tag([chunk])  # must not raise

    assert result == {}
    assert any("malformed" in r.getMessage().lower() or "parsed_output" in r.getMessage() for r in caplog.records)


def test_malformed_items_shape_returns_empty_dict_and_does_not_raise(monkeypatch):
    def parse_impl(kwargs, n):
        parsed = _FakeParsed(items="not-a-list-of-items")
        return _FakeResponse(parsed_output=parsed, stop_reason="end_turn")

    _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)
    chunk = make_chunk()

    result = claude_mod.tag([chunk])  # must not raise even though .model_dump() doesn't exist

    assert result == {}


# ---------------------------------------------------------------------------
# 5. RateLimitError is retried per policy, then yields an empty dict
# ---------------------------------------------------------------------------


def test_rate_limit_error_is_retried_then_yields_empty_dict(monkeypatch, caplog):
    _no_sleep(monkeypatch)

    def parse_impl(kwargs, n):
        raise FakeRateLimitError("rate limited")

    fake = _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)
    chunk = make_chunk()

    with caplog.at_level("WARNING", logger="tagger.claude"):
        result = claude_mod.tag([chunk])

    assert result == {}
    # Retried per policy, not failed on the first attempt.
    assert len(fake.calls) == claude_mod.MAX_ATTEMPTS
    assert len(fake.calls) > 1
    assert any("rate limit" in r.getMessage().lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. Every tag is passed through T10's shared validator
# ---------------------------------------------------------------------------


def test_valid_tag_from_backend_is_accepted_via_shared_validator(monkeypatch):
    chunk = make_chunk(text="He walked slowly to the door.")
    good_item = _FakeItem(chunk.chunk_id, "weary")  # in VOCABULARY, no echo

    def parse_impl(kwargs, n):
        return _FakeResponse(parsed_output=_FakeParsed([good_item]), stop_reason="end_turn")

    _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)

    result = claude_mod.tag([chunk])

    assert result == {chunk.chunk_id: "weary"}


def test_invalid_tag_from_backend_is_dropped_not_bypassed(monkeypatch, caplog):
    """Proves the adapter does not do its own validation, or none at all —
    only tagger.base.validate_tag() can accept or reject a tag. Feed it a
    shape-invalid tag the adapter itself never checks for."""
    chunk = make_chunk(text="He walked slowly to the door.")
    bad_item = _FakeItem(chunk.chunk_id, "Weary!!")  # uppercase + punctuation

    def parse_impl(kwargs, n):
        return _FakeResponse(parsed_output=_FakeParsed([bad_item]), stop_reason="end_turn")

    _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)

    with caplog.at_level("WARNING", logger="tagger.base"):
        result = claude_mod.tag([chunk])

    assert result == {}
    assert chunk.chunk_id not in result
    # The rejection was logged by tagger.base's shared validator, proving
    # the reject path actually went through it.
    assert any(chunk.chunk_id in r.getMessage() for r in caplog.records)


def test_echoed_tag_from_backend_is_dropped_by_shared_validator(monkeypatch):
    """A shape-valid tag that still fails validate_tag()'s echo check must
    also be dropped — again proving there is no adapter-local shortcut."""
    chunk = make_chunk(text="The storm battered the harbor through the night.")
    echoing_item = _FakeItem(chunk.chunk_id, "storm harbor")  # shape-valid, echoes text

    def parse_impl(kwargs, n):
        return _FakeResponse(parsed_output=_FakeParsed([echoing_item]), stop_reason="end_turn")

    _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)

    result = claude_mod.tag([chunk])

    assert result == {}


# ---------------------------------------------------------------------------
# 7. `import anthropic` is lazy — module import succeeds without it
# ---------------------------------------------------------------------------


def test_import_tagger_claude_succeeds_with_anthropic_absent(monkeypatch):
    """A plain fresh import of tagger.claude succeeds in this environment,
    where the real `anthropic` package is not installed at all (only fakes
    installed by other tests via sys.modules ever appear under that name)."""
    import importlib

    monkeypatch.delitem(sys.modules, "tagger.claude", raising=False)
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)

    module = importlib.import_module("tagger.claude")

    assert module is not None
    assert "anthropic" not in sys.modules


def test_importing_tagger_claude_does_not_trigger_import_of_anthropic(monkeypatch):
    """Reload tagger.claude with `import anthropic` monkeypatched to raise at
    module scope, proving the lazy-import discipline directly rather than by
    inference."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "anthropic" or name.startswith("anthropic."):
            raise ImportError(f"{name} must not be imported at tagger.claude module scope")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    monkeypatch.setattr(builtins, "__import__", blocking_import)
    monkeypatch.delitem(sys.modules, "tagger.claude", raising=False)

    reloaded = importlib.import_module("tagger.claude")

    assert reloaded is not None
    assert "anthropic" not in sys.modules


# ---------------------------------------------------------------------------
# 8. The API key never appears in a log line or exception message
# ---------------------------------------------------------------------------


def test_api_key_never_appears_in_logs(monkeypatch, caplog):
    secret = "sk-ant-super-secret-value-should-never-be-logged"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    _no_sleep(monkeypatch)

    def parse_impl(kwargs, n):
        # Simulate an SDK exception whose text happens to embed the key, the
        # way a connection/auth error message plausibly could.
        raise FakeAPIConnectionError(f"connection failed for key={secret}")

    _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)
    chunk = make_chunk()

    with caplog.at_level("DEBUG"):
        result = claude_mod.tag([chunk])

    assert result == {}
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in repr(record.getMessage())


# ---------------------------------------------------------------------------
# 9. Prompt caching placement: system block cached, chunks in the user turn
# ---------------------------------------------------------------------------


def test_system_block_is_cached_and_chunks_are_in_user_turn(monkeypatch):
    fake = _install_fake_anthropic(monkeypatch)
    chunk = make_chunk(chunk_id="ch02_0005", text="A distinctive marker phrase for this test.")

    claude_mod.tag([chunk])

    kwargs = fake.calls[0]

    system = kwargs["system"]
    assert isinstance(system, list)
    assert any(block.get("cache_control") == {"type": "ephemeral"} for block in system)

    # The chunk itself must not be embedded in the (stable, cacheable)
    # system block — it belongs in the user turn, after the breakpoint.
    system_text = " ".join(block.get("text", "") for block in system)
    assert chunk.chunk_id not in system_text

    messages = kwargs["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert chunk.chunk_id in str(messages[0]["content"])


# ---------------------------------------------------------------------------
# Bonus: server-side fallback wiring + cache_read_input_tokens is exercised
# ---------------------------------------------------------------------------


def test_server_side_fallback_beta_and_default_routing_are_set(monkeypatch):
    fake = _install_fake_anthropic(monkeypatch)
    chunk = make_chunk()

    claude_mod.tag([chunk])

    kwargs = fake.calls[0]
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"


def test_structured_output_uses_output_format_not_prose_scraping(monkeypatch):
    fake = _install_fake_anthropic(monkeypatch)
    chunk = make_chunk()

    claude_mod.tag([chunk])

    kwargs = fake.calls[0]
    assert kwargs["output_format"] is claude_mod.TagBatch


def test_cache_read_input_tokens_is_read_from_usage_and_logged(monkeypatch, caplog):
    chunk = make_chunk(text="He walked slowly to the door.")
    item = _FakeItem(chunk.chunk_id, "weary")

    def parse_impl(kwargs, n):
        return _FakeResponse(
            parsed_output=_FakeParsed([item]),
            stop_reason="end_turn",
            cache_read_input_tokens=4096,
        )

    _install_fake_anthropic(monkeypatch, parse_impl=parse_impl)

    with caplog.at_level("DEBUG", logger="tagger.claude"):
        result = claude_mod.tag([chunk])

    assert result == {chunk.chunk_id: "weary"}
    assert any("4096" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Failure contract: an empty batch never calls the API at all
# ---------------------------------------------------------------------------


def test_empty_batch_returns_empty_dict_without_calling_the_api(monkeypatch):
    fake = _install_fake_anthropic(monkeypatch)

    result = claude_mod.tag([])

    assert result == {}
    assert fake.calls == []
