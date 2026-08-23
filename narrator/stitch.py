"""ffmpeg chapter assembly. Owned by T04.

Two decisions here are one decision, not two: every wav is normalised on
arrival to 44100 Hz / mono / pcm_s16le (`normalize_wav`), and assembly uses
the ffmpeg concat **demuxer** with a list file, never the concat filter. The
demuxer does no format reconciliation between inputs — it just concatenates
byte streams — so it is only safe because every input already shares sample
rate, channel layout, and codec. Weakening the normalize step breaks the
demuxer; switching to the filter re-introduces the 300-chunk-chapter /
Windows 8191-char `CreateProcess` command-line limit that the demuxer exists
to avoid. See docs/simple-narrator-app/tickets/T04-stitch.md.

Gaps are silence files applied at stitch time from already-generated
per-chunk wavs. Re-tuning `gap_ms` and re-running stitch (not generate)
reassembles a whole chapter/book in seconds, at the cost of one ffmpeg pass
and zero API calls — nobody should ever regenerate audio just to change a
pause. Gap values themselves are never invented here; they are read from
config (T01) by the caller and passed in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from models import Chunk

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

_GAP_FILENAME_TEMPLATE = "gap_{ms}.wav"
_LOUDNORM_FILTER = "loudnorm=I=-19:TP=-3.0:LRA=11"


# ---------------------------------------------------------------------------
# Normalize on arrival
# ---------------------------------------------------------------------------


def normalize_wav(src: Path, dst: Path, sample_rate: int = 44100) -> Path:
    """Re-encode `src` to `sample_rate` Hz / mono / pcm_s16le, written to `dst`.

    Called the instant a wav comes back from the TTS (or is generated locally
    as silence for an unspeakable chunk), before it is ever recorded in the
    manifest as done. This is the precondition the concat demuxer below
    requires: it does no per-input re-encoding, so every segment in a chapter
    must already be byte-compatible with every other.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            str(src),
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )
    return dst


# ---------------------------------------------------------------------------
# Cached silence
# ---------------------------------------------------------------------------


def render_gap_wav(gaps_dir: Path, ms: int, sample_rate: int = 44100) -> Path:
    """Return the cached silence wav for `ms` milliseconds, rendering it once.

    Gap files are named by millisecond value (gap_900.wav, gap_3000.wav) and
    checked for existence before re-rendering, so a 300-chunk chapter with a
    flat 900 ms gap invokes ffmpeg for that gap exactly once, not 300 times.
    """
    gaps_dir.mkdir(parents=True, exist_ok=True)
    out_path = gaps_dir / _GAP_FILENAME_TEMPLATE.format(ms=ms)
    if out_path.exists():
        return out_path
    duration_s = ms / 1000.0
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=mono",
            "-t",
            str(duration_s),
            "-c:a",
            "pcm_s16le",
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )
    return out_path


# ---------------------------------------------------------------------------
# Concat demuxer list file
# ---------------------------------------------------------------------------


def _posix(path: Path) -> str:
    """Absolute, forward-slash path. The concat demuxer's parser treats a
    backslash as an escape character, so a raw Windows path mis-parses."""
    return str(path.resolve()).replace("\\", "/")


def _quote_concat_entry(path: Path) -> str:
    # Per the demuxer's quoting rules, a literal single quote inside the path
    # is escaped as '\''. Generated chunk ids are [a-z0-9_] so this shouldn't
    # arise in practice, but an input .txt filename could reach a path.
    escaped = _posix(path).replace("'", "'\\''")
    return f"file '{escaped}'"


def write_list_file(segments: Sequence[Path], list_path: Path) -> Path:
    """Write a concat-demuxer list file, one `file '...'` line per segment,
    in order. Paths are absolute with forward slashes only."""
    list_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_quote_concat_entry(seg) for seg in segments]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


# ---------------------------------------------------------------------------
# Segment interleaving
# ---------------------------------------------------------------------------


def build_segments(
    chunk_wav_paths: Sequence[tuple[Chunk, Path]],
    gaps_dir: Path,
    gap_ms: int,
    title_gap_ms: int,
    mid_paragraph_gap_ms: int | None = None,
) -> list[Path]:
    """Interleave chunk wavs with cached gap wavs: [chunk][gap][chunk][gap]...[chunk].

    Gaps go strictly BETWEEN segments — no leading gap before the first
    segment (typically the title), no trailing gap after the last chunk.
    A "title" chunk is followed by `title_gap_ms`. A "body" chunk is followed
    by `gap_ms`, unless `mid_paragraph_gap_ms` is configured (non-null) and
    that chunk's `boundary` is "mid_paragraph", in which case the shorter gap
    applies. The `boundary` field is always read, even when the differentiated
    behaviour is off (mid_paragraph_gap_ms is None) — it's config's decision,
    not this function's, whether to use it.
    """
    segments: list[Path] = []
    n = len(chunk_wav_paths)
    for i, (chunk, wav_path) in enumerate(chunk_wav_paths):
        segments.append(wav_path)
        if i == n - 1:
            continue  # never a trailing gap
        if chunk.kind == "title":
            gap = title_gap_ms
        elif mid_paragraph_gap_ms is not None and chunk.boundary == "mid_paragraph":
            gap = mid_paragraph_gap_ms
        else:
            gap = gap_ms
        segments.append(render_gap_wav(gaps_dir, gap))
    return segments


# ---------------------------------------------------------------------------
# ffmpeg assembly (concat DEMUXER — never the concat filter)
# ---------------------------------------------------------------------------


def build_ffmpeg_concat_argv(
    list_path: Path, out_path: Path, normalize: bool = False
) -> list[str]:
    """The one-and-only assembly command: concat demuxer + list file.

    Never build a `concat=n=N:v=0:a=1` filter graph here — that form puts
    every input path on the command line, and a real chapter (600+ paths
    once gaps are interleaved) blows past Windows' 8191-char CreateProcess
    limit. The demuxer takes one small list file instead, so this argv is a
    fixed handful of arguments regardless of chapter length.
    """
    argv = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path)]
    if normalize:
        # Applied inline, in the same pass, before the mp3 encode — never a
        # separate second loudnorm pass.
        argv += ["-af", _LOUDNORM_FILTER]
    argv += [
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "1",
        str(out_path),
    ]
    return argv


def run_ffmpeg_concat(list_path: Path, out_path: Path, normalize: bool = False) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    argv = build_ffmpeg_concat_argv(list_path, out_path, normalize=normalize)
    subprocess.run(argv, check=True, capture_output=True)
    return out_path


def ffprobe_duration_s(path: Path) -> float:
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# Public per-chapter entry point
# ---------------------------------------------------------------------------


def chapter_output_path(out_dir: Path, book: str, chapter_num: int, chapter_title: str) -> Path:
    return out_dir / book / f"Chapter {chapter_num:02d} - {chapter_title}.mp3"


def stitch_chapter(
    chunk_wav_paths: Sequence[tuple[Chunk, Path]],
    out_path: Path,
    gaps_dir: Path,
    list_dir: Path,
    gap_ms: int = 900,
    title_gap_ms: int = 3000,
    mid_paragraph_gap_ms: int | None = None,
    normalize: bool = False,
) -> Path:
    """Assemble one chapter's already-generated, already-normalised chunk wavs
    into `out_path` (an mp3). Consumes gap values; never invents them. This
    function touches only local wav files and ffmpeg — it makes no network
    call and never imports the TTS client, so re-stitching at a different
    `gap_ms` costs one ffmpeg pass and zero API calls.
    """
    segments = build_segments(
        chunk_wav_paths, gaps_dir, gap_ms, title_gap_ms, mid_paragraph_gap_ms
    )
    list_path = list_dir / "list.txt"
    write_list_file(segments, list_path)
    return run_ffmpeg_concat(list_path, out_path, normalize=normalize)


# ---------------------------------------------------------------------------
# Optional --single-file: concatenate chapters in order
# ---------------------------------------------------------------------------


def stitch_book(
    chapter_mp3_paths: Sequence[Path],
    out_path: Path,
    gaps_dir: Path,
    list_dir: Path,
    chapter_gap_ms: int = 2000,
    normalize: bool = False,
) -> Path:
    """Concatenate already-stitched chapter mp3s into one file, in order, with
    a separately configurable inter-chapter gap (bigger than the inter-chunk
    gap — a chapter break is a bigger pause than a paragraph break). Still
    the concat demuxer, same reasoning as `stitch_chapter`.
    """
    segments: list[Path] = []
    n = len(chapter_mp3_paths)
    for i, path in enumerate(chapter_mp3_paths):
        segments.append(path)
        if i != n - 1:
            segments.append(render_gap_wav(gaps_dir, chapter_gap_ms))
    list_path = list_dir / "book_list.txt"
    write_list_file(segments, list_path)
    return run_ffmpeg_concat(list_path, out_path, normalize=normalize)
