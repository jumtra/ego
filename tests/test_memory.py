"""Memory flows: file provider, alter memory search, alter skill installation, and alter session/checkpoint review submission."""
from __future__ import annotations
from pathlib import Path
import io
import json
import pytest
import shlex
import sys

from ego.adapters.codeintel import health, search, symbol
from ego.adapters.hosts import capability, build_command
from ego.adapters.memory import FileMemory
from ego.adapters.memory import extract_session_messages, submit_alter_checkpoint_review
from ego.adapters.runner import run_agent, parse_host_response
from ego.cli import main
from ego.domain.config import Config, save_config
from ego.domain.policy import check_command, check_hook_payload
import subprocess
from ego.shared.util import EgoError, alter_project_root
from ego.usecases.session import list_checkpoints


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
    monkeypatch.setenv("EGO_ALTER_AUTO_REVIEW", "1")  # session review is opt-in; these tests exercise it
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
    save_config(proj, Config(knowledge="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

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
    save_config(proj, Config(knowledge="file", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

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
    save_config(proj, Config(knowledge="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

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
    save_config(proj, Config(knowledge="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

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


_SESSION_ENV = ("EGO_RUN_ID", "EGO_RUN", "EGO_RUN_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID")


@pytest.fixture
def no_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SESSION_ENV:
        monkeypatch.delenv(name, raising=False)


def _submitted_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: dict) -> dict:
    _, log_path = _setup_fake_alter_mcp(tmp_path, monkeypatch)
    submit_alter_checkpoint_review(tmp_path, checkpoint)
    calls = _read_calls(log_path)
    assert len(calls) == 1
    return calls[0]["arguments"]


def test_a_checkpoint_from_an_ego_run_reports_to_the_run_session(tmp_path: Path, monkeypatch, no_session_env):
    args = _submitted_arguments(tmp_path, monkeypatch, {"task_id": "t1", "goal": "g", "run": "n-1-x", "next_steps": ["go"]})
    assert args["session_id"] == "ego-run-n-1-x"


def test_outside_an_ego_run_the_host_session_is_used(tmp_path: Path, monkeypatch, no_session_env):
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread-7")
    args = _submitted_arguments(tmp_path, monkeypatch, {"task_id": "t1", "goal": "g", "next_steps": ["go"]})
    assert args["session_id"] == "codex-thread-7"


def test_without_any_session_the_checkpoint_names_its_own_session(tmp_path: Path, monkeypatch, no_session_env):
    args = _submitted_arguments(tmp_path, monkeypatch, {"task_id": "t1", "goal": "g", "next_steps": ["go"]})
    assert args["session_id"] == "checkpoint-t1"


def test_each_field_submits_at_most_eight_items(tmp_path: Path, monkeypatch, no_session_env):
    items = [f"item {i}" for i in range(10)]
    args = _submitted_arguments(tmp_path, monkeypatch, {"task_id": "t1", "goal": "g", "completed": items, "evidence": items})
    assert [a["text"] for a in args["add"] if a["kind"] == "evidence"] == items[:8]
    assert len([a for a in args["add"] if a["kind"] == "fact"]) == 8


def test_session_checkpoint_succeeds_when_alter_mcp_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _setup_fake_alter_mcp(tmp_path, monkeypatch, fail=True)
    proj = tmp_path / "proj_ckpt_fail"
    proj.mkdir()
    save_config(proj, Config(knowledge="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))

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
    from ego.adapters.hosts import select_mcp_servers

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
    from ego.adapters.hosts import select_mcp_servers

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
    from ego.adapters import memory

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


def test_hook_session_end_is_a_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _, log_path = _setup_fake_alter_mcp(tmp_path, monkeypatch)
    proj = tmp_path / "proj_end"
    proj.mkdir()
    save_config(proj, Config(knowledge="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))
    payload = _write_host_payload(tmp_path, "claude", proj)

    # Kept for hooks.json written by older versions: exits 0, prints {}, records nothing, inside or outside `ego run`.
    for ego_run in ("claude", None):
        if ego_run is None:
            monkeypatch.delenv("EGO_RUN", raising=False)
        else:
            monkeypatch.setenv("EGO_RUN", ego_run)
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        assert main(["hook", "session-end", "--host", "claude", "--root", str(proj)]) == 0
        assert capsys.readouterr().out.strip() == "{}"
        assert _read_calls(log_path) == []


def test_file_memory(project):
    memory=FileMemory(project)
    one=memory.add("Auth token expiry",tags=["security"],evidence=["run-1"])
    assert len(memory.search("token"))==1
    assert memory.delete(one["id"])
    assert not memory.search("token")


def test_ego_memory_stays_file_when_memory_is_alter(project, tmp_path: Path, monkeypatch):
    # Process memory is local even with memory = "alter": `ego memory` writes the file store and never calls alter.
    _, log_path = _setup_fake_alter_mcp(tmp_path, monkeypatch)
    save_config(project, Config(knowledge="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))
    assert main(["memory", "add", "Use file memory for process records", "--root", str(project)]) == 0
    assert main(["memory", "search", "process", "--root", str(project)]) == 0
    assert (project / ".ego" / "memory" / "items.json").exists()
    assert not log_path.exists() or _read_calls(log_path) == []


def test_install_alter_skills_and_cli(project, monkeypatch, capsys):
    import json
    import subprocess
    from ego.cli import main
    from ego.adapters.memory import alter_skills_installed, install_alter_skills

    # Project skill marker detection
    assert not alter_skills_installed(project, home=project / "empty_home")
    (project / ".claude" / "skills" / "alter-context-loader").mkdir(parents=True)
    assert alter_skills_installed(project, home=project / "empty_home")

    # Missing npx fails closed unless dry-run
    monkeypatch.setattr("ego.adapters.memory.shutil.which", lambda name: None)
    with pytest.raises(EgoError) as missing:
        install_alter_skills(project, clients=["claude"])
    assert missing.value.code == "NPX_MISSING"
    dry = install_alter_skills(project, clients=["claude", "codex"], scope="project", agents=False, dry_run=True)
    assert dry["status"] == "DRY_RUN"
    assert "--project" in dry["command"] and "--no-agents" in dry["command"]
    assert "github:jumtra/alter-skills#v0.2.1" in dry["command"]

    # Mocked npx execution via CLI
    calls = []
    monkeypatch.setattr("ego.adapters.memory.shutil.which", lambda name: "/usr/local/bin/npx")
    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="copied 6 skills\n", stderr="")
    monkeypatch.setattr("ego.adapters.memory.subprocess.run", fake_run)

    assert main(["memory", "--root", str(project), "install-skills", "--clients", "claude,agy", "--scope", "global"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "INSTALLED" and out["clients"] == ["claude", "agy"]
    assert "--client" in calls[-1] and "claude,agy" in calls[-1]

    assert main(["apply", "--root", str(project), "--memory", "alter", "--install-alter-skills", "--dry-run"]) == 0
    apply_out = json.loads(capsys.readouterr().out)
    assert apply_out["alter_skills"]["status"] == "DRY_RUN"


def test_alter_project_auto_registers_on_apply_and_work(tmp_path: Path, monkeypatch):
    import json, sys
    from ego.domain.config import Config
    from ego.usecases.install import apply_install
    from ego.adapters.memory import ensure_alter_project
    from ego.usecases import work

    home = tmp_path / "home"
    home.mkdir()
    log_path = tmp_path / "alter_calls.jsonl"
    fake_mcp = tmp_path / "fake_alter_mcp.py"
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
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "alter": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(fake_mcp)],
                "env": {"ALTER_LOG": str(log_path)},
            }
        }
    }))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EGO_ALTER_AUTO_REGISTER", "1")
    monkeypatch.delenv("EGO_ALTER_MCP_COMMAND", raising=False)

    proj = tmp_path / "proj"
    proj.mkdir()
    res = apply_install(proj, Config(knowledge="alter", clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))
    assert res["alter_project"]["status"] == "registered"
    calls = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1
    assert calls[0]["name"] == "alter_bootstrap"
    assert calls[0]["arguments"] == {"cwd": str(proj.resolve())}

    # Second call without force uses cache and does not spawn MCP again
    cached = ensure_alter_project(proj)
    assert cached["status"] == "already_registered"
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1

    # New project without prior apply registers automatically during work.execute
    proj2 = tmp_path / "proj2"
    proj2.mkdir()
    (proj2 / ".ego").mkdir()
    (proj2 / ".ego/config.toml").write_text('memory = "alter"\n', encoding="utf-8")
    wres = work.execute(proj2, {"goal": "g", "nodes": [{"id": "n1", "kind": "agent", "prompt": "[[MOCK:ok]]", "write_allowlist": ["src/**"]}]}, host="mock")
    assert wres["final_status"] == "PASSED"
    calls = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert calls[1]["arguments"] == {"cwd": str(proj2.resolve())}


def test_apply_install_auto_installs_alter_skills_and_npx_fallback(tmp_path: Path, monkeypatch):
    import subprocess
    from ego.domain.config import Config
    from ego.usecases.install import apply_install
    from ego.adapters import memory

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EGO_ALTER_AUTO_INSTALL_SKILLS", "1")
    monkeypatch.setattr("ego.adapters.memory.shutil.which", lambda name: f"/usr/local/bin/{name}" if name in ("npx", "node") else None)

    script = home / ".npm" / "_npx" / "abc" / "node_modules" / "alter-skills" / "bin" / "install.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("// stub", encoding="utf-8")

    seen = []
    def fake_run(argv, **kw):
        seen.append(argv)
        if argv[0].endswith("npx"):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="copied 6 skills\n", stderr="")

    monkeypatch.setattr("ego.adapters.memory.subprocess.run", fake_run)

    proj = tmp_path / "proj"
    proj.mkdir()
    res = apply_install(proj, Config(knowledge="alter", hosts=["claude", "agy"], clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))
    assert res["alter_skills"]["status"] == "INSTALLED"
    assert res["alter_skills"]["output"] == "copied 6 skills"
    assert seen[0][0].endswith("npx")
    assert seen[1][0].endswith("node") and seen[1][1] == str(script)

    # Only agy installed when hosts=['claude', 'agy'] -> still installs for missing client
    (home / ".gemini" / "config" / "skills" / "alter-context-loader").mkdir(parents=True)
    res2 = apply_install(proj, Config(knowledge="alter", hosts=["claude", "agy"], clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))
    assert res2["alter_skills"]["status"] == "INSTALLED"
    assert len(seen) == 4

    # Both claude and agy installed -> skip npx
    (home / ".claude" / "skills" / "alter-context-loader").mkdir(parents=True)
    res3 = apply_install(proj, Config(knowledge="alter", hosts=["claude", "agy"], clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0))
    assert res3["alter_skills"] == {"status": "already_installed"}
    assert len(seen) == 4


def test_alter_project_root_maps_a_linked_worktree_to_its_main_checkout(tmp_path: Path):
    # Arrange
    main = tmp_path / "main"
    main.mkdir()
    git = lambda *a, cwd=main: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "init")
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", str(linked), "-b", "wt")
    # Act / Assert
    assert alter_project_root(linked) == main.resolve()
    assert alter_project_root(main) == main


def test_alter_project_root_follows_the_root_a_worker_was_pinned_to(tmp_path: Path, monkeypatch):
    # Arrange: a throwaway copy has no .git, so git alone cannot map it back
    copy = tmp_path / "copy"
    copy.mkdir()
    monkeypatch.setenv("EGO_ALTER_PROJECT_ROOT", str(tmp_path / "main"))
    # Act / Assert
    assert alter_project_root(copy) == tmp_path / "main"
