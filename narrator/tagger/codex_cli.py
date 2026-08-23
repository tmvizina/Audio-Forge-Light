"""Codex CLI delivery-tag adapter.

This backend invokes the locally installed Codex CLI instead of importing the
OpenAI SDK or reading ``OPENAI_API_KEY``. It is intentionally a separate
backend from ``tagger.codex`` so existing API-key users keep their current
behavior while users signed into the CLI can select ``codex-cli``.

The invocation follows the official non-interactive CLI contract:
``codex exec --ephemeral --sandbox read-only --output-schema ... -o ...``.
The schema-constrained final response is read from the temporary output file
and still passes through the shared validator before any tag is accepted.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from models import Chunk
from tagger.base import (
    TAG_JSON_SCHEMA,
    TAG_SYSTEM_GUIDE,
    build_tag_prompt,
    parse_and_validate_response,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_DIAGNOSTIC_CHARS = 500
_SECRET_ENV_VARS = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "FISH_API_KEY",
)


def _resolve_executable() -> str | None:
    configured = os.environ.get("CODEX_CLI_PATH") or "codex"
    candidate = os.path.expanduser(configured)
    if os.path.isfile(candidate):
        return candidate
    return shutil.which(configured)


def _timeout_seconds() -> float:
    raw = os.environ.get("CODEX_CLI_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "codex-cli tagger: invalid CODEX_CLI_TIMEOUT_SECONDS; using %.0fs",
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS
    return max(5.0, min(value, 3600.0))


def _cli_schema() -> dict:
    """Return a strict copy of the shared schema for ``--output-schema``."""
    schema = json.loads(json.dumps(TAG_JSON_SCHEMA))
    schema["additionalProperties"] = False
    item_schema = schema["properties"]["items"]["items"]
    item_schema["additionalProperties"] = False
    return schema


def _prompt(batch: list[Chunk]) -> str:
    return (
        f"{TAG_SYSTEM_GUIDE}\n\n"
        "The following JSON array contains the passages to tag:\n"
        f"{build_tag_prompt(batch)}\n\n"
        "Return the requested JSON object as your final answer. Do not use "
        "tools, inspect files, edit files, add commentary, or wrap the JSON "
        "in Markdown fences."
    )


def _read_json_text(output_path: Path, stdout: str) -> str | None:
    try:
        text = output_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    text = text.strip()
    if text:
        return text
    stdout = (stdout or "").strip()
    return stdout or None


def _parse_items(text: str | None) -> list | None:
    if not text:
        return None
    candidates = [text]
    if "```" in text:
        candidates.append(text.replace("```json", "").replace("```", "").strip())
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload["items"]
    return None


def _diagnostic(stderr: str) -> str:
    compact = " ".join((stderr or "").split())
    if len(compact) > _MAX_DIAGNOSTIC_CHARS:
        return compact[:_MAX_DIAGNOSTIC_CHARS] + "..."
    return compact


def _child_environment() -> dict[str, str]:
    """Keep CLI auth/config, but never pass provider secrets to the agent."""
    child_env = os.environ.copy()
    for name in _SECRET_ENV_VARS:
        child_env.pop(name, None)
    return child_env


def tag(batch: list[Chunk]) -> dict[str, str]:
    """Tag a batch with the locally authenticated Codex CLI.

    Tagging is an enhancement: missing CLI installation, missing login,
    timeout, malformed output, or any other subprocess failure logs a safe
    diagnostic and returns an empty mapping so the narration pipeline can
    continue untagged.
    """
    if not batch:
        return {}

    executable = _resolve_executable()
    if not executable:
        logger.error(
            "codex-cli tagger: Codex CLI not found; install it or set CODEX_CLI_PATH"
        )
        return {}

    with tempfile.TemporaryDirectory(prefix="audio-forge-codex-") as temp_dir:
        temp_root = Path(temp_dir)
        schema_path = temp_root / "tag-schema.json"
        output_path = temp_root / "tag-output.json"
        schema_path.write_text(
            json.dumps(_cli_schema(), ensure_ascii=False), encoding="utf-8"
        )
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=_child_environment(),
                input=_prompt(batch),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_timeout_seconds(),
                check=False,
                shell=False,
            )
        except FileNotFoundError:
            logger.error("codex-cli tagger: executable disappeared before launch")
            return {}
        except subprocess.TimeoutExpired:
            logger.error("codex-cli tagger: timed out waiting for Codex")
            return {}
        except OSError as exc:
            logger.error("codex-cli tagger: could not start Codex (%s)", type(exc).__name__)
            return {}

        if result.returncode != 0:
            detail = _diagnostic(result.stderr)
            logger.error(
                "codex-cli tagger: Codex exited with status %s%s",
                result.returncode,
                f": {detail}" if detail else "",
            )
            return {}

        raw_text = _read_json_text(output_path, result.stdout)
        raw_items = _parse_items(raw_text)
        if raw_items is None:
            logger.error("codex-cli tagger: malformed JSON response")
            return {}

        chunk_by_id = {chunk.chunk_id: chunk for chunk in batch}
        accepted, _rejected = parse_and_validate_response(raw_items, chunk_by_id)
        return accepted
