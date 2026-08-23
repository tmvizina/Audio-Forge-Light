# T01 — Scaffold + frozen contracts

**Depends on:** nothing. **Blocks:** every other ticket.
**Read:** `BUILD-PROMPT.md` §2 (lines 60–108), §12 (lines 1031–1098), §3.6 (lines 258–275),
and the event table in §11 (lines 1000–1020). **Do not read the rest yet.**

## Why this ticket exists

Every other ticket writes a module that composes against a shared shape: the chunk record,
the NDJSON event names, the config keys. If those are invented independently by six
parallel agents, nothing fits together. This ticket freezes them **once**, in code, before
any feature work starts.

You are not implementing any pipeline logic here. If you find yourself writing a sentence
splitter or an ffmpeg call, you have left your scope.

## Files you own

Create these and only these:

```
narrator/
  types.py              # frozen data shapes
  events.py             # NDJSON emitter
  preflight.py          # ffmpeg/ffprobe presence check
  config.json
  .env.example
  .gitignore
  requirements.txt
  tests/conftest.py     # shared fixtures (fake HTTP transport, ffmpeg helpers)
  server/package.json   # express only; no code yet
```

**Pre-empt the known parallel-wave collisions.** Wave 1 runs six agents at once against the
files you create here. Three collisions are already known — handle them now so they never
happen (see [ORCHESTRATION.md](ORCHESTRATION.md) §3):

- Add **`pydantic`** to `requirements.txt` now. T10 needs it for its structured-output
  schema, and a later ticket adding it would collide with another doing the same.
- Create **`tests/conftest.py`** with the shared fixtures every Wave-1 ticket will want: a
  fake HTTP transport (so no test hits the network), an ffmpeg-generated audio fixture
  helper, and a temp-workspace fixture. Several tickets need these; only you may add them.
- Your files become a **freeze list** the moment this ticket merges. No later ticket may
  edit `types.py`, `events.py`, `config.json`, `requirements.txt`, `.env.example`,
  `.gitignore`, or `tests/conftest.py` — they file a contract-change request instead.
  Design them to be complete, because changing them later costs a round trip across every
  in-flight branch.

Also create empty-but-importable stubs so parallel tickets have somewhere to land:
`chunker.py`, `fish_client.py`, `pool.py`, `stitch.py`, `refaudio.py`, `narrate.py`,
`tagger/__init__.py`, `tagger/base.py`, `tests/__init__.py`. Each stub is a module
docstring naming its owning ticket and nothing else.

## Files you must NOT touch

Anything not listed above. Do not implement the stubs.

## Task

**1. `.gitignore` — first, before anything else is committed.** It must contain `.env`
from the very first commit. `.env` must never enter git history, so this cannot be a
later cleanup. Also ignore `out/`, `reference/*.wav`, `__pycache__/`, `node_modules/`,
`*.pyc`.

**2. `types.py` — the frozen chunk record.** A `Chunk` dataclass carrying exactly these
fields, no more:

| field | type | meaning |
|---|---|---|
| `chunk_id` | `str` | stable id, format `ch07_0012` |
| `position` | `int` | order within the chapter |
| `text` | `str` | exact chunk text, **pre-tag** |
| `char_count` | `int` | `len(text)` |
| `text_hash` | `str` | `sha256("[tag] text")` if tagged, else `sha256(text)` |
| `kind` | `str` | `"title"` or `"body"` |
| `boundary` | `str` | `"ends_section"` / `"ends_paragraph"` / `"mid_paragraph"` |
| `over_cap` | `bool` | true only for a flagged single-sentence-over-cap chunk |

Provide the hash helper here, since three tickets need it and it must behave identically
in all of them:

```python
def compute_text_hash(text: str, tag: str | None = None) -> str:
    """The hash MUST cover the applied tag. A re-tagged chunk gets a new hash, which is
    what forces regeneration on resume. Hashing raw text only would silently serve audio
    generated under the old tag."""
    payload = f"[{tag}] {text}" if tag else text
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Provide JSON round-tripping (`to_dict` / `from_dict`) — `manifest.json` is written and
re-read by other tickets and must not drift in field naming.

**3. `events.py` — the frozen NDJSON contract.** One function per event type, each printing
exactly one JSON object per line to **stdout**, flushed immediately. All human-readable
logging goes to **stderr** via a separate helper. Freeze these event names and fields:

| event | key fields |
|---|---|
| `run_started` | `book`, `chapters`, `config`, `tagger` (the **resolved** backend, or `null`) |
| `chunked` | `chapter_id`, `chunk_count` |
| `tagged` | `chapter_id`, `tagged_count`, `dropped_count` |
| `chunk_done` | `chunk_id`, `i`, `n`, `latency_s`, `concurrency` |
| `chunk_failed` | `chunk_id`, `i`, `n`, `error` |
| `concurrency_changed` | `from`, `to`, `reason` |
| `stitched` | `chapter_id`, `path`, `duration_s` |
| `done` | `book`, `chapters_done`, `failed_chunks` |
| `error` | `message` |

Two hard requirements:

- **Flush every line** (`print(..., flush=True)` or run with `-u`). The Node server reads
  these as a live SSE stream; a buffered stdout makes the UI appear frozen.
- **A redaction guard.** No API key may ever appear in an event. Write the emitter so
  values are whitelisted per event type rather than dumping arbitrary dicts, and add a
  final defensive scrub that raises if a value matches a key-shaped pattern. The
  `run_started` event carries `config` — make sure that payload is the *sanitised* config,
  never `.env` contents.

**4. `preflight.py`.** Check `shutil.which("ffmpeg")` and `shutil.which("ffprobe")` —
both, separately; they are distinct binaries and a box can have one without the other.
Fail with an actionable message naming which is missing and that it must be on `PATH`.
This must run at startup of any command that will eventually shell out, so a missing
binary surfaces immediately rather than three steps into a long run.

**5. `config.json`.** Transcribe the full default config from §12 verbatim, every field
filled in. Note that `tagger.engine` defaults to **`"auto"`**, not `"none"` — delivery tags
are on by default (§6.0), and the `_comment` on that block must say so, name the resolution
order, and state that it bills a second API account.

JSON has no comments — use a `_comment` sidecar key, consistently. The `gaps` block must
carry the pacing rationale comment (900 default, 700 lower, 1200 upper and why it is not
the default here, and that re-tuning is one ffmpeg pass and zero API calls).

**6. `.env.example`.** All five variables listed with empty values and a one-line comment
each: `FISH_API_KEY` (required), `FISH_MODEL` (optional, defaults `s2.1-pro-free`),
`ANTHROPIC_API_KEY` (optional), `OPENAI_API_KEY` (optional), `OPENAI_TAG_MODEL` (required
only for `--tagger codex`, **no default** — say why in the comment).

**7. `requirements.txt`.** `msgpack`, `httpx`, `python-dotenv`, `pydantic`. Plus a commented
block for the optional extras (`anthropic`, `openai`) and a `requirements-dev.txt` with
`pytest`. `pydantic` is core rather than optional because T10's shared schema uses it and
T10 is on the default path.

**No numpy, no torch, no soundfile** — if you think you need one, you have misread the
spec; silence is a hand-built WAV header and everything else is ffmpeg.

## Invariants at risk in this ticket

- **#4** — `compute_text_hash` is where invariant 4 lives or dies. Get it right here and
  three downstream tickets inherit it correctly.
- **#9** — the redaction guard in `events.py` is the single chokepoint for key leakage.
- **#11** — `requirements.txt` is where the heavy-dependency rule is enforced.

## Definition of done

```bash
python -c "from types import Chunk; print('ok')"   # from the narrator/ dir
pytest tests/ -v
```

Ship at least these tests:

1. `Chunk` round-trips through `to_dict`/`from_dict` with every field preserved.
2. `compute_text_hash("abc", "weary") != compute_text_hash("abc", None)` — the tag changes
   the hash.
3. `compute_text_hash("abc", None)` is stable across calls.
4. Every event emitter produces exactly one line, parseable by `json.loads`, on stdout.
5. An event emitter given a value that looks like an API key raises rather than printing.
6. `preflight` fails with a message naming `ffprobe` when `ffprobe` is absent (monkeypatch
   `shutil.which`).

## Report back

- The final `Chunk` field list and the `compute_text_hash` implementation, verbatim.
- The final event-name table, including `run_started`'s `tagger` field.
- Confirmation that `pydantic` is in `requirements.txt` and `tests/conftest.py` exists.
- Confirmation that `.gitignore` contains `.env` and that no commit yet contains a `.env`.
- Anything in §2/§12 you found ambiguous and how you resolved it.
