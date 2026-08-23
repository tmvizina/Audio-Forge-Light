"""Shared pytest fixtures for the narrator test suite.

Owned by T01. No later ticket may edit this file directly — file a
contract-change request instead (see ORCHESTRATION.md).

Fixtures:
- `fake_transport`: factory that builds an httpx.MockTransport from a handler
  function, so fish_client / tagger tests never hit the real network.
- `workspace`: a temp directory laid out like the project root (reference/,
  out/, input/) so tests never touch the real project tree.
- `make_wav`: factory that shells out to ffmpeg to generate a short silent
  WAV file of a given duration, for stitch/pool tests that need real audio
  on disk. No numpy/soundfile, per the project's no-heavy-deps rule.
- `wav_duration_s`: measures a WAV's duration via the stdlib `wave` module.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from typing import Callable

import httpx
import pytest


@pytest.fixture
def fake_transport():
    """Return a factory: fake_transport(handler) -> httpx.MockTransport.

    `handler` is a callable(request: httpx.Request) -> httpx.Response, exactly
    the signature httpx.MockTransport expects. Use it to build an httpx.Client
    or httpx.AsyncClient with `transport=...` so no test ever hits the network.
    """

    def _build(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    return _build


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A throwaway project root: <tmp>/reference, <tmp>/out, <tmp>/input."""
    (tmp_path / "reference").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "input").mkdir()
    return tmp_path


@pytest.fixture(scope="session")
def _ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        pytest.skip("ffmpeg not found on PATH")
    return path


@pytest.fixture
def make_wav(tmp_path: Path, _ffmpeg_path: str) -> Callable[..., Path]:
    """Factory fixture: make_wav(name="clip.wav", duration_s=0.5, sample_rate=44100) -> Path.

    Generates a short silent WAV via ffmpeg's anullsrc filter (lavfi) rather
    than a Python audio library, consistent with the project's ffmpeg-only
    audio-manipulation rule.
    """

    def _make(
        name: str = "clip.wav", duration_s: float = 0.5, sample_rate: int = 44100
    ) -> Path:
        out_path = tmp_path / name
        subprocess.run(
            [
                _ffmpeg_path,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r={sample_rate}:cl=mono",
                "-t",
                str(duration_s),
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        return out_path

    return _make


@pytest.fixture
def wav_duration_s() -> Callable[[Path], float]:
    """Helper: measure a WAV file's duration in seconds via the stdlib `wave` module."""

    def _duration(path: Path) -> float:
        with wave.open(str(path), "rb") as f:
            frames = f.getnframes()
            rate = f.getframerate()
            return frames / float(rate)

    return _duration
