"""Claude delivery-tag backend (T11).

Implements the single T10 contract: `tag(batch: list[Chunk]) -> dict[str, str]`.
Registered lazily via `tagger.base._BACKEND_MODULES["claude"]` — nothing in
this file needs to be wired up elsewhere.

This adapter is on the default path (`--tagger auto` picks it whenever
`ANTHROPIC_API_KEY` is set), so every detail below is deliberate, not
incidental:

- `import anthropic` happens ONLY inside `_tag_impl()`, never at module
  scope, so a user with neither optional SDK installed can still import this
  module and run the whole pipeline untagged.
- `temperature`, `top_p`, and `budget_tokens` are NEVER sent — this model
  generation returns HTTP 400 for all three. Effort is controlled instead via
  `output_config={"effort": ...}`.
- Structured output goes through `client.beta.messages.parse(...,
  output_format=TagBatch)`, read off `response.parsed_output`. Free-text
  content is never scraped for JSON.
- `response.stop_reason == "refusal"` is checked BEFORE any content is
  touched — refusals arrive as a normal HTTP 200, not an exception.
- The stable system block (the shared tagging guide) carries
  `cache_control={"type": "ephemeral"}`; the per-batch chunk list is sent in
  the user turn, after the cache breakpoint.
- Server-side fallback is enabled (`betas=["server-side-fallback-2026-07-01"]`,
  `fallbacks="default"`) so a transient overload on the primary model doesn't
  hard-fail a batch outright.
- On any failure — refusal, malformed response, retries exhausted, an
  unexpected exception, or the SDK not being installed — the batch yields an
  empty dict. Tagging is enhancement; it must never block generation.
- Every tag returned by the model is passed through
  `tagger.base.parse_and_validate_response()`, the ONE shared validator. This
  file performs no validation of its own.
- Nothing that could contain the API key (raw exception text, response
  headers/body) is ever logged — only exception type names and, where the
  SDK exposes one, a bare numeric status code.

Model/effort override: `--tag-model` (owned by narrate.py / T09, which this
ticket blocks) is threaded through via the `AF_TAG_MODEL` / `AF_TAG_EFFORT`
environment variables, since the frozen `tag(batch) -> dict` signature takes
no config argument. `anthropic.Anthropic()` itself resolves
`ANTHROPIC_API_KEY` from the environment on its own; this module never reads
or threads the key by hand.
"""

from __future__ import annotations

import logging
import os
import time

from pydantic import BaseModel

from models import Chunk
from tagger.base import TAG_SYSTEM_GUIDE, build_tag_prompt, parse_and_validate_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured-output shape. `pydantic` is a required dependency (not an
# optional provider SDK), so this is safe to import at module scope.
# ---------------------------------------------------------------------------


class TagItem(BaseModel):
    chunk_id: str
    tag: str


class TagBatch(BaseModel):
    items: list[TagItem]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-opus-5"
# claude-haiku-4-5 is the cheap alternative (see --help / README) — never the
# default a user hasn't asked for.
DEFAULT_EFFORT = "low"

MAX_TOKENS = 4096
MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (2.0, 4.0, 8.0)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def tag(batch: list[Chunk]) -> dict[str, str]:
    """Tag `batch` via the Claude API. Returns {chunk_id: tag} for chunks the
    model successfully tagged; anything it declined, mistagged, or that
    failed for any reason is simply absent. Never raises."""
    if not batch:
        return {}

    try:
        return _tag_impl(batch)
    except Exception as exc:  # noqa: BLE001 - absolute last-resort safety net
        logger.warning(
            "tagger.claude: unexpected %s tagging batch of %d chunk(s); proceeding untagged",
            type(exc).__name__,
            len(batch),
        )
        return {}


def _tag_impl(batch: list[Chunk]) -> dict[str, str]:
    try:
        import anthropic
    except ImportError:
        logger.warning(
            "tagger.claude: 'anthropic' package is not installed; proceeding untagged"
        )
        return {}

    chunk_by_id = {c.chunk_id: c for c in batch}
    model = os.environ.get("AF_TAG_MODEL") or DEFAULT_MODEL
    effort = os.environ.get("AF_TAG_EFFORT") or DEFAULT_EFFORT

    request_kwargs = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [
            {
                "type": "text",
                "text": TAG_SYSTEM_GUIDE,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        # Per-batch chunk text goes in the user turn, after the system
        # breakpoint above, so it never invalidates the cached prefix.
        "messages": [{"role": "user", "content": build_tag_prompt(batch)}],
        # Effort, not sampling params, controls cost/depth on this model
        # generation. Never add temperature / top_p / budget_tokens here.
        "output_config": {"effort": effort},
        "output_format": TagBatch,
        "betas": [_FALLBACK_BETA],
        "fallbacks": "default",
    }

    client = anthropic.Anthropic()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.beta.messages.parse(**request_kwargs)
        except anthropic.RateLimitError:
            logger.warning(
                "tagger.claude: rate limited (attempt %d/%d)", attempt, MAX_ATTEMPTS
            )
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", "unknown")
            logger.warning(
                "tagger.claude: API status error %s (attempt %d/%d)",
                status,
                attempt,
                MAX_ATTEMPTS,
            )
        except anthropic.APIConnectionError:
            logger.warning(
                "tagger.claude: connection error (attempt %d/%d)", attempt, MAX_ATTEMPTS
            )
        except Exception as exc:  # noqa: BLE001 - typed chain above, broad catch last
            logger.warning(
                "tagger.claude: unexpected %s (attempt %d/%d)",
                type(exc).__name__,
                attempt,
                MAX_ATTEMPTS,
            )
        else:
            return _handle_response(response, chunk_by_id)

        if attempt < MAX_ATTEMPTS:
            time.sleep(_BACKOFF_SECONDS[attempt - 1])

    logger.warning(
        "tagger.claude: batch of %d chunk(s) failed after %d attempt(s); proceeding untagged",
        len(batch),
        MAX_ATTEMPTS,
    )
    return {}


def _handle_response(response, chunk_by_id: dict[str, Chunk]) -> dict[str, str]:
    """Turn a successful SDK response into {chunk_id: tag}. Never raises —
    any unexpected shape is treated as a failed batch."""
    # Refusals arrive as a normal HTTP 200. Check stop_reason BEFORE
    # touching parsed_output or any other content on the response.
    if getattr(response, "stop_reason", None) == "refusal":
        logger.warning(
            "tagger.claude: batch of %d chunk(s) refused by the model; proceeding untagged",
            len(chunk_by_id),
        )
        return {}

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        logger.warning(
            "tagger.claude: response had no parsed_output (malformed/unparseable); "
            "proceeding untagged"
        )
        return {}

    try:
        raw_items = [item.model_dump() for item in parsed.items]
    except Exception:  # noqa: BLE001 - malformed parsed_output shape
        logger.warning(
            "tagger.claude: parsed_output.items was not well-formed; proceeding untagged"
        )
        return {}

    # The ONE shared validator. No adapter-local validation, ever.
    accepted, _rejected = parse_and_validate_response(raw_items, chunk_by_id)

    usage = getattr(response, "usage", None)
    cache_read = getattr(usage, "cache_read_input_tokens", None) if usage is not None else None
    logger.debug(
        "tagger.claude: batch of %d chunk(s) processed, %d tag(s) accepted "
        "(cache_read_input_tokens=%s)",
        len(chunk_by_id),
        len(accepted),
        cache_read,
    )

    return accepted
