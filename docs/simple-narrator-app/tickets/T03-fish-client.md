# T03 — Fish Audio TTS client

**Depends on:** T01. **Blocks:** T09.
**Read:** `BUILD-PROMPT.md` §5 (lines 360–526). **Nothing else.**

## Why this ticket exists

The wire format is where a fresh build burns hours, and two of its details are
counter-intuitive enough that a reasonable-looking implementation fails silently rather
than loudly. Both were hand-verified. Transcribe them; do not adjust them to match what
you expect a REST API to look like.

## Files you own

`fish_client.py`.

## Files you must NOT touch

Everything else. You do not own retries or concurrency — that is T05 (`pool.py`). This
module makes **one** call and either returns bytes or raises. Keep it that simple.

## Task

**1. The request.** `POST https://api.fish.audio/v1/tts` with exactly three headers:

| header | value |
|---|---|
| `Authorization` | `Bearer <FISH_API_KEY>` |
| `Content-Type` | `application/msgpack` |
| `model` | the model id string |

**The model id goes in a header literally named `model` — not in the request body.** This
is the single most common way to get this API wrong. A `"model"` key in the body is not
where the server reads it from, so you lose control of which model runs, and because the
request still succeeds and still returns audio, the mistake does not announce itself.

Accepted ids: `s2.1-pro-free` (default), `s2-pro`, `s1`, `speech-1.5`.

**2. The body — msgpack, raw reference bytes.** `msgpack.packb(body, use_bin_type=True)`.
The reference audio goes in as **raw bytes**, not base64, not a data URL:

```python
body = {
    "text": text,          # verbatim — a leading "[tag] " marker must survive as-is
    "references": [{"audio": reference_audio, "text": reference_text}],  # transcript may be ""
    "format": "wav",
    "sample_rate": 44100,
    "normalize": True,
    "temperature": 0.7,
    "top_p": 0.7,
    "chunk_length": 300,
}
```

`temperature` clamps 0–2, `top_p` clamps 0–1, `chunk_length` clamps 100–300. The
`0.7 / 0.7` pair is the tuned narration default — calmer and more consistent take over
take than a hotter dialogue setting. `chunk_length: 300` means a ~200-char chunk renders in
one uninterrupted pass rather than being re-split internally by the API.

Voice comes **only** from the reference clip. There is no voice-model-id parameter and you
must never introduce one.

**3. The response.** The body **is** the audio bytes — there is no JSON envelope to unwrap
on success. Two failure rules:

- Non-2xx: read the response **text** and surface it. It carries the actual reason (bad
  reference audio, invalid model id, rate limit), which the status code alone does not.
- A **zero-length body on a 2xx is a failure, not silence.** Raise. Never write a 0-byte
  or near-empty file to disk as though it were valid audio.

**4. The unspeakable-text guard.** Exactly this predicate:

```python
def is_speakable(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text))
```

If it returns `False`, **never call the API.** Write a locally generated silent wav
instead, and keep the chunk's position in the manifest and stitch order — skip the network
call, never the slot.

This is not a tidiness rule. A chunk of pure punctuation (a stray `"."`, a `"***"` that
survived section splitting) sent to this API has produced **up to roughly 47 seconds of
grunting** in place of the fraction of a second it should occupy, silently corrupting the
finished chapter.

**5. The silent wav, hand-built.** A 44-byte RIFF/WAVE header plus zeroed 16-bit PCM data.
Default 200 ms, 44100 Hz, mono. **No audio library** — no numpy, no soundfile. This is a
dozen lines of `struct` and it is deliberately not a dependency.

**6. Signature.** Freeze this, because T09 calls it and T05 wraps it:

```python
def synthesize(text: str, api_key: str, model_id: str,
               reference_audio: bytes, reference_text: str,
               timeout_s: float = 180.0) -> bytes: ...
```

The API key is a parameter, never read from the environment inside this module, and
**never logged** — not in an exception message, not in a debug line, not in a repr of the
request.

## Invariants at risk in this ticket

- **#7** — model id in the header, not the body.
- **#8** — reference audio as raw bytes in the msgpack map.
- **#2** — never call the TTS on a chunk with no alphanumerics.
- **#9** — the key must not reach any log or error path.
- **#11** — hand-built WAV header, no audio library.

## Definition of done

```bash
pytest tests/test_fish_client.py -v
```

All tests run **offline** against a faked HTTP transport (`pytest-httpx` or a monkeypatched
`httpx` client). **No test may make a real network call or spend credit.**

Ship at least these tests:

1. **The acceptance test from §13.3**: a pure-punctuation chunk produces a silent wav and
   the HTTP client is **never invoked**. Assert on the fake transport's call count being
   zero — this is the test, not a nice-to-have.
2. The request carries a header named `model` with the right value, and the decoded msgpack
   body contains **no** `model` key.
3. The decoded msgpack body's `references[0]["audio"]` is `bytes` and equals the input
   bytes exactly — not a base64 string.
4. A 2xx with a zero-length body raises.
5. A non-2xx raises with the response text included in the message.
6. The API key does not appear in the raised exception message for either failure path.
7. The generated silent wav is a valid 44-byte-header RIFF file of the requested duration —
   verify with `ffprobe` that its duration is within 10 ms of 200 ms.
8. `is_speakable` returns `False` for `"."`, `"***"`, `"   "`, `"—"` and `True` for
   `"A"`, `"7"`, `"a."`.

## Report back

- The final `synthesize` implementation, verbatim.
- Confirmation that the fake-transport assertion in test 1 checks **call count**, not just
  output.
- The decoded body of one captured request, so the orchestrator can eyeball the wire shape.
- Confirmation that no test touches the network.
