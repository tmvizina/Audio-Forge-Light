"""Tagger base interface and structured-output schema/validator. Owned by T10.

This module is the shared contract consumed by every delivery-tag backend
(tagger/claude.py from T11, tagger/codex.py from T12) and by the pipeline
that drives them (narrate.py from T09). It defines:

- The `tag(batch) -> {chunk_id: tag}` interface both backends implement.
- The system guide + JSON schema both backends are prompted with.
- `validate_tag()` — the ONE shared validator every tag from every backend
  passes through before it is accepted. This is the entire safety mechanism
  standing between model-authored text and a TTS engine that will read
  whatever it's given aloud. A failing tag is dropped and logged; it is
  NEVER truncated into something that merely looks valid.
- `apply_tag()` — the single, deliberately trivial way a validated tag is
  turned into TTS input text.
- A name -> lazy loader backend registry, plus the `auto` resolution helper.
- `tags.json` read/write helpers for the human review step.

Import discipline: this module imports NO provider SDK (no `anthropic`, no
`openai`) and does no network I/O. It must be importable and fully testable
in an environment where neither package is installed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable

from models import Chunk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Shared contract
# ---------------------------------------------------------------------------
#
# Both backends implement exactly this signature:
#
#   def tag(batch: list[Chunk]) -> dict[str, str]:
#       """Returns {chunk_id: tag_string} for chunks the backend successfully
#       tagged. Chunks it failed or declined to tag are simply absent from
#       the returned dict."""
#
# Declared here as a type alias so callers/adapters can annotate against a
# single shared shape rather than restating it.
TagFn = Callable[[list], dict]


# ---------------------------------------------------------------------------
# 2. Vocabulary + validator
# ---------------------------------------------------------------------------

# Shipped vocabulary (~30 entries), verbatim from the ticket / BUILD-PROMPT §6.4.
# Every entry here MUST itself pass validate_tag() with no chunk text supplied
# (see test 7) — a shipped list that fails its own rules is a real, easy bug.
VOCABULARY: frozenset[str] = frozenset(
    {
        "weary",
        "urgent",
        "whispered",
        "bitter amusement",
        "cold",
        "grieving",
        "wry",
        "awed",
        "flat",
        "mocking",
        "tender",
        "resigned",
        "furious",
        "pleading",
        "hushed",
        "defiant",
        "bewildered",
        "wistful",
        "stern",
        "playful",
        "hesitant",
        "triumphant",
        "bleak",
        "warm",
        "sardonic",
        "anxious",
        "reverent",
        "exhausted",
        "menacing",
        "gentle",
    }
)

MAX_TAG_CHARS = 32

_TAG_SHAPE_RE = re.compile(r"^[a-z][a-z ,-]*$")

# Words too short/common to reliably indicate an echoed chunk word (avoids
# false positives like "a", "to", "in" appearing in both tag and text).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "is",
        "it",
        "as",
        "he",
        "she",
        "his",
        "her",
        "him",
        "was",
        "were",
        "with",
        "for",
        "that",
        "this",
        "not",
        "so",
        "no",
    }
)

_WORD_RE = re.compile(r"[a-z]+")


def _words(s: str) -> set[str]:
    return set(_WORD_RE.findall(s.lower()))


def validate_tag(tag: str, chunk_text: str = "") -> tuple[bool, str]:
    """Validate a single candidate tag string.

    Returns (True, "") if `tag` is accepted as-is, or (False, reason) if it
    must be dropped. Never modifies or repairs `tag` — the caller must use
    the original string only if this returns True, and must otherwise drop
    the tag entirely (generate untagged). This function performs no
    truncation of any kind.

    Checks, in order (each producing a distinct logged reason):
      1. non-empty
      2. length <= MAX_TAG_CHARS
      3. shape: ^[a-z][a-z ,-]*$ (lowercase words, space/comma/hyphen only)
      4. no word in `tag` is lifted verbatim from `chunk_text`
      5. membership in the shipped VOCABULARY
    """
    if not tag:
        return False, "empty tag"

    if len(tag) > MAX_TAG_CHARS:
        return False, f"tag exceeds {MAX_TAG_CHARS} chars (len={len(tag)})"

    if not _TAG_SHAPE_RE.match(tag):
        return False, "tag does not match ^[a-z][a-z ,-]*$"

    if chunk_text:
        tag_words = _words(tag) - _STOPWORDS
        text_words = _words(chunk_text) - _STOPWORDS
        echoed = tag_words & text_words
        if echoed:
            return False, f"tag echoes chunk text word(s): {sorted(echoed)}"

    if tag not in VOCABULARY:
        return False, "tag not in shipped vocabulary"

    return True, ""


def apply_tag(text: str, tag: str | None) -> str:
    """Apply a validated tag to chunk text for TTS input.

    Exactly `f"[{tag}] {text}"` when a tag is present, else `text` unchanged.
    Never expand the tag into a persona description or a sentence of
    direction — the bracketed word is the entire delivery instruction.
    """
    if not tag:
        return text
    return f"[{tag}] {text}"


# ---------------------------------------------------------------------------
# 3. System guide + JSON schema, shared by both backends
# ---------------------------------------------------------------------------

TAG_SYSTEM_GUIDE = f"""You are tagging short passages of narration text with a single \
delivery instruction for a text-to-speech engine.

For each passage you are given, choose exactly one tag from this fixed vocabulary that \
best captures HOW the line should be performed (its emotional delivery), never WHAT it \
is about:

{", ".join(sorted(VOCABULARY))}

Rules:
- Pick the closest single vocabulary entry. Do not invent new words or phrases.
- Never paraphrase or summarize the passage's content as the tag (do not tag a scene \
about rain "raining hard" just because "rain" appears in the text).
- If no vocabulary entry clearly fits, or the passage is too short/neutral to warrant a \
tag, omit that chunk_id from your response entirely rather than guessing.
- Return ONLY the JSON shape requested below, nothing else.

Respond with:
{{"items": [{{"chunk_id": "<id>", "tag": "<one vocabulary entry>"}}, ...]}}
"""

TAG_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["chunk_id", "tag"],
            },
        }
    },
    "required": ["items"],
}


def build_tag_prompt(batch: list[Chunk]) -> str:
    """Build the user-turn prompt listing chunk_id/text pairs for a batch.

    Shared so both adapters send byte-identical prompts for identical input.
    """
    lines = [f'{{"chunk_id": "{c.chunk_id}", "text": {json.dumps(c.text)}}}' for c in batch]
    return "[\n  " + ",\n  ".join(lines) + "\n]"


def parse_and_validate_response(
    raw_items: list[dict], chunk_by_id: dict[str, Chunk]
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Shared response handling for both adapters.

    `raw_items` is the backend's parsed `items` array (list of
    {"chunk_id": ..., "tag": ...} dicts) before validation. `chunk_by_id`
    maps chunk_id -> Chunk for the batch that was sent, used to check for
    echoed text.

    Returns (accepted, rejected):
      - accepted: {chunk_id: tag} for tags that passed validate_tag()
      - rejected: [(chunk_id, reason), ...] for everything dropped, in the
        order encountered — including unknown chunk_ids and malformed items.
    Every rejection is logged (chunk id + reason) here, so adapters do not
    need to duplicate logging.
    """
    accepted: dict[str, str] = {}
    rejected: list[tuple[str, str]] = []

    for item in raw_items:
        chunk_id = item.get("chunk_id") if isinstance(item, dict) else None
        tag = item.get("tag") if isinstance(item, dict) else None

        if not chunk_id or not isinstance(tag, str):
            reason = "malformed item (missing chunk_id or tag)"
            logger.warning("tag rejected: chunk_id=%r reason=%s", chunk_id, reason)
            rejected.append((str(chunk_id), reason))
            continue

        chunk = chunk_by_id.get(chunk_id)
        if chunk is None:
            reason = "unknown chunk_id (not in requested batch)"
            logger.warning("tag rejected: chunk_id=%s reason=%s", chunk_id, reason)
            rejected.append((chunk_id, reason))
            continue

        ok, reason = validate_tag(tag, chunk.text)
        if ok:
            accepted[chunk_id] = tag
        else:
            logger.warning("tag rejected: chunk_id=%s reason=%s", chunk_id, reason)
            rejected.append((chunk_id, reason))

    return accepted, rejected


# ---------------------------------------------------------------------------
# 4. Backend registry + auto resolution
# ---------------------------------------------------------------------------
#
# name -> (module_name, attr_name). The adapter module is imported lazily,
# by name, only when `get_backend(name)` is actually called — never at
# import time of this module and never at import time of narrate.py. This is
# what lets T11 and T12 each own a standalone file with nothing here to edit
# per-adapter, and what keeps this module SDK-free.
_BACKEND_MODULES: dict[str, tuple[str, str]] = {
    "claude": ("tagger.claude", "tag"),
    "codex": ("tagger.codex", "tag"),
    "codex-cli": ("tagger.codex_cli", "tag"),
}


def available_backends() -> list[str]:
    """Names of registered backends (registration only — no import happens)."""
    return sorted(_BACKEND_MODULES)


def get_backend(name: str) -> TagFn:
    """Resolve a backend name to its `tag(batch) -> dict` callable.

    Imports the adapter module lazily, by name, at call time — not at import
    time of this module. Raises KeyError for an unregistered name (no
    provider SDK is ever imported for a name that isn't "claude"/"codex").
    """
    if name not in _BACKEND_MODULES:
        raise KeyError(
            f"unknown tagger backend {name!r}; available: {available_backends()}"
        )
    module_name, attr_name = _BACKEND_MODULES[name]
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def codex_cli_available(env: dict | None = None) -> bool:
    """Return whether the configured Codex CLI executable can be found.

    This is a cheap executable lookup rather than a login probe. The CLI
    itself handles authentication when ``codex exec`` starts.
    ``CODEX_CLI_PATH`` may be either an absolute path or a command name.
    """
    e = env if env is not None else os.environ
    configured = e.get("CODEX_CLI_PATH") or "codex"
    candidate = os.path.expanduser(configured)
    if os.path.isfile(candidate):
        return True
    return shutil.which(configured) is not None


def resolve_auto(env: dict | None = None) -> str | None:
    """`--tagger auto` resolution, per BUILD-PROMPT §6.0.

    1. ANTHROPIC_API_KEY set -> "claude"
    2. else Codex CLI executable found -> "codex-cli"
    3. else OPENAI_API_KEY and OPENAI_TAG_MODEL both set -> "codex"
    4. else -> None (caller must proceed untagged and print the recommendation)

    `env` defaults to `os.environ`; a real mapping can be passed in tests to
    avoid mutating process environment.
    """
    e = env if env is not None else os.environ

    if e.get("ANTHROPIC_API_KEY"):
        return "claude"
    if codex_cli_available(e):
        return "codex-cli"
    if e.get("OPENAI_API_KEY") and e.get("OPENAI_TAG_MODEL"):
        return "codex"
    return None


# ---------------------------------------------------------------------------
# 5. tags.json review file
# ---------------------------------------------------------------------------


def write_tags_review(path: str | Path, records: list[dict]) -> None:
    """Write the `tags.json` review file: an array of
    {chunk_id, text_preview, tag} records, for hand editing before any TTS
    spend. Overwrites `path` if it already exists.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def read_tags_review(path: str | Path) -> list[dict]:
    """Read back a `tags.json` review file written by `write_tags_review`."""
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8"))


def build_review_records(
    batch: list[Chunk], tags: dict[str, str], preview_chars: int = 60
) -> list[dict]:
    """Build the `{chunk_id, text_preview, tag}` records for tags.json from a
    batch and its accepted tags dict. Chunks with no accepted tag get
    `"tag": null` so a human can fill one in by hand.
    """
    records = []
    for chunk in batch:
        preview = chunk.text[:preview_chars]
        records.append(
            {
                "chunk_id": chunk.chunk_id,
                "text_preview": preview,
                "tag": tags.get(chunk.chunk_id),
            }
        )
    return records
