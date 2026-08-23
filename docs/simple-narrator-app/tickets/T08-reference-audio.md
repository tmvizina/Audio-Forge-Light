# T08 — Reference audio + `prep-ref`

**Depends on:** T01. **Blocks:** T09.
**Read:** `BUILD-PROMPT.md` §7 (lines 738–767). **Nothing else.**

## Why this ticket exists

The reference clip determines the entire output voice. Everything downstream is
mechanical; this is the one input whose quality the user controls, and the most common
"the voice sounds wrong" report traces back to a clip that is too long, too noisy, or
mismatched with its transcript.

Small ticket. Land it before T09, which needs the loaded bytes.

## Files you own

`refaudio.py`, plus **only** the `prep-ref` subcommand registration in `narrate.py`.

## Files you must NOT touch

The rest of `narrate.py` — T09 owns the CLI. Add your subcommand and nothing else, and
leave the argument-parser structure extensible.

## Task

**1. Layout.** The reference lives at `reference/narrator.wav`, with an optional sibling
`reference/narrator.txt` holding its transcript. An empty transcript (`""`) is **legal**
and accepted by the API — but cloning quality is measurably better with one present, so
the absence should produce a warning on stderr, never an error.

**2. `prep-ref` subcommand.** Convert an arbitrary input recording into a conformant clip:

```
ffmpeg -i <input> -ar 44100 -ac 1 -c:a pcm_s16le reference/narrator.wav
```

Trim to **≤ 30 seconds** (`-t 30`, or a user-supplied `--start` / `--duration` window).
The output must be 44100 Hz, mono, 16-bit PCM — these match the sample rate the TTS
request declares, and a mismatch here is a silent quality loss.

**3. `--clean` flag (optional).** Applies a light cleanup chain before the final encode:

```
highpass=f=80,afftdn
```

A highpass to remove low-frequency rumble, then ffmpeg's built-in noise reduction. Useful
for a phone recording or a noisy room mic. Off by default — it is a mild degradation on
already-clean audio.

**4. Load once.** Read the reference bytes **exactly once at process startup** and reuse
the same in-memory `bytes` object for every API call in the run. Do not re-open the file
per chunk. A 30-second 44.1 kHz mono 16-bit wav is a small amount of memory to hold;
re-reading it hundreds of times per chapter on a light Windows machine with a slow disk is
pure wasted I/O for zero benefit. The bytes never change mid-run.

Expose this as a small loader returning `(audio_bytes, transcript_str)`, so T09 calls it
once and threads the result through.

**5. Validation on load.** Fail early and actionably if: the wav is missing; it is longer
than 30 s (suggest `prep-ref`); it is not 44100/mono/16-bit (suggest `prep-ref`). Use
`ffprobe` — it is already a declared dependency. A clear failure here saves the user from
a whole book generated in a degraded voice.

## Invariants at risk in this ticket

- **#11** — all audio inspection and conversion is ffmpeg/ffprobe subprocess work. No
  soundfile, no numpy.

## Definition of done

```bash
pytest tests/test_refaudio.py -v
```

Tests need real ffmpeg but **no network**. Generate fixtures with ffmpeg
(`anullsrc`, or a short tone) rather than committing binary audio to the repo.

Ship at least these tests:

1. `prep-ref` on a 60-second input produces a **≤ 30 s** output — assert with `ffprobe`.
2. The output is exactly 44100 Hz, mono, `pcm_s16le` — assert all three with `ffprobe`.
3. `--start`/`--duration` selects the requested window.
4. `--clean` adds `highpass=f=80,afftdn` to the filter chain, and its absence does not.
5. The loader reads the file **once** — call it, then generate 5 chunks against a fake
   client, and assert the file was opened exactly once (monkeypatch `open` or count via a
   spy on the loader).
6. A missing `narrator.txt` produces a warning on **stderr** and an empty transcript, not
   an exception.
7. Loading a 45-second reference fails with a message naming `prep-ref`.
8. Loading a 22050 Hz or stereo reference fails with a message naming `prep-ref`.

## Report back

- The `prep-ref` ffmpeg argv for both the plain and `--clean` cases.
- The loader signature and confirmation of the read-once behaviour with how you asserted it.
- The exact text of the three validation failure messages, since those are what the user
  actually sees when their voice comes out wrong.
