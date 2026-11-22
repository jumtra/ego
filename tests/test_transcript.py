"""Tests for host-neutral trace events and the Claude transcript reader."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ego.adapters.transcript import claude_transcript_path, read_events
from ego.domain.trace_schema import (
    CommandResultEvent,
    ToolCallEvent,
    TurnEvent,
    UsageEvent,
)


def test_read_events_parses_tools_results_usage_and_skips_bad_lines(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    long_output = "x" * 350
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_bash_ok",
                            "name": "Bash",
                            "input": {"command": "pytest -q"},
                        },
                        {
                            "type": "tool_use",
                            "id": "tool_edit",
                            "name": "Edit",
                            "input": {"file_path": "src/app.py"},
                        },
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            }
        ),
        "{this is malformed json",
        json.dumps(["not", "a", "dict"]),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_bash_ok",
                            "is_error": False,
                            "content": long_output,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_edit",
                            "is_error": False,
                            "content": "edited",
                        },
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_bash_fail",
                            "name": "Bash",
                            "input": {"command": "false"},
                        }
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_bash_fail",
                            "is_error": True,
                            "content": [{"type": "text", "text": "exit code 1"}],
                        }
                    ],
                },
            }
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    events = read_events(transcript)

    assert events == [
        TurnEvent(seq=1, role="assistant"),
        ToolCallEvent(
            seq=1,
            tool="Bash",
            command="pytest -q",
            file_path=None,
            tool_use_id="tool_bash_ok",
        ),
        ToolCallEvent(
            seq=1,
            tool="Edit",
            command=None,
            file_path="src/app.py",
            tool_use_id="tool_edit",
        ),
        UsageEvent(
            seq=1,
            usage={"input_tokens": 100, "output_tokens": 20},
            model="claude-sonnet-4-5",
        ),
        TurnEvent(seq=4, role="user"),
        CommandResultEvent(
            seq=4,
            command="pytest -q",
            is_error=False,
            output_head="x" * 300,
        ),
        TurnEvent(seq=5, role="assistant"),
        ToolCallEvent(
            seq=5,
            tool="Bash",
            command="false",
            file_path=None,
            tool_use_id="tool_bash_fail",
        ),
        TurnEvent(seq=6, role="user"),
        CommandResultEvent(
            seq=6,
            command="false",
            is_error=True,
            output_head="exit code 1",
        ),
    ]


def test_read_events_after_seq_filters_and_resolves_prior_bash(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "b1",
                            "name": "Bash",
                            "input": {"command": "echo hi"},
                        }
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "b1",
                            "is_error": False,
                            "content": "hi",
                        }
                    ],
                },
            }
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    events = read_events(transcript, after_seq=1)

    assert events == [
        TurnEvent(seq=2, role="user"),
        CommandResultEvent(seq=2, command="echo hi", is_error=False, output_head="hi"),
    ]


def test_read_events_missing_file_and_non_claude_host_return_empty(tmp_path: Path):
    assert read_events(tmp_path / "missing.jsonl") == []
    assert read_events(tmp_path) == []

    existing = tmp_path / "codex.jsonl"
    existing.write_text('{"type": "assistant"}\n', encoding="utf-8")
    assert read_events(existing, host="codex") == []
    assert read_events(existing, host="opencode") == []


def test_claude_transcript_path_resolves_and_validates(tmp_path: Path, monkeypatch):
    home = tmp_path / "claude_home"
    proj = tmp_path / "repo"
    proj.mkdir()
    target_dir = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(proj.resolve()))
    target_dir.mkdir(parents=True)
    session_file = target_dir / "sess_1.jsonl"
    session_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))

    assert claude_transcript_path(proj, "sess_1") == session_file
    assert claude_transcript_path(proj, "missing") is None
    assert claude_transcript_path(proj, "../escape") is None
    assert claude_transcript_path(proj, None) is None
