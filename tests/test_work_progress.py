"""Progress journal, `ego work watch`, and the richer compact summary of a managed run."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from ego import work
from ego.runner import RunResult
from ego.session import load_checkpoint
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


def test_parallel_wave_persists_a_passed_node_before_its_failing_sibling_ends(project, monkeypatch):
    """Node a passes, node b fails in the same wave. b waits until a is on disk (staging, record.json and the
    managed checkpoint); that proves a was saved as it finished, not at the end of the wave."""
    seen = {}

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        nid = re.search(r"NODE (\S+)", prompt).group(1)
        if nid == "a":
            (Path(cwd) / "a.txt").write_text("A")
        else:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    record = json.loads((project / ".ego/state/runs/w1/record.json").read_text())
                except (OSError, ValueError):
                    record = {}
                handoff = load_checkpoint(project, "w1")
                if record.get("node_status", {}).get("a") == "PASSED" and handoff and "a (PASSED)" in handoff["completed"]:
                    seen["a_saved_during_wave"] = True
                    break
                time.sleep(0.01)
        if nid == "a":
            return RunResult("PASS", "ok", "", 0, 0.0, host, cost_usd=0.0)
        return RunResult("FAIL", "boom", "", 1, 0.0, host, cost_usd=0.0)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    spec = {"goal": "g", "nodes": [
        {"id": "a", "prompt": "NODE a", "write_allowlist": ["*.txt"], "max_attempts": 1},
        {"id": "b", "prompt": "NODE b", "write_allowlist": ["*.txt"], "max_attempts": 1}]}
    result = execute(project, spec, run_id="w1")
    assert result["final_status"] == "FAILED"
    assert result["node_status"] == {"a": "PASSED", "b": "FAILED"}
    assert seen.get("a_saved_during_wave") is True, "node a was not persisted before node b finished"
    assert (project / ".ego/state/runs/w1/staging/a.txt").read_text() == "A"
    record = json.loads((project / ".ego/state/runs/w1/record.json").read_text())
    assert record["node_status"]["a"] == "PASSED"
    assert "a (PASSED)" in load_checkpoint(project, "w1")["completed"]
