"""ffmpeg / ffprobe presence check.

Owned by T01. Run this at the start of any command that will eventually shell
out to ffmpeg or ffprobe, so a missing binary surfaces immediately instead of
three steps into a long run.
"""

from __future__ import annotations

import shutil


class PreflightError(RuntimeError):
    """Raised when a required external binary is not on PATH."""


def check_ffmpeg_binaries() -> None:
    """Verify both `ffmpeg` and `ffprobe` are on PATH.

    They are distinct binaries — a box can have one without the other — so
    each is checked separately, and the error message names exactly which
    one is missing.
    """
    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    if shutil.which("ffprobe") is None:
        missing.append("ffprobe")

    if missing:
        names = " and ".join(missing)
        raise PreflightError(
            f"{names} not found on PATH. Install ffmpeg (which includes ffprobe) "
            "and ensure both binaries are on your PATH before running narrate.py."
        )
