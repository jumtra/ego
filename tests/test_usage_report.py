"""Per-phase token report: plan, work and per-task numbers, printed automatically by the CLI and the loop."""
import json
from pathlib import Path

from ego import planner
from ego.cli import main
from ego.loop import run_loop
from ego.runner import RunResult
from ego.usage import fmt_tokens, summary_line, tokens_of, usage_text
from ego.work import compact_result, usage_summary


def _attempt(host, usage, cost=None):
    return {"execution": {"host": host, "cost_usd": cost, "usage": usage}}


def test_tokens_include_cache_traffic_and_format_compactly():
    assert tokens_of({"input_tokens": 40, "output_tokens": 300, "cache_read_tokens": 52000, "cache_creation_tokens": 9000}) == 61340
    assert tokens_of(None) == 0
    assert [fmt_tokens(n) for n in (0, 999, 1200, 45600, 2_500_000)] == ["0", "999", "1.2k", "45.6k", "2.50M"]


def test_usage_text_shows_only_the_phases_that_ran():
    assert usage_text({"plan_tokens": 1200}) == "plan 1.2k tokens"
    assert usage_text({"work_tokens": 0, "by_task": {}}) == "work 0 tokens"
    line = usage_text({"plan_tokens": 1200, "work_tokens": 45600, "by_task": {"A": 8100, "B": 2000}})
    assert line == "plan 1.2k tokens · work 45.6k tokens (task A 8.1k, task B 2.0k) · total 46.8k tokens"
    assert usage_text({}) == "tokens: no model calls"


def test_run_usage_splits_plan_and_each_task_and_counts_the_planner_cost():
    record = {"planner_usage": {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.01},
              "results": {
                  "A": {"attempts": [_attempt("claude", {"input_tokens": 5, "output_tokens": 7, "cache_read_tokens": 100}, 0.1),
                                     _attempt("claude", {"input_tokens": 1, "output_tokens": 2}, 0.05)]},
                  "B": {"attempts": [_attempt("local", None)]},
                  "C": {"attempts": [_attempt("claude", {"input_tokens": 10, "output_tokens": 20}, 0.0)]}}}
    total = usage_summary(record)
    assert total["planned"] is True and total["plan_tokens"] == 1200
    assert total["by_task"] == {"A": 112 + 3, "C": 30} and total["work_tokens"] == 145
    assert total["model_calls"] == 4 and total["cost_usd"] == 0.16 and total["cost_known"] is True
    assert summary_line(total) == "plan 1.2k tokens · work 145 tokens (task A 115, task C 30) · total 1.3k tokens"


def test_run_without_planner_or_model_calls_reports_zero_work_and_no_plan():
    assert summary_line(usage_summary({"results": {"gen": {"attempts": [_attempt("local", None)]}}})) == "work 0 tokens"


def test_compact_result_prints_the_line_next_to_the_totals():
    result = {"run_id": "run-1", "final_status": "PASSED", "node_status": {"A": "PASSED"}, "changed_files": [],
              "results": {}, "planner_usage": {"input_tokens": 1000, "output_tokens": 200},
              "usage": usage_summary({"planner_usage": {"input_tokens": 1000, "output_tokens": 200},
                                      "results": {"A": {"attempts": [_attempt("claude", {"input_tokens": 9000})]}}})}
    out = compact_result(result)
    assert out["usage_text"] == "plan 1.2k tokens · work 9.0k tokens (task A 9.0k) · total 10.2k tokens"


def test_cli_run_reports_the_plan_phase_recorded_in_the_task_file(tmp_path: Path, capsys):
    (tmp_path / "gen.py").write_text("from pathlib import Path\nPath('result.txt').write_text('OK')\n")
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"goal": "g", "planner_usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 300},
                                "nodes": [{"id": "gen", "kind": "code", "argv": ["python3", "gen.py"],
                                           "write_allowlist": ["result.txt"]}]}))
    assert main(["work", "run", "--root", str(tmp_path), "--task", str(task)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["usage_text"] == "plan 1.5k tokens · work 0 tokens · total 1.5k tokens"


def test_cli_plan_writes_planning_cost_into_the_task_file(tmp_path: Path, monkeypatch, capsys):
    spec = {"goal": "g", "nodes": [{"id": "impl", "kind": "agent", "prompt": "p", "write_allowlist": ["src/**"]}]}

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        return RunResult("PASS", json.dumps(spec), "", 0, 0.0, host,
                         usage={"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 300})

    monkeypatch.setattr(planner, "run_agent", fake_agent)
    out_file = tmp_path / "task.json"
    assert main(["work", "plan", "--root", str(tmp_path), "--goal", "g", "--host", "claude", "--execute", "--out", str(out_file)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["usage_text"] == "plan 1.5k tokens" and printed["planner_usage"]["input_tokens"] == 1000
    assert json.loads(out_file.read_text())["planner_usage"]["output_tokens"] == 200


def test_loop_ledger_totals_plan_and_work_tokens_across_iterations(tmp_path: Path):
    (tmp_path / "make_all.py").write_text("from pathlib import Path\nPath('a.txt').write_text('A')\n")
    (tmp_path / "check.py").write_text("from pathlib import Path\nimport sys\nsys.exit(0 if Path('a.txt').exists() else 1)\n")
    calls = []

    def planner_stub(goal, history, workdir):
        calls.append(history)
        return {"task": {"goal": goal, "nodes": [{"id": "make", "kind": "code", "argv": ["python3", "make_all.py"],
                                                  "write_allowlist": ["*.txt"]}]},
                "planner_usage": {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.01}}

    result = run_loop(tmp_path, "make a", done_checks=[["python3", "check.py"]], planner=planner_stub,
                      scope=["*.txt"], plan_first=True)
    assert result["status"] == "VERIFIED" and len(calls) == 1
    assert result["usage"]["plan_tokens"] == 1200 and result["usage"]["planned"] is True
    assert result["usage_text"] == "plan 1.2k tokens · work 0 tokens · total 1.2k tokens"


def test_resume_of_a_run_whose_task_file_carries_plan_usage_does_not_mismatch(tmp_path: Path, capsys):
    (tmp_path / "gen.py").write_text("from pathlib import Path\nPath('result.txt').write_text('OK')\n")
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"goal": "g", "planner_usage": {"input_tokens": 10},
                                "nodes": [{"id": "gen", "kind": "code", "argv": ["python3", "gen.py"],
                                           "write_allowlist": ["result.txt"]}]}))
    assert main(["work", "run", "--root", str(tmp_path), "--task", str(task)]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert main(["work", "resume", run_id, "--root", str(tmp_path), "--task", str(task)]) == 0
    assert json.loads(capsys.readouterr().out)["final_status"] == "PASSED"
