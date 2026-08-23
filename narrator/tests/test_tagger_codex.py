"""Tests for tagger/codex.py: the OpenAI / Codex delivery-tag adapter (T12).

The `openai` package is deliberately not installed in this environment (see
the T12 ticket) -- every test here fakes the SDK entirely via `sys.modules`
injection, the same technique `tests/test_tagger_base.py` uses for its
lazy-import proof. No test spends credit or touches the network.

`tagger/claude.py` (T11) is being written concurrently by another agent and
may not exist yet. The parity test (test 1) therefore does not import it --
it fakes both adapters at the boundary where they hand off to T10's shared
`parse_and_validate_response()`, since that is the one call both real
adapters are required to make identically (T11/T12 point 5: no
adapter-local validation, ever).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import types
from pathlib import Path

import pytest

import tagger.base as base
import tagger.codex as codex
from models import Chunk, compute_text_hash

MODEL_ID = "test-tag-model-2026-01"


def make_chunk(
    chunk_id: str = "ch07_0012",
    text: str = "The road narrowed as evening fell across the valley.",
) -> Chunk:
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
# Fake `openai` SDK -- module-shaped, HTTP/SDK-layer only. No real network.
# ---------------------------------------------------------------------------


class _FakeHeaders(dict):
    def get(self, key, default=None):  # case-insensitive-ish, like httpx.Headers
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeHTTPResponse:
    def __init__(self, headers=None):
        self.headers = _FakeHeaders(headers or {})


def _sdk_response(payload: dict) -> types.SimpleNamespace:
    """A stand-in for an SDK Responses object exposing `output_text`."""
    return types.SimpleNamespace(output_text=json.dumps(payload), output=[])


def _sdk_response_raw_text(text: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(output_text=text, output=[])


def build_fake_openai_module():
    """A fresh fake `openai` module: OpenAI() client + the typed exception
    classes tagger/codex.py catches by name (openai.RateLimitError, etc.)."""

    class _FakeOpenAIError(Exception):
        pass

    class _FakeAPIError(_FakeOpenAIError):
        pass

    class _FakeAPITimeoutError(_FakeAPIError):
        pass

    class _FakeAPIConnectionError(_FakeAPIError):
        pass

    class _FakeAuthenticationError(_FakeAPIError):
        pass

    class _FakeRateLimitError(_FakeAPIError):
        def __init__(self, message="rate limited", retry_after=None):
            super().__init__(message)
            headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
            self.response = _FakeHTTPResponse(headers)

    class _Responses:
        create_fn = staticmethod(lambda **kwargs: _sdk_response({"items": []}))

        def create(self, **kwargs):
            return type(self).create_fn(**kwargs)

    class _OpenAI:
        def __init__(self, *args, **kwargs):
            self.responses = _Responses()

    fake = types.ModuleType("openai")
    fake.OpenAI = _OpenAI
    fake.OpenAIError = _FakeOpenAIError
    fake.APIError = _FakeAPIError
    fake.APITimeoutError = _FakeAPITimeoutError
    fake.APIConnectionError = _FakeAPIConnectionError
    fake.AuthenticationError = _FakeAuthenticationError
    fake.RateLimitError = _FakeRateLimitError
    fake._responses_cls = _Responses
    return fake


def install_fake_openai(monkeypatch, create_fn):
    """Install a fake `openai` module in sys.modules whose
    `client.responses.create(**kwargs)` calls `create_fn(**kwargs)`."""
    fake = build_fake_openai_module()
    fake._responses_cls.create_fn = staticmethod(create_fn)
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every test in this module runs the real retry loop at most 3 times;
    never actually sleep through the 2s/4s/8s backoff schedule."""
    monkeypatch.setattr(codex.time, "sleep", lambda seconds: None)


# ---------------------------------------------------------------------------
# 1. Parity acceptance test (SS13.8)
# ---------------------------------------------------------------------------


def test_codex_and_claude_shaped_output_produce_identical_downstream_artifacts(
    monkeypatch, tmp_path
):
    chunk_a = make_chunk("ch07_0001", "The road narrowed as evening fell.")
    chunk_b = make_chunk("ch07_0002", "A quiet inn waited at the crossing.")
    batch = [chunk_a, chunk_b]

    # Identical raw provider output, as if both Claude and OpenAI had
    # returned the exact same tags for the exact same batch -- including
    # one tag ("shape-invalid!!") that must be rejected identically by
    # both, proving neither adapter bypasses T10's validator.
    raw_items = [
        {"chunk_id": "ch07_0001", "tag": "weary"},
        {"chunk_id": "ch07_0002", "tag": "shape-invalid!!"},
    ]

    monkeypatch.setenv("OPENAI_TAG_MODEL", MODEL_ID)
    install_fake_openai(monkeypatch, lambda **kwargs: _sdk_response({"items": raw_items}))

    codex_tags = codex.tag(batch)

    # Stand-in for tagger/claude.py (T11, not yet importable): the real
    # Claude adapter is required to hand its raw items to this exact same
    # T10 helper with no adapter-local validation, so driving it directly
    # here is a faithful simulation of "what claude.py would produce" for
    # this batch.
    chunk_by_id = {c.chunk_id: c for c in batch}
    claude_tags, _claude_rejected = base.parse_and_validate_response(raw_items, chunk_by_id)

    assert codex_tags == claude_tags == {"ch07_0001": "weary"}

    codex_records = base.build_review_records(batch, codex_tags)
    claude_records = base.build_review_records(batch, claude_tags)
    assert codex_records == claude_records

    codex_path = tmp_path / "codex_tags.json"
    claude_path = tmp_path / "claude_tags.json"
    base.write_tags_review(codex_path, codex_records)
    base.write_tags_review(claude_path, claude_records)
    assert base.read_tags_review(codex_path) == base.read_tags_review(claude_path)

    # Resumability parity: identical accepted tags -> identical applied
    # text -> identical text_hash -> identical set of chunks flagged for
    # regeneration, regardless of which adapter produced the tags.
    for chunk in batch:
        codex_hash = compute_text_hash(chunk.text, codex_tags.get(chunk.chunk_id))
        claude_hash = compute_text_hash(chunk.text, claude_tags.get(chunk.chunk_id))
        assert codex_hash == claude_hash

    codex_regen = {cid for cid, t in codex_tags.items() if t}
    claude_regen = {cid for cid, t in claude_tags.items() if t}
    assert codex_regen == claude_regen == {"ch07_0001"}


# ---------------------------------------------------------------------------
# 2. OPENAI_TAG_MODEL unset -> immediate, clear, non-blocking failure
# ---------------------------------------------------------------------------


def test_missing_openai_tag_model_fails_immediately_with_required_message(
    monkeypatch, caplog
):
    monkeypatch.delenv("OPENAI_TAG_MODEL", raising=False)
    sys.modules.pop("openai", None)

    with caplog.at_level(logging.ERROR, logger="tagger.codex"):
        result = codex.tag([make_chunk()])

    assert result == {}
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "OPENAI_TAG_MODEL" in logged
    assert "models.list" in logged
    # The env check happens before the lazy `import openai` -- the SDK is
    # never even touched for a request that can't be made.
    assert "openai" not in sys.modules


# ---------------------------------------------------------------------------
# 3. Configured model id is used verbatim; no hardcoded id anywhere
# ---------------------------------------------------------------------------


def test_configured_model_id_used_in_request_and_no_hardcoded_id_in_module(monkeypatch):
    monkeypatch.setenv("OPENAI_TAG_MODEL", MODEL_ID)
    seen_kwargs = {}

    def _create(**kwargs):
        seen_kwargs.update(kwargs)
        return _sdk_response({"items": []})

    install_fake_openai(monkeypatch, _create)
    codex.tag([make_chunk()])

    assert seen_kwargs["model"] == MODEL_ID

    source = Path(codex.__file__).read_text(encoding="utf-8")
    assert not re.search(r"gpt-", source, re.IGNORECASE), (
        "found a hardcoded OpenAI model-id-shaped string in tagger/codex.py"
    )


# ---------------------------------------------------------------------------
# 4. Malformed response -> empty dict, pipeline continues
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_response",
    [
        _sdk_response_raw_text("not json at all"),
        _sdk_response({"no_items_key": True}),
        _sdk_response({"items": "not-a-list"}),
        types.SimpleNamespace(output_text="", output=[]),
    ],
)
def test_malformed_response_returns_empty_dict_and_pipeline_continues(
    monkeypatch, caplog, bad_response
):
    monkeypatch.setenv("OPENAI_TAG_MODEL", MODEL_ID)
    install_fake_openai(monkeypatch, lambda **kwargs: bad_response)

    with caplog.at_level(logging.ERROR, logger="tagger.codex"):
        result = codex.tag([make_chunk()])

    assert result == {}


# ---------------------------------------------------------------------------
# 5. Rate limit is retried per policy, then yields an empty dict
# ---------------------------------------------------------------------------


def test_rate_limit_error_retried_per_policy_then_yields_empty_dict(monkeypatch):
    monkeypatch.setenv("OPENAI_TAG_MODEL", MODEL_ID)
    call_count = {"n": 0}
    sleeps = []
    monkeypatch.setattr(codex.time, "sleep", lambda s: sleeps.append(s))

    fake_holder = {}

    def _create(**kwargs):
        call_count["n"] += 1
        raise fake_holder["fake"].RateLimitError("slow down")

    fake_holder["fake"] = install_fake_openai(monkeypatch, _create)

    result = codex.tag([make_chunk()])

    assert result == {}
    assert call_count["n"] == 3  # exhausted the full 3-attempt budget
    assert sleeps == [2.0, 4.0]  # backoff before attempts 2 and 3, none after the last


def test_rate_limit_error_honours_retry_after_header(monkeypatch):
    monkeypatch.setenv("OPENAI_TAG_MODEL", MODEL_ID)
    sleeps = []
    monkeypatch.setattr(codex.time, "sleep", lambda s: sleeps.append(s))
    fake_holder = {}

    def _create(**kwargs):
        raise fake_holder["fake"].RateLimitError("slow down", retry_after=30.0)

    fake_holder["fake"] = install_fake_openai(monkeypatch, _create)

    result = codex.tag([make_chunk()])

    assert result == {}
    assert sleeps == [30.0, 30.0]  # Retry-After overrides the computed schedule every time


# ---------------------------------------------------------------------------
# 6. An invalid tag from the fake is dropped by T10's validator
# ---------------------------------------------------------------------------


def test_invalid_tag_from_fake_is_dropped_by_shared_validator(monkeypatch):
    monkeypatch.setenv("OPENAI_TAG_MODEL", MODEL_ID)
    chunk = make_chunk("ch01_0001", "A calm morning began over the ridge.")
    install_fake_openai(
        monkeypatch,
        lambda **kwargs: _sdk_response(
            {"items": [{"chunk_id": chunk.chunk_id, "tag": "NOT-VALID-2"}]}
        ),
    )

    result = codex.tag([chunk])

    assert result == {}  # shape-invalid ("uppercase", digit) tag rejected, not bypassed


# ---------------------------------------------------------------------------
# 7. `import openai` does not happen at module import time
# ---------------------------------------------------------------------------


def test_import_openai_not_at_module_import_time(monkeypatch):
    import builtins
    import importlib

    sys.modules.pop("openai", None)
    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "openai" or name.startswith("openai."):
            raise ImportError(f"{name} must not be imported by importing tagger.codex")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    sys.modules.pop("tagger.codex", None)

    reloaded = importlib.import_module("tagger.codex")

    assert reloaded is not None
    assert "openai" not in sys.modules


# ---------------------------------------------------------------------------
# 8. The API key never appears in any logged line or exception message
# ---------------------------------------------------------------------------


def test_api_key_never_appears_in_logs_or_exceptions(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_TAG_MODEL", MODEL_ID)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-read-directly-by-tests")
    secret = "sk-testsecretvalueshouldneverleak1234567890"
    fake_holder = {}

    def _create(**kwargs):
        raise fake_holder["fake"].AuthenticationError(f"Incorrect API key provided: {secret}")

    fake_holder["fake"] = install_fake_openai(monkeypatch, _create)

    with caplog.at_level(logging.DEBUG):
        result = codex.tag([make_chunk()])

    assert result == {}
    logged_text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in logged_text
