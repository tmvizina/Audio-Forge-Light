# AGENTS.md — narrator

`narrator` turns a plain `.txt` novel into audiobook mp3s read in the user's own cloned
voice, via the hosted Fish Audio API. A Python CLI (`narrate.py`) is the real app; an
optional ~150-line Node/express server is a thin browser wrapper around it. It runs on a
light Windows machine — no GPU, no torch, no numpy. Pipeline stages: `chunk` → (optional
`tag`) → `generate` → `stitch`, or the combined `run`. Read the Invariants section before
changing anything.

## Repo map

| Path | What it owns |
|---|---|
| `narrate.py` | CLI entrypoint and stage dispatch (`chunk`, `tag`, `generate`, `stitch`, `run`, `prep-ref`) |
| `chunker.py` | Sentence-aware splitting and greedy packing into chunks |
| `fish_client.py` | Hosted Fish Audio TTS client (msgpack wire format) |
| `tagger/base.py` | Shared tagger contract, validator, JSON schema, vocabulary |
| `tagger/claude.py` | Anthropic-backed delivery tagger (optional) |
| `tagger/codex.py` | OpenAI-backed delivery tagger (optional) |
| `pool.py` | Adaptive-concurrency worker pool for `generate` |
| `stitch.py` | ffmpeg assembly: gaps, concat demuxer, final mp3 encode |
| `server/index.js` | Node/express wrapper; spawns `narrate.py`, streams its NDJSON to the browser |
| `server/` static page | One HTML page: pick book, pick chapter range, watch progress, download |
| `reference/` | User-supplied `narrator.wav` + `narrator.txt` (voice clone source) |
| `out/<book>/<chNN>/` | Per-chunk wavs, `manifest.json`, final chapter mp3 |
| `tests/` | Acceptance tests, all backends faked at the HTTP layer |
| `config.json` | Tunable defaults (chunk sizes, gap_ms, concurrency, model ids) |
| `.env` | API keys (`FISH_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) — gitignored |

## Commands

| Want to | Command |
|---|---|
| Install | `pip install -r requirements.txt` |
| Run everything for a book | `python narrate.py run --book mynovel.txt` |
| Run one stage | `python narrate.py chunk --book mynovel.txt` |
| Run a chapter range | `python narrate.py run --book mynovel.txt --chapters 3-7` |
| Force a redo (ignore resume cache) | `python narrate.py generate --book mynovel.txt --force` |
| Re-stitch with a different gap | `python narrate.py stitch --book mynovel.txt --gap-ms 700` |
| Prepare the voice reference | `python narrate.py prep-ref --wav reference/narrator.wav --txt reference/narrator.txt` |
| Run the tests | `pytest` |
| Start the browser UI | `node server/index.js` |

## INVARIANTS — do not casually "improve" these

1. **Never split a sentence in the chunker.** The boundary rule is terminal punctuation
   plus trailing quotes/brackets, followed by whitespace, followed by EOF / an opening
   quote / a capital letter. *Why:* naive `re.split(r'[.!?]')` breaks `U.S.` and
   `"Stop!" she cried.` *Breaks if violated:* a split sentence is audible as a hard cut
   mid-clause.

2. **Never call the TTS on a chunk with no alphanumerics.** The guard is
   `re.search(r'[A-Za-z0-9]', text)`. *Why:* a chunk of `"."` or `"***"` does not produce
   silence. *Breaks if violated:* it produces up to ~47 seconds of grunting from the
   model. Write a local silent wav instead.

3. **Markers stay ≤ 32 chars and go through the validator.** *Why:* long free-form
   direction in a leading `[bracket]` gets SPOKEN ALOUD verbatim by the TTS. *Breaks if
   violated:* minutes of the narrator reading its own stage directions. A failing tag is
   dropped and logged — **never truncated into something that looks valid.**

4. **`text_hash` covers the applied tag** — `sha256("[tag] text")`. *Why:* resume logic
   keys off this hash. *Breaks if violated:* if the hash covers only the raw text, a
   re-tagged chunk resumes to stale audio and the new tag silently never ships.

5. **Concat with the DEMUXER and a list file, never the concat FILTER.** *Why:* the
   filter puts every input on the command line. *Breaks if violated:* a 300-chunk
   chapter (600+ paths with gaps) blows past Windows' 8191-character command-line limit.
   The demuxer is only safe because every wav is normalised to 44100/mono/pcm_s16le on
   arrival — if you weaken that normalisation, the demuxer silently produces garbage.

6. **Gaps are applied only at stitch time.** Never bake a pause into a generated chunk,
   and never re-generate audio to change pacing. *Why:* re-tuning the gap must stay one
   ffmpeg pass and zero API calls. Defaults: 900 ms inter-chunk, 3000 ms after a chapter
   title; 1200 ms is the documented upper end of the lever, 700 ms the lower.

7. **The model goes in the `model` HTTP header, not the msgpack body.** *Breaks if
   violated:* the server does not read a model id from the body, so you lose control
   over which model runs. The request still succeeds and still returns audio, so this
   fails silently rather than loudly — check the header first when output quality is
   wrong.

8. **Reference audio ships as raw bytes inside the msgpack map.** Not base64, not a
   data URL.

9. **Keys never reach stdout, logs, or the NDJSON stream.** `.env` is gitignored.
   Redact on any error path that echoes a request.

10. **`anthropic` and `openai` are lazy imports inside their adapters.** *Why:* importing
    either at module scope makes the whole app fail to start for a user who installed
    neither.

11. **No numpy, torch, or soundfile.** *Why:* the install has to finish in seconds on a
    weak box. Silence and wav headers are hand-built; everything else goes through
    ffmpeg.

12. **A failed chunk never kills the run.** Record it, continue, print failures at the
    end.

## Sampling-parameter warning for the Claude tagger

On `claude-opus-5`, `temperature`, `top_p`, and `budget_tokens` were removed — each
returns HTTP 400. Use `output_config={"effort": "low"}` instead (values: low/medium/high).
This is the mistake most likely to be reintroduced from training data or older code.

Also: refusals arrive as HTTP 200, not an exception — check
`response.stop_reason == "refusal"` before reading content.

## The Python↔Node contract

Python prints one NDJSON object per line to **stdout**; human-readable logs go to
**stderr**. Node parses lines and re-implements no logic. Either half must work with the
other absent — the CLI is fully usable with the server never started, and vice versa.

Example event:

```json
{"event":"chunk_done","chunk_id":"ch07_0012","i":12,"n":340,"latency_s":3.9,"concurrency":3}
```

| Event | Fields (beyond `event`) |
|---|---|
| `run_started` | `book`, `chapters` |
| `chunked` | `chapter_id`, `n_chunks` |
| `tagged` | `chapter_id`, `n_tagged`, `n_skipped` |
| `chunk_done` | `chunk_id`, `i`, `n`, `latency_s`, `concurrency` |
| `chunk_failed` | `chunk_id`, `i`, `n`, `error` |
| `concurrency_changed` | `from`, `to`, `reason` |
| `stitched` | `chapter_id`, `out_path`, `duration_s` |
| `done` | `book`, `chapters_done` |
| `error` | `stage`, `message` |

Adding a field is fine; renaming or removing one breaks the UI.

## Extension points

**Adding a TTS backend.** Implement the interface `fish_client.py` implements:
`synthesize(text, reference_bytes, reference_text, **opts) -> bytes`. A new backend must:
return audio bytes that are 44100/mono-normalisable, raise on an empty body (never treat
that as silence), and never be called for unspeakable text — that guard lives above the
backend, not inside it. Register the backend where `fish_client.py` is selected in
`narrate.py`.

**Adding a tagger backend.** Implement the `tagger/base.py` contract
`tag(batch: list[Chunk]) -> dict[chunk_id, str]`. Reuse the SHARED validator and the
shared JSON schema `{"items":[{"chunk_id":…,"tag":…}]}` — never write a backend-specific
validator. Register it in the `--tagger` CLI choices. Parity requirement: any tagger
backend must leave a user of that backend with the full feature, not a subset.

## Running the tests

```
pytest
```

One command, run from the repo root. No test may spend API credit — every backend
(Fish, Claude, OpenAI) is faked at the HTTP layer.

Acceptance tests:
- chapter-boundary detection
- no-sentence-split packing
- unspeakable-chunk-makes-no-API-call
- forced-429-degrades-to-1-and-finishes
- stitched-duration-within-100ms
- resume-regenerates-nothing
- over-long-tag-rejected-not-truncated
- tagger-parity

## Common request → file to touch

| Request | Touch |
|---|---|
| Change chunk size | `chunker.py` + `config.json` |
| Change the pause | `config.json` / `--gap-ms`, re-run `stitch` only, never `generate` |
| Chapter headings not detected | the chapter regex in `chunker.py` |
| Voice sounds wrong | `reference/`, re-run `prep-ref` (not a code change) |
| Add an emotion vocabulary word | the vocabulary list in `tagger/base.py` |
| TTS request failing | `fish_client.py`, check the `model` header first |
| Run is too slow / too many 429s | `pool.py` concurrency policy |
| UI shows nothing | the NDJSON contract, check the child's stdout is unbuffered |
| Output file wrong format | the final encode in `stitch.py` |
| Add a CLI flag | `narrate.py` plus the precedence chain: CLI flag > `.env` > `config.json` > default |

## Gotchas

- Windows path handling goes through `pathlib`, with forward slashes in any path handed
  to ffmpeg (including concat list-file entries).
- Node spawns the Python interpreter explicitly (e.g. `python narrate.py ...`) rather
  than relying on a shebang or `shell: true`.
- Python stdout must be unbuffered (`-u` or `flush=True` on every print) or the NDJSON
  stream stalls in the browser UI.
- `ffprobe` is a separate binary from `ffmpeg`; both must be on `PATH`.
- The silence-wav cache is keyed by gap length only. A stale `gap_*.wav` left over from a
  changed sample rate must be deleted manually before it gets reused.
