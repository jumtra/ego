"""Integration tests for LoopGuard at Claude worker segment boundaries."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ego.adapters.runner import RunResult
from ego.domain import loopguard as Envelope
from ego.usecases import replan, work


def _transcript_file(claude_dir: Path, cwd: Path, session_id: str) -> Path:
    proj = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    path = claude_dir / "projects" / proj / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_stuck_segment(path: Path, *, repeat: int = 2) -> None:
    lines: list[dict] = []
    for i in range(repeat):
        tool_id = f"tool-{path.stat().st_size if path.exists() else 0}-{i}"
        lines.append({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": f"edit-{tool_id}", "name": "Edit", "input": {"file_path": "app.py"}},
                    {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "pytest tests/test_app.py"}},
                ],
            },
        })
        lines.append({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "is_error": True, "content": "FAILED tests/test_app.py::test_one - AssertionError"},
                ],
            },
        })
    with path.open("a", encoding="utf-8") as handle:
        for obj in lines:
            handle.write(json.dumps(obj) + "\n")


def _setup_repo(tmp_path: Path, *, dry_run: bool | None = None) -> Path:
    ego_dir = tmp_path / ".ego"
    ego_dir.mkdir(exist_ok=True)
    (ego_dir / "config.toml").write_text(
        'knowledge = "file"\n'
        "clear_suggest_tokens = 100000\n"
        "worker_segment_turns = 2\n"
        "max_turns = 10\n",
        encoding="utf-8",
    )
    lines = [
        "[loopguard]",
        "min_turns = 1",
        "edit_churn = 2",
        "error_outputs = 2",
    ]
    if dry_run is not None:
        lines.append(f"dry_run = {'true' if dry_run else 'false'}")
    (ego_dir / "defaults.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    return claude_dir


def _progress_events(tmp_path: Path, run_id: str) -> list[dict]:
    p = tmp_path / ".ego" / "state" / "runs" / run_id / "progress.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_loopguard_dry_run_default_completes_and_emits_events(tmp_path: Path, monkeypatch):
    claude_dir = _setup_repo(tmp_path, dry_run=None)  # default dry_run = true
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    prompts: list[str] = []

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        prompts.append(prompt)
        sid = "sess-dry"
        tpath = _transcript_file(claude_dir, Path(cwd), sid)
        _append_stuck_segment(tpath, repeat=2)
        if len(prompts) < 3:
            return RunResult("PASS", f"seg-{len(prompts)}", "", 0, 0.1, host, session_id=sid, stop_reason="error_max_turns", usage={"turns": 2})
        return RunResult("PASS", "done", "", 0, 0.1, host, session_id=sid, stop_reason="end_turn", usage={"turns": 2})

    monkeypatch.setattr(work, "run_agent", fake_agent)
    spec = {
        "goal": "Fix app",
        "nodes": [{"id": "c1", "kind": "agent", "role": "coder", "prompt": "Fix app.py", "write_allowlist": ["app.py"], "max_attempts": 1}],
    }
    res = work.execute(tmp_path, spec, host="claude", trust_cli=True, run_id="lg-dry")

    assert res["final_status"] == "PASSED"
    assert len(prompts) == 3
    assert all("Process check:" not in p for p in prompts)
    lg_events = [e for e in _progress_events(tmp_path, "lg-dry") if e["event"] == "loopguard"]
    assert len(lg_events) == 2
    assert all(e["dry_run"] is True for e in lg_events)
    assert [e["decision"] for e in lg_events] == ["nudge", "nudge"]
    assert Envelope._load_samples(tmp_path) == [6]


def test_loopguard_active_nudges_once_then_escalates_to_replan_and_alter_report(tmp_path: Path, monkeypatch):
    claude_dir = _setup_repo(tmp_path, dry_run=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    reported: list[tuple[str, str, str, dict]] = []

    class FakeAlterClient:
        def __init__(self, root: Path, timeout: int = 30):
            pass

        def report(self, plan_id: str, run_id: str, state: str, nodes: dict):
            reported.append((plan_id, run_id, state, json.loads(json.dumps(nodes))))
            return {"ok": True}

    monkeypatch.setattr("ego.adapters.alter_plan.AlterPlanClient", FakeAlterClient)

    prompts: list[str] = []

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        prompts.append(prompt)
        sid = "sess-active"
        tpath = _transcript_file(claude_dir, Path(cwd), sid)
        _append_stuck_segment(tpath, repeat=2)
        return RunResult(
            "PASS",
            f"seg-{len(prompts)}",
            "",
            0,
            0.1,
            host,
            session_id=sid,
            stop_reason="error_max_turns",
            cost_usd=0.25,
            usage={"turns": 2, "input_tokens": 100, "output_tokens": 50},
        )

    monkeypatch.setattr(work, "run_agent", fake_agent)
    spec = {
        "goal": "Fix app",
        "source": {"alter_plan": {"plan_id": "plan-lg"}},
        "nodes": [{"id": "c1", "kind": "agent", "role": "coder", "prompt": "Fix app.py", "write_allowlist": ["app.py"], "max_attempts": 1}],
    }
    res = work.execute(tmp_path, spec, host="claude", trust_cli=True, run_id="lg-active")

    assert res["final_status"] == "FAILED"
    assert len(prompts) == 2
    assert "Process check:" in prompts[1]

    attempt = res["results"]["c1"]["attempts"][-1]
    assert attempt["execution"]["reason"] == "STAGNATION"
    assert attempt["stagnation"]["count"] >= 2
    assert "pytest tests/test_app.py" in attempt["stagnation"]["commands"]
    assert attempt["feedback"].startswith("STAGNATION: ")
    assert "pytest tests/test_app.py" in attempt["feedback"]

    lg_events = [e for e in _progress_events(tmp_path, "lg-active") if e["event"] == "loopguard"]
    assert [(e["decision"], e["dry_run"]) for e in lg_events] == [("nudge", False), ("escalate", False)]

    failures = replan._failures(res, 500)
    assert len(failures) == 1
    assert failures[0]["stagnation"] == attempt["stagnation"]
    hist = replan.history_text(
        [{"n": 1, "status": "FAILED", "failures": failures, "passed": [], "changed_files": []}],
        max_chars=2000,
        feedback_chars=500,
        detailed=2,
    )
    assert hist is not None and "STAGNATION:" in hist and "stagnation:" in hist

    assert reported[-1][2] == "failed"
    node_payload = reported[-1][3]["c1"]
    assert node_payload["stagnation"] == attempt["stagnation"]
    assert node_payload["cost_usd"] == 0.5
