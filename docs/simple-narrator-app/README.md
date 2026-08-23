# narrator

Turn a plain `.txt` novel into audiobook mp3s, read start to finish in your own voice.

Give it a book and a 20–30 second recording of yourself talking, and it hands back
chapter-by-chapter mp3s narrated in a zero-shot clone of that voice, via the hosted Fish
Audio API. No GPU, no ML stack to install, no cast of characters — just one narrator,
reading straight through.

## What it does / what it doesn't

| Does | Doesn't |
|---|---|
| One narrator, start to finish | A cast of characters |
| Clones **your** voice from a short clip | Per-speaker / per-character voices |
| Handles a whole book, chapter by chapter | Dialogue attribution or speaker detection |
| Resumes cleanly after a drop or a crash | GPU inference — everything runs on the API |
| Runs on a light machine, cheap hosted TTS | Audio mastering beyond an optional loudness pass |
| A pacing knob you can retune without re-generating | DRM'd or PDF input — plain `.txt` only |

## Requirements

- **Python 3.11+**
- **ffmpeg and ffprobe on PATH.** Check with:

  ```powershell
  ffmpeg -version
  ffprobe -version
  ```

  If either command isn't found, install ffmpeg and make sure its `bin` folder is on
  your PATH before continuing.
- **Node 18+** — only if you want the browser UI. The CLI works fully without it.
- **A Fish Audio API key** — see below.

No GPU. No CUDA. No torch. This app never loads a model locally; every TTS call goes to
the hosted Fish Audio API.

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The core install is three packages: `msgpack`, `httpx`, `python-dotenv`. That's it —
no numpy, no soundfile, nothing heavy. It installs in seconds on a weak box.

`anthropic` and `openai` are optional extras. You only need one of them if you turn on
emotion tags (see below); if you never use `--tagger`, you never need to install them.

If you want the browser UI, also run:

```powershell
cd server
npm install
cd ..
```

## Getting a Fish Audio key

1. Sign up at [fish.audio](https://fish.audio).
2. Create an API key from your account settings.
3. Put it in a `.env` file in the project root:

   ```text
   FISH_API_KEY=your-key-here
   ```

`.env` is gitignored and the key is never logged.

You can also set `FISH_MODEL` in `.env` to pick which Fish model generates your audio.
It defaults to `s2.1-pro-free` (the free tier of S2.1 Pro). Other accepted values are
`s2-pro`, `s1`, and `speech-1.5`.

## Recording your narrator clip

This one recording determines how the whole book sounds, so it's worth doing carefully.

- **20–30 seconds**, in a quiet room. No music, no background hum, no other people talking.
- Read a **normal passage of prose** in the voice you actually want the book read in.
  Don't perform a character and don't put on a "reading voice" — the clone copies your
  energy level as much as your tone, so read the way you want 10 hours of audiobook to
  sound.
- **One continuous take.** No edits, no fades, no stitching two takes together.
- A phone voice memo is completely fine. A USB mic is better if you have one, but don't
  let the lack of one stop you.

Once you have the recording, convert it:

```powershell
python narrate.py prep-ref my-recording.m4a
```

This writes `reference/narrator.wav` — 44100 Hz, mono, 16-bit, trimmed to 30 seconds if
your clip runs long. If the room wasn't perfectly quiet, add a light denoise pass:

```powershell
python narrate.py prep-ref my-recording.m4a --clean
```

`--clean` applies a highpass filter plus noise reduction (`highpass=f=80,afftdn`) —
enough to knock down hum and hiss without processing the life out of your voice.

Then write down **exactly** what you said into `reference/narrator.txt`. This file is
optional — an empty file is legal — but the clone is noticeably better when the
transcript matches the audio, so it's worth the thirty seconds of typing.

**If the output sounds robotic or doesn't sound like you**, the usual cause is the
reference clip: too long, too noisy, or a transcript that doesn't match what's actually
in the audio. Fix the clip before touching anything else.

## Your book's text format

A single `.txt` file, UTF-8 encoded.

**Chapters** are marked by a standalone line reading `Chapter 7` or `Chapter 7: The Long
Road` — a colon, a dash, or an em-dash after the number all work. `Chapter 7.5` is
supported too and becomes chapter id `ch07_5`. Anything before the first chapter line is
treated as front matter and becomes `ch00`.

**Scene breaks** are `***`, `---`, or a blank-line run. The chunker never packs text
across a scene break — a break always starts a fresh chunk.

For example, this:

```text
Chapter 7: The Long Road

Tomas walked until the light gave out, and then he kept walking anyway, because
stopping meant thinking, and thinking meant remembering the house.

The road bent east. He didn't.

***

Three days later he crossed into Vale territory with nothing left in his pack but
a knife and a name he wasn't sure he still deserved.
```

produces a spoken title chunk — "Chapter Seven. The Long Road." — followed by three
seconds of silence, then the body, broken cleanly at the `***`.

## Run it

The one-command form does everything — chunk, generate, stitch:

```powershell
python narrate.py run --book mybook.txt
```

It prints progress as it goes: chunks written, chapters detected, generation progress
per chunk, then the finished mp3 paths.

Or run each stage yourself:

```powershell
python narrate.py chunk --book mybook.txt
python narrate.py tag --book mybook.txt        # optional, see below
python narrate.py generate --book mybook.txt
python narrate.py stitch --book mybook.txt
```

Useful flags on any stage:

- `--chapters ch01-ch03` — limit the run to a chapter range
- `--force` — regenerate even if a chunk already has audio
- `--normalize` — apply a loudness normalization pass at stitch time
- `--single-file` — stitch the whole book into one mp3 instead of one per chapter
- `--ramp-up` — allow concurrency to climb back up after a run of fast, clean calls
  (off by default; see Cost and time below)

If you'd rather click than type, start the browser UI:

```powershell
node server/index.js
```

Then open `http://localhost:3000`. The CLI and the UI are fully interchangeable —
neither one needs the other running. Use whichever fits the moment.

## Where the output lands

```text
out/
  mybook/
    ch00/
      ch00_0001.wav
      ch00_0002.wav
      ...
      manifest.json
    ch07/
      ch07_0001.wav
      ...
      manifest.json
    Chapter 07 - The Long Road.mp3
    ...
```

Each chapter folder holds the per-chunk wav files plus a `manifest.json` describing
every chunk (see below). The finished, stitched mp3 for each chapter lands one level up,
named from the chapter heading.

## Resuming

Re-running `generate` skips any chunk that already has audio **and** whose text hasn't
changed since that audio was made. If your connection drops at chunk 380 of 400, the
next run picks up at 380 — it costs you the last handful of chunks, not the whole book.

Pass `--force` if you want to regenerate everything regardless of what's already on
disk.

## Tuning the pause — the first knob to turn

If a chapter comes back sounding off, don't touch generation. Start here.

- The default gap between chunks is **900 ms**. The gap before a chapter title is
  **3000 ms**.
- If the read feels **rushed**, raise the gap toward **1200 ms**. If it **drags**, drop
  it toward **700 ms** — that lower number is pacing measured directly off a
  professionally produced audiobook, not a guess.
- Why the default sits at 900 and not 1200: 1200 ms is tuned for the moment the
  **voice changes** — a seam where two different speakers would otherwise collide if
  you cut too tight. This app has one narrator reading straight through, so 1200 ms
  in the middle of a paragraph reads to the ear as a paragraph break that isn't there.

The important part: **changing the gap costs one ffmpeg pass and zero API calls.**
Gaps are inserted at stitch time, from the per-chunk wav files that are already sitting
on disk. You never need to regenerate a book — or even a chapter — just to change how
long the pauses are. Re-stitch instead:

```powershell
python narrate.py stitch --book mybook.txt --gap-ms 1100
```

The chunker also records *why* each chunk ended — end of a scene, end of a paragraph,
or mid-paragraph — even though the app only applies one uniform gap today. That data is
there so a future `--mid-paragraph-gap-ms` flag could shorten just the mid-paragraph
seams without touching the paragraph and scene breaks.

## Optional: emotion tags

Off by default. When enabled, a small LLM pass reads each chunk and labels it with a
short delivery hint — `weary`, `bitter amusement`, `flat` — which gets applied as a
leading `[tag]` marker that the TTS reads as a direction rather than speaking aloud.

You can drive this with either provider, and both get full parity — pick whichever
account you already have:

```powershell
python narrate.py tag --book mybook.txt --tagger claude
python narrate.py tag --book mybook.txt --tagger codex
```

If you use the OpenAI path, you must set `OPENAI_TAG_MODEL` yourself in `.env`. There's
no built-in default, because OpenAI model ids change often enough that a hardcoded
default would eventually fail as a confusing 404. Run `client.models.list()` or check
your OpenAI dashboard to see what your account currently has access to, and set that.

Add `--tags-review` to stop right after `tags.json` is written, before any TTS calls
happen. That lets you read through the tags and hand-edit anything odd before spending
money generating audio.

One thing to know going in: tags are a **second API bill**, separate from Fish Audio.
It's small — one short call per chunk — but it's real, and it happens even if you never
touch generation afterward.

And a hard rule, worth knowing before it surprises you: tags are validated to short
lowercase words only. That limit exists because long, free-form direction text gets
**read aloud** by the TTS instead of being treated as a stage direction. A tag that
fails validation is silently dropped and the chunk generates untagged — it's never
truncated into something that merely looks valid.

## Cost and time — realistic expectations

Actual dollar cost depends entirely on which Fish Audio tier you're on and on pricing
that changes over time, so rather than quote you a number that might already be wrong,
here's the arithmetic:

A roughly 100,000-character novel packs into on the order of 500 chunks at ~200
characters each. Each chunk is one API call. At a starting concurrency of 3 and a call
latency of a few seconds, that's on the order of **tens of minutes** of wall-clock time
for a full novel — not hours, barring rate limiting.

The free `s2.1-pro-free` tier exists and works, but it's rate-limited, which is exactly
why this app degrades its own concurrency automatically when it sees a 429 or slow
responses (see below) rather than hammering the API and making things worse.

Before committing a whole book, price and time a single chapter first:

```powershell
python narrate.py run --book mybook.txt --chapters ch01
```

That gives you real numbers on your account, your tier, and your book's prose density —
far more useful than any figure this document could print.

Behind the scenes, concurrency starts at 3 and can drop to 1 under pressure: it steps
down whenever the median of the last five call latencies exceeds 1.75x the run's
baseline, or immediately on any 429, 5xx, or timeout. It only climbs back up if you pass
`--ramp-up`, and even then only after ten consecutive fast, clean calls.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| HTTP 401 | Bad or missing API key | Check `FISH_API_KEY` in `.env` |
| HTTP 402 | Out of Fish Audio credit | Top up or switch to `s2.1-pro-free` |
| HTTP 429 | Rate limited | Handled automatically — expect concurrency to drop to 1 until it clears |
| Empty response body | Treated as a failure, not silence | Retry; if persistent, check Fish Audio's status page |
| `ffmpeg not found` | ffmpeg/ffprobe not on PATH | Install ffmpeg and confirm with `ffmpeg -version` |
| Output sounds robotic or not like you | Reference clip too long, too noisy, or transcript mismatch | Re-record following the guidance above, or re-run `prep-ref --clean` |
| Narrator reads a stage direction out loud | A tag escaped validation | Turn tags off, or hand-edit `tags.json` and re-run `generate` |
| `OPENAI_TAG_MODEL` error on startup | Env var unset | Set it in `.env` to a model id from `client.models.list()` |
| A chapter heading wasn't detected | The heading line wasn't standalone | Put `Chapter N` (optionally `: Title`) on its own line with nothing else on it |
| A run restarts from zero instead of resuming | The chunk text or its tag changed, so its hash changed | Expected behavior — only edited chunks regenerate; unrelated chunks still skip |
