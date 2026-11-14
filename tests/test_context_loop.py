"""Context efficiency (trimming, telemetry, lean workers, audit) and the outer goal loop."""
import json
import os
from pathlib import Path

import pytest

from ego import hosts, work
from ego.cli import main
from ego.codeintel import text_fallback
from ego.config import Config, load_config
from ego.context import ALWAYS_ON_BUDGET, SKILL_BODY_BUDGET, audit, clip_output
from ego.loop import DONE_NODE, history_text, run_loop
from ego.runner import RunResult, parse_host_usage
from ego.util import EgoError
from ego.work import compact_result, execute, usage_summary


# ---------------------------------------------------------------- trimming

def test_clip_output_keeps_the_tail_where_failures_are_reported():
    text = "collected 900 items\n" + "." * 9000 + "\nFAILED tests/test_x.py::test_y - AssertionError: 3 != 4"
    clipped = clip_output(text, 1000)
    assert len(clipped) <= 1000
    assert clipped.endswith("AssertionError: 3 != 4")
    assert "chars omitted" in clipped and clipped.startswith("collected 900 items")
    assert clip_output("short", 1000) == "short"


def test_failing_check_feedback_reaches_the_retry_with_its_tail(tmp_path: Path, monkeypatch):
    (tmp_path / "noisy.py").write_text("import sys\nprint('x'*20000)\nprint('REAL FAILURE AT END')\nsys.exit(1)\n")
    prompts = []

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        prompts.append(prompt)
        return RunResult("PASS", "ok", "", 0, 0.0, host, cost_usd=0.01)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    result = execute(tmp_path, {"goal": "g", "nodes": [
        {"id": "impl", "kind": "agent", "prompt": "do it", "host": "claude", "write_allowlist": ["src/**"],
         "checks": [["python3", "noisy.py"]], "max_attempts": 2}]}, host="claude", trust_cli=True)
    assert result["final_status"] == "FAILED" and len(prompts) == 2
    assert "REAL FAILURE AT END" in prompts[1]
    assert len(prompts[1]) < 6000


# ---------------------------------------------------------------- telemetry

def test_usage_includes_prompt_cache_traffic():
    raw = json.dumps({"type": "result", "result": "ok", "total_cost_usd": 0.12, "num_turns": 7, "usage": {
        "input_tokens": 40, "output_tokens": 300, "cache_read_input_tokens": 52000, "cache_creation_input_tokens": 9000}})
    assert parse_host_usage("claude", raw) == {"input_tokens": 40, "output_tokens": 300, "cache_read_tokens": 52000,
                                                "cache_creation_tokens": 9000, "turns": 7}
    codex = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 80, "output_tokens": 5}})
    assert parse_host_usage("codex", codex)["cache_read_tokens"] == 80
    assert parse_host_usage("claude", "not json") is None


def test_run_usage_is_aggregated_and_unknown_cost_is_not_reported_as_zero():
    def attempt(host, cost, usage=None):
        return {"execution": {"host": host, "cost_usd": cost, "usage": usage}}
    record = {"results": {
        "a": {"attempts": [attempt("claude", 0.10, {"input_tokens": 5, "output_tokens": 7, "cache_read_tokens": 100}),
                           attempt("claude", 0.05, {"input_tokens": 1, "output_tokens": 2})]},
        "gate": {"attempts": [attempt("local", 0.0)]}}}
    total = usage_summary(record)
    assert total["model_calls"] == 2 and total["cost_usd"] == 0.15 and total["cost_known"] is True
    assert total["input_tokens"] == 6 and total["cache_read_tokens"] == 100
    record["results"]["b"] = {"attempts": [attempt("opencode", None)]}
    assert usage_summary(record)["cost_known"] is False


def test_cli_prints_compact_run_summary_not_the_record(tmp_path: Path, capsys):
    (tmp_path / "gen.py").write_text("from pathlib import Path\nPath('result.txt').write_text('OK')\n")
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"goal": "g", "nodes": [
        {"id": "gen", "kind": "code", "argv": ["python3", "gen.py"], "write_allowlist": ["result.txt"]}]}))
    assert main(["work", "run", "--root", str(tmp_path), "--task", str(task)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["final_status"] == "PASSED" and out["changed_files"] == ["result.txt"]
    assert "base_snapshot" not in out and "results" not in out
    assert out["usage"]["model_calls"] == 0 and out["record_path"].endswith("record.json")
    assert main(["work", "status", out["run_id"], "--root", str(tmp_path), "--verbose"]) == 0
    assert "base_snapshot" in json.loads(capsys.readouterr().out)


def test_compact_result_reports_clipped_failure_feedback():
    result = {"run_id": "run-1", "final_status": "FAILED", "node_status": {"a": "FAILED"}, "changed_files": [],
              "results": {"a": {"attempts": [{"feedback": "x" * 5000 + " boom"}], "output": "raw"}}}
    out = compact_result(result)
    assert out["failures"][0]["node"] == "a" and out["failures"][0]["feedback"].endswith("boom")
    assert len(out["failures"][0]["feedback"]) <= 800


# ---------------------------------------------------------------- lean workers

def _fake_claude(monkeypatch, tmp_path: Path, flags: str):
    folder = tmp_path / "vendor-bin"
    folder.mkdir()
    script = folder / "claude"
    script.write_text("#!/usr/bin/env python3\nimport sys\n"
                      "print('claude 9.9.9' if '--version' in sys.argv else " + repr(flags) + ")\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("EGO_CACHE_DIR", str(tmp_path / "cache"))


def test_lean_worker_drops_inherited_mcp_and_carries_turn_and_budget_limits(monkeypatch, tmp_path: Path):
    _fake_claude(monkeypatch, tmp_path, "--output-format --max-turns --permission-mode --strict-mcp-config --max-budget-usd auto")
    argv = hosts.build_command("claude", "p", read_only=False, max_turns=30, lean=True, max_budget_usd=1.5, use_stdin=True)
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--max-turns") + 1] == "30"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.50"
    inherit = hosts.build_command("claude", "p", read_only=False, use_stdin=True)
    assert "--strict-mcp-config" not in inherit and "--max-budget-usd" not in inherit


def test_lean_flags_are_skipped_when_the_installed_cli_lacks_them(monkeypatch, tmp_path: Path):
    _fake_claude(monkeypatch, tmp_path, "--output-format --max-turns --permission-mode auto")
    argv = hosts.build_command("claude", "p", read_only=False, lean=True, max_budget_usd=1.0, use_stdin=True)
    assert "--strict-mcp-config" not in argv and "--max-budget-usd" not in argv


def test_managed_worker_gets_configured_turns_not_the_planner_default(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        seen.update(kw)
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    spec = {"goal": "g", "nodes": [{"id": "impl", "kind": "agent", "prompt": "p", "host": "claude", "write_allowlist": ["src/**"]}]}
    execute(tmp_path, spec, host="claude", trust_cli=True)
    assert seen["max_turns"] == Config().max_turns == 30 and seen["lean"] is True
    spec["nodes"][0]["max_turns"] = 5
    execute(tmp_path, spec, host="claude", trust_cli=True)
    assert seen["max_turns"] == 5


def test_worker_context_config_is_validated(tmp_path: Path):
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text('worker_context = "inherit"\nmax_turns = 12\n')
    cfg = load_config(tmp_path)
    assert cfg.worker_context == "inherit" and cfg.max_turns == 12
    (tmp_path / ".ego/config.toml").write_text('worker_context = "everything"\n')
    with pytest.raises(EgoError):
        load_config(tmp_path)


# ---------------------------------------------------------------- audit and retrieval

def test_generated_harness_fits_its_context_budgets(project: Path):
    report = audit(project)
    assert report["status"] == "PASS", report["findings"]
    for host in ("claude", "agents"):
        assert 0 < report["hosts"][host]["always_on_est_tokens"] <= ALWAYS_ON_BUDGET
    bodies = [i for i in report["hosts"]["claude"]["items"] if i["part"] == "body" and "/skills/" in i["path"]]
    assert bodies and all(i["est_tokens"] <= SKILL_BODY_BUDGET for i in bodies)


def test_audit_flags_bloat_and_treats_path_scoped_rules_as_on_demand(project: Path, capsys):
    (project / ".claude/rules/api.md").write_text("---\npaths: src/api/**\n---\n" + "rule " * 2000)
    assert audit(project)["status"] == "PASS"
    with (project / "CLAUDE.md").open("a") as f:
        f.write("\n".join(f"- convention number {i} that nobody needs every turn" for i in range(260)))
    report = audit(project)
    problems = {f["problem"] for f in report["findings"]}
    assert report["status"] == "OVER_BUDGET"
    assert {"always-on context over budget", "instruction file too long"} <= problems
    assert main(["context", "audit", "--root", str(project)]) == 5
    assert json.loads(capsys.readouterr().out)["status"] == "OVER_BUDGET"


def test_natural_language_goal_finds_candidate_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/timeout_validation.py").write_text("def validate(value):\n    return value > 0\n")
    (tmp_path / "src/other.py").write_text("def unrelated():\n    pass\n")
    found = text_fallback(tmp_path, "Add timeout input validation and reject negative values")
    assert found["strategy"] == "local_term_rank"
    assert found["matches"][0]["path"] == "src/timeout_validation.py"
    assert all(m["path"] != "src/other.py" for m in found["matches"])


# ---------------------------------------------------------------- outer loop

def _loop_project(root: Path) -> None:
    (root / "make_a.py").write_text("from pathlib import Path\nPath('a.txt').write_text('A')\n")
    # Proves the second iteration starts from the verified staging of the first.
    (root / "make_b.py").write_text("from pathlib import Path\nassert Path('a.txt').read_text() == 'A'\nPath('b.txt').write_text('B')\n")
    (root / "check.py").write_text("from pathlib import Path\nimport sys\n"
                                    "missing=[n for n in ('a.txt','b.txt') if not Path(n).exists()]\n"
                                    "print('missing: ' + ', '.join(missing))\nsys.exit(1 if missing else 0)\n")
    (root / "noop.py").write_text("print('nothing to do')\n")


def _node(script: str, out: str) -> dict:
    return {"id": "step-" + out[0], "kind": "code", "argv": ["python3", script], "write_allowlist": [out]}


def test_loop_replans_from_bounded_history_and_carries_verified_work(tmp_path: Path):
    _loop_project(tmp_path)
    histories = []

    def planner(goal, history, workdir):
        histories.append(history)
        return {"goal": goal, "nodes": [_node("make_a.py", "a.txt") if history is None else _node("make_b.py", "b.txt")]}

    result = run_loop(tmp_path, "produce a and b", done_checks=[["python3", "check.py"]], planner=planner,
                      scope=["*.txt"], max_iterations=4)
    assert result["status"] == "VERIFIED"
    assert [it["status"] for it in result["iterations"]] == ["FAILED", "PASSED"]
    assert result["changed_files"] == ["a.txt", "b.txt"]
    assert not (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()
    assert histories[0] is None
    assert "failed goal check" in histories[1] and "missing: b.txt" in histories[1]
    assert "step-a" in histories[1] and len(histories[1]) <= 2600
    first_run = result["iterations"][0]["run_id"]
    assert not (tmp_path / ".ego/state/runs" / first_run / "staging").exists()
    checkpoint = json.loads((tmp_path / ".ego/memory/handoffs" / (result["loop_id"] + ".json")).read_text())
    assert checkpoint["status"] == "VERIFIED"


def test_loop_applies_only_with_approval_and_only_after_the_goal_holds(tmp_path: Path):
    _loop_project(tmp_path)
    with pytest.raises(EgoError):
        run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=lambda *a: {}, apply=True)

    def planner(goal, history, workdir):
        return {"goal": goal, "nodes": [_node("make_a.py", "a.txt") if history is None else _node("make_b.py", "b.txt")]}

    result = run_loop(tmp_path, "produce a and b", done_checks=[["python3", "check.py"]], planner=planner,
                      scope=["*.txt"], apply=True, approve=True)
    assert result["status"] == "COMPLETED" and "完了" in result["message"]
    assert (tmp_path / "a.txt").read_text() == "A" and (tmp_path / "b.txt").read_text() == "B"


def test_loop_stops_when_an_iteration_makes_no_progress(tmp_path: Path):
    _loop_project(tmp_path)
    calls = []

    def planner(goal, history, workdir):
        calls.append(1)
        return {"goal": goal, "nodes": [{"id": "noop", "kind": "code", "argv": ["python3", "noop.py"], "write_allowlist": ["x.txt"]}]}

    result = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=planner, max_iterations=6)
    assert result["status"] == "STALLED" and len(calls) == 2
    assert "未完了" in result["message"]


def test_loop_respects_iteration_and_budget_limits(tmp_path: Path):
    _loop_project(tmp_path)
    count = {"n": 0}

    def planner(goal, history, workdir):
        count["n"] += 1
        (tmp_path / f"w{count['n']}.py").write_text(f"from pathlib import Path\nPath('p{count['n']}.txt').write_text('x')\n")
        return {"task": {"goal": goal, "nodes": [{"id": f"w{count['n']}", "kind": "code", "argv": ["python3", f"w{count['n']}.py"],
                                                 "write_allowlist": ["*.txt"]}]},
                "planner_usage": {"cost_usd": 0.6}}

    capped = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=planner, max_iterations=2)
    assert capped["status"] == "MAX_ITERATIONS" and len(capped["iterations"]) == 2 and capped["cost_usd"] == 1.2
    count["n"] = 0
    broke = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=planner, max_iterations=9, max_budget_usd=1.0)
    assert broke["status"] == "BUDGET_EXHAUSTED" and len(broke["iterations"]) == 2


def test_loop_requires_a_measurable_allowed_check(tmp_path: Path):
    with pytest.raises(EgoError) as missing:
        run_loop(tmp_path, "g", done_checks=[], planner=lambda *a: {})
    assert missing.value.code == "DONE_CHECK_REQUIRED"
    with pytest.raises(EgoError) as denied:
        run_loop(tmp_path, "g", done_checks=[["bash", "-c", "true"]], planner=lambda *a: {})
    assert denied.value.code == "COMMAND_DENIED"


def test_rejected_plan_is_an_iteration_outcome_and_history_masks_old_iterations(tmp_path: Path):
    _loop_project(tmp_path)

    def planner(goal, history, workdir):
        return {"goal": goal, "nodes": [{"id": DONE_NODE, "kind": "code", "argv": ["python3", "noop.py"], "write_allowlist": ["x"]}]}

    result = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=planner, max_iterations=5)
    assert result["status"] == "STALLED" and result["iterations"][0]["status"] == "PLAN_REJECTED"
    iterations = [{"n": i, "status": "FAILED", "passed": ["n"], "changed_files": ["f.py"],
                   "failures": [{"node": "impl", "feedback": f"detail-{i} " + "y" * 3000}]} for i in range(1, 6)]
    text = history_text(iterations)
    assert len(text) <= 2600
    assert "Iteration 1: FAILED; failed impl" in text and "detail-1" not in text
    assert "detail-5" in text or "y" * 50 in text


def test_loop_cli_roundtrip_with_mock_planner(tmp_path: Path, capsys):
    _loop_project(tmp_path)
    (tmp_path / "ok.py").write_text("print('ok')\n")
    code = main(["work", "loop", "--root", str(tmp_path), "--goal", "nothing to change", "--done-check", "python3 ok.py"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "VERIFIED" and out["done_checks"] == [["python3", "ok.py"]]
    assert main(["work", "loop-status", out["loop_id"], "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"
    assert main(["work", "cancel", out["loop_id"], "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["loop_id"] == out["loop_id"]


def test_verified_loop_is_applied_later_only_with_approval(tmp_path: Path, capsys):
    _loop_project(tmp_path)

    def planner(goal, history, workdir):
        return {"goal": goal, "nodes": [_node("make_a.py", "a.txt") if history is None else _node("make_b.py", "b.txt")]}

    result = run_loop(tmp_path, "produce a and b", done_checks=[["python3", "check.py"]], planner=planner, scope=["*.txt"])
    assert result["status"] == "VERIFIED" and not (tmp_path / "a.txt").exists()
    assert main(["work", "loop-apply", result["loop_id"], "--root", str(tmp_path)]) != 0
    capsys.readouterr()
    assert not (tmp_path / "a.txt").exists()
    assert main(["work", "loop-apply", result["loop_id"], "--root", str(tmp_path), "--approve"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "COMPLETED"
    assert (tmp_path / "a.txt").read_text() == "A" and (tmp_path / "b.txt").read_text() == "B"


def test_stale_target_blocks_a_later_loop_apply(tmp_path: Path):
    from ego.loop import apply_loop
    _loop_project(tmp_path)
    (tmp_path / "a.txt").write_text("old")
    (tmp_path / "over.py").write_text("from pathlib import Path\nPath('a.txt').write_text('A')\nPath('b.txt').write_text('B')\n")
    planner = lambda goal, history, workdir: {"goal": goal, "nodes": [
        {"id": "w", "kind": "code", "argv": ["python3", "over.py"], "write_allowlist": ["*.txt"]}]}
    result = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=planner, scope=["*.txt"])
    assert result["status"] == "VERIFIED"
    (tmp_path / "a.txt").write_text("edited by the user meanwhile")
    with pytest.raises(EgoError) as stale:
        apply_loop(tmp_path, result["loop_id"], approve=True)
    assert stale.value.code == "STALE_BASE"
    assert (tmp_path / "a.txt").read_text() == "edited by the user meanwhile"


def test_planner_uses_the_configured_host_model(tmp_path: Path, monkeypatch):
    from ego import planner
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text('models = {claude = {model = "haiku", effort = "low"}}\n')
    seen = {}

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        seen.update(kw)
        spec = {"goal": "g", "nodes": [{"id": "impl", "kind": "agent", "prompt": "p", "write_allowlist": ["src/**"]}]}
        return RunResult("PASS", json.dumps(spec), "", 0, 0.0, host, cost_usd=0.02)

    monkeypatch.setattr(planner, "run_agent", fake_agent)
    result = planner.plan_work(tmp_path, "add validation", "claude", execute=True, history="Iteration 1: FAILED")
    assert seen["model"] == "haiku" and seen["effort"] == "low" and seen["read_only"] is True
    assert result["planner_usage"]["cost_usd"] == 0.02


def test_first_iteration_skips_the_planner_and_replans_only_after_failure(tmp_path: Path, monkeypatch):
    from ego import planner as planner_module
    _loop_project(tmp_path)
    (tmp_path / "both.py").write_text("from pathlib import Path\nPath('a.txt').write_text('A')\nPath('b.txt').write_text('B')\n")
    worker_prompts, plans = [], []

    def idle_worker(host, prompt, cwd, timeout=180, **kw):
        worker_prompts.append(prompt)
        return RunResult("PASS", "done", "", 0, 0.0, host, cost_usd=0.01)

    def fake_plan(root, goal, host, scope, *, execute, history, workdir):
        plans.append(history)
        return {"task": {"goal": goal, "nodes": [{"id": "both", "kind": "code", "argv": ["python3", "both.py"],
                                                  "write_allowlist": ["*.txt"]}]}, "planner_usage": {"cost_usd": 0.07}}

    monkeypatch.setattr(work, "run_agent", idle_worker)
    monkeypatch.setattr(planner_module, "plan_work", fake_plan)
    result = run_loop(tmp_path, "produce a and b", done_checks=[["python3", "check.py"]], host="claude",
                      scope=["*.txt"], trust_cli=True, execute_planner=True)
    assert result["status"] == "VERIFIED"
    assert [it["status"] for it in result["iterations"]] == ["FAILED", "PASSED"]
    assert len(plans) == 1 and "missing: a.txt, b.txt" in plans[0]
    assert "missing: a.txt, b.txt" in worker_prompts[1]  # in-node repair saw the done-check output
    assert result["cost_usd"] == round(0.01 * len(worker_prompts) + 0.07, 4)

    plans.clear()
    run_loop(tmp_path, "produce a and b", done_checks=[["python3", "check.py"]], host="claude",
             scope=["*.txt"], trust_cli=True, execute_planner=True, plan_first=True)
    assert plans[0] is None


def test_plan_with_a_forbidden_check_is_rejected_and_replanned_not_a_crash(tmp_path: Path):
    """Seen on a real host: the replanner proposed a `python3 -c` check and the loop died with ERROR."""
    _loop_project(tmp_path)
    histories = []

    def planner(goal, history, workdir):
        histories.append(history)
        return {"goal": goal, "nodes": [{"id": "impl", "kind": "agent", "prompt": "p", "write_allowlist": ["src/**"],
                                         "checks": [["python3", "-c", "import x"]]}]}

    result = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=planner, max_iterations=5)
    assert result["status"] == "STALLED"
    assert [it["status"] for it in result["iterations"]] == ["PLAN_REJECTED", "PLAN_REJECTED"]
    assert "Inline Python" in histories[1]
    assert not (tmp_path / ".ego" / "state" / "runs").exists()  # rejected before any staging copy


def test_approved_task_is_the_first_iteration_and_replanning_follows_only_on_failure(tmp_path: Path):
    _loop_project(tmp_path)
    calls = []

    def planner(goal, history, workdir):
        calls.append(history)
        return {"goal": goal, "nodes": [_node("make_b.py", "b.txt")]}

    approved = {"goal": "produce a and b", "nodes": [_node("make_a.py", "a.txt")],
                "planner_usage": {"cost_usd": 0.05}}
    result = run_loop(tmp_path, "produce a and b", done_checks=[["python3", "check.py"]], planner=planner,
                      scope=["*.txt"], task=approved, max_iterations=3)
    assert result["status"] == "VERIFIED"
    assert [it["status"] for it in result["iterations"]] == ["FAILED", "PASSED"]
    assert len(calls) == 1 and "missing: b.txt" in calls[0]  # the planner never replaced the approved first plan
    assert result["usage"]["planned"] and result["cost_usd"] >= 0.05
    ledger = json.loads((tmp_path / result["ledger_path"]).read_text())
    assert ledger["seeded_from_task"] is True


def test_approved_task_is_validated_and_excludes_plan_first(tmp_path: Path):
    _loop_project(tmp_path)
    with pytest.raises(EgoError) as broken:
        run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=lambda *a: {},
                 task={"goal": "g", "nodes": [{"id": "x", "kind": "code", "argv": ["python3", "noop.py"], "depends_on": ["y"],
                                                "write_allowlist": ["*.txt"]}]})
    assert broken.value.code == "INVALID_TASK"
    with pytest.raises(EgoError):
        run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=lambda *a: {},
                 task={"goal": "g", "nodes": [{"id": "x", "kind": "code", "argv": ["bash", "-c", "true"],
                                                "write_allowlist": ["*.txt"]}]})
    assert not (tmp_path / ".ego/state/loops").exists()
    with pytest.raises(EgoError) as both:
        run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], planner=lambda *a: {},
                 task={"goal": "g", "nodes": [_node("make_a.py", "a.txt")]}, plan_first=True)
    assert both.value.code == "INVALID_LOOP"


def test_loop_cli_runs_an_approved_task_file_with_its_goal(tmp_path: Path, capsys):
    _loop_project(tmp_path)
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"goal": "produce a and b", "nodes": [
        _node("make_a.py", "a.txt"), {**_node("make_b.py", "b.txt"), "depends_on": ["step-a"]}]}))
    code = main(["work", "loop", "--root", str(tmp_path), "--task", str(task_file),
                 "--done-check", "python3 check.py", "--scope", "*.txt"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "VERIFIED" and out["goal"] == "produce a and b"
    assert len(out["iterations"]) == 1
    assert main(["work", "loop", "--root", str(tmp_path), "--done-check", "python3 check.py"]) != 0
