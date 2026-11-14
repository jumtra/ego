"""Claude workers run in segments; at the context limit they checkpoint and continue in a fresh session."""
from pathlib import Path
from ego import worker
from ego.runner import RunResult
from ego.session import save_checkpoint
from ego.worker import run_worker


def result(stop="error_max_turns", sid="s1", status="PASS", turns=2, cost=0.1):
    return RunResult(status, "out", "", 0, 1.0, "claude", cost_usd=cost, session_id=sid, stop_reason=stop,
                     usage={"input_tokens": 10, "output_tokens": 5, "turns": turns})


def drive(tmp_path, monkeypatch, replies, tokens, host="claude", read_only=False, **kw):
    calls = []

    def agent(h, prompt, cwd, timeout, **k):
        calls.append({"prompt": prompt, **k})
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        return reply(cwd, k) if callable(reply) else reply

    monkeypatch.setattr(worker, "worker_context_tokens", lambda cwd, sid: tokens)
    events = []
    r = run_worker(host, "DO IT", tmp_path, 600, agent=agent, threshold=1000, segment_turns=2, tag="n-1-x",
                   max_turns=kw.pop("max_turns", 10), read_only=read_only, on_event=lambda **f: events.append(f), **kw)
    return r, calls, events


def test_below_the_limit_the_same_session_continues(tmp_path, monkeypatch):
    r, calls, events = drive(tmp_path, monkeypatch, [result(), result(stop="success")], tokens=500)
    assert r.status == "PASS" and r.segments == 2 and r.rotations == 0
    assert calls[0].get("resume_session") is None and calls[1]["resume_session"] == "s1"
    assert calls[1]["prompt"].startswith("Continue") and calls[0]["max_turns"] == 2
    assert [e["action"] for e in events] == ["continue"]
    assert r.usage["input_tokens"] == 20 and r.cost_usd == 0.2


def test_at_the_limit_it_checkpoints_and_resumes_in_a_fresh_session(tmp_path, monkeypatch):
    def save(cwd, k):
        assert k["env"]["EGO_RUN_ID"] == "n-1-x" and k["env"]["EGO_RUN"] == "claude"
        save_checkpoint(cwd, "t1", goal="g", next_steps=["finish B"], run=k["env"]["EGO_RUN_ID"])
        return result(stop="success", sid="s1", turns=1)
    r, calls, events = drive(tmp_path, monkeypatch, [result(), save, result(stop="success", sid="s2")], tokens=5000)
    assert r.status == "PASS" and r.rotations == 1 and r.segments == 3
    assert calls[1]["resume_session"] == "s1" and calls[1]["max_turns"] == 3 and "5000" in calls[1]["prompt"]
    assert calls[2]["resume_session"] is None
    assert "DO IT" in calls[2]["prompt"] and "finish B" in calls[2]["prompt"]
    assert [e["action"] for e in events] == ["rotate"]
    assert not list((tmp_path / ".ego/memory/handoffs").glob("*.json"))


def test_a_missing_checkpoint_fails_instead_of_losing_the_work(tmp_path, monkeypatch):
    r, calls, _ = drive(tmp_path, monkeypatch, [result(), result(stop="success")], tokens=5000)
    assert r.status == "FAIL" and r.reason == "NO_CHECKPOINT" and len(calls) == 2


def test_other_hosts_and_read_only_nodes_run_once(tmp_path, monkeypatch):
    for host, ro in (("codex", False), ("claude", True)):
        r, calls, _ = drive(tmp_path, monkeypatch, [result(stop="error_max_turns")], tokens=5000, host=host, read_only=ro)
        assert len(calls) == 1 and "resume_session" not in calls[0] and calls[0]["max_turns"] == 10


def test_turn_budget_is_shared_across_segments(tmp_path, monkeypatch):
    r, calls, _ = drive(tmp_path, monkeypatch, [result()], tokens=500, max_turns=4)
    assert r.status == "FAIL" and r.reason == "MAX_TURNS" and len(calls) == 2


def test_spend_is_capped_across_segments(tmp_path, monkeypatch):
    r, calls, _ = drive(tmp_path, monkeypatch, [result(cost=0.6)], tokens=500, max_budget_usd=1.0)
    assert r.reason == "BUDGET_EXHAUSTED" and len(calls) == 2
    assert abs(calls[1]["max_budget_usd"] - 0.4) < 1e-9


def test_worker_context_is_read_from_the_claude_session_record(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    import re, json
    d = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(proj.resolve()))
    d.mkdir(parents=True)
    (d / "abc.jsonl").write_text(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 10, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 90, "output_tokens": 5}}}) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    assert worker.worker_context_tokens(proj, "abc") == 800
    assert worker.worker_context_tokens(proj, "../x") is None and worker.worker_context_tokens(proj, None) is None
