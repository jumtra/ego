# Ported from microsoft/TokenEconomizer (MIT, 1bfeccd): tokeneconomizer/plugin/denylist.py
"""Reversible MCP tool pruning usecases for Claude Code."""
from __future__ import annotations

from collections.abc import Iterable
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from ..adapters.transcript import read_events
from ..domain.settings import ToolPruneSettings, load_settings
from ..domain.toolprune import propose as domain_propose
from ..domain.trace_schema import ToolCallEvent
from ..shared.util import EgoError, atomic_text, read_json

_STATE_REL = Path(".ego/state/toolprune.json")
_CLAUDE_SETTINGS_REL = Path(".claude/settings.local.json")
_SECONDS_PER_DAY = 86400


def _claude_project_dirs(root: Path) -> list[Path]:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    dirs: list[Path] = []
    for base in dict.fromkeys((str(root), str(root.resolve()))):
        slug = re.sub(r"[^A-Za-z0-9]", "-", base)
        candidate = home / "projects" / slug
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def _read_state(root: Path) -> dict[str, list[str]]:
    path = root / _STATE_REL
    data = read_json(path, {}) if path.exists() else {}
    if not isinstance(data, dict):
        return {"applied": [], "pinned": []}
    applied = [x for x in (data.get("applied") or []) if isinstance(x, str)]
    pinned = [x for x in (data.get("pinned") or []) if isinstance(x, str)]
    return {"applied": applied, "pinned": pinned}


def _write_state(root: Path, state: dict[str, list[str]]) -> None:
    payload = {
        "applied": sorted(set(state.get("applied") or [])),
        "pinned": sorted(set(state.get("pinned") or [])),
    }
    atomic_text(root / _STATE_REL, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_claude_deny_mcp(root: Path) -> list[str]:
    settings_path = root / _CLAUDE_SETTINGS_REL
    if not settings_path.exists():
        return []
    data = read_json(settings_path, {})
    if not isinstance(data, dict):
        return []
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return []
    deny = perms.get("deny")
    if not isinstance(deny, list):
        return []
    return [x for x in deny if isinstance(x, str) and x.startswith("mcp__")]


def scan(root: Path | str, s: ToolPruneSettings | None = None) -> dict[str, Any]:
    """Scan Claude transcripts for ``root`` to collect available and used tool counts."""
    root_path = Path(root)
    settings = s if s is not None else load_settings(root_path).toolprune
    cutoff = time.time() - settings.unused_days * _SECONDS_PER_DAY

    seen_files: dict[Path, Path] = {}
    for proj_dir in _claude_project_dirs(root_path):
        for tfile in sorted(proj_dir.glob("*.jsonl")):
            if tfile.is_file():
                seen_files.setdefault(tfile.resolve(), tfile)

    available: set[str] = set(_read_claude_deny_mcp(root_path))
    used_counts: dict[str, int] = {}
    sessions_seen = 0

    for tfile in seen_files.values():
        try:
            mtime = tfile.stat().st_mtime
        except OSError:
            continue
        is_recent = mtime >= cutoff
        if is_recent:
            sessions_seen += 1
        for ev in read_events(tfile):
            if isinstance(ev, ToolCallEvent):
                if ev.tool.startswith("mcp__"):
                    available.add(ev.tool)
                if is_recent:
                    used_counts[ev.tool] = used_counts.get(ev.tool, 0) + 1

    return {
        "available": sorted(available),
        "used_counts": used_counts,
        "sessions_seen": sessions_seen,
    }


def propose(root: Path | str, s: ToolPruneSettings | None = None) -> list[str]:
    """Propose unused ``mcp__`` tools for ``root``."""
    root_path = Path(root)
    settings = s if s is not None else load_settings(root_path).toolprune
    scanned = scan(root_path, settings)
    state = _read_state(root_path)
    return domain_propose(
        scanned["available"],
        scanned["used_counts"],
        state["pinned"],
        state["applied"],
        scanned["sessions_seen"],
        settings,
    )


def apply(root: Path | str, tools: Iterable[str], host: str = "claude") -> dict[str, Any]:
    """Apply tool pruning to Claude Code's ``.claude/settings.local.json``."""
    tool_list = list(tools)
    if host != "claude":
        return {"status": "propose_only", "host": host, "tools": tool_list}

    root_path = Path(root)
    settings_path = root_path / _CLAUDE_SETTINGS_REL
    data = read_json(settings_path, {}) if settings_path.exists() else {}
    if not isinstance(data, dict):
        raise EgoError("INVALID_JSON", f"{settings_path} must be a JSON object", 2)

    perms = data.get("permissions")
    if perms is None:
        perms = {}
        data["permissions"] = perms
    elif not isinstance(perms, dict):
        raise EgoError("INVALID_JSON", f"{settings_path}: permissions must be a JSON object", 2)

    deny = perms.get("deny")
    if deny is None:
        deny = []
        perms["deny"] = deny
    elif not isinstance(deny, list):
        raise EgoError("INVALID_JSON", f"{settings_path}: permissions.deny must be a list", 2)

    added: list[str] = []
    for tool in tool_list:
        if tool not in deny:
            deny.append(tool)
            added.append(tool)

    atomic_text(settings_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    state = _read_state(root_path)
    state["applied"] = sorted(set(state["applied"]) | set(tool_list))
    _write_state(root_path, state)
    return {"status": "applied", "added": added, "applied": state["applied"]}


def restore(root: Path | str, tool: str) -> dict[str, Any]:
    """Remove ``tool`` from ``permissions.deny`` and ``applied``, and pin it."""
    root_path = Path(root)
    settings_path = root_path / _CLAUDE_SETTINGS_REL
    if settings_path.exists():
        data = read_json(settings_path, {})
        if (
            isinstance(data, dict)
            and isinstance(data.get("permissions"), dict)
            and isinstance(data["permissions"].get("deny"), list)
        ):
            deny = data["permissions"]["deny"]
            if tool in deny:
                data["permissions"]["deny"] = [entry for entry in deny if entry != tool]
                atomic_text(settings_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    state = _read_state(root_path)
    state["applied"] = [entry for entry in state["applied"] if entry != tool]
    state["pinned"] = sorted(set(state["pinned"]) | {tool})
    _write_state(root_path, state)
    return {"status": "restored", "tool": tool}
