"""Tests for reference audio prep + loading (T08: refaudio.py)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import refaudio


# ---------------------------------------------------------------------------
# 1 & 2: prep-ref produces a <=30s, 44100/mono/pcm_s16le output
# ---------------------------------------------------------------------------


def test_prep_ref_trims_60s_input_to_30s_or_less(workspace, make_wav, _ffmpeg_path):
    src = make_wav("source.wav", duration_s=60.0, sample_rate=44100)
    out = workspace / "reference" / "narrator.wav"

    refaudio.run_prep_ref(src, out)

    info = refaudio.ffprobe_inspect(out)
    assert info["duration_s"] <= 30.0 + 0.5


def test_prep_ref_output_is_44100_mono_pcm_s16le(workspace, make_wav, _ffmpeg_path):
    # Deliberately mismatched source: 22050 Hz stereo, so the assertion proves
    # prep-ref actually converts rather than passing a lucky match through.
    src = workspace / "source_stereo.wav"
    subprocess.run(
        [
            _ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=22050:cl=stereo",
            "-t",
            "2",
            "-ar",
            "22050",
            "-ac",
            "2",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out = workspace / "reference" / "narrator.wav"

    refaudio.run_prep_ref(src, out)

    info = refaudio.ffprobe_inspect(out)
    assert info["sample_rate"] == 44100
    assert info["channels"] == 1
    assert info["sample_fmt"] == "s16"


# ---------------------------------------------------------------------------
# 3: --start/--duration selects the requested window
# ---------------------------------------------------------------------------


def test_prep_ref_start_and_duration_select_window(workspace, make_wav):
    src = make_wav("source.wav", duration_s=20.0, sample_rate=44100)
    out = workspace / "reference" / "narrator.wav"

    refaudio.run_prep_ref(src, out, start=5.0, duration=8.0)

    info = refaudio.ffprobe_inspect(out)
    assert info["duration_s"] == pytest.approx(8.0, abs=0.3)


def test_build_prep_ref_argv_places_start_and_duration_correctly():
    argv = refaudio.build_prep_ref_argv(
        "in.wav", "out.wav", start=5.0, duration=8.0
    )
    assert argv[argv.index("-ss") + 1] == "5.0"
    assert argv[argv.index("-i") + 1] == "in.wav"
    # -t must come after -i so duration is measured from the seek point.
    assert argv.index("-t") > argv.index("-i")
    assert argv[argv.index("-t") + 1] == "8.0"


# ---------------------------------------------------------------------------
# 4: --clean adds the filter chain; its absence does not
# ---------------------------------------------------------------------------


def test_clean_flag_adds_filter_chain():
    argv = refaudio.build_prep_ref_argv("in.wav", "out.wav", clean=True)
    assert "-af" in argv
    assert argv[argv.index("-af") + 1] == "highpass=f=80,afftdn"


def test_no_clean_flag_omits_filter_chain():
    argv = refaudio.build_prep_ref_argv("in.wav", "out.wav", clean=False)
    assert "-af" not in argv


# ---------------------------------------------------------------------------
# 5: the loader reads the reference wav exactly once
# ---------------------------------------------------------------------------


def test_loader_reads_reference_wav_exactly_once(workspace, make_wav, monkeypatch):
    clip = make_wav("clip.wav", duration_s=1.0, sample_rate=44100)
    ref_dir = workspace / "reference"
    shutil.copy(clip, ref_dir / "narrator.wav")
    (ref_dir / "narrator.txt").write_text("hello narrator", encoding="utf-8")

    # Spy on Path.read_bytes (what the loader uses to pull the wav into
    # memory) rather than builtins.open: pathlib binds to io.open directly,
    # which does not observe a builtins.open monkeypatch.
    opened: list[str] = []
    real_read_bytes = Path.read_bytes

    def spy_read_bytes(self, *args, **kwargs):
        opened.append(str(self))
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)

    audio_bytes, transcript = refaudio.load_reference(ref_dir)
    assert transcript == "hello narrator"

    # Reuse the same in-memory bytes object across 5 simulated chunk sends to
    # a fake client — the loader must not be asked to re-open the wav for any
    # of them.
    class FakeFishClient:
        def __init__(self):
            self.sent: list[bytes] = []

        def generate_chunk(self, reference_bytes: bytes, transcript: str, text: str):
            self.sent.append(reference_bytes)

    client = FakeFishClient()
    for i in range(5):
        client.generate_chunk(audio_bytes, transcript, f"chunk {i}")

    assert len(client.sent) == 5
    assert all(sent is audio_bytes for sent in client.sent)

    wav_opens = [p for p in opened if p.endswith("narrator.wav")]
    assert len(wav_opens) == 1


# ---------------------------------------------------------------------------
# 6: missing narrator.txt -> stderr warning + empty transcript, not an error
# ---------------------------------------------------------------------------


def test_missing_transcript_warns_on_stderr_and_returns_empty(
    workspace, make_wav, capsys
):
    clip = make_wav("clip.wav", duration_s=1.0, sample_rate=44100)
    ref_dir = workspace / "reference"
    shutil.copy(clip, ref_dir / "narrator.wav")
    assert not (ref_dir / "narrator.txt").exists()

    audio_bytes, transcript = refaudio.load_reference(ref_dir)

    assert transcript == ""
    assert isinstance(audio_bytes, bytes) and len(audio_bytes) > 0

    captured = capsys.readouterr()
    assert captured.out == ""  # never on stdout (NDJSON channel)
    assert "narrator.txt" in captured.err
    assert "warning" in captured.err.lower()


# ---------------------------------------------------------------------------
# 7: a 45s reference fails validation, naming prep-ref
# ---------------------------------------------------------------------------


def test_45s_reference_fails_naming_prep_ref(workspace, make_wav):
    clip = make_wav("clip.wav", duration_s=45.0, sample_rate=44100)
    ref_dir = workspace / "reference"
    shutil.copy(clip, ref_dir / "narrator.wav")

    with pytest.raises(refaudio.ReferenceAudioError) as exc_info:
        refaudio.load_reference(ref_dir)

    assert "prep-ref" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 8: a 22050 Hz or stereo reference fails validation, naming prep-ref
# ---------------------------------------------------------------------------


def test_22050hz_reference_fails_naming_prep_ref(workspace, _ffmpeg_path):
    ref_dir = workspace / "reference"
    subprocess.run(
        [
            _ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=22050:cl=mono",
            "-t",
            "2",
            "-ar",
            "22050",
            "-ac",
            "1",
            str(ref_dir / "narrator.wav"),
        ],
        check=True,
        capture_output=True,
    )

    with pytest.raises(refaudio.ReferenceAudioError) as exc_info:
        refaudio.load_reference(ref_dir)

    assert "prep-ref" in str(exc_info.value)


def test_stereo_reference_fails_naming_prep_ref(workspace, _ffmpeg_path):
    ref_dir = workspace / "reference"
    subprocess.run(
        [
            _ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "2",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(ref_dir / "narrator.wav"),
        ],
        check=True,
        capture_output=True,
    )

    with pytest.raises(refaudio.ReferenceAudioError) as exc_info:
        refaudio.load_reference(ref_dir)

    assert "prep-ref" in str(exc_info.value)
