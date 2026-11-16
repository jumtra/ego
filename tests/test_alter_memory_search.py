from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from ego.memory import AlterMemory


def _fake_mcp(tmp_path: Path) -> tuple[Path, Path]:
    log_path = tmp_path / "calls.jsonl"
    script = tmp_path / "fake_alter_mcp.py"
    script.write_text(
        "import json, os, sys\n"
        "log = os.environ['ALTER_LOG']\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    if req.get('method') == 'initialize':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'capabilities':{}}}) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif req.get('method') == 'tools/call':\n"
        "        with open(log, 'a', encoding='utf-8') as f:\n"
        "            f.write(json.dumps(req['params']) + '\\n')\n"
        "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'ok':True}}) + '\\n')\n"
        "        sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return script, log_path


def _calls(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def alter_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script, log_path = _fake_mcp(tmp_path)
    monkeypatch.setenv("EGO_ALTER_MCP_COMMAND", shlex.join([sys.executable, str(script)]))
    monkeypatch.setenv("ALTER_LOG", str(log_path))
    monkeypatch.delenv("EGO_ALTER_SEARCH_TOOL", raising=False)
    return log_path


def test_search_defaults_to_project_scoped_task_context(tmp_path: Path, alter_env: Path) -> None:
    AlterMemory(tmp_path).search("ego memory", limit=3)

    calls = _calls(alter_env)
    assert len(calls) == 1
    assert calls[0]["name"] == "alter_task_context"
    assert calls[0]["arguments"] == {"task": "ego memory", "cwd": str(tmp_path)}


def test_search_tool_env_override_keeps_legacy_arguments(
    tmp_path: Path, alter_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EGO_ALTER_SEARCH_TOOL", "alter_search")

    AlterMemory(tmp_path).search("ego memory", limit=3)

    calls = _calls(alter_env)
    assert calls[0]["name"] == "alter_search"
    assert calls[0]["arguments"] == {"query": "ego memory", "limit": 3}
