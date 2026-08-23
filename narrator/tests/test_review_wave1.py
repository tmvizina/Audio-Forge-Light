"""Adversarial review tests for Wave 1 (T03 fish_client, T04 stitch, T05 pool,
T08 refaudio, T10 tagger/base).

Written by the reviewer, not by any ticket's implementing agent. These are
NOT feature tests for a single module in isolation -- they specifically try
to break assumptions at the seams between modules, and to fill coverage
holes left by each ticket's own test suite.

Some tests in this file are EXPECTED TO FAIL. A failing test here is a
reproduction of a real defect, not a mistake in the test -- see the
docstring on each for what it proves and who owns the fix. Passing tests
are either confirmations that something holds, or new coverage for
previously-untested-but-correct behavior.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

import events
import refaudio
import stitch
from models import Chunk
from pool import AdaptivePool


def make_chunk(chunk_id: str, position: int, kind: str = "body") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        position=position,
        text="Some narration text.",
        char_count=20,
        text_hash=f"hash-{chunk_id}",
        kind=kind,
        boundary="ends_paragraph",
        over_cap=False,
    )


# ---------------------------------------------------------------------------
# 1. stitch.py: a colon in the chapter title silently corrupts the output.
#
# BUG (blocker, owned by T04 / stitch.py `chapter_output_path`):
# `chapter_output_path` builds `f"Chapter {n:02d} - {title}.mp3"` with zero
# sanitization of Windows/NTFS-illegal filename characters. `?`, `*`, `"`
# all fail LOUDLY (ffmpeg's CreateFile fails, subprocess.run(check=True)
# raises CalledProcessError) -- annoying but safe. A COLON does something far
# worse: NTFS silently treats `name:rest` as a named Alternate Data Stream on
# a base file called `name`. ffmpeg exits 0. `out_path.exists()` returns
# True. But the base file is 0 bytes and the actual mp3 audio is stashed in
# an ADS that virtually no downstream consumer (email, zip, cloud sync, most
# media players, a USB copy) will ever see. The pipeline reports success and
# the chapter is functionally gone, with no error anywhere in the run.
#
# Book chapter titles routinely contain a colon ("Chapter 7: The Reckoning"
# is literally this project's own chapter-naming convention), so this is not
# an exotic input.
# ---------------------------------------------------------------------------


def test_colon_in_chapter_title_does_not_silently_corrupt_output(tmp_path, make_wav):
    chunk = make_chunk("c0", 0)
    wav = make_wav("c0.wav", duration_s=0.3)

    out_path = stitch.chapter_output_path(
        tmp_path / "out", "mybook", 3, "The Reckoning: A New Dawn"
    )

    stitch.stitch_chapter(
        [(chunk, wav)], out_path, tmp_path / "_gaps", tmp_path / "_list",
        gap_ms=200, title_gap_ms=500,
    )

    # This is what "the chapter was actually produced" must mean: a file at
    # out_path with real audio bytes in it. Today, out_path.exists() is True
    # but the file is a 0-byte NTFS ADS host and the audio is invisible to
    # any normal consumer -- proving `chapter_output_path` needs to strip or
    # replace filesystem-illegal characters (at minimum `:`) before it is
    # ever used to build a path.
    assert out_path.exists()
    assert out_path.stat().st_size > 1000, (
        "chapter mp3 is 0 bytes on disk -- the audio silently went into an "
        "NTFS alternate data stream because the chapter title contained a "
        "colon and chapter_output_path never sanitized it"
    )


# ---------------------------------------------------------------------------
# 2. pool.py <-> fish_client.py seam: a plain RuntimeError carrying "429" in
# its text (exactly what fish_client.synthesize raises for a real 429) is
# NOT recognized as rate-limit-worthy by pool.py's degrade logic.
#
# This is the seam flagged in the review brief, made concrete: pool.py's
# `run_one` catches `except Exception as exc` and hands anything that is not
# already a `RateLimitError` / `ServerError` / `CallTimeoutError` instance to
# `_on_call_failure`, which -> `degrade_reason(error=exc, ...)` -> returns
# None for a bare RuntimeError no matter what its message says (pool.py
# deliberately never parses error strings -- see its own module docstring).
# fish_client.synthesize raises exactly a bare RuntimeError for every
# non-2xx status, 429 included, with the status code embedded only as text:
# `f"Fish TTS request failed ({resp.status_code}): {resp.text}"`.
#
# Net effect: wired together with no bridge, a real 429 from Fish would be
# silently retried at full concurrency instead of triggering the immediate
# degrade BUILD-PROMPT.md S8 requires, and `retry_after` (from the
# `Retry-After` header) is not even recoverable -- fish_client discards the
# response headers entirely once it raises.
#
# The bridge must live in T09 (narrate.py's per-chunk job_fn wrapper, the
# only place that imports both fish_client and pool): it must catch
# fish_client's RuntimeError, recover the HTTP status (today only possible
# by parsing the message text, since fish_client exposes no status_code
# attribute), and re-raise pool.RateLimitError / pool.ServerError
# accordingly. Recovering `retry_after` for pool's Retry-After override
# requires an actual fish_client.py change (surface the header on the
# exception) -- no amount of T09-side parsing can produce a value fish_client
# already threw away.
#
# This test documents CURRENT (buggy) behavior and is expected to PASS,
# proving the gap exists rather than proving a desired behavior.
# ---------------------------------------------------------------------------


def test_PROOF_plain_429_runtimeerror_does_not_trigger_degrade():
    async def job_fn(index: int, payload):
        # Exactly fish_client.synthesize's failure shape for a real 429.
        raise RuntimeError("Fish TTS request failed (429): rate limited, retry later")

    async def fake_sleep(_):
        return None

    async def main():
        p = AdaptivePool(max_workers=3, target=3)
        result = await p.run(
            [0], job_fn, courtesy_delay_s=0, sleep=fake_sleep, max_attempts=1
        )
        return p, result

    p, result = asyncio.run(main())

    # A real RateLimitError would drop target to 2 immediately. A bare
    # RuntimeError -- even one whose text says "429" -- does not, because
    # nothing currently bridges fish_client's exception shape to pool.py's
    # typed hierarchy.
    assert p.target == 3, (
        "pool.py did not degrade on a 429-shaped RuntimeError -- confirms no "
        "bridge exists yet between fish_client's plain RuntimeError and "
        "pool.py's typed RateLimitError/ServerError/CallTimeoutError"
    )
    assert result.failed == {0: "Fish TTS request failed (429): rate limited, retry later"}


# ---------------------------------------------------------------------------
# 3. events.py `_scrub`: only rejects a value that IS ENTIRELY key-shaped
# (`^[A-Za-z0-9_-]{20,}$`). A key-shaped run of characters EMBEDDED inside a
# longer sentence -- exactly the shape of an error message that quotes back
# part of a request/response, e.g. an API's own "invalid key: <key>" text --
# is not a full-string match and sails through unscrubbed onto stdout.
#
# This directly threatens invariant #9 ("API keys never reach stdout, logs,
# or exception messages") for any path that funnels a raw exception message
# into an NDJSON event field (chunk_failed's `error` field, fed by exactly
# the kind of message fish_client/pool produce). events.py is T01-owned and
# frozen, so the fix needs a contract-change request, not a wave-1 ticket
# edit -- flagging it here because it's exactly the invariant this review
# was asked to verify, not because it is one of the five modules in scope.
#
# Expected to FAIL, proving the gap.
# ---------------------------------------------------------------------------


def test_scrub_rejects_key_shaped_substring_embedded_in_message(capsys):
    secret = "sk-fish-ABCDEFGHIJKLMNOPQRSTUVWXYZ01234"  # 40 chars, key-shaped
    message = f"Fish TTS request failed (401): invalid api key {secret} supplied"

    with pytest.raises(events.SecretLeakError):
        events.emit_chunk_failed("ch01_0000", 0, 10, message)

    assert secret not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 4. stitch.normalize_wav has ZERO test coverage anywhere in the suite (T04's
# own tests never call it; nothing else in the codebase calls it yet either,
# since T09 -- the only place that would wire "wav just arrived from Fish"
# through it -- hasn't been built). Prove it actually does what its
# docstring claims before anyone builds on top of it.
#
# Expected to PASS -- this is new coverage for previously-unverified-but-
# apparently-correct code, not a bug report.
# ---------------------------------------------------------------------------


def test_normalize_wav_actually_reencodes_mismatched_input(tmp_path, _ffmpeg_path):
    src = tmp_path / "mismatched.wav"
    subprocess.run(
        [
            _ffmpeg_path, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=stereo",
            "-t", "1", "-ar", "22050", "-ac", "2", str(src),
        ],
        check=True, capture_output=True,
    )

    dst = tmp_path / "out" / "normalized.wav"
    stitch.normalize_wav(src, dst)

    info = refaudio.ffprobe_inspect(dst)
    assert info["sample_rate"] == 44100
    assert info["channels"] == 1
    assert info["sample_fmt"] == "s16"


# ---------------------------------------------------------------------------
# 5. pool.py's courtesy delay: EVERY existing test in test_pool.py passes
# `courtesy_delay_s=0` to disable it for speed, so `_CourtesyGate` -- a named
# requirement in BUILD-PROMPT.md S8 ("insert a short fixed delay between the
# start of consecutive calls... even at full concurrency") -- has zero
# coverage of its actual behavior anywhere in the shipped suite. Prove it
# really does serialize call *starts* pool-wide (not per-worker) at full
# concurrency.
#
# Expected to PASS -- new coverage, not a bug report.
# ---------------------------------------------------------------------------


def test_courtesy_gate_serializes_call_starts_pool_wide():
    delay_s = 0.15
    start_times: list[float] = []

    async def job_fn(index: int, payload):
        start_times.append(time.monotonic())
        await asyncio.sleep(0.01)  # cheap "work"
        return index

    async def main():
        p = AdaptivePool(max_workers=3, target=3)
        return await p.run(
            list(range(4)), job_fn, courtesy_delay_s=delay_s, max_attempts=1
        )

    result = asyncio.run(main())
    assert len(result.results) == 4
    start_times.sort()

    gaps = [b - a for a, b in zip(start_times, start_times[1:])]
    print(f"\n[courtesy gate] inter-start gaps: {[round(g, 3) for g in gaps]}")
    # Even with 3 free workers, consecutive call starts must be spaced by
    # ~delay_s pool-wide -- if the gate were (bug) per-worker instead of
    # pool-wide, 3 of the 4 calls could start back-to-back.
    assert all(g >= delay_s * 0.8 for g in gaps), (
        "consecutive call starts were not spaced by the courtesy delay -- "
        "the gate may not be serializing pool-wide"
    )


# ---------------------------------------------------------------------------
# 6. AdaptivePool.__init__ does not clamp `target` to the floor of 1 the way
# `set_target` does. A caller that (by config bug) constructs
# AdaptivePool(max_workers=N, target=0) gets a pool where every worker event
# is cleared from the start -- every worker parks forever and never pulls a
# single job. `set_target` guards against this (`max(1, ...)`); `__init__`
# does not. Minor/defensive-coding gap, not exercised by any T05 test.
# ---------------------------------------------------------------------------


def test_init_with_target_zero_is_clamped_to_floor():
    p = AdaptivePool(max_workers=3, target=0)
    assert p.target == 1
    assert p.events[0].is_set()
