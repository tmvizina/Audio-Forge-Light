"""Reference audio prep (prep-ref). Owned by T08.

The user's voice reference lives at `reference/narrator.wav`, with an optional
sibling `reference/narrator.txt` holding its transcript. This module provides:

- `build_prep_ref_argv` / `run_prep_ref`: convert an arbitrary recording into a
  conformant reference clip (44100 Hz, mono, pcm_s16le, <= 30s), with an
  optional `--clean` denoise chain.
- `ffprobe_inspect` / `validate_reference`: inspect and validate an existing
  reference clip against that same contract, failing early and actionably.
- `load_reference`: read the reference bytes + transcript exactly once, for
  the caller to reuse across every Fish API call in the run.

Invariant #11: all audio inspection/conversion here is ffmpeg/ffprobe
subprocess work. No soundfile, no numpy.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from events import log

REQUIRED_SAMPLE_RATE = 44100
REQUIRED_CHANNELS = 1
REQUIRED_SAMPLE_FMT = "s16"  # ffprobe's name for 16-bit PCM (pcm_s16le)
MAX_DURATION_S = 30.0
# Small tolerance for encoder/container rounding (e.g. a 30.00s request can
# probe back as 30.01s) so a clip trimmed to exactly the limit never fails
# its own validation.
_DURATION_TOLERANCE_S = 0.5

CLEAN_FILTER_CHAIN = "highpass=f=80,afftdn"


class ReferenceAudioError(RuntimeError):
    """Raised when the reference clip is missing or fails validation.

    Every message names `prep-ref` so the user knows exactly which command
    fixes the problem.
    """


def build_prep_ref_argv(
    input_path: Path | str,
    output_path: Path | str,
    *,
    start: float | None = None,
    duration: float | None = None,
    clean: bool = False,
) -> list[str]:
    """Build the `prep-ref` ffmpeg argv without running it.

    Kept separate from `run_prep_ref` so callers/tests can inspect the exact
    command (and print it for the user) without touching disk.

    - `start`: seek offset in seconds, applied as a fast input-side `-ss`
      before `-i`. Omitted entirely when None (start from 0).
    - `duration`: clip length in seconds, applied as `-t` after `-i` (so it
      is relative to `start`). Defaults to `MAX_DURATION_S` (30s) when None;
      the caller is responsible for not requesting more than 30s.
    - `clean`: when True, inserts the `-af highpass=f=80,afftdn` cleanup
      chain before the final encode args.
    """
    argv: list[str] = ["ffmpeg", "-y"]
    if start is not None:
        argv += ["-ss", str(start)]
    argv += ["-i", str(input_path)]
    argv += ["-t", str(duration if duration is not None else MAX_DURATION_S)]
    if clean:
        argv += ["-af", CLEAN_FILTER_CHAIN]
    argv += [
        "-ar",
        str(REQUIRED_SAMPLE_RATE),
        "-ac",
        str(REQUIRED_CHANNELS),
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    return argv


def run_prep_ref(
    input_path: Path | str,
    output_path: Path | str,
    *,
    start: float | None = None,
    duration: float | None = None,
    clean: bool = False,
) -> Path:
    """Run `prep-ref`: convert `input_path` into a conformant reference clip
    at `output_path`. Creates the parent directory if needed. Raises
    `ReferenceAudioError` (wrapping the ffmpeg failure) if ffmpeg exits
    non-zero."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    argv = build_prep_ref_argv(
        input_path, output_path, start=start, duration=duration, clean=clean
    )
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise ReferenceAudioError(
            f"prep-ref failed converting {input_path} -> {output_path}: "
            f"{exc.stderr.strip() if exc.stderr else exc}"
        ) from exc
    return output_path


def ffprobe_inspect(path: Path | str) -> dict:
    """Inspect a wav file's audio stream via ffprobe.

    Returns {"sample_rate": int, "channels": int, "sample_fmt": str,
    "duration_s": float}. Raises `ReferenceAudioError` if the file does not
    exist or ffprobe fails to read it.
    """
    path = Path(path)
    if not path.exists():
        raise ReferenceAudioError(
            f"Reference audio not found: {path}. Run `prep-ref` to create it "
            "from a source recording, e.g. "
            f"`python narrate.py prep-ref --input <file> --output {path}`."
        )

    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels,sample_fmt:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise ReferenceAudioError(
            f"ffprobe could not read {path}: "
            f"{exc.stderr.strip() if exc.stderr else exc}. Re-run `prep-ref` "
            "against a valid source recording."
        ) from exc

    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise ReferenceAudioError(
            f"{path} has no audio stream ffprobe could read. Re-run `prep-ref` "
            "against a valid source recording."
        )
    stream = streams[0]
    fmt = data.get("format") or {}

    return {
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "sample_fmt": str(stream["sample_fmt"]),
        "duration_s": float(fmt["duration"]),
    }


def validate_reference(path: Path | str) -> dict:
    """Validate `path` against the reference-audio contract: must exist, be
    <= 30s, and be 44100 Hz / mono / 16-bit PCM. Returns the ffprobe info
    dict on success. Raises `ReferenceAudioError` (naming `prep-ref`) on any
    failure, so a whole book is never generated in a degraded voice."""
    path = Path(path)
    info = ffprobe_inspect(path)  # raises if missing, naming prep-ref

    if info["duration_s"] > MAX_DURATION_S + _DURATION_TOLERANCE_S:
        raise ReferenceAudioError(
            f"Reference audio at {path} is {info['duration_s']:.1f}s, longer "
            f"than the {MAX_DURATION_S:.0f}s limit. Re-run `prep-ref` to trim "
            f"it, e.g. `python narrate.py prep-ref --input <file> --output "
            f"{path} --duration 30`."
        )

    if (
        info["sample_rate"] != REQUIRED_SAMPLE_RATE
        or info["channels"] != REQUIRED_CHANNELS
        or info["sample_fmt"] != REQUIRED_SAMPLE_FMT
    ):
        raise ReferenceAudioError(
            f"Reference audio at {path} is {info['sample_rate']} Hz / "
            f"{info['channels']}ch / {info['sample_fmt']}, but must be "
            f"{REQUIRED_SAMPLE_RATE} Hz / mono / 16-bit PCM (pcm_s16le). "
            f"Re-run `prep-ref` to convert it, e.g. `python narrate.py "
            f"prep-ref --input <file> --output {path}`."
        )

    return info


def load_reference(reference_dir: Path | str = Path("reference")) -> tuple[bytes, str]:
    """Read `reference/narrator.wav` (+ optional sibling `narrator.txt`)
    exactly once and return `(audio_bytes, transcript_str)`.

    Call this once at process startup and thread the returned `bytes` object
    through every Fish API call in the run — never re-open or re-read the
    wav file per chunk. Validates the wav via `validate_reference` before
    reading it, so a bad reference fails before any API calls are made.

    A missing `narrator.txt` is a warning on stderr (via `events.log`), not
    an error: an empty transcript ("") is legal and accepted by the Fish
    API, just lower quality.
    """
    reference_dir = Path(reference_dir)
    wav_path = reference_dir / "narrator.wav"
    txt_path = reference_dir / "narrator.txt"

    validate_reference(wav_path)
    audio_bytes = wav_path.read_bytes()

    if txt_path.exists():
        transcript = txt_path.read_text(encoding="utf-8").strip()
    else:
        log(
            f"warning: {txt_path} not found; proceeding with an empty "
            "transcript. Cloning quality is measurably better with a "
            "transcript present — consider adding one."
        )
        transcript = ""

    return audio_bytes, transcript
