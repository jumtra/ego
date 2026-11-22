"""Host transcript locators and host-neutral trace event readers."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..domain.trace_schema import (
    CommandResultEvent,
    ToolCallEvent,
    TraceEvent,
    TurnEvent,
    UsageEvent,
)

_FILE_PATH_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_OUTPUT_HEAD_CHARS = 300


def claude_transcript_path(cwd: Path, session_id: str | None) -> Path | None:
    """Locate Claude Code's JSONL transcript for ``cwd`` and ``session_id``."""
    if not session_id or not all(c.isalnum() or c in "-_" for c in session_id):
        return None
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for base in (str(cwd), str(cwd.resolve())):
        path = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", base) / f"{session_id}.jsonl"
        if path.is_file():
            return path
    return None


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _entry_role(obj: dict[str, Any], message: dict[str, Any] | None) -> str | None:
    if message is not None and isinstance(message.get("role"), str) and message["role"]:
        return message["role"]
    if isinstance(obj.get("role"), str) and obj["role"]:
        return obj["role"]
    entry_type = obj.get("type")
    if entry_type in ("user", "assistant"):
        return entry_type
    return None


def read_events(
    path: Path | str,
    *,
    after_seq: int = 0,
    host: str = "claude",
) -> list[TraceEvent]:
    """Read host-neutral trace events from a transcript file without raising on bad lines."""
    if host != "claude":
        return []
    transcript = Path(path)
    events: list[TraceEvent] = []
    bash_commands: dict[str, str] = {}
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as handle:
            for seq, raw_line in enumerate(handle, start=1):
                try:
                    obj = json.loads(raw_line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue

                raw_msg = obj.get("message")
                message = raw_msg if isinstance(raw_msg, dict) else None
                role = _entry_role(obj, message)

                # Track earlier Bash tool_use_ids even when seq <= after_seq so later tool_results resolve.
                content = message.get("content") if message is not None else obj.get("content")
                is_assistant = role == "assistant" or obj.get("type") == "assistant"
                is_user = role == "user" or obj.get("type") == "user"

                if is_assistant and isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        if block.get("name") == "Bash":
                            tool_use_id = block.get("id")
                            inp = block.get("input")
                            cmd = inp.get("command") if isinstance(inp, dict) else None
                            if isinstance(tool_use_id, str) and isinstance(cmd, str):
                                bash_commands[tool_use_id] = cmd

                if seq <= after_seq:
                    continue

                if role is not None:
                    events.append(TurnEvent(seq=seq, role=role))

                if is_assistant and isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        tool = block.get("name")
                        if not isinstance(tool, str) or not tool:
                            continue
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        raw_id = block.get("id")
                        tool_use_id = raw_id if isinstance(raw_id, str) else None
                        command = (
                            inp.get("command")
                            if tool == "Bash" and isinstance(inp.get("command"), str)
                            else None
                        )
                        file_path = (
                            inp.get("file_path")
                            if tool in _FILE_PATH_TOOLS and isinstance(inp.get("file_path"), str)
                            else None
                        )
                        events.append(
                            ToolCallEvent(
                                seq=seq,
                                tool=tool,
                                command=command,
                                file_path=file_path,
                                tool_use_id=tool_use_id,
                            )
                        )

                if is_user and isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            continue
                        tool_use_id = block.get("tool_use_id")
                        if not isinstance(tool_use_id, str) or tool_use_id not in bash_commands:
                            continue
                        output_head = _extract_text(block.get("content"))[:_OUTPUT_HEAD_CHARS]
                        events.append(
                            CommandResultEvent(
                                seq=seq,
                                command=bash_commands[tool_use_id],
                                is_error=bool(block.get("is_error")),
                                output_head=output_head,
                            )
                        )

                if is_assistant and message is not None and isinstance(message.get("usage"), dict):
                    raw_model = message.get("model")
                    model = raw_model if isinstance(raw_model, str) else None
                    events.append(UsageEvent(seq=seq, usage=dict(message["usage"]), model=model))
    except OSError:
        return []
    return events
