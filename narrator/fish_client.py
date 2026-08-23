"""Hosted Fish Audio TTS client. Owned by T03.

Makes exactly one network call per invocation of `synthesize()` and either
returns the raw audio bytes or raises. Retries and concurrency belong to
`pool.py` (T05), not here.

Two counter-intuitive wire-format facts, hand-verified against the live API:

1. The model id goes in an HTTP header literally named `model` — never in the
   msgpack body. A `"model"` key in the body is silently ignored by the
   server; the request still succeeds against whatever the account's default
   model is, so the mistake never announces itself.
2. The reference audio goes into the msgpack body as raw bytes (via
   `msgpack.packb(..., use_bin_type=True)`), not base64 and not a data URL.
"""

from __future__ import annotations

import re
import struct

import httpx
import msgpack

FISH_URL = "https://api.fish.audio/v1/tts"
_SECRET_RUN_RE = re.compile(r"[A-Za-z0-9_-]{20,}")


def is_speakable(text: str) -> bool:
    """True if `text` contains at least one letter or digit.

    Chunks that fail this (pure punctuation, whitespace, a stray "***" that
    survived section splitting) must never be sent to the API — see
    `write_silent_wav` below. Sending one has produced up to ~47 seconds of
    grunting/noise in place of the fraction of a second it should occupy.
    """
    return bool(re.search(r"[A-Za-z0-9]", text))


def write_silent_wav(path, duration_ms: int = 200, sample_rate: int = 44100) -> None:
    """Hand-build a silent mono 16-bit PCM WAV file at `path`.

    A 44-byte RIFF/WAVE header followed by zeroed sample data. Deliberately
    uses no audio library (no numpy, no soundfile) — this is the local
    substitute for a TTS call on an unspeakable chunk, and it must keep the
    chunk's slot in the manifest and stitch order.
    """
    n_samples = int(sample_rate * duration_ms / 1000.0)
    data_bytes = n_samples * 2  # 16-bit mono
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_bytes))
        f.write(b"WAVEfmt ")
        f.write(
            struct.pack(
                "<IHHIIHH",
                16,  # fmt chunk size
                1,  # PCM
                1,  # mono
                sample_rate,
                sample_rate * 2,  # byte rate
                2,  # block align
                16,  # bits per sample
            )
        )
        f.write(b"data")
        f.write(struct.pack("<I", data_bytes))
        f.write(b"\x00" * data_bytes)


def synthesize(
    text: str,
    api_key: str,
    model_id: str,
    reference_audio: bytes,
    reference_text: str,
    timeout_s: float = 180.0,
) -> bytes:
    """Call the hosted Fish Audio TTS API once and return the raw audio bytes.

    Raises RuntimeError on any non-2xx response (with the response text
    included) or on a zero-length 2xx body. The API key is never included in
    any exception message.
    """
    body = {
        "text": text,
        "references": [{"audio": reference_audio, "text": reference_text}],
        "format": "wav",
        "sample_rate": 44100,
        "normalize": True,
        "temperature": 0.7,
        "top_p": 0.7,
        "chunk_length": 300,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/msgpack",
        "model": model_id,
    }

    with httpx.Client() as client:
        resp = client.post(
            FISH_URL,
            headers=headers,
            content=msgpack.packb(body, use_bin_type=True),
            timeout=timeout_s,
        )

    if resp.status_code // 100 != 2:
        # The response TEXT carries the real failure reason (bad reference
        # audio, invalid model id, rate limit) — surface it, not just the
        # status code. Never interpolate api_key into this message.
        safe_text = _SECRET_RUN_RE.sub("<redacted>", resp.text)
        raise RuntimeError(f"Fish TTS request failed ({resp.status_code}): {safe_text}")

    if len(resp.content) == 0:
        # A zero-length 2xx body is a failure, not silence. Never write it
        # to disk as though it were valid audio.
        raise RuntimeError("Fish TTS returned an empty body")

    return resp.content
