# Ported from microsoft/TokenEconomizer (MIT, 1bfeccd): tokeneconomizer/toolprune/stage.py
"""Pure domain logic for proposing unused MCP tools to prune."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .settings import ToolPruneSettings


def propose(
    available: Iterable[str],
    used_counts: Mapping[str, int],
    pinned: Iterable[str],
    applied: Iterable[str],
    sessions_seen: int,
    s: ToolPruneSettings,
) -> list[str]:
    """Return sorted unused ``mcp__`` tool names eligible for pruning."""
    if sessions_seen < s.min_sessions:
        return []
    pinned_set = set(pinned)
    applied_set = set(applied)
    return sorted(
        {
            name
            for name in available
            if name.startswith("mcp__")
            and used_counts.get(name, 0) == 0
            and name not in pinned_set
            and name not in applied_set
        }
    )
