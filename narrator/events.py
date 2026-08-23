"""Frozen NDJSON event contract for the narrator CLI.

Owned by T01. No later ticket may edit this file directly — file a
contract-change request instead (see ORCHESTRATION.md).

Every `emit_*` function prints exactly one JSON object per line to **stdout**,
flushed immediately — the Node server reads this as a live stream, so a
buffered line makes the UI look frozen. Human-readable/debug logging never
goes to stdout; use `log()`, which writes to stderr.

Redaction: `emit_*` functions only accept a fixed, whitelisted set of fields
per event type (see the table below) — arbitrary dicts are never dumped
wholesale. On top of that whitelist, every outgoing payload is run through
`_scrub()`, which recursively rejects any string value that is *key-shaped*
(a long run of secret-alphabet characters with no separators, e.g. what an
API key looks like) and raises `SecretLeakError` instead of printing it.

| event                  | key fields                                              |
|-------------------------|----------------------------------------------------------|
| run_started             | book, chapters, config, tagger (resolved backend or null)|
| chunked                 | chapter_id, chunk_count                                   |
| tagged                  | chapter_id, tagged_count, dropped_count                   |
| chunk_done              | chunk_id, i, n, latency_s, concurrency                     |
| chunk_failed            | chunk_id, i, n, error                                     |
| concurrency_changed     | from, to, reason                                          |
| stitched                | chapter_id, path, duration_s                               |
| done                    | book, chapters_done, failed_chunks                         |
| error                   | message                                                    |
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# A bare run of 20+ "secret alphabet" characters (alnum, -, _) with no
# whitespace or path/URL separators is treated as key-shaped. Paths, chunk
# ids, and prose all contain "/", ".", or spaces, so they never trip this.
_KEY_SHAPED_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class SecretLeakError(ValueError):
    """Raised when an event payload contains a value that looks like a secret."""


def _scrub(value: Any, path: str = "$") -> Any:
    """Recursively validate a value contains nothing key-shaped. Returns value unchanged."""
    if isinstance(value, str):
        if _KEY_SHAPED_RE.match(value):
            raise SecretLeakError(
                f"refusing to emit event: value at {path} looks like a secret/API key"
            )
        return value
    if isinstance(value, dict):
        for k, v in value.items():
            _scrub(v, f"{path}.{k}")
        return value
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _scrub(v, f"{path}[{i}]")
        return value
    # numbers, bools, None: nothing to scrub
    return value


def _write(event: str, fields: dict) -> None:
    payload = {"event": event, **fields}
    _scrub(payload)
    print(json.dumps(payload), flush=True)


def log(message: str) -> None:
    """Human-readable logging. Always stderr, never stdout — stdout is NDJSON-only."""
    print(message, file=sys.stderr, flush=True)


def emit_run_started(book: str, chapters: list, config: dict, tagger: str | None) -> None:
    """`tagger` is the resolved backend name ('claude' / 'codex') or None when untagged.
    `config` must already be the sanitised config (never raw .env contents)."""
    _write(
        "run_started",
        {"book": book, "chapters": chapters, "config": config, "tagger": tagger},
    )


def emit_chunked(chapter_id: str, chunk_count: int) -> None:
    _write("chunked", {"chapter_id": chapter_id, "chunk_count": chunk_count})


def emit_tagged(chapter_id: str, tagged_count: int, dropped_count: int) -> None:
    _write(
        "tagged",
        {
            "chapter_id": chapter_id,
            "tagged_count": tagged_count,
            "dropped_count": dropped_count,
        },
    )


def emit_chunk_done(
    chunk_id: str, i: int, n: int, latency_s: float, concurrency: int
) -> None:
    _write(
        "chunk_done",
        {
            "chunk_id": chunk_id,
            "i": i,
            "n": n,
            "latency_s": latency_s,
            "concurrency": concurrency,
        },
    )


def emit_chunk_failed(chunk_id: str, i: int, n: int, error: str) -> None:
    _write("chunk_failed", {"chunk_id": chunk_id, "i": i, "n": n, "error": error})


def emit_concurrency_changed(from_: int, to: int, reason: str) -> None:
    _write("concurrency_changed", {"from": from_, "to": to, "reason": reason})


def emit_stitched(chapter_id: str, path: str, duration_s: float) -> None:
    _write("stitched", {"chapter_id": chapter_id, "path": path, "duration_s": duration_s})


def emit_done(book: str, chapters_done: int, failed_chunks: int) -> None:
    _write("done", {"book": book, "chapters_done": chapters_done, "failed_chunks": failed_chunks})


def emit_error(message: str) -> None:
    _write("error", {"message": message})
