"""Tests for reversible MCP tool pruning (domain, usecases, and CLI)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

from ego.cli import main
from ego.domain.settings import ToolPruneSettings
from ego.usecases import toolprune


def _tool_use_line(name: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_1", "name": name, "input": {}}],
            },
        }
    )


def _write_transcript(
    claude_dir: Path,
    root: Path,
    session_id: str,
    tools: list[str],
    *,
    age_days: float = 0.0,
) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    proj_dir = claude_dir / "projects" / slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{session_id}.jsonl"
    lines = [_tool_use_line(t) for t in tools]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if age_days > 0:
        ts = time.time() - age_days * 86400
        os.utime(path, (ts, ts))
    return path


def test_below_min_sessions_returns_empty(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    _write_transcript(claude_dir, root, "s1", ["mcp__unused__a"], age_days=20)
    for i in range(4):
        _write_transcript(claude_dir, root, f"r{i}", ["Read"])

    assert toolprune.propose(root, ToolPruneSettings(unused_days=14, min_sessions=5)) == []


def test_propose_apply_restore_lifecycle(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    # Older transcript introduces mcp__unused__alpha and mcp__unused__beta.
    _write_transcript(claude_dir, root, "old1", ["mcp__unused__alpha", "mcp__unused__beta"], age_days=20)
    # 5 recent transcripts use mcp__used__gamma and Read, never alpha or beta.
    for i in range(5):
        _write_transcript(claude_dir, root, f"recent{i}", ["Read", "mcp__used__gamma"])

    proposed = toolprune.propose(root)
    assert proposed == ["mcp__unused__alpha", "mcp__unused__beta"]

    res_apply = toolprune.apply(root, proposed, host="claude")
    assert res_apply["status"] == "applied"
    settings = json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["deny"] == ["mcp__unused__alpha", "mcp__unused__beta"]

    # Already applied -> no longer proposed.
    assert toolprune.propose(root) == []

    # Restore one tool -> removed from deny and applied, pinned so never proposed again.
    res_restore = toolprune.restore(root, "mcp__unused__alpha")
    assert res_restore["status"] == "restored"
    settings = json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["deny"] == ["mcp__unused__beta"]

    state = json.loads((root / ".ego" / "state" / "toolprune.json").read_text(encoding="utf-8"))
    assert state["applied"] == ["mcp__unused__beta"]
    assert state["pinned"] == ["mcp__unused__alpha"]
    assert toolprune.propose(root) == []


def test_apply_non_claude_host_is_propose_only(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    res = toolprune.apply(root, ["mcp__unused__a"], host="codex")
    assert res["status"] == "propose_only"
    assert not (root / ".claude" / "settings.local.json").exists()
    assert not (root / ".ego" / "state" / "toolprune.json").exists()


def test_apply_preserves_existing_settings_and_avoids_duplicates(tmp_path):
    root = tmp_path / "repo"
    claude_cfg = root / ".claude" / "settings.local.json"
    claude_cfg.parent.mkdir(parents=True)
    claude_cfg.write_text(
        json.dumps(
            {
                "theme": "dark",
                "permissions": {
                    "allow": ["Bash(pytest:*)"],
                    "deny": ["mcp__unused__a"],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    res = toolprune.apply(root, ["mcp__unused__a", "mcp__unused__b"], host="claude")
    assert res["added"] == ["mcp__unused__b"]

    updated = json.loads(claude_cfg.read_text(encoding="utf-8"))
    assert updated["theme"] == "dark"
    assert updated["permissions"]["allow"] == ["Bash(pytest:*)"]
    assert updated["permissions"]["deny"] == ["mcp__unused__a", "mcp__unused__b"]


def test_cli_tools_propose_apply_restore(tmp_path, monkeypatch, capsys):
    claude_dir = tmp_path / "claude"
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    _write_transcript(claude_dir, root, "old1", ["mcp__unused__cli"], age_days=20)
    for i in range(5):
        _write_transcript(claude_dir, root, f"r{i}", ["Read"])

    assert main(["tools", "propose", "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == ["mcp__unused__cli"]

    # Without --yes: prints would-apply list only, does not write settings.
    assert main(["tools", "apply", "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == ["mcp__unused__cli"]
    assert not (root / ".claude" / "settings.local.json").exists()

    # With --yes: applies to settings.local.json.
    assert main(["tools", "apply", "--yes", "--root", str(root)]) == 0
    apply_out = json.loads(capsys.readouterr().out)
    assert apply_out["status"] == "applied"
    assert (root / ".claude" / "settings.local.json").exists()

    # Restore via CLI.
    assert main(["tools", "restore", "mcp__unused__cli", "--root", str(root)]) == 0
    restore_out = json.loads(capsys.readouterr().out)
    assert restore_out["status"] == "restored"
    assert main(["tools", "propose", "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == []
