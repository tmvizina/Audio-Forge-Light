"""Frozen data shapes shared across the narrator pipeline.

Owned by T01. No later ticket may edit this file directly — file a
contract-change request instead (see ORCHESTRATION.md).

NOTE ON THE MODULE NAME: this file is deliberately NOT called `types.py`.
`narrator/` sits on `sys.path` for every invocation style this project uses,
so a root-level `types.py` would shadow the standard library's `types` module
process-wide. The stdlib itself (`enum`, `re`, `dataclasses`, `inspect`, …)
does `from types import ...` internally, so that shadowing breaks unrelated
imports across the whole program. Never add a module here whose name collides
with a stdlib module.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, fields

CHUNK_KINDS = ("title", "body")
BOUNDARIES = ("ends_section", "ends_paragraph", "mid_paragraph")


def compute_text_hash(text: str, tag: str | None = None) -> str:
    """The hash MUST cover the applied tag. A re-tagged chunk gets a new hash, which is
    what forces regeneration on resume. Hashing raw text only would silently serve audio
    generated under the old tag."""
    payload = f"[{tag}] {text}" if tag else text
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Chunk:
    """A single unit of narration text and its bookkeeping metadata.

    The field set is frozen by T01 and consumed verbatim by chunker.py,
    fish_client.py, pool.py, stitch.py, narrate.py, and the tagger package. Do not
    add, rename, or remove fields without a contract-change request.
    """

    chunk_id: str
    position: int
    text: str
    char_count: int
    text_hash: str
    kind: str  # "title" or "body"
    boundary: str  # "ends_section" / "ends_paragraph" / "mid_paragraph"
    over_cap: bool

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Chunk":
        names = {f.name for f in fields(Chunk)}
        return Chunk(**{k: data[k] for k in names})
