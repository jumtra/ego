"""Progress journal, `ego work watch`, and the richer compact summary of a managed run."""
import json
import subprocess
import sys
from ego.work import compact_result, execute, status, watch


def task():
    return {"goal": "g", "nodes": [
        {"id": "a", "prompt": "p", "write_allowlist": ["src/*"]},
        {"id": "b", "prompt": "p", "write_allowlist": ["src/*"], "depends_on": ["a"]}]}


def events(root, run_id):
    path = root / ".ego/state/runs" / run_id / "progress.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_events_are_ordered_and_end_with_run_finished(project):
    execute(project, task(), run_id="r1")
    evs = events(project, "r1")
    names = [e["event"] for e in evs]
    assert names[0] == "run_started" and names[-1] == "run_finished"
    assert names.index("node_started") < names.index("node_finished")
    assert names.count("node_finished") == 2 and "attempt_finished" in names
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    assert evs[-1]["final_status"] == "PASSED"


def test_compact_result_has_node_checks_and_next_action(project):
    t = task()
    t["nodes"][0]["checks"] = [["pytest", "--version"]]
    result = execute(project, t, run_id="r2")
    out = compact_result(result, task_file="plan.json")
    assert out["nodes"]["a"]["checks"][0]["argv"] == ["pytest", "--version"]
    assert out["nodes"]["a"]["checks"][0]["status"] == "PASS"
    assert "plan.json" in out["next_action"] and "--apply --approve" in out["next_action"]
    assert "next_action" not in compact_result({**result, "applied": True})


def test_watch_replays_and_honours_after(project):
    execute(project, task(), run_id="r3")
    lines = []
    last = watch(project, "r3", write=lines.append, poll=0.01)
    total = len(events(project, "r3"))
    assert len(lines) == total and last == total
    lines2 = []
    assert watch(project, "r3", after=2, write=lines2.append, poll=0.01) == total
    assert len(lines2) == total - 2


def test_status_reports_progress_tail(project):
    execute(project, task(), run_id="r4")
    tail = status(project, "r4")["progress"]
    assert tail and tail[-1]["event"] == "run_finished"


def test_cli_run_with_run_id_echoes_events_to_stderr(project):
    (project / "t.json").write_text(json.dumps(task()))
    p = subprocess.run([sys.executable, "-m", "ego", "work", "run", "--root", str(project), "--task", str(project / "t.json"),
                        "--run-id", "r5"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert (project / ".ego/state/runs/r5/record.json").exists()
    assert '"event": "run_started"' in p.stderr
    assert json.loads(p.stdout)["nodes"]["a"]["status"] == "PASSED"
