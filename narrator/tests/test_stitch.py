"""Tests for ffmpeg chapter assembly (T04).

Covers the acceptance test from BUILD-PROMPT.md §13.5 plus the ticket's
required regression tests: forward-slash-only list files, concat DEMUXER
(never the filter), the Windows 8191-char command-line guard on a 300-chunk
chapter, no leading/trailing gaps, gap-wav caching, zero-API-call
re-stitching, and single-pass --normalize.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import stitch
from models import Chunk


def make_chunk(
    chunk_id: str,
    position: int,
    kind: str = "body",
    boundary: str = "ends_paragraph",
    text: str = "Some narration text.",
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        position=position,
        text=text,
        char_count=len(text),
        text_hash=f"hash-{chunk_id}",
        kind=kind,
        boundary=boundary,
        over_cap=False,
    )


# ---------------------------------------------------------------------------
# 1. Acceptance test (§13.5): stitched duration == sum(chunks) + sum(gaps), ±100ms
# ---------------------------------------------------------------------------


def test_acceptance_stitched_duration_matches_sum_within_100ms(tmp_path, make_wav):
    title = make_chunk("title", 0, kind="title")
    b1 = make_chunk("b1", 1)
    b2 = make_chunk("b2", 2)
    b3 = make_chunk("b3", 3)

    title_wav = make_wav("title.wav", duration_s=0.4)
    w1 = make_wav("b1.wav", duration_s=1.0)
    w2 = make_wav("b2.wav", duration_s=1.0)
    w3 = make_wav("b3.wav", duration_s=1.0)

    pairs = [(title, title_wav), (b1, w1), (b2, w2), (b3, w3)]

    gaps_dir = tmp_path / "_gaps"
    list_dir = tmp_path / "_list"
    out_path = tmp_path / "out" / "ch01.mp3"

    gap_ms = 300
    title_gap_ms = 700

    stitch.stitch_chapter(
        pairs, out_path, gaps_dir, list_dir, gap_ms=gap_ms, title_gap_ms=title_gap_ms
    )

    expected_chunks_s = 0.4 + 1.0 + 1.0 + 1.0
    expected_gaps_s = (title_gap_ms + gap_ms + gap_ms) / 1000.0
    expected_total = expected_chunks_s + expected_gaps_s

    actual = stitch.ffprobe_duration_s(out_path)
    error = abs(actual - expected_total)
    print(
        f"\n[test 1] expected={expected_total:.3f}s actual={actual:.3f}s "
        f"error={error * 1000:.1f}ms"
    )
    assert error <= 0.1


# ---------------------------------------------------------------------------
# 2. List file: forward slashes only
# ---------------------------------------------------------------------------


def test_list_file_forward_slashes_only(tmp_path):
    seg1 = tmp_path / "sub dir" / "a.wav"
    seg2 = tmp_path / "sub dir" / "b.wav"
    list_path = tmp_path / "_list" / "list.txt"

    stitch.write_list_file([seg1, seg2], list_path)
    content = list_path.read_text(encoding="utf-8")

    assert "\\" not in content
    assert content.count("file '") == 2


# ---------------------------------------------------------------------------
# 3. Demuxer used, filter absent
# ---------------------------------------------------------------------------


def test_argv_uses_concat_demuxer_never_the_filter(tmp_path):
    list_path = tmp_path / "list.txt"
    out_path = tmp_path / "out.mp3"

    argv = stitch.build_ffmpeg_concat_argv(list_path, out_path)

    assert "-f" in argv
    assert argv[argv.index("-f") + 1] == "concat"
    assert "-safe" in argv
    assert argv[argv.index("-safe") + 1] == "0"

    joined = " ".join(argv)
    assert "concat=n=" not in joined
    assert "[0:a]" not in joined


# ---------------------------------------------------------------------------
# 4. Command-line-length guard: 300 chunks stays under Windows' 8191-char limit
# ---------------------------------------------------------------------------


def test_command_line_length_guard_300_chunks(tmp_path):
    n = 300
    pairs = []
    for i in range(n):
        chunk = make_chunk(f"c{i:04d}", i)
        wav_path = tmp_path / "chunks" / f"c{i:04d}.wav"
        pairs.append((chunk, wav_path))

    gaps_dir = tmp_path / "_gaps"
    list_dir = tmp_path / "_list"
    out_path = tmp_path / "out" / "ch01.mp3"

    segments = stitch.build_segments(pairs, gaps_dir, gap_ms=900, title_gap_ms=3000)
    # 300 chunks with a gap between every pair = 599 total segments.
    assert len(segments) == 2 * n - 1

    list_path = stitch.write_list_file(segments, list_dir / "list.txt")
    argv = stitch.build_ffmpeg_concat_argv(list_path, out_path)

    cmdline = subprocess.list2cmdline(argv)
    print(f"\n[test 4] 300-chunk argv command-line length = {len(cmdline)} chars")
    assert len(cmdline) < 8191


# ---------------------------------------------------------------------------
# 5. No leading gap, no trailing gap
# ---------------------------------------------------------------------------


def test_no_leading_or_trailing_gap(tmp_path):
    gaps_dir = tmp_path / "_gaps"
    body_chunks = [make_chunk(f"b{i}", i) for i in range(3)]
    pairs = [(c, tmp_path / f"{c.chunk_id}.wav") for c in body_chunks]

    segments = stitch.build_segments(pairs, gaps_dir, gap_ms=500, title_gap_ms=1500)

    assert len(segments) == 5  # 3 chunks + 2 gaps
    assert segments[0].name == "b0.wav"
    assert segments[-1].name == "b2.wav"

    gap_segments = [s for s in segments if s.parent == gaps_dir]
    assert len(gap_segments) == 2
    assert all(s.name == "gap_500.wav" for s in gap_segments)

    # A title chunk prepended adds exactly one more (title) gap, still no
    # leading gap before the title and no trailing gap after the last chunk.
    title = make_chunk("title", -1, kind="title")
    pairs_with_title = [(title, tmp_path / "title.wav")] + pairs
    segments2 = stitch.build_segments(pairs_with_title, gaps_dir, gap_ms=500, title_gap_ms=1500)

    assert len(segments2) == 7  # 4 chunks + 3 gaps
    assert segments2[0].name == "title.wav"
    assert segments2[-1].name == "b2.wav"

    gap_segments2 = [s for s in segments2 if s.parent == gaps_dir]
    assert len(gap_segments2) == 3
    assert gap_segments2[0].name == "gap_1500.wav"  # after the title
    assert gap_segments2[1].name == "gap_500.wav"
    assert gap_segments2[2].name == "gap_500.wav"


# ---------------------------------------------------------------------------
# 6. Gap wav cached: second call for the same ms does not re-invoke ffmpeg
# ---------------------------------------------------------------------------


def test_gap_wav_rendered_once_and_reused(tmp_path):
    gaps_dir = tmp_path / "_gaps"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-wav-bytes")
        return subprocess.CompletedProcess(argv, 0)

    with patch("stitch.subprocess.run", side_effect=fake_run) as mock_run:
        p1 = stitch.render_gap_wav(gaps_dir, 900)
        p2 = stitch.render_gap_wav(gaps_dir, 900)

    assert p1 == p2
    assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# 7. Re-stitching at a different gap_ms: duration changes, zero TTS calls
# ---------------------------------------------------------------------------


def test_restitch_different_gap_changes_duration_zero_api_calls(tmp_path, make_wav):
    sys.modules.pop("fish_client", None)

    chunks = [make_chunk(f"c{i}", i) for i in range(3)]
    wavs = [make_wav(f"c{i}.wav", duration_s=0.5) for i in range(3)]
    pairs = list(zip(chunks, wavs))

    gaps_dir = tmp_path / "_gaps"
    list_dir = tmp_path / "_list"

    out1 = tmp_path / "out1.mp3"
    stitch.stitch_chapter(pairs, out1, gaps_dir, list_dir, gap_ms=300, title_gap_ms=1000)
    d1 = stitch.ffprobe_duration_s(out1)

    out2 = tmp_path / "out2.mp3"
    stitch.stitch_chapter(pairs, out2, gaps_dir, list_dir, gap_ms=900, title_gap_ms=1000)
    d2 = stitch.ffprobe_duration_s(out2)

    # 3 chunks -> 2 inter-chunk gaps; raising gap_ms by 600ms adds ~1.2s.
    print(f"\n[test 7] d1={d1:.3f}s d2={d2:.3f}s delta={(d2 - d1):.3f}s")
    assert (d2 - d1) == pytest.approx(1.2, abs=0.15)

    assert "fish_client" not in sys.modules


# ---------------------------------------------------------------------------
# 8. --normalize adds loudnorm to the same encode pass, not a second one
# ---------------------------------------------------------------------------


def test_normalize_flag_adds_loudnorm_in_same_pass(tmp_path):
    list_path = tmp_path / "list.txt"
    out_path = tmp_path / "out.mp3"

    argv = stitch.build_ffmpeg_concat_argv(list_path, out_path, normalize=True)

    assert argv.count("-af") == 1
    af_idx = argv.index("-af")
    assert argv[af_idx + 1] == "loudnorm=I=-19:TP=-3.0:LRA=11"
    # One argv, one ffmpeg invocation: mp3 encode args live in this same list.
    assert "-c:a" in argv
    assert "libmp3lame" in argv
    assert argv[0] == stitch.FFMPEG

    argv_off = stitch.build_ffmpeg_concat_argv(list_path, out_path, normalize=False)
    assert "-af" not in argv_off
