"""CLI entrypoint (chunk | tag | generate | stitch | run | prep-ref) and resumability. Owned by T09.

T08 owns only the `prep-ref` subcommand below (registration + handler). The
rest of this file — `chunk`, `tag`, `generate`, `stitch`, `run`, and any
shared resumability/orchestration logic — belongs to T09. `build_parser()` is
intentionally structured so each subcommand is added by its own
`_add_<name>_subcommand(subparsers)` helper that calls `subparsers.add_parser`
and sets `set_defaults(func=...)`; T09 can add more such helpers and call them
from `build_parser()` alongside `_add_prep_ref_subcommand` without touching
this ticket's code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

import chunker
import events
import fish_client
import preflight
import refaudio
import stitch
from models import Chunk, compute_text_hash
from pool import AdaptivePool, RateLimitError, ServerError
from tagger import base as tagger_base

DEFAULT_REFERENCE_OUTPUT = "reference/narrator.wav"
DEFAULT_PREP_REF_DURATION_S = 30.0
ROOT = Path(__file__).resolve().parent


def _add_prep_ref_subcommand(subparsers: "argparse._SubParsersAction") -> None:
    """Register `prep-ref`: convert an arbitrary recording into a conformant
    reference clip (44100 Hz, mono, pcm_s16le, <= 30s), optionally denoised."""
    parser = subparsers.add_parser(
        "prep-ref",
        help=(
            "Convert a recording into reference/narrator.wav "
            "(44100 Hz, mono, 16-bit PCM, <= 30s)."
        ),
    )
    parser.add_argument(
        "--input", required=True, help="Path to the source recording to convert."
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_REFERENCE_OUTPUT,
        help=f"Output path for the conformant clip (default: {DEFAULT_REFERENCE_OUTPUT}).",
    )
    parser.add_argument(
        "--start",
        type=float,
        default=None,
        help="Start offset into the input, in seconds (default: 0).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help=(
            "Clip duration in seconds, applied from --start "
            f"(default and max: {DEFAULT_PREP_REF_DURATION_S:.0f})."
        ),
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=f"Apply the cleanup chain '{refaudio.CLEAN_FILTER_CHAIN}' before encoding.",
    )
    parser.set_defaults(func=_run_prep_ref)


def _run_prep_ref(args: argparse.Namespace) -> int:
    duration = args.duration if args.duration is not None else DEFAULT_PREP_REF_DURATION_S
    duration = min(duration, DEFAULT_PREP_REF_DURATION_S)

    output_path = refaudio.run_prep_ref(
        args.input,
        args.output,
        start=args.start,
        duration=duration,
        clean=args.clean,
    )
    print(f"Wrote {output_path}")
    return 0


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_config() -> tuple[dict, dict]:
    defaults = {
        "paths": {"reference_dir": "reference/", "output_dir": "out/"},
        "chunking": {"target_chars": 200, "max_chars": 300, "min_chars": 60, "hard_split_chars": 600},
        "gaps": {"chunk_gap_ms": 900, "title_gap_ms": 3000, "chapter_gap_ms": 2000, "mid_paragraph_gap_ms": None},
        "concurrency": {"start": 3, "ramp_up": False},
        "fish": {"model": "s2.1-pro-free"},
        "tagger": {"engine": "codex-cli", "effort": "low"},
        "normalize_output": False,
    }
    config_path = ROOT / "config.json"
    loaded = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config = _merge(defaults, loaded)
    file_env = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    env = {k: v for k, v in file_env.items() if v is not None}
    env.update(os.environ)
    return config, env


def _effective_config(args: argparse.Namespace) -> tuple[dict, dict]:
    config, env = load_config()
    if args.gap_ms is not None:
        config["gaps"]["chunk_gap_ms"] = args.gap_ms
    elif env.get("GAP_MS"):
        config["gaps"]["chunk_gap_ms"] = int(env["GAP_MS"])
    if args.concurrency is not None:
        config["concurrency"]["start"] = max(1, args.concurrency)
    elif env.get("CONCURRENCY"):
        config["concurrency"]["start"] = max(1, int(env["CONCURRENCY"]))
    if args.model:
        config["fish"]["model"] = args.model
    elif env.get("FISH_MODEL"):
        config["fish"]["model"] = env["FISH_MODEL"]
    if args.tagger is not None:
        config["tagger"]["engine"] = args.tagger
    config["tagger"]["tag_model"] = args.tag_model or env.get("OPENAI_TAG_MODEL")
    if args.tag_model:
        env["OPENAI_TAG_MODEL"] = args.tag_model
    for name in ("CODEX_CLI_PATH", "CODEX_CLI_TIMEOUT_SECONDS"):
        if env.get(name):
            os.environ[name] = str(env[name])
    if args.ramp_up:
        config["concurrency"]["ramp_up"] = True
    return config, env


def _book_name(book: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", book.stem).strip("._") or "book"


def _out_dir(book: Path, config: dict) -> Path:
    value = Path(config["paths"].get("output_dir", "out/"))
    return (value if value.is_absolute() else ROOT / value) / _book_name(book)


def _chunks_file(book: Path, config: dict) -> Path:
    return _out_dir(book, config) / "chunks.json"


def _manifest_file(book: Path, chapter_id: str, config: dict) -> Path:
    return _out_dir(book, config) / chapter_id / "manifest.json"


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _write_chunks(book: Path, config: dict, chapters: list[dict]) -> None:
    _write_json(
        _chunks_file(book, config),
        [{**{k: v for k, v in c.items() if k != "chunks"}, "chunks": [x.to_dict() for x in c["chunks"]]} for c in chapters],
    )


def _read_chunks(book: Path, config: dict) -> list[dict]:
    path = _chunks_file(book, config)
    if not path.exists():
        return _chunk_book(book, config)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [{**{k: v for k, v in c.items() if k != "chunks"}, "chunks": [Chunk.from_dict(x) for x in c.get("chunks", [])]} for c in raw]


def _chunk_book(book: Path, config: dict) -> list[dict]:
    return chunker.build_chapter_chunks(
        book.read_text(encoding="utf-8"),
        target_chars=int(config["chunking"]["target_chars"]),
        max_chars=int(config["chunking"]["max_chars"]),
        min_chars=int(config["chunking"]["min_chars"]),
        hard_split_chars=int(config["chunking"]["hard_split_chars"]),
    )


def _select(chapters: list[dict], selection: str | None) -> list[dict]:
    if not selection:
        return chapters
    wanted = {x.strip().lower() for x in selection.split(",") if x.strip()}
    result = []
    for chapter in chapters:
        cid = chapter["chapter_id"].lower()
        if cid in wanted:
            result.append(chapter)
            continue
        match = re.fullmatch(r"(?:ch)?(\d+)(?:-(?:ch)?(\d+))?", selection.strip(), re.I)
        cm = re.fullmatch(r"ch(\d+)(?:_.*)?", cid, re.I)
        if match and cm and int(match.group(1)) <= int(cm.group(1)) <= int(match.group(2) or match.group(1)):
            result.append(chapter)
    return result


def _sanitize_config(config: dict) -> dict:
    return json.loads(json.dumps(config))


def _resolve_tagger(requested: str, env: dict) -> str | None:
    if requested == "none":
        return None
    if requested == "auto":
        resolved = tagger_base.resolve_auto(env)
        if resolved:
            if resolved == "codex-cli":
                events.log("tagger resolved to codex-cli; using the saved Codex CLI sign-in")
            else:
                events.log(f"tagger resolved to {resolved}; delivery tagging bills that account")
        else:
            events.log("no tagger available; continuing untagged (set ANTHROPIC_API_KEY, OPENAI_API_KEY + OPENAI_TAG_MODEL, or install/sign in to Codex CLI)")
        return resolved
    if requested == "codex-cli":
        if not tagger_base.codex_cli_available(env):
            raise RuntimeError(
                "--tagger codex-cli requires the Codex CLI; install it or set CODEX_CLI_PATH"
            )
        events.log("tagger resolved to codex-cli; using the saved Codex CLI sign-in")
        return requested
    required = "ANTHROPIC_API_KEY" if requested == "claude" else "OPENAI_API_KEY"
    if not env.get(required):
        raise RuntimeError(f"--tagger {requested} requires {required}")
    events.log(f"tagger resolved to {requested}; delivery tagging bills that account")
    return requested


def _tags_path(book: Path, config: dict) -> Path:
    return _out_dir(book, config) / "tags.json"


def _read_tags(book: Path, config: dict) -> dict[str, str]:
    path = _tags_path(book, config)
    if not path.exists():
        return {}
    return {x["chunk_id"]: x["tag"] for x in json.loads(path.read_text(encoding="utf-8")) if x.get("tag")}


def _run_chunk(args, config, env, resolved_tagger: str | None = None) -> list[dict]:
    book = Path(args.book).expanduser().resolve()
    if not book.exists():
        raise RuntimeError(f"book not found: {book}")
    chapters = _chunk_book(book, config)
    _write_chunks(book, config, chapters)
    events.emit_run_started(book.name, [c["chapter_id"] for c in chapters], _sanitize_config(config), resolved_tagger)
    for chapter in chapters:
        events.emit_chunked(chapter["chapter_id"], len(chapter["chunks"]))
    return chapters


def _run_tag(args, config, env, chapters: list[dict] | None = None, resolved_tagger: str | None = None) -> None:
    book = Path(args.book).expanduser().resolve()
    chapters = _select(chapters or _read_chunks(book, config), args.chapters)
    resolved = resolved_tagger if resolved_tagger is not None else _resolve_tagger(config["tagger"]["engine"], env)
    tags: dict[str, str] = {}
    for chapter in chapters:
        if resolved:
            if resolved == "codex" and config["tagger"].get("tag_model"):
                os.environ["OPENAI_TAG_MODEL"] = str(config["tagger"]["tag_model"])
            tags.update(tagger_base.get_backend(resolved)(chapter["chunks"]))
        tagged = sum(x.chunk_id in tags for x in chapter["chunks"])
        events.emit_tagged(chapter["chapter_id"], tagged, len(chapter["chunks"]) - tagged)
    tagger_base.write_tags_review(
        _tags_path(book, config),
        tagger_base.build_review_records([x for c in chapters for x in c["chunks"]], tags),
    )


def _manifest(path: Path) -> dict:
    if not path.exists():
        return {"chunks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("chunks", {})
        return data
    except (OSError, json.JSONDecodeError):
        return {"chunks": {}}


def _translate_fish_error(error: BaseException) -> BaseException:
    match = re.search(r"\((4\d\d|5\d\d)\)", str(error))
    if not match:
        return error
    code = int(match.group(1))
    if code == 429:
        return RateLimitError("Fish TTS rate limited")
    if code >= 500:
        return ServerError(f"Fish TTS server error ({code})")
    return error


async def _generate_async(args, config, env, chapters: list[dict]) -> int:
    preflight.check_ffmpeg_binaries()
    book = Path(args.book).expanduser().resolve()
    reference = Path(args.reference) if args.reference else Path(config["paths"].get("reference_dir", "reference/"))
    reference = reference.parent if reference.suffix.lower() == ".wav" else reference
    reference = reference if reference.is_absolute() else ROOT / reference
    reference_audio, reference_text = refaudio.load_reference(reference)
    if not env.get("FISH_API_KEY"):
        raise RuntimeError("FISH_API_KEY is not set")
    tags = _read_tags(book, config)
    pending = []
    selected = _select(chapters, args.chapters)
    for chapter in selected:
        manifest_path = _manifest_file(book, chapter["chapter_id"], config)
        manifest = _manifest(manifest_path)
        for chunk in chapter["chunks"]:
            tag = tags.get(chunk.chunk_id)
            applied = tagger_base.apply_tag(chunk.text, tag)
            text_hash = compute_text_hash(chunk.text, tag)
            wav = _out_dir(book, config) / chapter["chapter_id"] / f"{chunk.chunk_id}.wav"
            record = manifest["chunks"].get(chunk.chunk_id, {})
            if not args.force and record.get("status") == "done" and wav.exists() and record.get("text_hash") == text_hash:
                events.emit_chunk_done(chunk.chunk_id, chunk.position, len(chapter["chunks"]), 0.0, int(config["concurrency"]["start"]))
            else:
                pending.append((chapter, chunk, applied, text_hash, wav, manifest, manifest_path))
        _write_json(manifest_path, manifest)

    target = max(1, int(config["concurrency"]["start"]))
    pool = AdaptivePool(max_workers=target, target=target)

    async def job(index, item):
        chapter, chunk, text, text_hash, wav, manifest, manifest_path = item
        wav.parent.mkdir(parents=True, exist_ok=True)
        if not fish_client.is_speakable(text):
            fish_client.write_silent_wav(wav)
            return wav
        try:
            audio = await asyncio.to_thread(fish_client.synthesize, text, env["FISH_API_KEY"], config["fish"]["model"], reference_audio, reference_text)
        except Exception as error:
            translated = _translate_fish_error(error)
            raise translated
        raw = wav.with_suffix(".raw.wav")
        raw.write_bytes(audio)
        try:
            stitch.normalize_wav(raw, wav)
        finally:
            raw.unlink(missing_ok=True)
        return wav

    def completed(outcome):
        chapter, chunk, text, text_hash, wav, manifest, manifest_path = pending[outcome.index]
        entry = {"chunk_id": chunk.chunk_id, "text_hash": text_hash, "wav_path": str(wav), "status": "done" if outcome.success else "failed"}
        if outcome.success:
            events.emit_chunk_done(chunk.chunk_id, chunk.position, len(chapter["chunks"]), outcome.latency_s, outcome.concurrency)
        else:
            entry["error"] = outcome.error
            events.emit_chunk_failed(chunk.chunk_id, chunk.position, len(chapter["chunks"]), outcome.error or "unknown error")
        manifest["chunks"][chunk.chunk_id] = entry
        _write_json(manifest_path, manifest)

    result = await pool.run(pending, job, ramp_up=bool(config["concurrency"].get("ramp_up")), on_job_done=completed)
    for index, message in result.failed.items():
        events.log(f"chunk {pending[index][1].chunk_id} failed: {message}")
    return len(result.failed)


def _run_generate(args, config, env, chapters=None) -> int:
    book = Path(args.book).expanduser().resolve()
    return asyncio.run(_generate_async(args, config, env, chapters or _read_chunks(book, config)))


def _run_stitch(args, config, env, chapters=None) -> list[Path]:
    preflight.check_ffmpeg_binaries()
    book = Path(args.book).expanduser().resolve()
    outputs = []
    for chapter in _select(chapters or _read_chunks(book, config), args.chapters):
        manifest = _manifest(_manifest_file(book, chapter["chapter_id"], config))
        paths = []
        for chunk in chapter["chunks"]:
            default = _out_dir(book, config) / chapter["chapter_id"] / f"{chunk.chunk_id}.wav"
            wav = Path(manifest["chunks"].get(chunk.chunk_id, {}).get("wav_path", default))
            if wav.exists():
                paths.append((chunk, wav))
        if not paths:
            continue
        number = int(re.search(r"ch(\d+)", chapter["chapter_id"]).group(1)) if re.search(r"ch(\d+)", chapter["chapter_id"]) else 0
        title = chapter.get("title") or chapter["chapter_id"]
        out = stitch.chapter_output_path(_out_dir(book, config).parent, _book_name(book), number, title)
        output = stitch.stitch_chapter(paths, out, _out_dir(book, config) / "_gaps", _out_dir(book, config) / "_lists", gap_ms=int(config["gaps"]["chunk_gap_ms"]), title_gap_ms=int(config["gaps"]["title_gap_ms"]), mid_paragraph_gap_ms=config["gaps"].get("mid_paragraph_gap_ms"), normalize=bool(args.normalize or config.get("normalize_output")))
        outputs.append(output)
        events.emit_stitched(chapter["chapter_id"], str(output), stitch.ffprobe_duration_s(output))
    if args.single_file and outputs:
        outputs.append(stitch.stitch_book(outputs, _out_dir(book, config) / f"{_book_name(book)}.mp3", _out_dir(book, config) / "_gaps", _out_dir(book, config) / "_lists", chapter_gap_ms=int(config["gaps"]["chapter_gap_ms"])))
    return outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="narrate.py", description="Narrator pipeline CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_prep_ref_subcommand(subparsers)
    for name in ("chunk", "tag", "generate", "stitch", "run"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--book", required=True)
        sub.add_argument("--reference", default=None)
        sub.add_argument("--chapters", default=None)
        sub.add_argument("--gap-ms", type=int, default=None)
        sub.add_argument("--concurrency", type=int, default=None)
        sub.add_argument("--ramp-up", action="store_true")
        sub.add_argument("--normalize", action="store_true")
        sub.add_argument("--single-file", action="store_true")
        sub.add_argument("--force", action="store_true")
        sub.add_argument("--tagger", choices=("auto", "none", "claude", "codex", "codex-cli"), default=None)
        sub.add_argument("--tag-model", default=None)
        sub.add_argument("--tags-review", action="store_true")
        sub.add_argument("--model", default=None)
        sub.set_defaults(func=_run_pipeline_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as error:
        # stdout is an NDJSON contract for pipeline commands; never print a
        # traceback there. The error event is still machine-readable.
        if getattr(args, "command", None) != "prep-ref":
            events.emit_error(str(error))
        events.log(f"error: {error}")
        return 1


def _run_pipeline_command(args: argparse.Namespace) -> int:
    config, env = _effective_config(args)
    book = Path(args.book).expanduser().resolve()
    if args.command == "chunk":
        _run_chunk(args, config, env)
        return 0
    chapters = _read_chunks(book, config)
    if args.command == "tag":
        _run_tag(args, config, env, chapters)
        return 0
    if args.command == "generate":
        return _run_generate(args, config, env, chapters)
    if args.command == "stitch":
        _run_stitch(args, config, env, chapters)
        return 0
    resolved = _resolve_tagger(config["tagger"]["engine"], env)
    chapters = _run_chunk(args, config, env, resolved)
    _run_tag(args, config, env, chapters, resolved)
    if args.tags_review:
        events.log("--tags-review requested; stopping before TTS")
        return 0
    failures = _run_generate(args, config, env, chapters)
    outputs = _run_stitch(args, config, env, chapters)
    events.emit_done(book.stem, len(outputs), failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
