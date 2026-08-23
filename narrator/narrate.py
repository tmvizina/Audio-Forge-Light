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
import sys

import refaudio

DEFAULT_REFERENCE_OUTPUT = "reference/narrator.wav"
DEFAULT_PREP_REF_DURATION_S = 30.0


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


def build_parser() -> argparse.ArgumentParser:
    """Top-level CLI parser. Add new subcommands here via a dedicated
    `_add_<name>_subcommand(subparsers)` helper, following the `prep-ref`
    pattern above — this is the extension point T09 builds on."""
    parser = argparse.ArgumentParser(
        prog="narrate.py", description="Narrator pipeline CLI."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_prep_ref_subcommand(subparsers)
    # T09: _add_chunk_subcommand(subparsers)
    # T09: _add_tag_subcommand(subparsers)
    # T09: _add_generate_subcommand(subparsers)
    # T09: _add_stitch_subcommand(subparsers)
    # T09: _add_run_subcommand(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
