"""Tests for the Fish Audio TTS client (T03).

Every test here runs fully offline against `httpx.MockTransport` via the
shared `fake_transport` fixture from `tests/conftest.py`. No test may make a
real network call or spend Fish Audio credit.
"""

from __future__ import annotations

import shutil
import subprocess

import httpx
import msgpack
import pytest

import fish_client


def _patch_client(monkeypatch, fake_transport, handler):
    """Route fish_client's internal httpx.Client through a MockTransport.

    Returns the list of requests actually sent through the fake transport,
    so tests can assert on call count (not just on response handling).
    """
    calls = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    transport = fake_transport(counting_handler)
    real_client_cls = httpx.Client  # capture before patching to avoid recursion
    monkeypatch.setattr(
        fish_client.httpx,
        "Client",
        lambda *args, **kwargs: real_client_cls(transport=transport),
    )
    return calls


# --- Test 1: unspeakable-chunk guard -----------------------------------


def test_unspeakable_chunk_never_calls_api(monkeypatch, fake_transport, tmp_path):
    """A pure-punctuation chunk must produce a silent wav without the HTTP
    client ever being invoked. This asserts on the fake transport's CALL
    COUNT, not merely on the output file existing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Fish TTS API must not be called for unspeakable text")

    calls = _patch_client(monkeypatch, fake_transport, handler)

    text = "***"
    assert fish_client.is_speakable(text) is False

    out_path = tmp_path / "chunk_silent.wav"
    # This mirrors the guard the orchestrator (pool.py / narrate.py) is
    # required to implement: check is_speakable before ever calling
    # synthesize(), and route to write_silent_wav() instead.
    if fish_client.is_speakable(text):
        fish_client.synthesize(text, "fake-key", "s2.1-pro-free", b"ref-bytes", "")
    else:
        fish_client.write_silent_wav(out_path)

    assert len(calls) == 0
    assert out_path.exists()
    assert out_path.stat().st_size == 44 + int(44100 * 200 / 1000.0) * 2


# --- Test 2: model id in header, not body -------------------------------


def test_model_id_in_header_not_in_body(monkeypatch, fake_transport):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, content=b"RIFF-fake-audio-bytes")

    _patch_client(monkeypatch, fake_transport, handler)

    fish_client.synthesize(
        "Hello world.", "fake-key", "s2-pro", b"reference-bytes", "ref transcript"
    )

    req = captured["request"]
    assert req.headers["model"] == "s2-pro"

    body = msgpack.unpackb(req.content, raw=False)
    assert "model" not in body


# --- Test 3: reference audio is raw bytes, not base64 -------------------


def test_reference_audio_is_raw_bytes(monkeypatch, fake_transport):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, content=b"fake-audio-bytes")

    _patch_client(monkeypatch, fake_transport, handler)

    ref_bytes = b"RIFF\x00\x00\x00\x00WAVEfmt not-really-a-wav-but-raw-bytes"
    fish_client.synthesize("Hello.", "fake-key", "s1", ref_bytes, "a transcript")

    body = msgpack.unpackb(captured["request"].content, raw=False)
    audio = body["references"][0]["audio"]
    assert isinstance(audio, bytes)
    assert audio == ref_bytes


# --- Test 4: zero-length 2xx body raises --------------------------------


def test_zero_length_2xx_body_raises(monkeypatch, fake_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    _patch_client(monkeypatch, fake_transport, handler)

    with pytest.raises(RuntimeError, match="empty"):
        fish_client.synthesize("Hello.", "fake-key", "s1", b"ref-bytes", "")


# --- Test 5: non-2xx raises with response text included ------------------


def test_non_2xx_raises_with_response_text(monkeypatch, fake_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid reference audio: too short")

    _patch_client(monkeypatch, fake_transport, handler)

    with pytest.raises(RuntimeError) as exc_info:
        fish_client.synthesize("Hello.", "fake-key", "s1", b"ref-bytes", "")

    assert "invalid reference audio: too short" in str(exc_info.value)
    assert "400" in str(exc_info.value)


# --- Test 6: API key never leaks into exception messages ------------------


def test_api_key_not_in_exception_message_on_empty_body(monkeypatch, fake_transport):
    secret = "sk-fish-super-secret-do-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    _patch_client(monkeypatch, fake_transport, handler)

    with pytest.raises(RuntimeError) as exc_info:
        fish_client.synthesize("Hello.", secret, "s1", b"ref-bytes", "")

    assert secret not in str(exc_info.value)


def test_api_key_not_in_exception_message_on_non_2xx(monkeypatch, fake_transport):
    secret = "sk-fish-super-secret-do-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    _patch_client(monkeypatch, fake_transport, handler)

    with pytest.raises(RuntimeError) as exc_info:
        fish_client.synthesize("Hello.", secret, "s1", b"ref-bytes", "")

    assert secret not in str(exc_info.value)


# --- Test 7: hand-built silent wav is a valid 44-byte-header RIFF file ---


def test_silent_wav_is_valid_and_duration_within_10ms(tmp_path):
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe not found on PATH")

    path = tmp_path / "silent.wav"
    fish_client.write_silent_wav(path, duration_ms=200, sample_rate=44100)

    expected_data_bytes = int(44100 * 200 / 1000.0) * 2
    assert path.stat().st_size == 44 + expected_data_bytes

    with open(path, "rb") as f:
        header = f.read(44)
    assert header[0:4] == b"RIFF"
    assert header[8:16] == b"WAVEfmt "
    assert header[36:40] == b"data"

    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    duration_s = float(result.stdout.strip())
    assert abs(duration_s - 0.200) <= 0.010


# --- Test 8: is_speakable ------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (".", False),
        ("***", False),
        ("   ", False),
        ("—", False),  # em dash
        ("A", True),
        ("7", True),
        ("a.", True),
    ],
)
def test_is_speakable(text, expected):
    assert fish_client.is_speakable(text) is expected
