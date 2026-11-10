from pathlib import Path
import json
import pytest
from ego.work import execute,validate_task,status
from ego.evals import run_eval,compare,load_suite
from ego.util import SEHError

def test_code_workflow_staged_apply(project):
    (project/"generate.py").write_text("from pathlib import Path\nPath('output.txt').write_text('ok')\n")
    (project/"check.py").write_text("from pathlib import Path\nassert Path('output.txt').read_text() == 'ok'\n")
    task={"nodes":[{"id":"write","kind":"code","argv":["python3","generate.py"],"write_allowlist":["output.txt"],"checks":[["python3","check.py"]]},
                   {"id":"gate","kind":"gate","depends_on":["write"],"argv":["python3","check.py"]}]}
    result=execute(project,task)
    assert result["final_status"]=="PASSED"
    assert not (project/"output.txt").exists()
    assert status(project,result["run_id"])["final_status"]=="PASSED"
    with pytest.raises(SEHError):execute(project,task,apply=True)
    applied=execute(project,task,apply=True,approve=True)
    assert applied["applied"]
    assert (project/"output.txt").read_text()=="ok"

def test_workflow_scope_rejected(project):
    (project/"write.py").write_text("from pathlib import Path\nPath('unrelated.txt').write_text('bad')\n")
    task={"nodes":[{"id":"write","kind":"code","argv":["python3","write.py"],"write_allowlist":["allowed.txt"],"max_attempts":1}]}
    result=execute(project,task)
    assert result["final_status"]=="FAILED"
    assert not (project/"unrelated.txt").exists()

def test_workflow_cycles(project):
    with pytest.raises(SEHError):validate_task({"nodes":[{"id":"a","kind":"agent","prompt":"x","write_allowlist":["a"],"depends_on":["b"]},{"id":"b","kind":"agent","prompt":"x","write_allowlist":["b"],"depends_on":["a"]}]})

def test_evaluation(project):
    path=project/"suite.json"
    path.write_text(json.dumps({"cases":[
        {"id":"yes","prompt":"[[MOCK:success]]","split":"training","checks":{"contains":["success"]}},
        {"id":"neg","prompt":"[[MOCK:OK]]","split":"security","checks":{"not_contains":["secret"]}},
        {"id":"hold","prompt":"[[MOCK:done]]","split":"holdout","checks":{"contains":["done"]}},
        {"id":"reg","prompt":"[[MOCK:test]]","split":"regression","checks":{"regex":["test"]}}
    ]}),encoding="utf8")
    result=run_eval(project,path,host="mock",trials=2)
    assert result["all_passed"]
    assert compare(result,result)["comparison"]=="NO_GAIN"
    assert load_suite(path)
    assert run_eval(project,path,host="mock",split="training")["all_passed"]

def test_eval_regression(project):
    path=project/"suite.json"
    path.write_text(json.dumps([{"id":"x","prompt":"[[MOCK:hello]]","checks":{"contains":["hello"]}}]))
    baseline=run_eval(project,path,host="mock",save=False)
    other=run_eval(project,path,host="mock",prompt_prefix="Preamble",save=False)
    assert compare(baseline,other)["comparison"]=="NO_GAIN"
    other["summary"]["x"]["passes"]=0
    assert compare(baseline,other)["comparison"]=="REGRESSION"


def test_worktree_isolation_and_cleanup(tmp_path):
    import subprocess
    root = tmp_path / "git_project"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, capture_output=True, check=True)
    (root / "hello.txt").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
    apply_install_to_root = True

    from ego.install import apply_install
    from ego.config import Config
    apply_install(root, Config())
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add ego"], cwd=root, capture_output=True, check=True)

    (root / "write_script.py").write_text("from pathlib import Path\nPath('result.txt').write_text('done')\n")
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add script"], cwd=root, capture_output=True, check=True)

    task = {"nodes": [{"id": "write", "kind": "code", "argv": ["python3", "write_script.py"], "write_allowlist": ["result.txt"]}]}

    # 1. worktree with cleanup (default)
    res_clean = execute(root, task, worktree=True, cleanup_worktree=True)
    assert res_clean["final_status"] == "PASSED"
    assert res_clean["worktree"] is True
    assert res_clean["worktree_cleaned"] is True
    staging_path = root / ".ego" / "state" / "runs" / res_clean["run_id"] / "staging"
    assert not staging_path.exists()
    assert not (root / "result.txt").exists()

    # 2. worktree without cleanup (keep_worktree)
    res_keep = execute(root, task, worktree=True, cleanup_worktree=False)
    assert res_keep["final_status"] == "PASSED"
    assert res_keep["worktree"] is True
    assert res_keep.get("worktree_cleaned") is not True
    staging_keep = root / ".ego" / "state" / "runs" / res_keep["run_id"] / "staging"
    assert staging_keep.exists()
    assert (staging_keep / "result.txt").read_text() == "done"
    assert not (root / "result.txt").exists()


def _git_project(root):
    import subprocess
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / "gen.py").write_text("from pathlib import Path\nPath('result.txt').write_text('done')\n")
    for argv in (["git", "init", "-q"], ["git", "add", "."],
                 ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", "init"]):
        subprocess.run(argv, cwd=root, capture_output=True, check=True)
    return root


def test_worktree_run_measures_changes_against_head_not_the_dirty_working_tree(tmp_path):
    root = _git_project(tmp_path / "repo")
    (root / "src" / "a.py").write_text("x = 2  # uncommitted\n")
    (root / "untracked.txt").write_text("keep me\n")
    task = {"nodes": [{"id": "write", "kind": "code", "argv": ["python3", "gen.py"], "write_allowlist": ["result.txt", "src/**"]}]}
    result = execute(root, task, worktree=True, apply=True, approve=True)
    assert result["final_status"] == "PASSED" and result["applied"]
    assert result["changed_files"] == ["result.txt"]
    assert (root / "result.txt").read_text() == "done"
    assert (root / "src" / "a.py").read_text() == "x = 2  # uncommitted\n"
    assert (root / "untracked.txt").read_text() == "keep me\n"


def test_resume_after_cancel_continues_the_run(tmp_path):
    from ego.work import cancel
    (tmp_path / "check.py").write_text("from pathlib import Path\nimport sys\nsys.exit(0 if Path('ready').exists() else 1)\n")
    task = {"nodes": [{"id": "gate", "kind": "gate", "argv": ["python3", "check.py"]}]}
    first = execute(tmp_path, task)
    assert first["final_status"] == "FAILED"
    cancel(tmp_path, first["run_id"])
    (tmp_path / ".ego" / "state" / "runs" / first["run_id"] / "staging" / "ready").write_text("")
    resumed = execute(tmp_path, task, run_id=first["run_id"], resume=True)
    assert resumed["final_status"] != "CANCELLED"
