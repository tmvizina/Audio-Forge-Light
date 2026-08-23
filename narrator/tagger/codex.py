"""OpenAI / Codex delivery-tag adapter. Owned by T12.

Implements the T10 contract: `tag(batch: list[Chunk]) -> dict[str, str]`.
This is a full-parity backend, not a fallback -- an OpenAI-only user must
lose zero functionality relative to `tagger/claude.py`. It reuses every
shared piece from `tagger/base.py` verbatim (the system guide, the JSON
schema, the prompt builder, and -- critically -- `validate_tag()` via
`parse_and_validate_response()`) and adds nothing adapter-local on top of
that validation.

Two things called out deliberately, both load-bearing:

1. `OPENAI_TAG_MODEL` is a REQUIRED config value with NO hardcoded
   fallback, not even a plausible-looking one. OpenAI model ids churn, and
   a stale hardcoded default does not fail loudly at build time -- it
   fails months later, at runtime, as a confusing HTTP 404 that looks like
   an auth or network problem rather than what it actually is: a
   decommissioned model id. If the variable is unset, this module logs a
   clear failure naming `OPENAI_TAG_MODEL` and pointing at
   `client.models.list()` to discover valid current ids, then returns an
   empty dict -- same non-blocking contract as every other failure mode
   here, per BUILD-PROMPT SS6.3 / T12 point 4.

2. The structured-output call shape below was hand-verified against
   OpenAI's live, current documentation immediately before writing this
   file (not recalled from training data -- the spec for this ticket
   explicitly flagged this one call shape as unverified):

     https://developers.openai.com/api/docs/guides/structured-outputs
     (platform.openai.com/docs/guides/structured-outputs 301-redirects
     here as of 2026-08)

   Current shape is the Responses API, `client.responses.create()`, with
   the JSON schema passed under a top-level `text` kwarg (NOT
   `response_format`, which is the older Chat Completions convention):

     client.responses.create(
         model=...,
         input=[{"role": "system", ...}, {"role": "user", ...}],
         text={"format": {"type": "json_schema", "name": ..., "schema": ..., "strict": ...}},
     )

   `strict: True` requires `additionalProperties: false` on every object
   level of the schema and every property to be `required` -- T10's
   `TAG_JSON_SCHEMA` (frozen, not ours to edit) does not set
   `additionalProperties`, so this adapter passes `strict: False` and
   relies on T10's `parse_and_validate_response()` / `validate_tag()` to
   reject anything that isn't well-formed or in vocabulary -- exactly the
   same safety net the Claude adapter depends on, and the reason a
   non-strict schema here is still safe: nothing downstream trusts the
   model's output shape unchecked.

`import openai` happens lazily, inside `tag()`, never at module scope, so
this module (and the whole test suite) imports cleanly in an environment
where the `openai` package is not installed.
"""

from __future__ import annotations

import json
import logging
import os
import time

from models import Chunk
from pool import backoff_seconds
from tagger.base import (
    TAG_JSON_SCHEMA,
    TAG_SYSTEM_GUIDE,
    build_tag_prompt,
    parse_and_validate_response,
)

logger = logging.getLogger(__name__)

# Retry policy mirrors the schedule already published in pool.py (T05):
# 3 attempts total, backoff 2s -> 4s -> 8s between attempts, overridden by
# a Retry-After value when the API supplies one. Reused via
# `pool.backoff_seconds` rather than restated here so the two never drift.
_MAX_ATTEMPTS = 3

# Name used to label the JSON-schema response format. This is a schema
# identifier, not a model id -- OPENAI_TAG_MODEL is the only model id this
# module ever sends.
_SCHEMA_NAME = "tag_response"


def _extract_retry_after(exc: BaseException) -> float | None:
    """Best-effort extraction of a Retry-After value (seconds) from a rate
    limit error's response headers. Returns None if unavailable/unparseable
    -- callers fall back to the computed backoff schedule in that case."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    value = None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_response_text(response: object) -> str | None:
    """Pull the model's raw JSON text out of a Responses API result object.

    Prefers the SDK's `output_text` convenience property; falls back to
    walking `response.output[*].content[*].text` for a stand-in object that
    doesn't provide the convenience attribute. Returns None if neither is
    present/non-empty -- callers treat that as a malformed response.
    """
    text = getattr(response, "output_text", None)
    if text:
        return text

    output = getattr(response, "output", None)
    if not output:
        return None
    for item in output:
        content = getattr(item, "content", None)
        if not content:
            continue
        for part in content:
            part_text = getattr(part, "text", None)
            if part_text:
                return part_text
    return None


def _parse_items(response: object) -> list | None:
    """Parse the `{"items": [...]}` shape out of a response, or None if the
    response is malformed in any way (not JSON, not an object, no `items`
    array). Never raises -- a malformed response is an ordinary, expected
    outcome here, not a bug."""
    text = _extract_response_text(response)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    items = parsed.get("items")
    if not isinstance(items, list):
        return None
    return items


def tag(batch: list[Chunk]) -> dict[str, str]:
    """Tag a batch of chunks via the hosted OpenAI API.

    Returns {chunk_id: tag} for chunks this backend successfully tagged and
    that passed T10's shared validator. On ANY failure -- missing config,
    exhausted retries, a malformed response, an unexpected exception -- this
    returns an empty dict. The failure is logged; the caller proceeds
    untagged. Tagging is enhancement, never a blocker.
    """
    if not batch:
        return {}

    model = os.environ.get("OPENAI_TAG_MODEL")
    if not model:
        logger.error(
            "OPENAI_TAG_MODEL is not set; the codex tagger has no model id to use "
            "and will not guess one. Set OPENAI_TAG_MODEL to a valid model id for "
            "your account -- use client.models.list() to discover current ids."
        )
        return {}

    try:
        import openai  # lazy: never imported merely by importing tagger.codex
    except ImportError:
        logger.error("codex tagger: optional openai package is not installed")
        return {}

    try:
        client = openai.OpenAI()
    except Exception:
        logger.error("codex tagger: failed to construct the OpenAI client")
        return {}

    chunk_by_id = {c.chunk_id: c for c in batch}
    prompt = build_tag_prompt(batch)
    request_kwargs = {
        "model": model,
        "input": [
            {"role": "system", "content": TAG_SYSTEM_GUIDE},
            {"role": "user", "content": prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": _SCHEMA_NAME,
                "schema": TAG_JSON_SCHEMA,
                "strict": False,
            }
        },
    }

    response = None
    last_error: BaseException | None = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = client.responses.create(**request_kwargs)
        except openai.RateLimitError as exc:
            last_error = exc
            logger.warning(
                "codex tagger: rate limited (attempt %d/%d)", attempt + 1, _MAX_ATTEMPTS
            )
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(backoff_seconds(attempt, _extract_retry_after(exc)))
            continue
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            last_error = exc
            logger.warning(
                "codex tagger: transient error (attempt %d/%d): %s",
                attempt + 1,
                _MAX_ATTEMPTS,
                type(exc).__name__,
            )
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(backoff_seconds(attempt, None))
            continue
        except openai.AuthenticationError as exc:
            # Not retryable: retrying with the same bad credential wastes
            # the retry budget for no benefit. Never log str(exc) -- SDK
            # exception messages are not a channel this module trusts with
            # the API key.
            logger.error("codex tagger: authentication failed (%s)", type(exc).__name__)
            return {}
        except openai.APIError as exc:
            # Broad provider-error fallback (bad request, server error,
            # decommissioned model id producing a 404, etc.) -- most
            # specific cases are handled above this. Not retried: these are
            # request-shape or server-side failures, not transient ones.
            logger.error("codex tagger: API error (%s)", type(exc).__name__)
            return {}
        except Exception as exc:
            # Broadest fallback last, per T12's typed-error-handling order.
            logger.error("codex tagger: unexpected error (%s)", type(exc).__name__)
            return {}
        else:
            break

    if response is None:
        logger.error(
            "codex tagger: exhausted %d attempts (%s)",
            _MAX_ATTEMPTS,
            type(last_error).__name__ if last_error is not None else "unknown",
        )
        return {}

    raw_items = _parse_items(response)
    if raw_items is None:
        logger.error("codex tagger: malformed response (could not parse items)")
        return {}

    accepted, _rejected = parse_and_validate_response(raw_items, chunk_by_id)
    return accepted
