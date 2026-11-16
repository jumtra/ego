from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ego.cli import main
from ego.config import Config, save_config
from ego.memory import extract_session_messages
from ego.session import list_checkpoints


def _setup_fake_alter_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    log_path = tmp_path / "alter_calls.jsonl"
    fake_mcp = tmp_path / ("fake_alter_mcp_fail.py" if fail else "fake_alter_mcp.py")
    if fail:
        fake_mcp.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    if req.get('method') == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'capabilities':{}}}) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif req.get('method') == 'tools/call':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':req['id'],'error':{'code':-32000,'message':'boom'}}) + '\\n')\n"
            "        sys.stdout.flush()\n",
            encoding="utf-8",
        )
    else:
        fake_mcp.write_text(
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
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alter": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(fake_mcp)],
                        "env": {"ALTER_LOG": str(log_path)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("EGO_ALTER_MCP_COMMAND", raising=False)
    monkeypatch.delenv("EGO_ALTER_AUTO_REVIEW", raising=False)
    return home, log_path


def _read_calls(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_extract_session_messages_claude_jsonl_filters_and_redacts(tmp_path: Path):
    transcript = tmp_path / "claude.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user", "content": "Use token=ghp_123456789012345678901234567890123456"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret internal thought"},
                    {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "ls"}},
                    {"type": "text", "text": "Done checking repo."},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file1.py"}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    turns = extract_session_messages(transcript, host="claude")
    assert len(turns) == 2
    assert turns[0]["role"] == "user"
    assert "ghp_" not in turns[0]["text"]
    assert "[REDACTED]" in turns[0]["text"]
    assert turns[1] == {"role": "assistant", "text": "Done checking repo."}


def test_extract_session_messages_codex_jsonl(tmp_path: Path):
    transcript = tmp_path / "codex.jsonl"
    rows = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix bug with api_key=sk-1234567890123456789012345"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Patched the parser."},
                    {"type": "reasoning", "text": "hidden"},
                ],
            },
        },
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    turns = extract_session_messages(transcript, host="codex")
    assert len(turns) == 2
    assert turns[0]["role"] == "user"
    assert "sk-123456" not in turns[0]["text"]
    assert turns[1] == {"role": "assistant", "text": "Patched the parser."}


def test_extract_session_messages_agy_jsonl(tmp_path: Path):
    transcript = tmp_path / "agy.jsonl"
    rows = [
        {"type": "USER_INPUT", "content": "Analyze auth.py"},
        {
            "type": "PLANNER_RESPONSE",
            "thinking": "internal chain of thought",
            "tool_calls": [{"name": "read_file"}],
            "content": "Auth looks solid.",
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    turns = extract_session_messages(transcript, host="agy")
    assert turns == [
        {"role": "user", "text": "Analyze auth.py"},
        {"role": "assistant", "text": "Auth looks solid."},
    ]


def test_extract_session_messages_opencode_messages():
    messages = [
        {"role": "user", "content": "Hello OpenCode"},
        {"role": "assistant", "content": "Hi!"},
        {"role": "tool", "content": "ignored tool output"},
    ]
    turns = extract_session_messages(host="opencode", messages=messages)
    assert turns == [
        {"role": "user", "text": "Hello OpenCode"},
        {"role": "assistant", "text": "Hi!"},
    ]


def _write_host_payload(tmp_path: Path, host: str, proj: Path) -> str:
    if host == "claude":
        transcript = tmp_path / "claude_hook.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "Claude turn"}}) + "\n",
            encoding="utf-8",
        )
        return json.dumps({"cwd": str(proj), "session_id": "sess-claude", "transcript_path": str(transcript)})
    if host == "codex":
        transcript = tmp_path / "codex_hook.jsonl"
        transcript.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Codex turn"}}) + "\n",
            encoding="utf-8",
        )
        return json.dumps({"cwd": str(proj), "session_id": "sess-codex", "transcript_path": str(transcript)})
    if host == "agy":
        transcript = tmp_path / "agy_hook.jsonl"
        transcript.write_text(
            json.dumps({"type": "USER_INPUT", "content": "AGY turn"}) + "\n",
            encoding="utf-8",
        )
        return json.dumps({"cwd": str(proj), "conversationId": "sess-agy", "transcriptPath": str(transcript)})
    if host == "opencode":
        return json.dumps(
            {
                "cwd": str(proj),
                "session_id": "sess-opencode",
                "messages": [{"role": "user", "content": "OpenCode turn"}],
            }
        )
    raise ValueError(host)


@pytest.mark.parametrize("host", ["claude", "codex", "agy", "opencode"])
def test_hook_stop_submits_once_and_deduplicates_across_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
):
    _, log_path = _setup_fake_alter_mcp(tmp_path, monkeypatch)
    proj = tmp_path / f"proj_{host}"
    proj.mkdir()
    save_config(proj, Config(memory="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

    payload = _write_host_payload(tmp_path, host, proj)

    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = main(["hook", "stop", "--host", host, "--root", str(proj)])
    assert rc == 0
    capsys.readouterr()

    calls = _read_calls(log_path)
    assert len(calls) == 1
    assert calls[0]["name"] == "alter_record_conversation"
    assert calls[0]["arguments"]["session_id"] == f"sess-{host}"

    # Duplicate state is skipped
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc2 = main(["hook", "stop", "--host", host, "--root", str(proj)])
    assert rc2 == 0
    capsys.readouterr()
    assert len(_read_calls(log_path)) == 1


def test_hook_stop_skips_when_memory_is_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _, log_path = _setup_fake_alter_mcp(tmp_path, monkeypatch)
    proj = tmp_path / "proj_file"
    proj.mkdir()
    save_config(proj, Config(memory="file", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

    payload = _write_host_payload(tmp_path, "claude", proj)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = main(["hook", "stop", "--host", "claude", "--root", str(proj)])
    assert rc == 0
    capsys.readouterr()
    assert _read_calls(log_path) == []


def test_hook_stop_fails_open_on_mcp_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _setup_fake_alter_mcp(tmp_path, monkeypatch, fail=True)
    proj = tmp_path / "proj_fail"
    proj.mkdir()
    save_config(proj, Config(memory="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

    payload = _write_host_payload(tmp_path, "claude", proj)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = main(["hook", "stop", "--host", "claude", "--root", str(proj)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, dict)


def test_session_checkpoint_calls_alter_session_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _, log_path = _setup_fake_alter_mcp(tmp_path, monkeypatch)
    proj = tmp_path / "proj_ckpt"
    proj.mkdir()
    save_config(proj, Config(memory="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

    rc = main(
        [
            "--root",
            str(proj),
            "session",
            "checkpoint",
            "--task-id",
            "feat-x",
            "--goal",
            "Implement feature X",
            "--completed",
            "tests passing",
            "--next",
            "open PR",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    calls = _read_calls(log_path)
    assert len(calls) == 1
    assert calls[0]["name"] == "alter_session_digest"
    assert calls[0]["arguments"]["action"] == "patch"
    assert calls[0]["arguments"]["cwd"] == str(proj.resolve())


def test_session_checkpoint_succeeds_when_alter_mcp_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _setup_fake_alter_mcp(tmp_path, monkeypatch, fail=True)
    proj = tmp_path / "proj_ckpt_fail"
    proj.mkdir()
    save_config(proj, Config(memory="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

    rc = main(
        [
            "--root",
            str(proj),
            "session",
            "checkpoint",
            "--task-id",
            "feat-y",
            "--goal",
            "Implement feature Y",
            "--completed",
            "blocked on review",
            "--next",
            "address comments",
        ]
    )
    assert rc == 0
    capsys.readouterr()
    checkpoints = list_checkpoints(proj)
    assert len(checkpoints) == 1
    assert checkpoints[0]["task_id"] == "feat-y"


def test_select_mcp_servers_prefers_agy_config_for_agy_host(tmp_path: Path, monkeypatch):
    from ego.hosts import select_mcp_servers

    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    (home / ".gemini" / "config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"alter": {"command": "node", "args": ["claude-mcp.mjs"], "env": {"TOKEN": "c"}}}}),
        encoding="utf-8",
    )
    (home / ".gemini" / "config" / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alter": {
                        "command": "node",
                        "args": ["agy-mcp.mjs", "--profile", "p1"],
                        "env": {"TOKEN": "a"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert select_mcp_servers(proj, ["alter"], host="claude")["alter"]["args"] == ["claude-mcp.mjs"]
    assert select_mcp_servers(proj, ["alter"], host="agy")["alter"]["args"] == ["agy-mcp.mjs", "--profile", "p1"]

    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"alter": {"command": "node", "args": ["project-mcp.mjs"]}}}),
        encoding="utf-8",
    )
    assert select_mcp_servers(proj, ["alter"], host="agy")["alter"]["args"] == ["project-mcp.mjs"]


def test_select_mcp_servers_supports_codex_and_opencode(tmp_path: Path, monkeypatch):
    from ego.hosts import select_mcp_servers

    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    (home / ".codex").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.alter]\ncommand = "node"\nargs = ["codex-mcp.mjs"]\n[mcp_servers.alter.env]\nTOKEN = "cx"\n',
        encoding="utf-8",
    )
    (home / ".config" / "opencode" / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "alter": {
                        "type": "local",
                        "command": ["node", "opencode-mcp.mjs"],
                        "environment": {"TOKEN": "oc"},
                        "enabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cx = select_mcp_servers(proj, ["alter"], host="codex")["alter"]
    assert cx["command"] == "node" and cx["args"] == ["codex-mcp.mjs"] and cx["env"] == {"TOKEN": "cx"}

    oc = select_mcp_servers(proj, ["alter"], host="opencode")["alter"]
    assert oc["command"] == "node" and oc["args"] == ["opencode-mcp.mjs"] and oc["env"] == {"TOKEN": "oc"}


def test_alter_mcp_spec_uses_active_host_and_env_detection(tmp_path: Path, monkeypatch):
    from ego import memory

    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    (home / ".gemini" / "config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("EGO_ALTER_MCP_COMMAND", raising=False)
    for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_CI", "OPENCODE", "ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID"):
        monkeypatch.delenv(var, raising=False)

    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"alter": {"command": "node", "args": ["claude-mcp.mjs"]}}}),
        encoding="utf-8",
    )
    (home / ".gemini" / "config" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"alter": {"command": "node", "args": ["agy-mcp.mjs", "--profile", "p1"], "env": {"ALTER_DATA_DIR": "/tmp/d"}}}}),
        encoding="utf-8",
    )

    assert memory._alter_mcp_spec(proj) == (["node", "claude-mcp.mjs"], None)
    with memory.use_host("agy"):
        assert memory._alter_mcp_spec(proj) == (["node", "agy-mcp.mjs", "--profile", "p1"], {"ALTER_DATA_DIR": "/tmp/d"})

    monkeypatch.setenv("ANTIGRAVITY_AGENT", "1")
    assert memory._alter_mcp_spec(proj) == (["node", "agy-mcp.mjs", "--profile", "p1"], {"ALTER_DATA_DIR": "/tmp/d"})


def test_hook_session_end_records_conversation_and_skips_auto_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _, log_path = _setup_fake_alter_mcp(tmp_path, monkeypatch)
    proj = tmp_path / "proj_end"
    proj.mkdir()
    save_config(proj, Config(memory="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))
    payload = _write_host_payload(tmp_path, "claude", proj)

    # `ego run` rotates sessions itself (automatic clear): nothing is recorded
    monkeypatch.setenv("EGO_RUN", "claude")
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert main(["hook", "session-end", "--host", "claude", "--root", str(proj)]) == 0
    capsys.readouterr()
    assert _read_calls(log_path) == []

    monkeypatch.delenv("EGO_RUN")
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert main(["hook", "session-end", "--host", "claude", "--root", str(proj)]) == 0
    capsys.readouterr()
    calls = _read_calls(log_path)
    assert len(calls) == 1 and calls[0]["name"] == "alter_record_conversation"
    assert calls[0]["arguments"]["session_id"] == "sess-claude"
