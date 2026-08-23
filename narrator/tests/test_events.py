"""Tests for the frozen NDJSON event emitters (T01 contracts)."""

from __future__ import annotations

import json

import pytest

import events


ALL_EMITTERS = [
    lambda: events.emit_run_started("Dragon Wings", ["ch01", "ch02"], {"gaps": {"chunk_gap_ms": 900}}, "claude"),
    lambda: events.emit_chunked("ch07", 42),
    lambda: events.emit_tagged("ch07", 40, 2),
    lambda: events.emit_chunk_done("ch07_0012", 12, 42, 1.23, 3),
    lambda: events.emit_chunk_failed("ch07_0012", 12, 42, "timeout"),
    lambda: events.emit_concurrency_changed(3, 1, "latency creep"),
    lambda: events.emit_stitched("ch07", "out/book/ch07/ch07.mp3", 812.5),
    lambda: events.emit_done("Dragon Wings", 12, 0),
    lambda: events.emit_error("unrecoverable failure"),
]


@pytest.mark.parametrize("emit", ALL_EMITTERS)
def test_each_emitter_prints_exactly_one_parseable_json_line(emit, capsys):
    emit()
    captured = capsys.readouterr()

    lines = [line for line in captured.out.splitlines() if line]
    assert len(lines) == 1

    parsed = json.loads(lines[0])
    assert "event" in parsed

    # Nothing should ever land on stderr from an emitter.
    assert captured.err == ""


def test_run_started_carries_resolved_tagger_backend(capsys):
    events.emit_run_started("Dragon Wings", ["ch01"], {}, "claude")
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["event"] == "run_started"
    assert parsed["tagger"] == "claude"


def test_run_started_tagger_is_null_when_untagged(capsys):
    events.emit_run_started("Dragon Wings", ["ch01"], {}, None)
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["tagger"] is None


def test_emitter_raises_rather_than_printing_a_key_shaped_value(capsys):
    key_shaped = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ"

    with pytest.raises(events.SecretLeakError):
        events.emit_error(key_shaped)

    # Nothing must have been printed before the raise.
    captured = capsys.readouterr()
    assert captured.out == ""


def test_emitter_raises_on_key_shaped_value_nested_in_config(capsys):
    key_shaped = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ"

    with pytest.raises(events.SecretLeakError):
        events.emit_run_started("book", [], {"fish": {"api_key": key_shaped}}, None)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_log_writes_to_stderr_not_stdout(capsys):
    events.log("debug message")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "debug message" in captured.err
