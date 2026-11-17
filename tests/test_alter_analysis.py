"""Alter analysis flow (N4): launch gating, pending pickup, per-host analyze_session, build_command allowed_tools,
keep_turns persistence, turn pruning and config validation. Every host, process and model call is a fake."""
from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

import pytest

from ego.adapters import hostcompat, memory, runner
from ego.adapters import hosts
from ego.adapters.runner import RunResult
from ego.domain import config as config_mod
from ego.domain import retention
from ego.domain.config import Config, DEFAULT_ALTER_ANALYZER
from ego.shared.util import EgoError, read_json, write_json
from ego.usecases import alter_analysis as aa

SID = "sess-1"
CONV = "conv-123"


# ---------- helpers / fakes ----------

def _cfg(**over) -> Config:
    base = dict(knowledge="alter", alter_analysis=True)
    base.update(over)
    return Config(**base)


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    return r


@pytest.fixture
def set_cfg(monkeypatch):
    """Replace the config loader used by the usecase (imported lazily inside functions)."""
    def _set(cfg: Config):
        monkeypatch.setattr(config_mod, "load_config", lambda root: cfg)
    return _set


@pytest.fixture(autouse=True)
def _no_analyzer_env(monkeypatch):
    monkeypatch.delenv(aa.ANALYZER_ENV, raising=False)


class FakePopen:
    calls: list[dict] = []

    def __init__(self, args, **kwargs):
        FakePopen.calls.append({"args": args, **kwargs})


@pytest.fixture
def popen(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(aa.subprocess, "Popen", FakePopen)
    return FakePopen.calls


def _turns(n: int = 4) -> list[dict]:
    return [{"id": f"t{i}", "sequence": i, "role": "user" if i % 2 == 0 else "assistant", "text": f"msg {i}"}
            for i in range(n)]


def _write_turns(root: Path, sid: str, host: str = "claude", n: int = 4, conv: str | None = CONV,
                 age_seconds: float = 0.0) -> Path:
    path = memory.alter_turns_path(root, sid)
    write_json(path, {"session_id": sid, "host": host, "conversation_id": conv, "turns": _turns(n)})
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(path, (t, t))
    return path


def _state(root: Path) -> dict:
    return read_json(aa._state_path(root), {}).get("sessions", {})


@pytest.fixture
def fake_host(monkeypatch):
    """Fake inspect_host (controls availability/support) and run_agent (records calls, returns a scripted status)."""
    ctl = {"available": True, "supported": True, "status": "PASS", "calls": [], "inspected": []}

    def fake_inspect(host, **kw):
        ctl["inspected"].append(host)
        return {"host": host, "available": ctl["available"], "supported": ctl["supported"],
                "reason": "", "flags": [], "version": "fake"}

    def fake_run(host, prompt, cwd, timeout=180, **kw):
        ctl["calls"].append({"host": host, "prompt": prompt, "cwd": cwd, "timeout": timeout, **kw})
        ok = ctl["status"] == "PASS"
        return RunResult(status=ctl["status"], output="", stderr="", exit_code=0 if ok else 1,
                         elapsed_seconds=0.0, host=host, reason="" if ok else "boom")

    monkeypatch.setattr(hostcompat, "inspect_host", fake_inspect)
    monkeypatch.setattr(runner, "run_agent", fake_run)
    return ctl


# ---------- 1. launch_analysis gating ----------

def test_launch_off_by_default(root, popen, set_cfg):
    set_cfg(Config())  # alter_analysis defaults to False, knowledge defaults to file
    res = aa.launch_analysis(root, SID)
    assert res["status"] == "skipped" and res["reason"] == "disabled"
    assert popen == []


def test_launch_skipped_when_knowledge_is_file(root, popen, set_cfg):
    _write_turns(root, SID)
    set_cfg(_cfg(knowledge="file"))
    assert aa.launch_analysis(root, SID) == {"status": "skipped", "reason": "disabled"}
    assert popen == []


def test_launch_recursion_guard_when_env_set(root, popen, set_cfg, monkeypatch):
    _write_turns(root, SID)
    set_cfg(_cfg())
    monkeypatch.setenv(aa.ANALYZER_ENV, "1")
    assert aa.launch_analysis(root, SID) == {"status": "skipped", "reason": "inside analyzer"}
    assert popen == []


def test_launch_recursion_guard_ignores_zero(root, popen, set_cfg, monkeypatch):
    _write_turns(root, SID)
    set_cfg(_cfg())
    monkeypatch.setenv(aa.ANALYZER_ENV, "0")
    assert aa.launch_analysis(root, SID)["status"] == "launched"


@pytest.mark.parametrize("n", [0, 1, 3])
def test_launch_skipped_with_too_few_turns(root, popen, set_cfg, n):
    if n:
        _write_turns(root, SID, n=n)
    set_cfg(_cfg())
    res = aa.launch_analysis(root, SID)
    assert res["status"] == "skipped" and res["reason"] == "not enough turns"
    assert popen == []


def test_launch_skipped_when_already_analyzed(root, popen, set_cfg):
    _write_turns(root, SID)
    write_json(aa._state_path(root), {"schema_version": 1, "sessions": {SID: {"status": "analyzed"}}})
    set_cfg(_cfg())
    assert aa.launch_analysis(root, SID)["reason"] == "analyzed"
    assert popen == []


def test_launch_positive_spawns_detached_analyze(root, popen, set_cfg):
    _write_turns(root, SID)
    set_cfg(_cfg())
    res = aa.launch_analysis(root, SID)
    assert res == {"status": "launched", "session_id": SID}
    assert len(popen) == 1
    call = popen[0]
    assert call["args"][1:] == ["-m", "ego", "session", "analyze", SID]
    assert call["args"][0] == sys.executable
    assert call["start_new_session"] is True
    assert call["env"][aa.ANALYZER_ENV] == "1"
    assert call["cwd"] == str(root.resolve())


def test_launch_fails_open_when_spawn_raises(root, set_cfg, monkeypatch):
    _write_turns(root, SID)
    set_cfg(_cfg())

    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(aa.subprocess, "Popen", boom)
    res = aa.launch_analysis(root, SID)
    assert res["status"] == "error" and "no exec" in res["reason"]


# ---------- 2. launch_pending ----------

def test_pending_launches_at_most_one(root, popen, set_cfg):
    _write_turns(root, "a", age_seconds=10)
    _write_turns(root, "b", age_seconds=20)
    set_cfg(_cfg())
    res = aa.launch_pending(root)
    assert res == {"status": "launched", "session_id": "a"}  # newest first
    assert len(popen) == 1


def test_pending_ignores_sessions_older_than_48h(root, popen, set_cfg):
    _write_turns(root, "old", age_seconds=49 * 3600)
    set_cfg(_cfg())
    assert aa.launch_pending(root) == {"status": "skipped", "reason": "nothing pending"}
    assert popen == []


def test_pending_accepts_session_just_inside_48h(root, popen, set_cfg):
    _write_turns(root, "edge", age_seconds=47 * 3600)
    set_cfg(_cfg())
    assert aa.launch_pending(root)["status"] == "launched"


def test_pending_skips_analyzed_and_attempt_exhausted(root, popen, set_cfg):
    assert aa.MAX_ATTEMPTS == 2
    _write_turns(root, "done", age_seconds=10)
    _write_turns(root, "spent", age_seconds=20)
    write_json(aa._state_path(root), {"schema_version": 1, "sessions": {
        "done": {"status": "analyzed"},
        "spent": {"status": "failed", "attempts": aa.MAX_ATTEMPTS},
    }})
    set_cfg(_cfg())
    assert aa.launch_pending(root)["reason"] == "nothing pending"
    assert popen == []


def test_pending_retries_while_attempts_below_limit(root, popen, set_cfg):
    _write_turns(root, "retry", age_seconds=10)
    write_json(aa._state_path(root), {"schema_version": 1, "sessions": {
        "retry": {"status": "failed", "attempts": aa.MAX_ATTEMPTS - 1}}})
    set_cfg(_cfg())
    assert aa.launch_pending(root) == {"status": "launched", "session_id": "retry"}


def test_pending_skips_when_disabled_or_inside_analyzer(root, popen, set_cfg, monkeypatch):
    _write_turns(root, SID)
    set_cfg(_cfg(alter_analysis=False))
    assert aa.launch_pending(root)["reason"] == "disabled"
    set_cfg(_cfg())
    monkeypatch.setenv(aa.ANALYZER_ENV, "1")
    assert aa.launch_pending(root)["reason"] == "disabled"
    assert popen == []


def test_pending_no_saved_sessions(root, popen, set_cfg):
    set_cfg(_cfg())
    assert aa.launch_pending(root) == {"status": "skipped", "reason": "no saved sessions"}


# ---------- 3. analyze_session per host ----------

HOSTS = ["claude", "codex", "agy", "opencode"]


@pytest.mark.parametrize("host", HOSTS)
def test_analyze_success_marks_done_and_targets_conversation(root, set_cfg, fake_host, host):
    _write_turns(root, SID, host=host)
    set_cfg(_cfg())
    res = aa.analyze_session(root, SID)
    assert res["status"] == "analyzed" and res["conversation_id"] == CONV and res["attempts"] == 1
    call = fake_host["calls"][0]
    assert call["host"] == host
    assert CONV in call["prompt"] and "Do not record a new conversation" in call["prompt"]
    assert call["allowed_tools"] == aa.ANALYZER_TOOLS
    assert call["mcp_servers"] == ["alter"]
    assert call["read_only"] is False and call["lean"] is True
    assert call["env"][aa.ANALYZER_ENV] == "1" and call["env"]["EGO_ALTER_AUTO_REVIEW"] == "0"
    entry = _state(root)[SID]
    assert entry["status"] == "analyzed" and entry["attempts"] == 1 and entry["host"] == host


def test_analyze_opencode_default_model(root, set_cfg, fake_host):
    _write_turns(root, SID, host="opencode")
    set_cfg(_cfg())  # no alter_analyzer override -> packaged default
    aa.analyze_session(root, SID)
    assert fake_host["calls"][0]["model"] == "opencode/glm-5.3-flash-free"
    assert DEFAULT_ALTER_ANALYZER == {"opencode": {"model": "opencode/glm-5.3-flash-free"}}


@pytest.mark.parametrize("host", ["claude", "codex", "agy"])
def test_analyze_no_default_model_for_other_hosts(root, set_cfg, fake_host, host):
    _write_turns(root, SID, host=host)
    set_cfg(_cfg())
    aa.analyze_session(root, SID)
    assert fake_host["calls"][0]["model"] is None


def test_analyze_per_host_override_wins_over_session_host(root, set_cfg, fake_host):
    _write_turns(root, SID, host="claude")
    set_cfg(_cfg(alter_analyzer={"claude": {"model": "sonnet"}, "opencode": {"model": "x/y"}}))
    aa.analyze_session(root, SID)
    assert fake_host["calls"][0]["model"] == "sonnet"


def test_analyze_override_for_other_host_does_not_apply(root, set_cfg, fake_host):
    _write_turns(root, SID, host="codex")
    set_cfg(_cfg(alter_analyzer={"claude": {"model": "sonnet"}}))
    aa.analyze_session(root, SID)
    assert fake_host["calls"][0]["model"] is None


@pytest.mark.parametrize("host", HOSTS)
def test_analyze_unavailable_host_skips_run(root, set_cfg, fake_host, host):
    _write_turns(root, SID, host=host)
    set_cfg(_cfg())
    fake_host["available"] = False
    res = aa.analyze_session(root, SID)
    assert res["status"] == "failed" and res["reason"] == "host unavailable"
    assert fake_host["calls"] == []
    assert _state(root)[SID]["status"] == "failed"


@pytest.mark.parametrize("host", HOSTS)
def test_analyze_unsupported_host_skips_run(root, set_cfg, fake_host, host):
    _write_turns(root, SID, host=host)
    set_cfg(_cfg())
    fake_host["supported"] = False
    res = aa.analyze_session(root, SID)
    assert res["reason"] == "host unsupported"
    assert fake_host["calls"] == []


def test_run_failure_counts_an_attempt_and_retries_are_capped(root, set_cfg, fake_host, popen):
    _write_turns(root, SID, host="claude")
    set_cfg(_cfg())
    fake_host["status"] = "FAIL"

    first = aa.analyze_session(root, SID)
    assert first["status"] == "failed" and first["attempts"] == 1
    assert _state(root)[SID]["attempts"] == 1

    second = aa.analyze_session(root, SID)
    assert second["attempts"] == 2
    assert _state(root)[SID]["status"] == "failed"

    # launch_pending stops retrying once MAX_ATTEMPTS is reached.
    assert aa.launch_pending(root)["reason"] == "nothing pending"
    assert popen == []
    assert len(fake_host["calls"]) == 2


def test_analyze_missing_conversation_id_fails_without_running(root, set_cfg, fake_host):
    _write_turns(root, SID, conv=None)
    set_cfg(_cfg())
    res = aa.analyze_session(root, SID)
    assert res["reason"] == "no conversation id"
    assert fake_host["calls"] == []


def test_analyze_skipped_when_disabled_or_too_short(root, set_cfg, fake_host):
    _write_turns(root, SID, n=2)
    set_cfg(_cfg())
    assert aa.analyze_session(root, SID)["reason"] == "not enough turns"
    set_cfg(_cfg(alter_analysis=False))
    assert aa.analyze_session(root, SID)["reason"] == "disabled"
    assert fake_host["calls"] == []


def test_analyze_fails_open_on_exception(root, set_cfg, monkeypatch):
    _write_turns(root, SID)
    set_cfg(_cfg())

    def boom(*a, **k):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(runner, "run_agent", boom)
    monkeypatch.setattr(hostcompat, "inspect_host", lambda host, **k: {"available": True, "supported": True})
    res = aa.analyze_session(root, SID)
    assert res["status"] == "error" and "runner exploded" in res["reason"]
    assert _state(root)[SID]["status"] == "failed"


# ---------- 4a. build_command allowed_tools ----------

def _fake_capability_and_inspect(monkeypatch, host: str):
    # Union of the switches each host's command builder requires, so every host can be built offline.
    flags = ["--output-format", "--format", "--print", "--json", "--max-turns", "--strict-mcp-config", "--mcp-config",
             "--model", "--effort", "--allowedTools", "--permission-mode", "--resume", "--max-budget-usd", "--mode",
             "--yolo", "--sandbox", "--dangerously-skip-permissions"]
    monkeypatch.setattr(hosts, "capability",
                        lambda h: hosts.HostCapability(h, f"/bin/{h}", True, True, "cli", "ready"))
    monkeypatch.setattr(hostcompat, "inspect_host",
                        lambda h, **k: {"host": h, "available": True, "supported": True, "flags": flags,
                                        "permission_modes": ["auto", "plan"], "version": "fake"})


def test_build_command_claude_allowed_tools(monkeypatch):
    _fake_capability_and_inspect(monkeypatch, "claude")
    argv = hosts.build_command("claude", "p", read_only=False, allowed_tools=aa.ANALYZER_TOOLS)
    i = argv.index("--allowedTools")
    assert argv[i + 1] == ",".join(aa.ANALYZER_TOOLS)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def test_build_command_claude_without_allowed_tools_has_no_allowlist(monkeypatch):
    _fake_capability_and_inspect(monkeypatch, "claude")
    argv = hosts.build_command("claude", "p", read_only=False)
    assert "--allowedTools" not in argv
    assert "dontAsk" not in argv


@pytest.mark.parametrize("host", ["codex", "opencode", "agy"])
def test_build_command_allowed_tools_is_noop_for_other_hosts(monkeypatch, host):
    _fake_capability_and_inspect(monkeypatch, host)
    base = hosts.build_command(host, "p", read_only=False)
    with_tools = hosts.build_command(host, "p", read_only=False, allowed_tools=aa.ANALYZER_TOOLS)
    assert with_tools == base
    assert "--allowedTools" not in with_tools and "mcp__alter" not in " ".join(with_tools)


# ---------- 4b. keep_turns file ----------

@pytest.fixture
def alter_submit(monkeypatch):
    """Fake the MCP round-trip and message extraction of submit_alter_session_review."""
    turns = [{"role": "user", "text": f"hello {i}"} for i in range(5)]
    monkeypatch.setattr(memory, "extract_session_messages", lambda *a, **k: [dict(t) for t in turns])
    monkeypatch.setattr(memory, "use_host", lambda host: contextlib.nullcontext())
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["fake-alter"], None))

    class FakeMCP:
        def __init__(self, cmd, timeout=10, env=None):
            pass

        def call_tool(self, name, args):
            assert name == "alter_record_conversation"
            return {"structuredContent": {"conversation_id": CONV}}

    monkeypatch.setattr(memory, "MCPStdio", FakeMCP)
    monkeypatch.setattr(memory, "_alter_auto_review_enabled", lambda: True)
    return turns


def test_keep_turns_persists_turns_and_conversation_id(root, alter_submit):
    res = memory.submit_alter_session_review(root, session_id=SID, host="claude", keep_turns=True)
    assert res["status"] == "submitted" and res["conversation_id"] == CONV
    saved = read_json(memory.alter_turns_path(root, SID), {})
    assert saved["session_id"] == SID and saved["host"] == "claude" and saved["conversation_id"] == CONV
    assert len(saved["turns"]) == 5
    assert all(isinstance(t["id"], str) and isinstance(t["sequence"], int) for t in saved["turns"])


def test_keep_turns_false_writes_no_turn_file(root, alter_submit):
    res = memory.submit_alter_session_review(root, session_id=SID, host="claude")
    assert res["status"] == "submitted" and "conversation_id" not in res
    assert not memory.alter_turns_path(root, SID).exists()


def test_alter_turns_path_sanitises_session_id(root):
    p = memory.alter_turns_path(root, "../../etc passwd")
    assert p.parent == root.resolve() / ".ego" / "state" / "alter_turns"
    assert p.name.endswith(".json") and "/" not in p.name


# ---------- 4c. _prune_alter_turns: 7 days / 50 files ----------

def _touch(path: Path, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    t = time.time() - age_seconds
    os.utime(path, (t, t))


def test_prune_constants():
    assert retention.ALTER_TURNS_KEEP == 50
    assert retention.ALTER_TURNS_MAX_AGE_DAYS == 7


def test_prune_removes_older_than_seven_days(root):
    base = root / ".ego" / "state" / "alter_turns"
    _touch(base / "old.json", 8 * 86400)
    _touch(base / "fresh.json", 1 * 86400)
    assert retention._prune_alter_turns(root) == 1
    assert not (base / "old.json").exists() and (base / "fresh.json").exists()


def test_prune_keeps_newest_fifty(root):
    base = root / ".ego" / "state" / "alter_turns"
    for i in range(55):
        _touch(base / f"s{i:02d}.json", (i + 1) * 60)  # s00 newest
    removed = retention._prune_alter_turns(root)
    kept = sorted(p.name for p in base.glob("*.json"))
    assert removed == 5 and len(kept) == 50
    assert "s00.json" in kept and "s49.json" in kept
    assert "s50.json" not in kept and "s54.json" not in kept


def test_prune_combines_age_and_count(root):
    base = root / ".ego" / "state" / "alter_turns"
    _touch(base / "stale.json", 9 * 86400)
    for i in range(51):
        _touch(base / f"f{i:02d}.json", (i + 1) * 60)
    removed = retention._prune_alter_turns(root)
    assert removed == 2  # one stale + one over the 50 cap
    assert len(list(base.glob("*.json"))) == 50


def test_prune_missing_dir_is_noop(root):
    assert retention._prune_alter_turns(root) == 0


# ---------- 4d. config validation ----------

def test_config_defaults_valid():
    cfg = Config()
    cfg.validate()
    assert cfg.alter_analysis is False
    assert cfg.alter_analysis_budget_usd == 0.5
    assert cfg.alter_analysis_timeout_seconds == 600


@pytest.mark.parametrize("kwargs, needle", [
    ({"alter_analysis": "yes"}, "alter_analysis must be a boolean"),
    ({"alter_analyzer": ["opencode"]}, "alter_analyzer must be a dictionary"),
    ({"alter_analyzer": {"nonesuch": {"model": "m"}}}, "Unsupported host"),
    ({"alter_analyzer": {"claude": "sonnet"}}, "alter_analyzer.claude must be a table"),
    ({"alter_analyzer": {"claude": {"model": "m", "effort": "low"}}}, "alter_analyzer.claude must be a table"),
    ({"alter_analyzer": {"claude": {"model": ""}}}, "alter_analyzer.claude.model must be a non-empty string"),
    ({"alter_analyzer": {"claude": {"model": 3}}}, "alter_analyzer.claude.model must be a non-empty string"),
    ({"alter_analysis_budget_usd": -0.1}, "alter_analysis_budget_usd must be a number >= 0"),
    ({"alter_analysis_budget_usd": True}, "alter_analysis_budget_usd must be a number >= 0"),
    ({"alter_analysis_timeout_seconds": 0}, "alter_analysis_timeout_seconds must be an integer > 0"),
    ({"alter_analysis_timeout_seconds": 1.5}, "alter_analysis_timeout_seconds must be an integer > 0"),
])
def test_config_rejects_invalid_alter_settings(kwargs, needle):
    with pytest.raises(EgoError) as exc:
        Config(**kwargs).validate()
    assert exc.value.code == "INVALID_CONFIG" and needle in str(exc.value)


def test_config_accepts_zero_budget_and_host_override():
    cfg = Config(alter_analysis_budget_usd=0, alter_analyzer={"claude": {"model": "sonnet"}})
    cfg.validate()
