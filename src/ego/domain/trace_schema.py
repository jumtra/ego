"""Host-neutral trace event dataclasses emitted by transcript adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCallEvent:
    seq: int
    tool: str
    command: str | None = None
    file_path: str | None = None
    tool_use_id: str | None = None


@dataclass(frozen=True)
class CommandResultEvent:
    seq: int
    command: str
    is_error: bool
    output_head: str


@dataclass(frozen=True)
class UsageEvent:
    seq: int
    usage: dict[str, Any]
    model: str | None = None


@dataclass(frozen=True)
class TurnEvent:
    seq: int
    role: str


TraceEvent = ToolCallEvent | CommandResultEvent | UsageEvent | TurnEvent
