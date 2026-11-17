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
from ego.usecases import ptyrun

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
    ctl = {"available": True, "supported": True, "status": "PASS", "calls": [], "inspected": [], "output": None}

    def fake_inspect(host, **kw):
        ctl["inspected"].append(host)
        return {"host": host, "available": ctl["available"], "supported": ctl["supported"],
                "reason": "", "flags": [], "version": "fake"}

    def fake_run(host, prompt, cwd, timeout=180, **kw):
        ctl["calls"].append({"host": host, "prompt": prompt, "cwd": cwd, "timeout": timeout, **kw})
        ok = ctl["status"] == "PASS"
        # By default the analyzer reports that alter accepted the analysis for CONV; tests may override.
        default = f"done\nALTER_ANALYSIS conversation={CONV} result=attached" if ok else ""
        out = ctl["output"] if ctl["output"] is not None else default
        return RunResult(status=ctl["status"], output=out, stderr="", exit_code=0 if ok else 1,
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


def test_launch_reanalyzes_when_session_grew_since_analysis(root, popen, set_cfg):
    _write_turns(root, SID, n=6)
    write_json(aa._state_path(root), {"schema_version": 1, "sessions": {SID: {"status": "analyzed", "turns": 4}}})
    set_cfg(_cfg())
    assert aa.launch_analysis(root, SID)["status"] == "launched"
    write_json(aa._state_path(root), {"schema_version": 1, "sessions": {SID: {"status": "analyzed", "turns": 6}}})
    assert aa.launch_analysis(root, SID)["reason"] == "analyzed"


def test_analyze_records_analyzed_turn_count(root, set_cfg, fake_host):
    _write_turns(root, SID, n=6)
    set_cfg(_cfg())
    assert aa.analyze_session(root, SID)["status"] == "analyzed"
    assert _state(root)[SID]["turns"] == 6


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
    # The conversation is already recorded: go straight to the skill's fallback (extract) step.
    assert "Skip step 1" in call["prompt"] and f"alter_extract_cognitive` with `conversation_id: \"{CONV}\"" in call["prompt"]
    assert "`turn_id` = the turn's `id`" in call["prompt"] and "`turn_seq` = its `sequence`" in call["prompt"]
    assert call["allowed_tools"] == aa.ANALYZER_TOOLS
    assert call["mcp_servers"] == ["alter"]
    assert call["read_only"] is False and call["lean"] is True
    assert call["env"][aa.ANALYZER_ENV] == "1" and call["env"]["EGO_ALTER_AUTO_REVIEW"] == "0"
    entry = _state(root)[SID]
    assert entry["status"] == "analyzed" and entry["attempts"] == 1 and entry["host"] == host


@pytest.mark.parametrize("output", [
    "",                                                        # no marker at all
    f"ALTER_ANALYSIS conversation={CONV} result=not_attached",  # analyzer says alter did not accept
    "ALTER_ANALYSIS conversation=other-conv result=attached",  # marker names a different conversation
    f"ALTER_ANALYSIS conversation={CONV} result=attached\nALTER_ANALYSIS conversation={CONV} result=not_attached",  # last marker wins
])
def test_analyze_not_marked_analyzed_without_confirmed_attachment(root, set_cfg, fake_host, output):
    _write_turns(root, SID, host="claude")
    set_cfg(_cfg())
    fake_host["output"] = output
    res = aa.analyze_session(root, SID)
    assert res["status"] == "failed"
    entry = _state(root)[SID]
    assert entry["status"] == "failed" and entry["attempts"] == 1 and entry["reason"]


def test_analyze_run_failure_is_failed_even_with_attached_marker(root, set_cfg, fake_host):
    _write_turns(root, SID, host="claude")
    set_cfg(_cfg())
    fake_host["status"] = "FAIL"
    fake_host["output"] = f"ALTER_ANALYSIS conversation={CONV} result=attached"
    assert aa.analyze_session(root, SID)["status"] == "failed"


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


def test_conversation_id_unwraps_mcp_content_text_json():
    res = {"content": [{"type": "text", "text": f'{{"saved": true, "id": "{CONV}"}}'}]}
    assert memory._conversation_id(res) == CONV
    assert memory._conversation_id({"content": [{"type": "text", "text": "not json"}]}) is None


def test_ensure_turn_ids_mirrors_alter_defaults():
    # alter stores a missing id as `turn-${sequence}` and a missing sequence as the index;
    # the analyzer cites these ids, so ego's saved copy must use the same rule.
    turns = [{"role": "user", "text": "a"},                          # neither: index 0
             {"role": "assistant", "text": "b", "sequence": 7},      # sequence only
             {"role": "user", "text": "c", "id": "u-1", "sequence": 9},
             {"role": "assistant", "text": "d", "id": "a-1"}]        # id only: sequence = index
    out = memory._ensure_turn_ids(turns)
    assert [(t["id"], t["sequence"]) for t in out] == [("turn-0", 0), ("turn-7", 7), ("u-1", 9), ("a-1", 3)]


def test_keep_turns_false_writes_no_turn_file(root, alter_submit):
    res = memory.submit_alter_session_review(root, session_id=SID, host="claude")
    assert res["status"] == "submitted" and "conversation_id" not in res
    assert not memory.alter_turns_path(root, SID).exists()


def test_alter_turns_path_sanitises_session_id(root):
    p = memory.alter_turns_path(root, "../../etc passwd")
    assert p.parent == root.resolve() / ".ego" / "state" / "alter_turns"
    assert p.name.endswith(".json") and "/" not in p.name


def test_resolve_alter_session_groups_ego_run_segments(monkeypatch):
    monkeypatch.delenv("EGO_RUN", raising=False)
    monkeypatch.delenv("EGO_RUN_ID", raising=False)
    assert memory.resolve_alter_session("host-1") == ("host-1", None)
    monkeypatch.setenv("EGO_RUN", "claude")
    monkeypatch.setenv("EGO_RUN_ID", "abc123")
    assert memory.resolve_alter_session("host-1") == ("ego-run-abc123", "host-1")
    assert memory.resolve_alter_session("host-2") == ("ego-run-abc123", "host-2")
    assert memory.resolve_alter_session("") == ("ego-run-abc123", "default")
    sid_a, seg_a = memory.resolve_alter_session("", transcript="/tmp/seg-a.jsonl")
    sid_a2, seg_a2 = memory.resolve_alter_session("", transcript="/tmp/seg-a.jsonl")
    sid_b, seg_b = memory.resolve_alter_session("", transcript="/tmp/seg-b.jsonl")
    assert sid_a == sid_a2 == sid_b == "ego-run-abc123"
    assert seg_a == seg_a2 and seg_a != seg_b and seg_a.startswith("transcript-")


def test_merge_segment_turns_guarantees_unique_custom_ids():
    merged = memory._merge_segment_turns([
        {"id": "s1", "turns": [{"role": "user", "text": "a", "id": "foo"}, {"role": "assistant", "text": "b", "id": "foo-2"}]},
        {"id": "s2", "turns": [{"role": "user", "text": "c", "id": "foo"}]},
    ])
    ids = [t["id"] for t in merged]
    assert ids == ["foo", "foo-2", "foo-2-1"]
    assert len(set(ids)) == len(ids)


def test_submit_alter_session_review_merges_cleared_segments(root, monkeypatch):
    sent: list[dict[str, object]] = []
    current: list[dict[str, object]] = []
    monkeypatch.setattr(memory, "extract_session_messages", lambda *a, **k: [dict(t) for t in current])
    monkeypatch.setattr(memory, "use_host", lambda host: contextlib.nullcontext())
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda r: (["fake-alter"], None))
    monkeypatch.setattr(memory, "_alter_auto_review_enabled", lambda: True)

    class FakeMCP:
        def __init__(self, cmd, timeout=10, env=None):
            pass

        def call_tool(self, name, args):
            assert name == "alter_record_conversation"
            sent.append(args)
            return {"structuredContent": {"conversation_id": CONV}}

    monkeypatch.setattr(memory, "MCPStdio", FakeMCP)

    run_sid = memory.alter_run_session_id("run-1")
    current[:] = [
        {"role": "user", "text": "seg1 u0", "id": "turn-0", "sequence": 0},
        {"role": "assistant", "text": "seg1 a1", "id": "turn-1", "sequence": 1},
    ]
    r1 = memory.submit_alter_session_review(root, session_id=run_sid, host="claude", keep_turns=True, segment_id="seg-1")
    assert r1["status"] == "submitted" and r1["count"] == 2

    current[:] = [
        {"role": "user", "text": "seg2 u0", "id": "turn-0", "sequence": 0},
        {"role": "assistant", "text": "seg2 a1", "id": "turn-1", "sequence": 1},
    ]
    r2 = memory.submit_alter_session_review(root, session_id=run_sid, host="claude", keep_turns=True, segment_id="seg-2")
    assert r2["status"] == "submitted" and r2["count"] == 4
    msgs2 = sent[-1]["messages"]
    assert [m["text"] for m in msgs2] == ["seg1 u0", "seg1 a1", "seg2 u0", "seg2 a1"]
    assert [(m["id"], m["sequence"]) for m in msgs2] == [("turn-0", 0), ("turn-1", 1), ("turn-2", 2), ("turn-3", 3)]

    # Re-sending the same segment does not duplicate; growing seg-2 updates it in place.
    r_same = memory.submit_alter_session_review(root, session_id=run_sid, host="claude", keep_turns=True, segment_id="seg-2")
    assert r_same["status"] == "skipped" and r_same["reason"] == "unchanged"

    current.append({"role": "user", "text": "seg2 u2", "id": "turn-2", "sequence": 2})
    r3 = memory.submit_alter_session_review(root, session_id=run_sid, host="claude", keep_turns=True, segment_id="seg-2")
    assert r3["status"] == "submitted" and r3["count"] == 5
    saved_turns = read_json(memory.alter_turns_path(root, run_sid), {})
    assert len(saved_turns["turns"]) == 5
    assert [t["text"] for t in saved_turns["turns"]] == ["seg1 u0", "seg1 a1", "seg2 u0", "seg2 a1", "seg2 u2"]


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
    assert cfg.alter_analysis_timeout_seconds == 600
    assert not hasattr(cfg, "alter_analysis_budget_usd")


@pytest.mark.parametrize("kwargs, needle", [
    ({"alter_analysis": "yes"}, "alter_analysis must be a boolean"),
    ({"alter_analyzer": ["opencode"]}, "alter_analyzer must be a dictionary"),
    ({"alter_analyzer": {"nonesuch": {"model": "m"}}}, "Unsupported host"),
    ({"alter_analyzer": {"claude": "sonnet"}}, "alter_analyzer.claude must be a table"),
    ({"alter_analyzer": {"claude": {"model": "m", "effort": "low"}}}, "alter_analyzer.claude must be a table"),
    ({"alter_analyzer": {"claude": {"model": ""}}}, "alter_analyzer.claude.model must be a non-empty string"),
    ({"alter_analyzer": {"claude": {"model": 3}}}, "alter_analyzer.claude.model must be a non-empty string"),
    ({"alter_analysis_timeout_seconds": 0}, "alter_analysis_timeout_seconds must be an integer > 0"),
    ({"alter_analysis_timeout_seconds": 1.5}, "alter_analysis_timeout_seconds must be an integer > 0"),
])
def test_config_rejects_invalid_alter_settings(kwargs, needle):
    with pytest.raises(EgoError) as exc:
        Config(**kwargs).validate()
    assert exc.value.code == "INVALID_CONFIG" and needle in str(exc.value)


def test_config_accepts_host_override():
    cfg = Config(alter_analyzer={"claude": {"model": "sonnet"}})
    cfg.validate()


# ---------- 5. ego run end / hook wiring ----------

def test_no_host_gets_a_session_end_hook():
    from ego.domain.templates import generated_files
    files = generated_files(Config(knowledge="alter", hosts=["claude", "codex", "opencode", "agy"], auto_resume=False,
                                   clear_suggest_tokens=0, usage_switch_percent=0))
    import json
    assert "SessionEnd" not in json.loads(files[".claude/settings.json"])["hooks"]
    assert "SessionEnd" not in json.loads(files[".codex/hooks.json"])["hooks"]
    assert "SessionEnd" not in json.loads(files[".agents/hooks.json"])["ego"]
    assert "session-end" not in files[".opencode/plugins/ego.ts"]


def test_docs_and_templates_do_not_mention_session_end():
    repo = Path(__file__).resolve().parents[1]
    sources = [repo / "src/ego/domain/templates.py", *sorted((repo / "docs").rglob("*.md"))]
    assert sources
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "SessionEnd" not in text, path
        assert "session-end" not in text, path


def test_hooks_noop_inside_analyzer(root, monkeypatch, capsys):
    import io, sys
    from types import SimpleNamespace
    from ego import cli
    monkeypatch.setenv(aa.ANALYZER_ENV, "1")
    for event in ("stop", "session-start", "session-end", "user-prompt"):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        rc = cli._cmd_hook(SimpleNamespace(root=str(root), event=event, host="claude"))
        assert rc == 0
        assert capsys.readouterr().out.strip() == "{}"


def test_ego_run_end_launches_analysis_for_the_logical_session(root, set_cfg, monkeypatch):
    from types import SimpleNamespace
    recorder: list[tuple] = []
    seen: dict = {}
    mode = {"interrupt": False}

    def fake_run(host, argv, run_root, threshold, *, env=None, **kw):
        seen["run_id"] = env[ptyrun.RUN_ID_ENV]
        if mode["interrupt"]:
            raise KeyboardInterrupt
        return (0, 0)

    tty = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(sys, "stdin", tty)
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(ptyrun.shutil, "which", lambda name: "/bin/true")
    monkeypatch.setattr(aa, "launch_analysis", lambda r, sid: recorder.append((r, sid)) or {"status": "launched"})
    monkeypatch.setattr(ptyrun, "run", fake_run)

    # Normal end of the wrapper: one analysis for the whole ego run.
    set_cfg(_cfg())
    assert ptyrun.run_host("claude", [], root, 1000) == 0
    assert recorder == [(root.resolve(), f"ego-run-{seen['run_id']}")]

    # The wrapper is interrupted: the end still launches analysis, exactly once.
    recorder.clear()
    mode["interrupt"] = True
    with pytest.raises(KeyboardInterrupt):
        ptyrun.run_host("claude", [], root, 1000)
    assert len(recorder) == 1

    # alter analysis off: nothing launches.
    recorder.clear()
    mode["interrupt"] = False
    set_cfg(_cfg(alter_analysis=False))
    ptyrun.run_host("claude", [], root, 1000)
    assert recorder == []


def test_stop_hook_starts_no_watcher(root, set_cfg, monkeypatch, popen, capsys):
    import io
    import json
    from types import SimpleNamespace
    from ego import cli
    recorded: list = []
    monkeypatch.setattr(memory, "submit_alter_session_review", lambda *a, **k: recorded.append(k) or {"status": "submitted"})
    set_cfg(_cfg())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": SID, "cwd": str(root)})))
    assert cli._cmd_hook(SimpleNamespace(root=str(root), event="stop", host="agy")) == 0
    capsys.readouterr()
    assert recorded  # the stop hook still submits the turn to alter
    assert popen == []  # but it never starts an analysis or watcher process


def test_end_detection_code_is_gone():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    banned = ("IDLE_SECONDS", "launch_pending", "start_idle_watch", "analyze_when_idle", "--when-idle")
    hits = [f"{p.relative_to(repo)}: {b}"
            for p in sorted((repo / "src").rglob("*.py"))
            for b in banned if b in p.read_text(encoding="utf-8")]
    assert hits == []
