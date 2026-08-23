"""Tests for the ffmpeg/ffprobe presence check (T01 contracts)."""

from __future__ import annotations

import pytest

import preflight


def test_passes_when_both_binaries_present(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")
    preflight.check_ffmpeg_binaries()  # must not raise


def test_fails_naming_ffprobe_when_ffprobe_absent(monkeypatch):
    def fake_which(name):
        return "/usr/bin/ffmpeg" if name == "ffmpeg" else None

    monkeypatch.setattr(preflight.shutil, "which", fake_which)

    with pytest.raises(preflight.PreflightError) as exc_info:
        preflight.check_ffmpeg_binaries()

    assert "ffprobe" in str(exc_info.value)


def test_fails_naming_ffmpeg_when_ffmpeg_absent(monkeypatch):
    def fake_which(name):
        return "/usr/bin/ffprobe" if name == "ffprobe" else None

    monkeypatch.setattr(preflight.shutil, "which", fake_which)

    with pytest.raises(preflight.PreflightError) as exc_info:
        preflight.check_ffmpeg_binaries()

    assert "ffmpeg" in str(exc_info.value)


def test_fails_naming_both_when_both_absent(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

    with pytest.raises(preflight.PreflightError) as exc_info:
        preflight.check_ffmpeg_binaries()

    message = str(exc_info.value)
    assert "ffmpeg" in message
    assert "ffprobe" in message
