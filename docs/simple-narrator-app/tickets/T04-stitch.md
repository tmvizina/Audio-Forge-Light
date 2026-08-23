# T04 — ffmpeg stitching

**Depends on:** T01. **Blocks:** T09.
**Read:** `BUILD-PROMPT.md` §10 (lines 894–1001). **Nothing else.**

## Why this ticket exists

Two decisions in this module look like arbitrary style choices and are not: the
normalise-on-arrival step and the concat **demuxer**. They are one decision, and reversing
either one breaks the other. An agent that "cleans this up" to use the concat filter
produces a build that passes small tests and then fails to launch the process on a real
300-chunk chapter.

## Files you own

`stitch.py`.

## Files you must NOT touch

Everything else. You do not decide gap *values* — they come from config (T01). You
consume them.

## Task

**1. Normalise on arrival.** Every wav returned from the TTS is converted to **44100 Hz /
mono / pcm_s16le** before it is recorded in the manifest as done. This makes every segment
in a chapter byte-compatible with every other, which is the exact precondition the concat
demuxer requires. Do not weaken this: the demuxer does no format reconciliation, it
concatenates byte streams.

**2. Cached silence.** Pre-render each distinct gap length once and cache it on disk by
millisecond value (`gap_900.wav`, `gap_3000.wav`). Check existence before re-rendering —
do not regenerate the same 900 ms file 300 times in a chapter. The literal command:

```
ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 0.9 -c:a pcm_s16le gap_900.wav
```

Duration is passed as seconds-as-float (`ms / 1000.0`).

**3. Assembly — concat DEMUXER with a list file. Never the concat filter.** This is a hard
rule, not a preference. The filter (`[0:a][1:a]...concat=n=N:v=0:a=1[out]`) requires every
input on the command line. A 300-chunk chapter with a gap between each pair is 600+ paths
on one command line, and Windows enforces an **8191-character** limit on
`CreateProcess`'s `lpCommandLine`. A real chapter blows past it and the process fails to
launch. The demuxer takes one small list file instead.

`list.txt` format:

```
file 'C:/audiobooks/mybook/out/mybook/ch01/ch01_0000.wav'
file 'C:/audiobooks/mybook/out/mybook/_gaps/gap_900.wav'
file 'C:/audiobooks/mybook/out/mybook/ch01/ch01_0001.wav'
```

Three warnings that will bite:

- Paths must be **absolute** and use **forward slashes**, even on Windows. The demuxer's
  parser treats backslashes as escape characters and mis-parses a Windows path. Convert
  with `str(path).replace("\\", "/")`.
- Escape any single quote per the demuxer's rules (`'\''`). Generated chunk ids are
  `[a-z0-9_]` so this should not arise — but the input `.txt` filename can reach a path.
- Invoke with **`-safe 0`** — the demuxer rejects absolute paths without it.

Literal command:

```
ffmpeg -f concat -safe 0 -i list.txt -c:a libmp3lame -b:a 128k -ar 44100 -ac 1 out.mp3
```

**4. Segment order.** `[title][3000 ms][chunk][900 ms][chunk][900 ms]…[chunk]`. Gaps go
**between** segments only — never a leading gap before the title, never a trailing gap
after the last chunk. Build by interleaving, then drop the trailing gap.

**5. Final encode.** One pass: `-c:a libmp3lame -b:a 128k -ar 44100 -ac 1`. Behind a
`--normalize` flag (**off** by default), add `-af loudnorm=I=-19:TP=-3.0:LRA=11` inline in
the same pass — ACX-compatible audiobook targets. Do **not** run loudnorm as a separate
second pass.

**6. Output paths.** `out/<book>/Chapter NN - Title.mp3` per chapter. A `--single-file`
option concatenates all chapters in order with a separately configurable inter-chapter gap
(default 2000 ms — a chapter break is a bigger pause than a paragraph break).

**7. Gap values come from config, and only apply here.** Default inter-chunk 900 ms, title
3000 ms. Never bake a pause into a generated chunk. Re-stitching with a different `gap_ms`
must be a pure ffmpeg operation costing **zero API calls** — that property is what makes
gap tuning free, and it is the whole reason gaps live in this module and not upstream.

Honour the `boundary` field on each chunk record if `mid_paragraph_gap_ms` is set in
config; if it is `null` (the default), apply one flat gap everywhere. The field must be
read even though the differentiated behaviour is optional scope.

## Invariants at risk in this ticket

- **#5** — concat demuxer, never the filter. This ticket is invariant 5.
- **#6** — gaps only at stitch time.
- **#11** — no numpy/soundfile; all audio work is ffmpeg subprocess calls.

## Definition of done

```bash
pytest tests/test_stitch.py -v
```

Tests need real ffmpeg (it is a declared dependency) but **no network**.

Ship at least these tests:

1. **The acceptance test from §13.5**: stitch a set of generated-silence "chunks" of known
   durations with known gaps; assert the `ffprobe` duration of the output equals
   `Σ chunks + Σ gaps` within **±100 ms**.
2. The list file contains **forward slashes only** — assert no `\` appears in it.
3. The list file is used; assert the ffmpeg argv contains `-f concat` and `-safe 0` and
   does **not** contain a `concat=n=` filter string.
4. **The command-line-length guard**: build a chapter of 300 chunks and assert the
   generated ffmpeg command line stays under 8191 characters. This is the test that stops
   a future agent reverting to the filter.
5. No leading gap and no trailing gap — a 3-chunk chapter yields exactly 2 interleaved gap
   segments (plus the title gap if a title chunk is present).
6. The gap wav for a given ms value is rendered once and reused; assert the second call
   does not re-invoke ffmpeg for it.
7. Re-stitching the same chunks with a different `gap_ms` changes the output duration and
   makes **zero** calls to the TTS client (assert the fish client is not imported/invoked).
8. `--normalize` adds `loudnorm=I=-19:TP=-3.0:LRA=11` to the same pass, not a second one.

## Report back

- The exact ffmpeg argv for one real stitch, so the orchestrator can eyeball it.
- The measured command-line length for the 300-chunk case from test 4.
- The measured duration error from test 1.
- Confirmation that `-safe 0` is present and the filter is absent.
