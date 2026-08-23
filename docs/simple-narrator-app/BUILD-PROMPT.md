# BUILD-PROMPT.md — Narrator: a self-narrated audiobook CLI

You are building **Narrator**, a small standalone Python CLI application that turns a
`.txt` novel into a full audiobook narrated in the user's own cloned voice, using the
hosted Fish Audio text-to-speech API. The pipeline is: read a `.txt` file → split it into
short (~200-character) chunks without ever breaking a sentence or packing across a
section break → clone the user's voice from a short reference recording → generate audio
for every chunk against the hosted Fish Audio API with adaptive concurrency → stitch the
per-chunk audio into per-chapter files with tuned inter-chunk silence → optionally expose
all of this through a thin local Node/Express server with a single static HTML control
page. It must run comfortably on a lightweight Windows machine with no GPU and no heavy
Python scientific stack.

> **Whichever agent you are:** build in the order the sections are numbered. Section 13
> lists acceptance tests — run them; do not report done until they pass. When finished,
> write `AGENTS.md` (Codex and Claude Code both read it) and a `CLAUDE.md` that points at
> it.

Sections 8–14 continue below with the remaining implementation detail (concurrency pool,
stitching, the CLI/server contract, config, and acceptance tests). Read the whole
document before writing code — later sections constrain earlier ones (in particular, the
manifest fields in §3 and the NDJSON event contract referenced later must match exactly).

## 1. Goal + hard requirements

Build exactly this, no more, no less, in this order of priority:

1. **Input**: a single `.txt` file containing a novel (front matter + chapters marked by
   standalone `Chapter N` lines).
2. **Chunking**: split the full text into chunks that target 200 characters, packed
   greedily from whole sentences. A chunk must **never break a sentence** and must
   **never pack across a section break** (a `***`/`---` divider, a Markdown heading, a
   run of blank lines, or a chapter boundary). See §3 for the exact algorithm.
3. **Voice cloning**: every chunk is spoken in the user's own voice, cloned zero-shot from
   a short reference recording the user supplies (`reference/narrator.wav`), via the
   **hosted** Fish Audio TTS API — no local model, no local GPU. See §5.
4. **Concurrency**: generate chunks concurrently, starting at concurrency **3**, with
   automatic degrade down to a floor of **1** when the API shows signs of strain
   (latency creep or errors), and optional opt-in ramp-back-up. See §8.
5. **Stitching**: assemble the per-chunk wav files into a per-chapter audio file with
   `ffmpeg`, inserting a tunable **900 ms** gap of silence between ordinary chunks. See
   §10 (silence generation and concat mechanics).
6. **Chapter titles**: a detected chapter title is synthesized as its own chunk — the
   chapter number spoken as words, e.g. "Chapter Seven. The Long Road." — followed by a
   longer **3000 ms** gap before the chapter body begins. See §4.
7. **Optional server wrapper**: a thin Node/Express server that shells out to this same
   Python CLI and streams its progress to a static HTML page. It is optional and it is
   thin — it adds no pipeline logic of its own.
8. **Target machine**: a light Windows machine — no GPU, modest RAM, possibly a slow
   disk. Every dependency choice must respect this (see §2).

**The Python CLI is the real application.** It must be fully installable, runnable, and
testable with `python narrate.py ...` alone, from a plain terminal, with the Node server
never started. The Node server is a convenience wrapper on top of it, nothing more —
it must add zero pipeline logic, so that the two halves work correctly with either one
absent: the CLI works with no `server/` directory present at all, and the server (once
built) works only by invoking the CLI as a subprocess and relaying its output, never by
reimplementing chunking, generation, or stitching itself.

## 2. Layout and dependencies

Lay out the project exactly like this:

```
narrator/
  narrate.py          # CLI: chunk | tag | generate | stitch | run | prep-ref
  chunker.py          # sentence-aware packing
  fish_client.py      # hosted Fish Audio TTS
  tagger/             # delivery tags (ON by default): base.py, claude.py, codex.py
  pool.py             # adaptive concurrency
  stitch.py           # ffmpeg assembly
  server/             # Node wrapper (index.js + one static HTML page)
  reference/          # narrator.wav + narrator.txt (user drops these in)
  out/<book>/<chNN>/  # chunk wavs, manifest.json, chapter mp3
  tests/              # the acceptance tests
  config.json / .env / README.md / AGENTS.md / CLAUDE.md
```

Use `pathlib.Path` everywhere for filesystem paths, and prefer forward slashes in any
path you print, log, or write into `list.txt` (ffmpeg concat demuxer file lists) — even
on Windows, forward slashes in these strings avoid escaping headaches. Windows has an
**8191-character command-line length limit**; keep this in mind for anything that builds
a single long command line (see §10 for why the concat filter is banned here).

**Python dependencies: `msgpack`, `httpx`, `python-dotenv`. Nothing else in core.**
Specifically: **no `numpy`, no `torch`, no `soundfile`.** The reason is deployment
speed, not code elegance — this app must `pip install` in seconds on a weak, possibly
offline-adjacent Windows box, and none of those three heavy packages are needed:
silence generation is a hand-built 44-byte WAV header (§5), audio manipulation is
delegated entirely to the external `ffmpeg`/`ffprobe` binaries, and TTS inference is a
remote HTTP call. Every time you're tempted to reach for `numpy` to manipulate raw PCM,
stop and either write it by hand or shell out to ffmpeg instead.

`anthropic` and `openai` are **optional extras**, needed only by the optional delivery-tag
feature (§6). Each is imported **lazily, inside its adapter module, and never at module
scope** — `tagger/claude.py` does `import anthropic` inside its functions, not at the top
of the file, and the same for `tagger/codex.py` and `openai`. This means a user who never
installs the tagger extras can still run `chunk`, `generate`, and `stitch` without either
package present. Tagging defaults to `auto` (§6.0), which resolves to *untagged* when no
LLM key is set, so the no-extras install still runs end to end; an import error can only
surface once a user has actually supplied a key or forced `--tagger claude`/`--tagger codex`.

`ffmpeg` and `ffprobe` on `PATH` are the **only binary dependency**. Check for both at
startup (`shutil.which`) and fail with a clear, actionable message if either is missing —
do not let a missing binary surface as a cryptic subprocess error three steps into a run.

Node dependencies: `express`, and **only** `express`. No framework, no bundler, no
templating engine — one route file and one static HTML page is the entire surface area.

## 3. Chunking spec

Chunking is the single most failure-prone part of this app to get "close enough" wrong.
Transcribe the following exactly; do not improvise a simpler splitter.

**Size bounds** (all in characters, all must appear as named constants):

| constant | value | meaning |
|---|---|---|
| `target_chars` | 200 | soft target — a chunk closes as soon as it reaches this |
| `max_chars` | 300 | hard cap — a chunk must never exceed this by packing |
| `min_chars` | 60 | below this, a chunk is a merge candidate (§3.4) |
| `hard_split_chars` | 600 | only past this does a single sentence get split at all |

**3.1 Sentence boundary detection.** `re.split(r'[.!?]')` is the standard wrong answer —
it shatters `U.S.`, `3.5`, and `"Stop!" she cried.` into garbage fragments. Use this
walk instead:

```python
import re

_SENT_END = re.compile(r"[.!?]+[\"'”’\)\]]*")
_OPENERS = "\"'“‘([“‘"

def split_sentences(text: str) -> list[str]:
    sentences = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        rest = text[end:]
        if rest and not rest[0].isspace():
            # e.g. "U.S." or "3.5" — punctuation immediately followed by
            # a non-space character is not a sentence boundary
            continue
        nxt = rest.lstrip()
        if nxt == "" or nxt[0].isupper() or nxt[0] in _OPENERS:
            # end of text, or the next real character starts a new
            # sentence (capital letter or an opening quote/bracket)
            sentences.append(text[start:end].strip())
            start = end
        # else: not a boundary — e.g. mid-sentence "Mr. Smith" where the
        # next word is lowercase; keep accumulating
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences
```

This is why `"Stop!" she cried.` survives as one sentence (the character after the
closing quote is lowercase `s`, not a boundary) while `U.S. Government` does not split at
the first period (immediately followed by a non-space capital `G`... more precisely,
immediately followed by a non-space character at all, which is the `rest[0].isspace()`
guard) — no characters are ever dropped by this walk.

**3.2 Greedy packing loop.** Once you have a flat list of sentences for a packable unit
(see §3.3 for what a "unit" is), pack them greedily:

```python
def pack(sentences: list[str], target_chars=200, max_chars=300) -> list[str]:
    chunks = []
    buf: list[str] = []
    bc = 0  # buffered char count for the chunk under construction

    def flush():
        nonlocal buf, bc
        if buf:
            chunks.append(" ".join(buf))
        buf = []
        bc = 0

    for sent in sentences:
        add_chars = len(sent) + (1 if buf else 0)  # +1 for the joining space
        if buf and (bc + add_chars > max_chars):
            # HARD cap would be exceeded — flush what we have FIRST,
            # then start a fresh chunk with this sentence
            flush()
            add_chars = len(sent)
        buf.append(sent)
        bc += add_chars
        if bc >= target_chars:
            # SOFT target reached — close now rather than waiting for
            # the hard cap, so chunks cluster near target_chars instead
            # of piling up at max_chars
            flush()
    flush()
    return chunks
```

Chunks join with a single space. The distinction that matters: the **hard cap check
flushes before appending**, so a chunk is never pushed over `max_chars`; the **soft
target check flushes after appending**, so a chunk is allowed to land anywhere between
roughly `target_chars` and `max_chars` and simply stops early once it's "big enough."
This is what keeps the size distribution clustered near 200 instead of every chunk
crawling all the way to 300.

A single sentence longer than `max_chars` becomes its own chunk and is **flagged**
(`over_cap = true` in the manifest, §3.6) rather than being split — splitting a sentence
mid-clause damages meaning and prosody for a marginal case. Only past
`hard_split_chars = 600` do you split it at all, and even then split at the **nearest
clause punctuation** (a comma, semicolon, or colon nearest the midpoint) rather than at
an arbitrary character offset, so the two halves still read as plausible spoken
fragments.

**3.3 Section and chapter boundaries — never pack across them.** Before sentence
splitting and packing, segment the raw chapter text into **packable units** at every:

- section-break marker line: a line that is only `***`, `---`, or similar (regex like
  `^\s*[\*\-—]{3,}\s*$`)
- Markdown heading line (`^\s*#{1,6}\s`)
- run of 2+ consecutive blank lines (treat as a soft section break, same rule)
- chapter boundary (handled separately in §4 — a chapter's chunks never merge into the
  next chapter's)

A chunk is never allowed to span two of these units. This is a hard rule with no
exceptions, because a spoken narration that runs a scene break straight into the next
scene without a pause reads as a continuity error, not just a stylistic one.

**Ordinary paragraph breaks are different — packing MAY cross them.** Do not treat every
blank line between paragraphs as a hard boundary. At a 200-character target, refusing to
pack across paragraph breaks would shatter short-paragraph dialogue (a common pattern:
one line of dialogue per paragraph) into a flood of tiny one-line chunks, each incurring
its own gap and its own API call. Instead: packing is allowed to continue across a single
paragraph break, but once a chunk has reached `min_chars`, prefer to close it at the next
paragraph boundary rather than packing further — i.e., treat a paragraph boundary as a
preferred-but-not-mandatory close point once the chunk is already "big enough" to stand
on its own. Record which kind of boundary a chunk actually ended on (see §3.6); this data
is what a future boundary-aware gap refinement would read.

**3.4 Small-chunk merge (fixed point).** After packing, sweep for chunks under
`min_chars` and fold them into an adjacent chunk:

- A chunk below `min_chars` merges into whichever **neighbour is smaller** (prefer the
  smaller of the two adjacent chunks — this keeps the resulting sizes more even than
  always picking, say, the left neighbour).
- Only merge if the combined result still fits under the hard cap:
  `fits(a, b) = len(a) + 1 + len(b) <= max_chars`.
- This is **not a single pass**. Run it as `while changed and len(out) > 1: ...`,
  rescanning from the top every time a merge happens, until a full pass produces no more
  merges (a fixed point). One pass is not enough: merging chunk 5 into chunk 4 can make
  chunk 4 itself newly eligible to merge into chunk 3.
- Why this matters: without it, orphan fragments — a two-word chapter tail, a short
  interjection stranded before a long sentence, a solitary "Yes." left dangling by a
  section-break split — render as isolated blips of near-silence or click artifacts at
  the TTS layer instead of reading naturally.

**3.5 Chunk kinds.** A packable unit produces `kind="body"` chunks. The chapter title
line (§4) produces exactly one `kind="title"` chunk, generated and gapped differently
from body chunks — never merge a title chunk with adjacent body chunks in §3.4.

**3.6 Manifest fields — every chunk record must carry exactly these fields:**

- `chunk_id` — stable id, e.g. `ch07_0012`
- `position` — integer order within the chapter
- `text` — the chunk's exact text (pre-tag; see §6 for how a tag composes with this)
- `char_count` — `len(text)`
- `text_hash` — `sha256("[tag] text")` if a tag is applied, else `sha256(text)`; **the
  hash must cover the applied tag**, not just the raw text — otherwise a re-tag silently
  reuses stale audio on a resumed run, because the resumability check only looks at the
  hash (see §6 and §9)
- `kind` — `"title"` or `"body"`
- `boundary` — `"ends_section"`, `"ends_paragraph"`, or `"mid_paragraph"` — records why
  this specific chunk ended where it did, whether or not you act on it yet; this is the
  hook a future `--mid-paragraph-gap-ms` refinement reads, and it must be populated even
  though the stitcher currently applies one flat gap everywhere (§10)
- `over_cap` — boolean, true only for the flagged single-sentence-over-`max_chars` case
  in §3.2

## 4. Chapter detection + the title chunk

**4.1 Detecting chapters.** Scan the input line by line for a standalone chapter-heading
line matching:

```
^\s*Chapter\s+(\d+(?:\.\d+)?)\s*[:\-–—]?\s*(.*)$
```

Group 1 is the chapter number (integer or `N.N`); group 2, if non-empty, is the chapter
title text. Everything before the first match is **front matter** and becomes its own
pseudo-chapter, `ch00`. Everything from one match up to (but not including) the next
match is that chapter's body.

**4.2 Chapter id derivation.**

```python
_CHAPTER_NUM_RE = re.compile(r"chapter\s+(\d+(?:\.\d+)?)", re.IGNORECASE)

def chapter_id(num_str: str) -> str:
    if "." in num_str:
        whole, frac = num_str.split(".", 1)
        return f"ch{int(whole):02d}_{frac}"
    return f"ch{int(num_str):02d}"
```

`Chapter 7` → `ch07`. `Chapter 7.5` → `ch07_5`. Text with no chapter heading at all (or
text before the first heading) → `ch00`.

**4.3 The title chunk.** The detected chapter heading is not folded into the first body
chunk. It becomes its **own** chunk, `kind="title"`, and it is spoken with the chapter
number **spelled out as words**, not read as a bare numeral — TTS engines read a lone
digit "7" inconsistently (sometimes "seven," sometimes spelling out "seven-period," in
one heard case dropping it silently). For chapter title `"Chapter 7: The Long Road"`,
the spoken text is:

```
"Chapter Seven. The Long Road."
```

For a fractional chapter like `Chapter 7.5`, spell it as `"Chapter Seven Point Five."`
— literally the word "Point" between the two spelled-out numbers, mirroring how a person
reads a decimal aloud.

Write a small local helper for this — do **not** add a number-to-words dependency for
it. Chapter numbers in a novel are small integers (essentially always under 200); a
lookup-table-plus-simple-composition function for `ones`/`teens`/`tens`/`hundreds` is a
dozen lines and covers every real case:

```python
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
          "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty",
         "ninety"]

def int_to_words(n: int) -> str:
    if n < 10:
        return _ONES[n] or "zero"
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    prefix = f"{_ONES[hundreds]} hundred"
    return prefix if rest == 0 else f"{prefix} {int_to_words(rest)}"

def chapter_number_words(num_str: str) -> str:
    if "." in num_str:
        whole, frac = num_str.split(".", 1)
        return f"{int_to_words(int(whole))} point {int_to_words(int(frac))}"
    return int_to_words(int(num_str))
```

If the chapter title carries no subtitle text (group 2 empty), speak just
`"Chapter Seven."`.

**4.4 Gap after the title.** The title chunk is followed by the **3000 ms** title gap,
not the standard 900 ms inter-chunk gap — a deliberately longer pause that reads as a
chapter break to the listener rather than a mid-scene beat. See §5 for how gap wavs are
generated and §10 for how they're spliced into the stitch.

## 5. Fish Audio client — the exact wire format

This is the part of the app that is worth the most care to get right on the first try,
because a wrong field silently produces wrong audio rather than an error.

**5.1 Endpoint and headers.**

```
POST https://api.fish.audio/v1/tts
```

Three headers, exactly:

| header | value |
|---|---|
| `Authorization` | `Bearer <FISH_API_KEY>` |
| `Content-Type` | `application/msgpack` |
| `model` | the model id string |

**The model id goes in a header literally named `model` — not in the request body.**
This is the single most common mistake when calling this API. A `"model"` key placed in
the msgpack body is not where the server reads it from, so you lose control over which
model actually runs — and since the request still succeeds and still returns audio, the
mistake does not announce itself. If output quality is not what your selected model
should produce, check this header first. Accepted model ids: `s2.1-pro-free` (default — free tier of
S2.1 Pro), `s2-pro`, `s1`, `speech-1.5`. Make the model id a config value defaulting to
`s2.1-pro-free`.

**5.2 Body — msgpack, with RAW reference bytes.** The body is a msgpack-encoded map, not
JSON. In Python: `msgpack.packb(body, use_bin_type=True)`. The reference audio goes into
the map as **raw bytes** — not base64-encoded, not a data URL, just the literal bytes of
the wav file read straight off disk.

```python
body = {
    "text": text,  # verbatim string; a leading "[tag] " marker (§6) must survive as-is
    "references": [
        {"audio": reference_wav_bytes, "text": reference_transcript},  # transcript may be ""
    ],
    "format": "wav",
    "sample_rate": 44100,
    "normalize": True,
    "temperature": 0.7,
    "top_p": 0.7,
    "chunk_length": 300,
}
```

Field notes:

- `chunk_length` clamps to the range 100–300 (the API's own default is 200). Use **300**
  — at 300, a ~200-character chunk is small enough to render in one uninterrupted
  generation pass instead of being internally re-split by the API.
- `temperature` clamps to 0–2, `top_p` clamps to 0–1. Use **0.7 / 0.7** — these are the
  tuned defaults for narration specifically (a more expressive dialogue-heavy use case
  would run hotter, around 0.85/0.8, but that is not this app: this is a single-narrator,
  mostly-uninflected read, and 0.7/0.7 is calmer and more consistent take over take).
- `references` is a list because the API supports multiple reference clips, but this app
  always sends exactly one — the user's single narrator recording (§7). Voice comes
  **only** from this reference clip (zero-shot cloning); there is no separate
  voice-model-id parameter to set, and you must never mix in a Fish Audio stock-voice id.
- Emotion/delivery direction has **no separate API field**. The only channel for it is a
  leading `[bracket]` marker prepended to `text` and passed through verbatim — see §6 for
  the validated, capped version of this app builds, and the failure mode below for why it
  must be short.

Compact client sketch:

```python
import httpx
import msgpack

FISH_URL = "https://api.fish.audio/v1/tts"

def synthesize(text: str, api_key: str, model_id: str,
               reference_audio: bytes, reference_text: str,
               timeout_s: float = 180.0) -> bytes:
    body = {
        "text": text,
        "references": [{"audio": reference_audio, "text": reference_text}],
        "format": "wav",
        "sample_rate": 44100,
        "normalize": True,
        "temperature": 0.7,
        "top_p": 0.7,
        "chunk_length": 300,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/msgpack",
        "model": model_id,
    }
    resp = httpx.post(FISH_URL, headers=headers,
                       content=msgpack.packb(body, use_bin_type=True),
                       timeout=timeout_s)
    if resp.status_code // 100 != 2:
        # non-2xx: the response TEXT carries the real reason — surface it, don't
        # swallow it into a generic "HTTP error"
        raise RuntimeError(f"Fish TTS {resp.status_code}: {resp.text}")
    if len(resp.content) == 0:
        # a zero-length 2xx body is a FAILURE, not silence — never write it to disk
        raise RuntimeError("Fish TTS returned an empty body")
    return resp.content
```

The response body **is** the audio bytes directly — there is no JSON envelope to unwrap
on success. A non-2xx response's response **text** carries the actual failure detail
(bad reference audio, invalid model id, rate limit, etc.) — always read and surface it
rather than just the status code. A **zero-length body on a 2xx** is a failure condition,
not "the model produced silence" — raise an error, never write an empty or near-empty
file to disk as if it were valid audio.

**5.3 Failure mode 1 — unspeakable chunks.** Some chunks, after chunking and merging,
end up with no actual speakable content — an isolated `"."`, a stray `"***"` that
survived section splitting, a chunk that is pure punctuation. Guard against sending
these to the API at all:

```python
import re

def is_speakable(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text))
```

If `is_speakable(text)` is `False`, **never call the API for this chunk.** Instead,
write a locally generated silent wav in its place and keep the chunk's position in the
manifest and in the stitch order — the pipeline must not skip the slot, only skip the
network call. The real-world consequence of skipping this guard is not a clean error: an
unspeakable chunk sent to the API has produced **up to roughly 47 seconds of grunting or
noise** in place of the fraction of a second that chunk should occupy, silently bloating
and corrupting the finished chapter file.

Build the silent wav by hand — a 44-byte RIFF/WAVE header followed by zeroed 16-bit PCM
sample data, default duration 200 ms, sample rate 44100, mono. No audio library is
required for this:

```python
import struct

def write_silent_wav(path, duration_ms: int = 200, sample_rate: int = 44100) -> None:
    n_samples = int(sample_rate * duration_ms / 1000.0)
    data_bytes = n_samples * 2  # 16-bit mono
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_bytes))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                             sample_rate * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", data_bytes))
        f.write(b"\x00" * data_bytes)
```

(Note the header is `RIFF` + size + `WAVEfmt ` + fmt-chunk fields + `data` + size — 44
bytes total before the zeroed sample data, hence "44-byte RIFF header.")

**5.4 Failure mode 2 — direction leaking into speech.** If a delivery marker prepended to
`text` is long or free-form (a sentence of stage direction rather than a short tag word),
the TTS model does not treat it as silent direction — **it speaks the direction out
loud, verbatim, as if it were dialogue.** This has actually happened in a related system:
roughly **6.3 minutes** of finished narration turned out to be the model reading its own
stage directions aloud before anyone caught it, because the marker text was too long and
too sentence-like for the model to recognize as a non-speech instruction. The only
defense is keeping markers short and tightly validated — this is fully specified in §6;
build the delivery-tag feature to that validator exactly, with no looser path anywhere
in the pipeline that lets an unvalidated string reach `[bracket] text`.

## 6. Delivery tags — ON by default, Claude AND Codex at full parity

Build a `tag` stage in `narrate.py`, run between `chunk` and `generate`:
`narrate.py tag --tagger auto|none|claude|codex`, **default `auto`.**

**This stage is on by default and it is the recommended path.** In a narrator-only
pipeline — one voice, no cast, no dialogue-attribution map — a short delivery tag
prepended to the text (`[weary] ...`) is the **only** expressive lever available at all.
Without it every chunk reads in exactly the same flat register regardless of what's
happening in the scene: a death and a breakfast are delivered identically. The TTS is
perfectly capable of grief, urgency, and dry amusement — a tag is the only channel
through which it can be told which one this passage needs. An untagged book is the same
book read by someone who hasn't read it.

That is why this defaults on rather than off. It is a real, audible quality difference in
the finished audio, not a nicety.

**6.0 `auto` resolution — and the recommendation when it can't run.** `auto` picks a
backend from what the environment actually has, in this order:

1. `ANTHROPIC_API_KEY` set → use the Claude adapter (§6.2).
2. else `OPENAI_API_KEY` **and** `OPENAI_TAG_MODEL` set → use the Codex adapter (§6.3).
3. else → proceed **untagged**, and print a prominent recommendation to stderr.

Step 3 is what keeps the app runnable with nothing but a Fish key. It must **degrade, not
fail** — a user who hasn't set up an LLM key still gets their audiobook. But it must not
degrade quietly:

```
NOTE: generating UNTAGGED. Every chunk will be read in the same flat register.
      For markedly more natural delivery - emotional cadence matched to each
      passage - set one of these and re-run:
        ANTHROPIC_API_KEY=...                       (uses claude-opus-5)
        OPENAI_API_KEY=... and OPENAI_TAG_MODEL=...  (uses your OpenAI account)
      Re-tagging only regenerates chunks whose tag changed. See README "Emotion tags".
```

`--tagger none` is the explicit opt-out and **must silence that recommendation** — a user
who has decided is not nagged on every run. `--tagger claude` or `--tagger codex` forces a
specific backend and **fails loudly** if its key is missing, rather than silently falling
back: an explicit choice that cannot be honoured is an error, not a downgrade.

Emit the resolved backend in the `run_started` event and on stderr, so it is never a
mystery which path a given run took.

**Because this is now the default path, §6.4's validator is on the critical path too.**
Every default run sends model-authored text into the TTS input. The validator is the only
thing standing between that and §5.4's failure mode. Build it first and build it strictly.

**6.1 Shared contract, backend-independent (`tagger/base.py`).** Both backends implement
the same function signature and are driven identically by the rest of the pipeline:

```python
def tag(batch: list[Chunk]) -> dict[str, str]:
    """Returns {chunk_id: tag_string} for chunks the backend successfully tagged.
    Chunks it failed or declined to tag are simply absent from the returned dict."""
```

Both backends receive the same system guide describing the vocabulary and rules below,
and both are asked to return the same JSON shape:

```json
{"items": [{"chunk_id": "ch07_0012", "tag": "weary"}, ...]}
```

Both backends' raw output is passed through the **same validator** (§6.4) before a tag
is accepted — validation logic lives once, shared, not duplicated per adapter.
Everything backend-specific (the API client, auth, model-id handling, retries, typed
exceptions) stays inside that backend's own adapter file. **A user who only has Codex
available and never touches the Claude adapter must lose zero functionality** — every
tag-stage feature (validation, review file, resumability via `text_hash`) must work
identically regardless of which adapter produced the tags.

**6.2 Claude adapter (`tagger/claude.py`).**

- Model id: `claude-opus-5`. Expose `--tag-model` to override it. `claude-haiku-4-5` is
  the cheap alternative — name it in `--help` text and in the README, but leave the
  choice to the **user**, not a hardcoded fallback the builder picks unasked.
- **`temperature`, `top_p`, and `budget_tokens` were removed on this model generation.
  Sending any of them returns HTTP 400.** This is flagged specifically because it is the
  single most likely mistake carried in from older code or training data that still
  assumes sampling params are always accepted — do not set them, do not expose CLI flags
  for them.
- Effort control instead: pass `output_config={"effort": "low"}` on the request. If tags
  come back generic or repetitive in testing, raise this to `"medium"` — leave it
  configurable, but ship `"low"` as the default (short output, cheap, this task doesn't
  need deep reasoning).
- Use structured output, not prose-scraping:
  `client.messages.parse(..., output_format=TagBatch)`, reading the result off
  `response.parsed_output`. Define the pydantic shape:

  ```python
  from pydantic import BaseModel

  class TagItem(BaseModel):
      chunk_id: str
      tag: str

  class TagBatch(BaseModel):
      items: list[TagItem]
  ```

  Do not attempt to parse JSON out of free-text content — `parsed_output` is the
  contract.
- Use prompt caching on the stable system block: mark it with
  `cache_control={"type": "ephemeral"}`. It needs to be **at least 1024 tokens** to
  actually cache — a short system prompt below that floor will not cache no matter how
  it's marked, so make sure the shared tagging guide (vocabulary, rules, examples) is
  substantial enough to clear it. Verify caching is actually working by checking
  `usage.cache_read_input_tokens` across consecutive batches in the same run; if it stays
  at zero across batches that should be hitting a warm cache, the diagnosis is that
  something **volatile leaked into the cached prefix** — an embedded timestamp, an
  unsorted dict whose key order varies, a per-call random id — breaking the exact-prefix
  match caching requires. Find and remove the volatile element rather than accepting the
  cache miss.
- **Refusals arrive as a normal HTTP 200, not an exception.** Check
  `response.stop_reason == "refusal"` **before** attempting to read `parsed_output` or
  any content off the response. A refused batch should behave like a failed batch (empty
  dict returned, logged, chunks proceed untagged) — do not let it crash on unexpected
  content shape.
- Enable the beta server-side fallback so a transient overload on the primary model
  doesn't hard-fail the batch: `betas=["server-side-fallback-2026-07-01"]` and
  `fallbacks="default"` on the `client.beta.messages.*` call path.
- Catch typed exceptions **most-specific first**: `RateLimitError`, then
  `APIStatusError`, then `APIConnectionError`, with a final broad `except Exception` as
  the last resort. On any of these, retry per §6.3's retry policy before giving up on
  the batch.
- `anthropic.Anthropic()` reads `ANTHROPIC_API_KEY` from the environment on its own —
  do not thread the key through by hand.

**6.3 Codex / OpenAI adapter (`tagger/codex.py`).**

- **The model id is a required config value with no default: `OPENAI_TAG_MODEL`.** Do
  not invent a hardcoded fallback model id here, even a plausible-looking one. The
  reasoning that must actually go into the code and the README: OpenAI model ids churn
  over time, and a stale hardcoded default doesn't fail loudly at build time — it fails
  months later, at runtime, as a confusing HTTP 404 that looks like an auth or network
  problem rather than what it actually is (a decommissioned model id). Fail immediately
  and clearly if `OPENAI_TAG_MODEL` is unset: name the exact environment variable in the
  error message, and point the user at `client.models.list()` as how to discover valid
  current ids for their account.
- `openai.OpenAI()` reads `OPENAI_API_KEY` from the environment on its own.
- **The exact structured-output/JSON-schema parameter name and call shape for the
  OpenAI SDK were deliberately not verified for this document.** Before writing this
  adapter, confirm the current structured-output parameter name and call shape against
  OpenAI's live, current documentation — do not trust recall or training data for this
  one call shape. This is the same discipline applied to the Fish `model` header in §5,
  with the opposite conclusion: that one was hand-verified this session and is safe to
  transcribe literally; this one was not, and must be independently re-checked before it
  ships.
- Mirror the Claude adapter's retry behavior and typed-error handling (most-specific
  exception types first, then a broad fallback), and mirror its failure contract: **on
  any failure the batch yields an empty tag dict, the failure is logged, and generation
  proceeds untagged** rather than blocking the pipeline. Tagging is enhancement, never a
  hard dependency of the generate stage.

**6.4 Tag validation — mandatory, and this is where the feature turns into the bug in
§5.4 if skipped.** Every tag from either backend passes through one shared validator
before it is accepted:

- **≤ 32 characters.** This app's cap is deliberately well under the ~64-character
  threshold where speech-leaking becomes likely (§5.4) — short tags are what the model
  reliably absorbs as a direction to perform rather than text to read aloud.
- Must match `^[a-z][a-z ,-]*$` — lowercase words only, spaces/commas/hyphens allowed as
  separators, **no other punctuation, and never a full sentence.**
- Reject a tag that contains words lifted verbatim from the chunk's own text — this
  catches the model paraphrasing or summarizing the line instead of directing its
  delivery (e.g. tagging a chunk about rain as `"raining hard"` because "rain" is in the
  source text, rather than an actual delivery instruction).
- Check the tag against a shipped vocabulary of roughly thirty allowed words/phrases.
  Ship (at minimum) this list: `weary`, `urgent`, `whispered`, `bitter amusement`,
  `cold`, `grieving`, `wry`, `awed`, `flat`, `mocking`, `tender`, `resigned`, `furious`,
  `pleading`, `hushed`, `defiant`, `bewildered`, `wistful`, `stern`, `playful`,
  `hesitant`, `triumphant`, `bleak`, `warm`, `sardonic`, `anxious`, `reverent`,
  `exhausted`, `menacing`, `gentle`. Reject anything not on this list (or a small,
  explicitly-allowed set of comma-joined combinations of listed words, if you choose to
  support that — but do not accept arbitrary free text just because it happens to match
  the regex).
- **A failing tag is dropped and logged; the chunk generates untagged. Never truncate a
  bad tag down into something that merely looks valid** — truncation can turn a rejected
  36-character phrase into a 32-character prefix that passes the length check while
  still being nonsense, or worse, still being a fragment of leaked direction. Reject
  outright; do not repair.

**6.5 Wiring into generation.**

- A validated tag is applied at generate time as exactly `f"[{tag}] {text}"` — nothing
  more elaborate. Never expand a tag into a persona description, and never construct a
  sentence of direction around it (that is precisely the failure mode in §5.4). The
  bracketed word is the entire delivery instruction.
- Because a tag changes what is actually sent to the TTS API, it must change the chunk's
  identity for resumability purposes: `text_hash = sha256(f"[{tag}] {text}")` when a tag
  is applied (plain `sha256(text)` when it is not — see §3.6). This is what makes
  re-tagging behave correctly: a re-run after editing `tags.json` regenerates **exactly**
  the chunks whose tag actually changed, and leaves every other chunk's cached audio
  alone, because only their hashes changed.
- Human-in-the-loop review is part of the contract, not an afterthought: the `tag` stage
  writes `tags.json` — an array of `{chunk_id, text_preview, tag}` records — so a human
  can hand-edit any tag before it's ever used to spend Fish Audio credits.
  `narrate.py tag --tags-review` runs the tagging stage and then **stops the pipeline
  there**, before any TTS generation happens, specifically so the user can open
  `tags.json`, fix or clear tags they disagree with, and only then run `generate`.
- State plainly, in the README and the config comments, that tagging is a **second API
  bill layered on top of Fish Audio** — small per call (short cached system prompt, tiny
  structured output), but real money. Because this now defaults **on**, that disclosure
  is not optional fine print: a user who sets an `ANTHROPIC_API_KEY` for some other
  purpose must not discover this pipeline spending it by surprise. Name the cost in the
  `run_started` stderr line ("tagging via claude-opus-5 — this bills your Anthropic
  account"), and document `--tagger none` as the one-flag opt-out everywhere tagging is
  mentioned.

## 7. Reference audio handling

The user's voice reference lives at `reference/narrator.wav`, with an optional sibling
`reference/narrator.txt` holding its transcript. An empty transcript (`""`) is legal and
accepted by the Fish API — but cloning quality is measurably better with a transcript
present, so the README should encourage supplying one even though the pipeline works
without it.

Provide a `prep-ref` subcommand that converts an arbitrary input recording into a
conformant reference clip:

```
ffmpeg -i <input> -ar 44100 -ac 1 -c:a pcm_s16le reference/narrator.wav
```

trimmed to **≤ 30 seconds** (use ffmpeg's `-t 30` or trim to a user-supplied
`--start`/`--duration` window), matching the constraints in §5's reference-audio
requirements (44100 Hz, mono, 16-bit PCM). Add an optional `--clean` flag that runs a
light denoise/cleanup filter chain before the final encode:
`highpass=f=80,afftdn` (a highpass to remove low-frequency rumble, followed by FFmpeg's
built-in noise reduction filter) — useful for a reference recorded on a phone or a noisy
room mic.

**Read the reference audio bytes exactly once, at process startup, and reuse the same
in-memory `bytes` object for every single Fish API call in the run** — do not re-open
and re-read the wav file per chunk. A 30-second 44.1kHz mono 16-bit wav is a small
amount of memory to hold for the run's duration, and re-reading it from disk hundreds of
times per chapter on a "light Windows machine" (§1) with a potentially slow disk is pure
wasted I/O for zero benefit; the bytes never change mid-run.

## 8. Adaptive concurrency (`pool.py`)

Build a fixed pool of `N` worker coroutines (`asyncio`) that pull chunk jobs off one
ordered queue. Each worker writes its result into a results array/dict keyed by the
chunk's index, not by arrival order. **Completion order must never affect output
order** — a slow chunk 12 finishing after chunk 40 must still land at position 12 in
the stitched output. The stitcher reads the manifest by chunk index, never by the
order results arrived in.

`target_concurrency` starts at **3**. The pool itself is sized at the maximum you'll
ever allow (e.g. 3, matching the starting target — you never need more workers alive
than the ceiling), and shrinking the effective concurrency does NOT cancel or kill a
worker that's mid-request. Instead, give every worker an index `0..N-1` and an
`asyncio.Event` per index. Before a worker pulls its next job, it checks: is my index
< `target_concurrency`? If yes, pull and run. If no, `await` its park event. When
`target_concurrency` is raised, set the events for the newly-eligible indices; when
lowered, just stop setting the event for a retired index — a worker that's already
running a job finishes that job (it doesn't re-check target mid-request) and only
parks on its *next* iteration. This is how a fixed pool shrinks without cancelling
work already in flight.

```python
import asyncio

class AdaptivePool:
    def __init__(self, max_workers: int, target: int = 3):
        self.target = target
        self.events = [asyncio.Event() for _ in range(max_workers)]
        self._sync_events()

    def _sync_events(self):
        for i, ev in enumerate(self.events):
            if i < self.target:
                ev.set()
            else:
                ev.clear()

    def set_target(self, n: int):
        self.target = max(1, n)   # floor is 1
        self._sync_events()

    async def worker(self, idx: int, queue: "asyncio.Queue"):
        while True:
            await self.events[idx].wait()   # park here if idx >= target
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await run_job(job)               # may itself call set_target() on failure
            queue.task_done()
```

**Baseline:** the median latency of the first 5 *successful* calls in the run. Compute
it once and freeze it; all later degrade/ramp decisions compare against this frozen
baseline, not a rolling one.

**Degrade** one step (3→2→1) when EITHER:
- the median of the last 5 completed-call latencies exceeds `baseline * 1.75`, OR
- any call returns 429, a 5xx, or times out.

A 429 degrades **immediately**, regardless of the latency median. **Floor is 1** —
never degrade to 0; a stuck run is worse than a slow one.

**Recovery is opt-in** via `--ramp-up` (default **off**). When enabled: after 10
consecutive fast successes (latency at or under baseline, no errors), step
concurrency back up by one, capped at the original starting value of 3. Without the
flag, once degraded, the run stays degraded — this is deliberate, not a missing
feature: a run that already hit trouble should stay conservative rather than
oscillate.

**Retries:** 3 attempts per chunk, backoff 2 s → 4 s → 8 s. If the response carries a
`Retry-After` header, sleep that long instead of the computed backoff. A chunk that
exhausts all 3 attempts is recorded as **failed** in the manifest (not written as
silence, not aborting the run) — **the run continues** to the remaining chunks, and
every failure is collected and printed in a summary at the end. A 400-chunk book must
not die because chunk 214 hit a transient 502.

**Timeout:** 180 s per request (Fish TTS can be slow at longer `chunk_length`
settings; do not set this lower).

**Courtesy delay:** insert a short fixed delay between the start of consecutive calls
(e.g. 250–500 ms) even at full concurrency, so a free-tier API key is never hammered
with a burst. This is independent of the adaptive logic — it applies at every
concurrency level.

**Logging:** every concurrency state change — degrade or ramp-up — logs exactly one
line to stderr in this shape:

```
concurrency 3 → 2 (median 8.4s vs baseline 3.9s)
```

or, for an immediate 429 degrade:

```
concurrency 3 → 2 (429 received)
```

## 9. Resumability

Every chapter directory `out/<book>/<chNN>/` contains a `manifest.json` recording, per
chunk: its `chunk_id`, `text_hash` (see §3 — this hash is computed over the
tag-decorated text, `sha256("[tag] text")`, so a re-tagged chunk gets a new hash), and
the path to its wav file.

On a re-run, before dispatching a chunk to the pool, check: does the wav file exist on
disk, AND does the manifest's recorded `text_hash` for that chunk match the hash of
the chunk's current (possibly re-tagged) text? If both hold, **skip it** — no API
call, no pool slot consumed. `--force` bypasses this check entirely and regenerates
every chunk.

This is not an optimization, it's a reliability requirement: a 400-chunk book run must
never restart from zero because of one network blip, one rate-limit exhaustion, or the
user closing the terminal. Cross-reference §3 explicitly: because `text_hash` covers
the *applied* delivery tag and not just the raw sentence text, changing the tagger
output for a chunk (re-tagging it, or turning tagging on/off) correctly invalidates
that chunk's cache entry and forces regeneration — a re-tagged chunk must never
silently keep serving audio generated under the old tag.

Write the manifest **incrementally** — update and persist it to disk after each
individual chunk completes (success or terminal failure), not only once at the end of
the chapter. A crash, kill, or power loss partway through a chapter must lose at most
the one chunk that was in flight, never the whole chapter's progress. Use an
atomic-ish write (write to `manifest.json.tmp`, then replace) so a crash mid-write of
the manifest file itself never corrupts it.

## 10. ffmpeg stitching (`stitch.py`)

**Normalize on arrival.** The instant a wav comes back from Fish Audio (or is
generated locally as silence for an unspeakable chunk), re-encode it to
44100 Hz / mono / `pcm_s16le` and save that as the chunk's stored wav. Do this before
it's ever written to the manifest as done. This makes every segment in a chapter
byte-compatible with every other, which is the exact precondition the concat demuxer
needs (see below) — normalize-on-arrival and the demuxer choice are one decision, not
two.

**Silence is pre-rendered once per distinct gap length and cached on disk.** Don't
regenerate the same 900 ms silence file 300 times in a chapter. The literal command:

```
ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 0.9 -c:a pcm_s16le gap_900.wav
```

Pass the duration as seconds-as-float (`ms / 1000.0`). Name cached gap files by their
millisecond value (`gap_900.wav`, `gap_3000.wav`) and check for existence before
re-rendering.

**Assembly uses the ffmpeg concat DEMUXER with a list file — never the concat
filter.** This is a deliberate, non-obvious choice; state it as a hard rule, not a
style preference. The concat filter (`[0:a][1:a]...concat=n=N:v=0:a=1[out]`) requires
listing every single input file on the command line. A 300-chunk chapter, once you
interleave a gap file between every pair of chunks, is 600+ file paths on one command
line — and Windows enforces an **8191-character** command-line length limit
(`CreateProcess`'s `lpCommandLine`). A real chapter blows past that and the process
fails to launch. The concat demuxer sidesteps this entirely: it takes one small list
file as its only "many inputs" argument. It's safe here specifically *because* of the
normalize-on-arrival step above — the demuxer does no re-encoding or format
reconciliation between segments, it just concatenates the byte streams, so every input
must already share sample rate, channel layout, and codec, which they do.

`list.txt` line format (one line per segment, alternating chunk/gap in order):

```
file 'C:/audiobooks/mybook/out/mybook/ch01/ch01_0000.wav'
file 'C:/audiobooks/mybook/out/mybook/_gaps/gap_900.wav'
file 'C:/audiobooks/mybook/out/mybook/ch01/ch01_0001.wav'
```

**Warnings:**
- Paths must be **absolute** and use **forward slashes**, even on Windows — the concat
  demuxer's parser treats backslashes as escape characters and will mis-parse a
  Windows-style path. Convert with `str(path).replace("\\", "/")` or build paths with
  `pathlib.PurePosixPath` when writing the list file.
- If any path could contain a single quote, escape it per ffmpeg's concat demuxer
  quoting rules (`'\''`); in practice, keep generated filenames to a safe charset
  (chunk ids are already `[a-z0-9_]`) and this won't come up.
- Invoke with `-safe 0` — required because the demuxer refuses relative-looking or
  "unsafe" paths by default; absolute paths need this flag explicitly enabled.

Literal stitch command:

```
ffmpeg -f concat -safe 0 -i list.txt -c:a libmp3lame -b:a 128k -ar 44100 -ac 1 out.mp3
```

**Segment order:** `[title][3000 ms][chunk][900 ms][chunk][900 ms]…[chunk]`. Gaps go
**between** segments only — never a leading gap before the title, never a trailing gap
after the last chunk. Build the list by interleaving, then drop the gap you'd
otherwise emit after the final chunk.

**Final encode:** one pass, `-c:a libmp3lame -b:a 128k -ar 44100 -ac 1`. Behind a
`--normalize` flag (off by default), insert an `-af loudnorm=I=-19:TP=-3.0:LRA=11`
filter in the same pass — those are ACX-compatible audiobook loudness targets
(integrated loudness -19 LUFS, true peak -3 dBTP, loudness range 11 LU). Don't run
loudnorm as a separate second pass; ffmpeg's `-af` applies it inline before the mp3
encode.

**Output paths:** `out/<book>/Chapter NN - Title.mp3` per chapter. A `--single-file`
flag instead concatenates every chapter (in order) into one file, with a
separately configurable inter-chapter gap (distinct from the inter-chunk gap —
default it larger, e.g. 2000 ms, since a chapter break is a bigger pause than a
paragraph break).

**Gap-pacing lever.** `gap_ms` (inter-chunk) is **the first knob to turn** on listener
feedback, and it must be documented as such in the README: if the read feels rushed,
raise it toward 1200; if it drags, drop it toward 700. The three anchor numbers:

| Value | Role |
|---|---|
| 700 ms | documented lower end — pacing measured off a professionally produced audiobook excerpt |
| 900 ms | **default** |
| 1200 ms | documented upper end — but NOT the default here |

1200 ms is not the default because of where it comes from: it was tuned for
speaker-change seams, where two different voices need a clear gap so they don't run
together. This app is a single narrator voice reading prose, dialogue-light — at
1200 ms, a mid-paragraph chunk boundary (see `boundary` in §3) reads to the ear as a
deliberate paragraph break, which is wrong for a boundary that isn't actually one.
900 ms is the narrator-appropriate middle.

State plainly, and repeat in the README and in-code comments: **re-tuning the gap
costs one ffmpeg pass and zero API calls.** Gaps are silence files applied at stitch
time from already-generated per-chunk wavs — changing `gap_ms` and re-running `stitch`
(not `generate`) reassembles the whole book in seconds. Nobody should ever regenerate
audio just to change a pause.

A boundary-aware refinement stays available but **off by default**: because the
chunker records, per chunk, why it ended (`ends_section` / `ends_paragraph` /
`mid_paragraph` — see §3), a future `--mid-paragraph-gap-ms <n>` flag can apply a
shorter gap specifically at `mid_paragraph` boundaries while leaving `ends_paragraph`
and `ends_section` boundaries at the full `gap_ms`. Implement the manifest field now;
the differentiated-gap flag itself is optional scope, but don't discard the boundary
data needed to add it later.

## 11. Node wrapper (`server/`)

A thin, optional local UI — roughly 150 lines, `express` only, no build step, no
React, no bundler, no TypeScript. It exists purely to make the CLI clickable; it must
add no logic of its own.

**`GET /`** — one static HTML page (inline `<style>`/`<script>`, no external assets)
containing:
- a file picker for the input `.txt`
- a picker for the reference clip (`reference/narrator.wav`)
- numeric fields for `gap_ms` and `concurrency`
- a tagger selector (`none` / `claude` / `codex`)
- Start and Cancel buttons
- a live-scrolling log panel
- an `<audio>` element that lets the user preview each chapter mp3 as it finishes

**`POST /api/run`** accepts the form fields as JSON and spawns
`python narrate.py run <args...>` as a child process (see the Windows note below).
Only one run at a time; a second `POST /api/run` while one is active returns 409.

**`GET /api/events`** is a Server-Sent-Events stream that forwards the child process's
stdout, line by line, to the browser as SSE `data:` frames. It does not parse or
interpret the JSON — it passes the line through and lets the front-end JS decide how
to render it.

**`POST /api/cancel`** kills the running child process (`SIGTERM`, then `SIGKILL` on
Windows via `child.kill()` — Node's `kill()` maps to `TerminateProcess` there).

**The stdout/stderr contract, restated here:** the Python CLI
prints exactly one NDJSON object per line to **stdout** for every event worth showing
in the UI; all human-readable/debug logging goes to **stderr** and is not part of the
UI contract (the server may still forward stderr to its own console for debugging, but
must not feed it to `/api/events`). Node's only job is: spawn, read stdout lines,
`JSON.parse` each, forward to SSE. **It re-implements none of the chunking, retry, or
stitching logic** — if the Node layer needs to know something (chunk counts, current
concurrency, which chapter is stitching), that information must already be a field
in an event Python already emits, never recomputed in JS.

Example event line (exactly as Python must print it, one JSON object per line):

```json
{"event":"chunk_done","chunk_id":"ch07_0012","i":12,"n":340,"latency_s":3.9,"concurrency":3}
```

Event types the CLI must emit at minimum:

| event | when | key fields |
|---|---|---|
| `run_started` | run begins, args resolved | `book`, `chapters`, `config` |
| `chunked` | a chapter finishes chunking | `chapter_id`, `chunk_count` |
| `tagged` | a chapter finishes the optional tag pass | `chapter_id`, `tagged_count`, `dropped_count` |
| `chunk_done` | one chunk's wav is ready (fresh or resumed) | `chunk_id`, `i`, `n`, `latency_s`, `concurrency` |
| `chunk_failed` | a chunk exhausted retries | `chunk_id`, `i`, `n`, `error` |
| `concurrency_changed` | pool degrades or ramps | `from`, `to`, `reason` |
| `stitched` | a chapter mp3 is written | `chapter_id`, `path`, `duration_s` |
| `done` | whole run finished | `book`, `chapters_done`, `failed_chunks` |
| `error` | unrecoverable run-level failure | `message` |

**The CLI is fully usable with the server never started** — every feature (`chunk`,
`tag`, `generate`, `stitch`, `run`, `prep-ref`) works standalone from a terminal with
no Node process anywhere. **And the server is fully usable without the CLI being
invoked by hand** — a user who only ever clicks Start in the browser gets the same
result as a user who only ever types `narrate.py run`. Neither is a required path
through the other.

**Windows spawn detail:** spawn the interpreter explicitly —
`spawn(pythonExePath, ["narrate.py", "run", ...args])` where `pythonExePath` is
resolved to the venv's `python.exe` (or plain `"python"` if no venv is used) — never
rely on a shebang line (Windows doesn't execute `.py` files directly via one) and
never pass `{ shell: true }` to `spawn` (it invokes `cmd.exe`, which mangles argument
quoting differently than a direct spawn and is an unnecessary injection surface for
user-supplied file paths).

## 12. Config + secrets

**`.env`** holds:
- `FISH_API_KEY` (required)
- `FISH_MODEL` (optional, defaults to `s2.1-pro-free`)
- `ANTHROPIC_API_KEY` (optional, only needed for `--tagger claude`)
- `OPENAI_API_KEY` (optional, only needed for `--tagger codex`)
- `OPENAI_TAG_MODEL` (required *only if* `--tagger codex` is used — no default; see §6
  and the troubleshooting table)

None of these values may ever be logged, printed to stdout (including inside an NDJSON
event), or written into any file under `out/`. `.env` must be listed in `.gitignore`
**from the very first commit** of the project — never add it and remove it later,
which leaves it in git history.

**`config.json`** holds everything else: paths, gap timings, concurrency, chunk size
bounds, and tagger settings. JSON has no comment syntax, so use a `_comment` sidecar
key next to any value that needs one, consistently, throughout the file. Show every
field filled in with its default:

```json
{
  "paths": {
    "input_dir": "input/",
    "reference_dir": "reference/",
    "output_dir": "out/"
  },
  "chunking": {
    "target_chars": 200,
    "max_chars": 300,
    "min_chars": 60,
    "hard_split_chars": 600
  },
  "gaps": {
    "_comment": "inter-chunk gap: 700ms(lower)-900ms(default)-1200ms(upper, tuned for speaker changes, too long for narrator-only mid-paragraph seams). Re-tuning = one ffmpeg pass, zero API calls.",
    "chunk_gap_ms": 900,
    "title_gap_ms": 3000,
    "chapter_gap_ms": 2000,
    "mid_paragraph_gap_ms": null
  },
  "concurrency": {
    "start": 3,
    "floor": 1,
    "ramp_up": false
  },
  "fish": {
    "model": "s2.1-pro-free",
    "sample_rate": 44100,
    "temperature": 0.7,
    "top_p": 0.7,
    "chunk_length": 300
  },
  "tagger": {
    "_comment": "ON by default. 'auto' = claude if ANTHROPIC_API_KEY, else codex if OPENAI_API_KEY + OPENAI_TAG_MODEL, else untagged with a printed recommendation. Tags carry the emotional cadence that makes the read sound like someone who has read the book; untagged, every chunk lands in the same flat register. This is a SECOND API bill on top of Fish. Opt out with 'none'.",
    "engine": "auto",
    "max_tag_chars": 32,
    "claude_model": "claude-opus-5",
    "effort": "low"
  },
  "normalize_output": false
}
```

**Precedence, highest to lowest:** CLI flag > `.env` > `config.json` > built-in
default. State this order explicitly in the README: a `--gap-ms 700` flag always wins
over `config.json`'s `chunk_gap_ms`, and an `.env` value always wins over
`config.json` but loses to an explicit flag.

Ship a `.env.example` (checked into git, unlike `.env` itself) listing every key name
above with an empty or placeholder value and a one-line comment on each.

## 13. Acceptance tests (`tests/`)

The building agent must **run these before reporting the build done** — a build that
compiles but hasn't been exercised against these is not finished. All tests run
**offline**, against faked HTTP calls — no test may spend real API credit. Use
`pytest` with `pytest-httpx` or a hand-rolled fake transport/monkeypatch on the client;
either is fine as long as no real network call happens. Runner: `pytest`, single
command `pytest tests/ -v` runs the whole suite.

1. **Chapter-boundary fixture.** Build a fixture `.txt` containing front matter (before
   any `Chapter` heading), `Chapter 1`, `Chapter 2: Title`, and `Chapter 7.5`. Assert
   the chunker/chapter-splitter produces chapter ids exactly `ch00`, `ch01`, `ch02`,
   `ch07_5` (per the derivation in §4), and that each chapter's first chunk is a
   `kind: "title"` chunk carrying the right title text.

2. **No-sentence-split fixture.** Build a ~250-word fixture chunked at `max_chars=200`
   that specifically includes `U.S.` (abbreviation with an internal period) and
   `"Stop!" she cried.` (terminal punctuation inside closing quotes followed by a
   lowercase continuation). Run the sentence splitter (§3) directly and assert its
   output sentence list treats `U.S.` as non-terminal and treats
   `"Stop!" she cried.` as a single sentence. Then run chunking and reassemble all
   chunk texts (rejoining with single spaces) and assert the reassembly, sentence-split
   again, equals the original sentence list — i.e. no sentence boundary was
   introduced or destroyed by chunk packing.

3. **Pure-punctuation silence, no API call.** Feed a chunk whose text is only
   punctuation (e.g. `"..."` or `"***"` — fails the unspeakable-text guard in §5).
   Inject a fake HTTP client that raises/fails the test if it is ever invoked. Assert
   a valid silent wav is produced at the chunk's expected path and that the fake
   client's call count is exactly 0.

4. **Forced 429 drops concurrency and still finishes.** Fake the HTTP client to return
   429 on a specific chunk (or the Nth call). Run a small multi-chunk chapter through
   the pool. Assert: `target_concurrency` reaches 1 by the end (or at minimum, dropped
   at least one step immediately following the 429, per §8's "429 degrades
   immediately" rule), a `concurrency_changed` event was emitted, and the run still
   completes with every chunk accounted for (either `chunk_done` or `chunk_failed` for
   every chunk — none silently missing).

5. **Stitched duration check.** Generate a small chapter (faked TTS returning
   fixed-duration silence/tone wavs of known length), stitch it, and use `ffprobe` to
   measure the output mp3's duration. Assert it equals
   `Σ(chunk durations) + Σ(gap durations)` within **±100 ms** (mp3 encoding introduces
   small padding; 100 ms is the tolerance, not a target to hit exactly).

6. **Resumed run makes zero API calls.** Run a small chapter to completion once. Run
   it again with the same input and same tags (no `--force`). Assert the fake HTTP
   client's call count on the second run is exactly 0, and that the manifest and wav
   files are byte-identical to the first run's.

7. **Tag validation rejects, never truncates.** Construct a fake tagger response that
   returns an over-long, sentence-shaped string as a tag (e.g. a 60-character phrase
   with punctuation, violating both the 32-char cap and the `^[a-z][a-z ,-]*$`
   pattern from §6). Assert the chunk is generated **untagged** (no `[bracket]` prefix
   reaches the Fish request body) and that the drop is recorded/logged — e.g. a
   `tagged` event's `dropped_count` increments, or an explicit warning line is
   emitted. Explicitly assert the tag text does NOT appear, truncated or otherwise, in
   the text sent to the fake TTS client.

8. **Tagger parity.** With both `--tagger claude` and `--tagger codex` pointed at
   faked HTTP layers (fake Anthropic response, fake OpenAI response) returning
   equivalent tag decisions for the same fixture chapter, assert both produce a valid
   `tags.json` (same schema) and that downstream chunk generation behaves identically
   between the two runs (same tags applied to the same chunk ids).

9. **`auto` resolution (§6.0).** Four cases, each asserted separately by manipulating the
   environment: `ANTHROPIC_API_KEY` only → resolves to `claude`; `OPENAI_API_KEY` +
   `OPENAI_TAG_MODEL` only → resolves to `codex`; both set → resolves to `claude` (order
   matters); neither → resolves to **untagged**, the run **still completes end to end**,
   and the recommendation is printed to **stderr** (assert it is on stderr and *not* on
   stdout, where it would corrupt the NDJSON stream).

10. **`--tagger none` silences the recommendation** — same key-less environment as case 4
    above, but with the explicit flag: the run completes untagged and stderr carries no
    recommendation. A user who has decided is not nagged on every run.

11. **An explicit backend with a missing key fails loudly.** `--tagger claude` with no
    `ANTHROPIC_API_KEY` must raise a clear error naming the variable — it must **not**
    silently fall back to untagged. Assert the same for `--tagger codex`.

## 14. Troubleshooting table

| Symptom | Likely cause | Fix |
|---|---|---|
| HTTP 401 from Fish | missing/invalid `FISH_API_KEY`, or `Bearer` prefix dropped | check `.env`, confirm header is literally `Authorization: Bearer <key>` |
| HTTP 402 from Fish | account/credits exhausted on the Fish side | check the Fish Audio dashboard billing/usage; not a code bug |
| HTTP 429 from Fish | rate limit hit | expected under load — pool should auto-degrade (§8); if it doesn't, check `concurrency_changed` events are firing |
| Empty response body from Fish (0 bytes) | Fish returned a failure state without a clear error code | treat as a failure, not silence — the client must raise, never write a 0-byte wav (§5) |
| `ffmpeg not found` / `ffprobe not found` | not on PATH | install ffmpeg and confirm `ffmpeg -version` / `ffprobe -version` work from the same shell the app runs in |
| Robotic or wrong-sounding voice | reference clip too long, too noisy, or its transcript doesn't match the audio | keep reference ≤30 s, clean single-speaker audio, transcript text matching what's actually said (§7) |
| Narrator reads a stage direction aloud | a delivery tag escaped validation and reached the TTS as free text | check the tag against the ≤32-char cap and `^[a-z][a-z ,-]*$` vocabulary (§6); a passing regex but semantically sentence-like tag is still a bug — tighten the tagger's allowed vocabulary |
| `codex` tagger fails immediately naming `OPENAI_TAG_MODEL` | env var unset | this is deliberate, not a bug — there is no default model id (§6); set `OPENAI_TAG_MODEL` in `.env` |
| Run says "generating UNTAGGED" and the read sounds flat | `auto` found no LLM key, so it degraded (§6.0) | set `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` + `OPENAI_TAG_MODEL`, then re-run — only chunks whose tag changed regenerate |
| Unexpected charges on an Anthropic/OpenAI account | tagging is **on by default** and resolved to a key you had set for something else (§6.0) | run with `--tagger none`, or set `tagger.engine` to `"none"` in `config.json` |
| `--tagger claude` errors instead of falling back | intended — an explicit backend that can't run is an error, not a downgrade (§6.0) | set the named key, or use `--tagger auto` if you want graceful degradation |
| "command line too long" / process fails to launch during stitch | someone reverted §10's concat demuxer back to the concat filter | use the concat demuxer with a list file, never inline every input on the command line |
| A resumed run regenerates every chunk in a chapter | the delivery tag changed (tagger re-run, or tagger toggled on/off) | this is **correct** behaviour, not a bug — `text_hash` covers the applied tag (§3), so a tag change must invalidate the cache |

## Before you report done

- [ ] All 8 acceptance tests in §13 pass, run with a single `pytest tests/ -v`, and none of them made a real network call.
- [ ] `.env` is listed in `.gitignore`, and grepping the full git history and every committed file for the literal `FISH_API_KEY`/`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` values turns up nothing — and no key appears in any stdout NDJSON event or stderr log line.
- [ ] The CLI runs a two-chapter fixture end-to-end (`chunk` → `generate` → `stitch`, or the combined `run` command) from a plain terminal with the Node server never started, producing two valid chapter mp3s.
- [ ] The Node server, started once, runs that same two-chapter job through the browser UI without the CLI being invoked by hand anywhere in the process, and the resulting mp3s are equivalent.
- [ ] `AGENTS.md` and `CLAUDE.md` are written at the project root, each describing the repo layout (§2), the run/test commands, and the config precedence (§12) — so either the human or a future coding agent can pick the project back up cold.
