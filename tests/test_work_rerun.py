"""Rerunning only what failed: independent nodes finish, `--from` carries verified nodes, resume carries failure feedback."""
import json
import re
import subprocess
from pathlib import Path

from ego import work
from ego.cli import main
from ego.runner import RunResult
from ego.work import execute


def _agent_driver(monkeypatch) -> list[tuple[str, str]]:
    """Fake worker. `WRITE f=v` writes f; `FIXED f=v` writes v instead when the prompt carries feedback `want-v`;
    a judge approves only when good.txt says `good`. Returns (node id, prompt) per run."""
    ran: list[tuple[str, str]] = []

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        nid = re.search(r"NODE (\S+)", prompt).group(1)
        ran.append((nid, prompt))
        for name, text in re.findall(r"WRITE (\S+?)=(\S+)", prompt):
            (Path(cwd) / name).write_text(text)
        for name, text in re.findall(r"FIXED (\S+?)=(\S+)", prompt):
            (Path(cwd) / name).write_text(text if f"want-{text}" in prompt else "bad")
        output = "ok"
        if nid == "judge":
            good = (Path(cwd) / "good.txt").read_text() == "good"
            output = "VERDICT: APPROVE" if good else "- good.txt:1: want-good\nVERDICT: REJECT"
        return RunResult("PASS", output, "", 0, 0.0, host, cost_usd=0.0)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    return ran


def _agent(nid: str, prompt: str, deps=(), checks=None, **extra) -> dict:
    node = {"id": nid, "kind": "agent", "prompt": f"NODE {nid} {prompt}", "write_allowlist": ["*.txt"],
            "depends_on": list(deps), "max_attempts": 1, **extra}
    if checks:
        node["checks"] = checks
    return node


def _scripts(root: Path) -> None:
    (root / "check_x.py").write_text("from pathlib import Path\nimport sys\n"
                                     "v=Path('x.txt').read_text()\nprint('x.txt is '+v+', want-ok')\nsys.exit(v!='ok')\n")
    (root / "noop.py").write_text("print('ok')\n")


def test_failure_stops_only_its_dependents_and_independent_nodes_finish(tmp_path: Path, monkeypatch):
    ran = _agent_driver(monkeypatch)
    _scripts(tmp_path)
    task = {"goal": "g", "nodes": [_agent("x", "WRITE x.txt=bad", checks=[["python3", "check_x.py"]]),
                                   _agent("after-x", "WRITE y.txt=Y", ["x"]),
                                   _agent("free", "WRITE z.txt=Z")]}
    result = execute(tmp_path, task, extra_allow=["*.py"])
    assert result["final_status"] == "FAILED"
    assert result["node_status"] == {"x": "FAILED", "free": "PASSED"}
    assert sorted(n for n, _ in ran) == ["free", "x"]


def test_cli_from_reruns_only_the_corrected_node(tmp_path: Path, monkeypatch, capsys):
    ran = _agent_driver(monkeypatch)
    _scripts(tmp_path)
    task = {"goal": "g", "nodes": [_agent("x", "WRITE x.txt=bad", checks=[["python3", "check_x.py"]]),
                                   _agent("after-x", "WRITE y.txt=Y", ["x"]),
                                   _agent("free", "WRITE z.txt=Z")]}
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task))
    assert main(["work", "run", "--root", str(tmp_path), "--task", str(path), "--run-id", "first"]) == 5
    task["nodes"][0] = _agent("x", "WRITE x.txt=ok", checks=[["python3", "check_x.py"]])
    path.write_text(json.dumps(task))
    capsys.readouterr()
    assert main(["work", "run", "--root", str(tmp_path), "--task", str(path), "--run-id", "second", "--from", "first"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["final_status"] == "PASSED"
    assert [n for n, _ in ran][2:] == ["x", "after-x"]  # `free` was carried, not run again
    record = json.loads((tmp_path / ".ego/state/runs/second/record.json").read_text())
    assert record["carried"] == ["free"] and record["changed_files"] == ["x.txt", "y.txt", "z.txt"]


def test_resume_reruns_a_failed_node_with_its_last_failure_as_feedback(tmp_path: Path, monkeypatch):
    ran = _agent_driver(monkeypatch)
    _scripts(tmp_path)
    task = {"goal": "g", "nodes": [_agent("x", "FIXED x.txt=ok", checks=[["python3", "check_x.py"]]),
                                   _agent("free", "WRITE z.txt=Z")]}
    first = execute(tmp_path, task, extra_allow=["*.py"], run_id="r1")
    assert first["final_status"] == "FAILED"
    second = execute(tmp_path, task, run_id="r1", resume=True)
    assert second["final_status"] == "PASSED"
    assert [n for n, _ in ran][2:] == ["x"]
    assert "x.txt is bad, want-ok" in ran[-1][1]


def test_resume_after_a_judge_reject_sends_the_findings_to_the_reviewed_coder(tmp_path: Path, monkeypatch):
    ran = _agent_driver(monkeypatch)
    _scripts(tmp_path)
    task = {"goal": "g", "nodes": [
        _agent("code", "FIXED good.txt=good", role="coder", host="mock"),
        {"id": "gate", "kind": "gate", "depends_on": ["code"], "argv": ["python3", "noop.py"]},
        _agent("judge", "review", ["gate"], role="judge", reviews="code", max_retries=0, host="mock",
               write_allowlist=[])]}
    first = execute(tmp_path, task, extra_allow=["*.py"], run_id="j1")
    assert first["final_status"] == "FAILED" and first["node_status"]["judge"] == "FAILED"
    second = execute(tmp_path, task, run_id="j1", resume=True)
    assert second["final_status"] == "PASSED"
    assert [n for n, _ in ran] == ["code", "judge", "code", "judge"]
    assert "want-good" in ran[2][1] and "want-good" not in ran[0][1]


def test_failed_worktree_run_keeps_its_staging_for_a_rerun(tmp_path: Path, monkeypatch):
    _agent_driver(monkeypatch)
    root = tmp_path / "repo"
    root.mkdir()
    _scripts(root)
    for argv in (["git", "init"], ["git", "config", "user.name", "T"], ["git", "config", "user.email", "t@example.com"],
                 ["git", "add", "."], ["git", "commit", "-m", "init"]):
        subprocess.run(argv, cwd=root, capture_output=True, check=True)
    task = {"goal": "g", "nodes": [_agent("x", "WRITE x.txt=bad", checks=[["python3", "check_x.py"]])]}
    result = execute(root, task, worktree=True, cleanup_worktree=True, run_id="w1")
    assert result["final_status"] == "FAILED" and not result.get("worktree_cleaned")
    assert (root / ".ego/state/runs/w1/staging").is_dir()
    subprocess.run(["git", "worktree", "remove", "--force", str(root / ".ego/state/runs/w1/staging")],
                   cwd=root, capture_output=True)
