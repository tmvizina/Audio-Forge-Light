"""Tests for the Codex CLI delivery-tag adapter.

All subprocess calls are replaced with a local fake. These tests never invoke
Codex, consume account usage, or require a CLI login.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import tagger.codex_cli as codex_cli
from models import Chunk, compute_text_hash


def make_chunk(
    chunk_id: str = "ch07_0001",
    text: str = "The road narrowed as evening fell across the valley.",
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        position=1,
        text=text,
        char_count=len(text),
        text_hash=compute_text_hash(text),
        kind="body",
        boundary="ends_paragraph",
        over_cap=False,
    )


def _output_path(command: list[str]) -> Path:
    return Path(command[command.index("--output-last-message") + 1])


def test_cli_backend_uses_read_only_schema_and_shared_validator(monkeypatch):
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        schema_path = Path(command[command.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        assert schema["properties"]["items"]["items"]["additionalProperties"] is False
        _output_path(command).write_text(
            json.dumps(
                {
                    "items": [
                        {"chunk_id": "ch07_0001", "tag": "weary"},
                        {"chunk_id": "ch07_9999", "tag": "urgent"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(codex_cli, "_resolve_executable", lambda: "codex")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    assert codex_cli.tag([make_chunk()]) == {"ch07_0001": "weary"}
    command = captured["command"]
    assert command[:5] == ["codex", "exec", "--ephemeral", "--sandbox", "read-only"]
    assert command[-1] == "-"
    assert captured["kwargs"]["input"].endswith("in Markdown fences.")
    child_env = captured["kwargs"]["env"]
    assert all(name not in child_env for name in codex_cli._SECRET_ENV_VARS)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["check"] is False
    assert "OPENAI_API_KEY" not in codex_cli._prompt([make_chunk()])


def test_cli_backend_rejects_malformed_output(monkeypatch):
    def fake_run(command, **kwargs):
        _output_path(command).write_text("not json", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(codex_cli, "_resolve_executable", lambda: "codex")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    assert codex_cli.tag([make_chunk()]) == {}


def test_cli_backend_degrades_on_process_failure(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(codex_cli, "_resolve_executable", lambda: "codex")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    assert codex_cli.tag([make_chunk()]) == {}
