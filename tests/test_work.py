from dataclasses import replace
"""Work flows: run/resume/--from rerun, watch/cancel, parallel staging, worktree isolation, reviewer gate, worker rotation, loop/planner."""
from pathlib import Path
import json
import os
import pytest
import re
import subprocess
import sys
import time

from ego.adapters import hosts
from ego.adapters.codeintel import text_fallback
from ego.adapters.runner import RunResult
from ego.adapters.runner import RunResult, parse_host_usage
from ego.cli import main
from ego.domain.config import default_config
from ego.domain.config import default_config, load_config
from ego.domain.config import default_config, load_config, save_config
from ego.domain.review import parse_verdict
from ego.domain.policy import check_command, check_write
from ego.domain.task_routing import build_task_prompt, resolve_routes
from ego.domain.templates import skill_md
from ego.shared.util import EgoError
from ego.shared.util import EgoError, exclusive_lock
from ego.shared.util import EgoError, safe_path
from ego.shared.util import sha256, write_json
from ego.usecases import planner
from ego.usecases import work
from ego.usecases import worker
from ego.usecases.install import apply_install
from ego.usecases.session import load_checkpoint
from ego.usecases.session import save_checkpoint
from ego.usecases.work import compact_result, execute, status, watch
from ego.usecases.work import compact_result, execute, usage_summary
from ego.usecases.work import execute
from ego.usecases.work import execute, cancel
from ego.usecases.work import execute, validate_task
from ego.usecases.work import execute, validate_task, status
from ego.usecases.worker import run_worker


def test_work_entry_delegates_managed_execution_and_keeps_approvals():
    from ego.domain.templates import WORK_REFERENCES
    work = skill_md('work')
    managed = WORK_REFERENCES['managed']
    assert 'ego work plan' in work and 'references/managed.md' in work
    assert 'ego work plan' in managed
    assert 'ego work run' in managed
    assert 'ego work resume' in managed
    assert 'Do not recursively invoke /work' in managed
    assert 'Never infer approval' in work and 'Never infer approval' in managed
    assert 'ego memory search' in work
    assert 'ego memory add' in work
    assert 'seh ' not in work + ''.join(WORK_REFERENCES.values())


def test_work_plan_skill_is_graph_format(tmp_path: Path):
    plan = skill_md('work-plan')
    assert 'node graph' in plan and 'この計画で進めてよいですか' in plan
    for name in ('work', 'work-plan'):
        body = skill_md(name)
        assert 'implemented exactly as written' in body and 'drift in a different direction' in body
    apply_install(tmp_path, default_config())
    assert (tmp_path / '.claude/skills/work-plan/SKILL.md').read_text() == plan


def test_work_plan_nodes_match_managed_fields():
    plan = skill_md('work-plan')
    assert '**Non-goals / Assumptions / Provides:**' in plan
    assert '1-4 observable acceptance criteria' in plan
    assert "assumptions turns out false, stop and ask" in plan
    for field in ('`requirement`', '`steps`', '`intent`', '`done_when`', '`non_goals`', '`assumptions`', '`provides`'):
        assert field in plan


def test_approved_plan_is_handed_to_the_engine_not_replanned():
    from ego.domain.templates import WORK_REFERENCES
    plan = skill_md('work-plan')
    assert 'do not plan it again with `ego work plan`' in plan
    assert 'ego work run --root . --task' in plan and 'max_replans' in plan
    assert 'approved a `work-plan` graph' in WORK_REFERENCES['managed']


def test_work_plan_executes_through_managed_with_progress():
    from ego.domain.templates import WORK_REFERENCES
    plan = skill_md('work-plan')
    for needle in ('Do not edit project files yourself', 'ego work watch', '--run-id', 'next_action', 'Monitor'):
        assert needle in plan
    assert 'ego work watch <id>' in WORK_REFERENCES['managed']


def test_work_skills_forbid_external_planning():
    plan = skill_md('work-plan')
    assert 'Do not invoke external brainstorming or planning skills' in plan


def test_work_skills_finish_with_yes_no_pr_and_cleanup():
    work = skill_md('work')
    assert 'yes/no' in work and 'gh pr create' in work and 'git worktree remove' in work
    assert 'Finish' in skill_md('work-plan')
    from ego.domain.templates import WORK_REFERENCES
    assert 'Finish' in WORK_REFERENCES['managed']


def test_v0012_work_skill_refreshes():
    from ego.domain.templates import LEGACY_WORK_HASHES
    assert '645c9862f86b9ea4e7db50885ceee9bd10fd98de3225a96f822878b212cd5039' in LEGACY_WORK_HASHES
    assert sha256(skill_md('work')) not in LEGACY_WORK_HASHES


def test_owned_custom_work_skill_is_preserved(tmp_path: Path):
    cfg = default_config()
    apply_install(tmp_path, cfg)
    canonical = tmp_path / '.ego/spec/skills/work/SKILL.md'
    custom = canonical.read_text().replace('Choose the **smallest useful workflow**.', 'Custom workflow.')
    canonical.write_text(custom)
    import json
    ownership = json.loads((tmp_path / '.ego/ownership.json').read_text())
    ownership['files']['.ego/spec/skills/work/SKILL.md'] = sha256(custom)
    write_json(tmp_path / '.ego/ownership.json', ownership)
    apply_install(tmp_path, cfg)
    assert canonical.read_text() == custom


def test_managed_python_checks_ignore_nested_bytecode_caches(tmp_path: Path):
    from ego.usecases.work import execute
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/value.py').write_text('VALUE = 42\n')
    (tmp_path / 'check.py').write_text(
        'from src.value import VALUE\nfrom pathlib import Path\n'
        'assert VALUE == 42\nPath("result.txt").write_text("OK")\n'
    )
    result = execute(tmp_path, {'goal': 'Run Python validation', 'nodes': [
        {'id': 'validate', 'kind': 'code', 'argv': ['python3', 'check.py'],
         'write_allowlist': ['result.txt']}
    ]}, apply=True, approve=True)
    assert result['final_status'] == 'PASSED'
    assert result['changed_files'] == ['result.txt']
    assert (tmp_path / 'result.txt').read_text() == 'OK'


def test_planner_prompt_requires_requirement_and_steps(tmp_path: Path, monkeypatch):
    captured = {}
    spec = {'goal': 'g', 'nodes': [{
        'id': 'impl', 'kind': 'agent', 'intent': 'i', 'prompt': 'p',
        'requirement': {'who': 'w', 'what': 'w', 'when': 'w', 'where': 'w', 'why': 'w', 'how': 'w'},
        'steps': ['Edit src/ego/x.py function f: return 1'],
        'done_when': ['tests pass'], 'write_allowlist': ['src/**'],
    }]}

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        captured['prompt'] = prompt
        return RunResult('PASS', json.dumps(spec), '', 0, 0.0, host)

    monkeypatch.setattr(planner, 'run_agent', fake_agent)
    result = planner.plan_work(tmp_path, 'g', host='claude', execute=True)
    prompt = captured['prompt']
    assert 'requirement' in prompt and 'steps' in prompt
    assert "Never write 'consider'" in prompt
    assert 'shared contracts/types in an upstream node' in prompt
    assert result['task']['nodes'][0]['steps'] == ['Edit src/ego/x.py function f: return 1']


def test_custom_agent_discovery_task_routing_and_non_lean_alter(tmp_path: Path, monkeypatch):
    import pytest
    from ego.usecases import work
    from ego.domain.config import default_config, save_config
    from ego.adapters.detect import detect, discover_agents
    from ego.domain.task_routing import build_task_prompt, validate_node_notes
    from ego.shared.util import EgoError

    (tmp_path / '.claude/agents').mkdir(parents=True)
    (tmp_path / '.claude/agents/domain-expert.md').write_text(
        '---\nname: domain-expert\ndescription: Domain glossary specialist\n---\nAlways verify domain terms.\n',
        encoding='utf-8',
    )
    (tmp_path / '.codex/agents').mkdir(parents=True)
    (tmp_path / '.codex/agents/db-checker.toml').write_text(
        'description = "Database schema checker"\n',
        encoding='utf-8',
    )

    agents = discover_agents(tmp_path)
    assert [a['name'] for a in agents] == ['db-checker', 'domain-expert']
    assert detect(tmp_path)['custom_agents'] == ['db-checker', 'domain-expert']

    # validate_node_notes accepts valid agent/mcp on agent nodes and rejects invalid ones
    validate_node_notes({'id': 'n1', 'kind': 'agent', 'agent': 'domain-expert', 'mcp': ['alter']})
    with pytest.raises(EgoError):
        validate_node_notes({'id': 'n1', 'kind': 'agent', 'agent': '../escape'})
    with pytest.raises(EgoError):
        validate_node_notes({'id': 'n1', 'kind': 'code', 'agent': 'domain-expert'})
    with pytest.raises(EgoError):
        validate_node_notes({'id': 'n1', 'kind': 'code', 'mcp': ['alter']})

    # build_task_prompt embeds custom agent definition and alter/MCP retrieval instructions
    prompt = build_task_prompt(
        {'goal': 'Add alter support', 'knowledge': 'alter'},
        {'id': 'n1', 'prompt': 'Implement feature', 'agent': 'domain-expert', 'mcp': ['alter']},
        tmp_path,
    )
    assert '## Assigned custom agent' in prompt and 'domain-expert' in prompt
    assert 'Always verify domain terms.' in prompt
    assert '## Required MCP / Knowledge context' in prompt
    assert 'alter_task_context' in prompt

    # work.execute passes lean=True and mcp_servers=['alter'] when cfg.knowledge == 'alter'
    save_config(tmp_path, replace(default_config(), knowledge='alter', worker_context='lean'))
    captured_lean = []
    captured_mcp = []

    def fake_run_worker(host, p, cwd, timeout=180, **kw):
        captured_lean.append(kw.get('lean'))
        captured_mcp.append(kw.get('mcp_servers'))
        return RunResult('PASS', 'ok', '', 0, 0.0, host)

    monkeypatch.setattr(work, 'run_worker', fake_run_worker)
    res = work.execute(tmp_path, {'goal': 'g', 'nodes': [{'id': 'n1', 'kind': 'agent', 'prompt': 'p', 'write_allowlist': ['src/**']}]}, host='claude', trust_cli=True)
    assert res['final_status'] == 'PASSED'
    assert captured_lean == [True]
    assert captured_mcp == [['alter']]

    # planner.plan_work passes lean=True, mcp_servers=['alter'], and includes custom agents when cfg.knowledge == 'alter'
    captured_plan = {}

    def fake_plan_agent(host, p, cwd, timeout=180, **kw):
        captured_plan['prompt'] = p
        captured_plan['lean'] = kw.get('lean')
        captured_plan['mcp_servers'] = kw.get('mcp_servers')
        return RunResult('PASS', json.dumps({'goal': 'g', 'nodes': [{'id': 'n1', 'kind': 'agent', 'prompt': 'p', 'write_allowlist': ['src/**']}]}), '', 0, 0.0, host)

    monkeypatch.setattr(planner, 'run_agent', fake_plan_agent)
    planner.plan_work(tmp_path, 'g', host='claude', execute=True)
    assert captured_plan['lean'] is True
    assert captured_plan['mcp_servers'] == ['alter']
    assert 'domain-expert' in captured_plan['prompt']
    assert 'alter' in captured_plan['prompt']


def test_code_workflow_staged_apply(project):
    (project/"generate.py").write_text("from pathlib import Path\nPath('output.txt').write_text('ok')\n")
    (project/"check.py").write_text("from pathlib import Path\nassert Path('output.txt').read_text() == 'ok'\n")
    task={"nodes":[{"id":"write","kind":"code","argv":["python3","generate.py"],"write_allowlist":["output.txt"],"checks":[["python3","check.py"]]},
                   {"id":"gate","kind":"gate","depends_on":["write"],"argv":["python3","check.py"]}]}
    result=execute(project,task)
    assert result["final_status"]=="PASSED"
    assert not (project/"output.txt").exists()
    assert status(project,result["run_id"])["final_status"]=="PASSED"
    with pytest.raises(EgoError):execute(project,task,apply=True)
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
    with pytest.raises(EgoError):validate_task({"nodes":[{"id":"a","kind":"agent","prompt":"x","write_allowlist":["a"],"depends_on":["b"]},{"id":"b","kind":"agent","prompt":"x","write_allowlist":["b"],"depends_on":["a"]}]})


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

    from ego.usecases.install import apply_install
    from ego.domain.config import default_config
    apply_install(root, default_config())
    subprocess.run(["git", "add", "-f", "."], cwd=root, capture_output=True, check=True)  # generated files are git-excluded
    subprocess.run(["git", "commit", "-m", "add ego"], cwd=root, capture_output=True, check=True)

    (root / "write_script.py").write_text("from pathlib import Path\nPath('result.txt').write_text('done')\n")
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add script"], cwd=root, capture_output=True, check=True)

    task = {"nodes": [{"id": "write", "kind": "code", "argv": ["python3", "write_script.py"], "write_allowlist": ["result.txt"]}]}

    # 1. worktree with cleanup (default): unapplied PASS keeps staging until applied
    res_clean = execute(root, task, worktree=True, cleanup_worktree=True)
    assert res_clean["final_status"] == "PASSED"
    assert res_clean["worktree"] is True
    assert res_clean.get("worktree_cleaned") is not True
    staging_path = root / ".ego" / "state" / "runs" / res_clean["run_id"] / "staging"
    assert staging_path.exists()
    assert (staging_path / "result.txt").read_text() == "done"
    assert not (root / "result.txt").exists()

    # Applying via resume applies edits and cleans up worktree and branch
    res_applied = execute(
        root, task, run_id=res_clean["run_id"], resume=True,
        worktree=True, cleanup_worktree=True, apply=True, approve=True,
    )
    assert res_applied["final_status"] == "PASSED"
    assert res_applied["applied"] is True
    assert res_applied["worktree_cleaned"] is True
    assert not staging_path.exists()
    assert (root / "result.txt").read_text() == "done"
    assert res_applied["branch_deleted"] is True
    branches = subprocess.run(["git", "branch", "--list", "ego-task-*"], cwd=root, capture_output=True, text=True).stdout
    assert f"ego-task-{res_clean['run_id']}" not in branches

    # 2. worktree without cleanup (keep_worktree): keeps staging even after apply
    (root / "result.txt").unlink()
    res_keep = execute(root, task, worktree=True, cleanup_worktree=False, apply=True, approve=True)
    assert res_keep["final_status"] == "PASSED"
    assert res_keep["applied"] is True
    assert res_keep["worktree"] is True
    assert res_keep.get("worktree_cleaned") is not True
    staging_keep = root / ".ego" / "state" / "runs" / res_keep["run_id"] / "staging"
    assert staging_keep.exists()
    assert (staging_keep / "result.txt").read_text() == "done"
    assert (root / "result.txt").read_text() == "done"


def test_resume_apply_with_missing_staging_raises_staging_missing(tmp_path):
    import shutil
    from ego.shared.util import read_json

    root = _git_project(tmp_path / "repo")
    task = {"nodes": [{"id": "write", "kind": "code", "argv": ["python3", "gen.py"], "write_allowlist": ["result.txt"]}]}

    first = execute(root, task, worktree=True, cleanup_worktree=False, run_id="missing-staging")
    assert first["final_status"] == "PASSED"
    assert first["changed_files"] == ["result.txt"]

    staging = root / ".ego" / "state" / "runs" / "missing-staging" / "staging"
    shutil.rmtree(staging)

    with pytest.raises(EgoError) as exc:
        execute(root, task, run_id="missing-staging", resume=True, apply=True, approve=True)
    assert exc.value.code == "STAGING_MISSING"

    record = read_json(root / ".ego" / "state" / "runs" / "missing-staging" / "record.json")
    assert record["changed_files"] == ["result.txt"]
    assert (root / "src" / "a.py").read_text() == "x = 1\n"
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
    from ego.usecases.work import cancel
    (tmp_path / "check.py").write_text("from pathlib import Path\nimport sys\nsys.exit(0 if Path('ready').exists() else 1)\n")
    task = {"nodes": [{"id": "gate", "kind": "gate", "argv": ["python3", "check.py"]}]}
    first = execute(tmp_path, task)
    assert first["final_status"] == "FAILED"
    cancel(tmp_path, first["run_id"])
    (tmp_path / ".ego" / "state" / "runs" / first["run_id"] / "staging" / "ready").write_text("")
    resumed = execute(tmp_path, task, run_id=first["run_id"], resume=True)
    assert resumed["final_status"] != "CANCELLED"


def task():
    return {"goal": "g", "nodes": [
        {"id": "a", "prompt": "p", "write_allowlist": ["src/*"]},
        {"id": "b", "prompt": "p", "write_allowlist": ["src/*"], "depends_on": ["a"], "write_allowlist": ["**"]}]}


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
    t["nodes"][0]["checks"] = [["python3", "--version"]]
    result = execute(project, t, run_id="r2")
    out = compact_result(result, task_file="plan.json")
    assert out["nodes"]["a"]["checks"][0]["argv"] == ["python3", "--version"]
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
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src_dir + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
    p = subprocess.run([sys.executable, "-m", "ego", "work", "run", "--root", str(project), "--task", str(project / "t.json"),
                        "--run-id", "r5"], capture_output=True, text=True, env=env)
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


def _agent_driver(monkeypatch) -> list[tuple[str, str]]:
    """Fake worker. `WRITE f=v` writes f; `FIXED f=v` writes v instead when the prompt carries feedback `want-v`;
    a reviewer approves only when good.txt says `good`. Returns (node id, prompt) per run."""
    ran: list[tuple[str, str]] = []

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        nid = re.search(r"NODE (\S+)", prompt).group(1)
        ran.append((nid, prompt))
        for name, text in re.findall(r"WRITE (\S+?)=(\S+)", prompt):
            (Path(cwd) / name).write_text(text)
        for name, text in re.findall(r"FIXED (\S+?)=(\S+)", prompt):
            (Path(cwd) / name).write_text(text if f"want-{text}" in prompt else "bad")
        output = "ok"
        if nid == "reviewer":
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


def test_resume_after_a_reviewer_reject_sends_the_findings_to_the_reviewed_coder(tmp_path: Path, monkeypatch):
    ran = _agent_driver(monkeypatch)
    _scripts(tmp_path)
    task = {"goal": "g", "nodes": [
        _agent("code", "FIXED good.txt=good", role="coder", host="mock"),
        {"id": "gate", "kind": "gate", "depends_on": ["code"], "argv": ["python3", "noop.py"]},
        _agent("reviewer", "review", ["gate"], role="reviewer", reviews="code", max_rounds=0, host="mock",
               write_allowlist=[])]}
    first = execute(tmp_path, task, extra_allow=["*.py"], run_id="j1")
    assert first["final_status"] == "FAILED" and first["node_status"]["reviewer"] == "FAILED"
    second = execute(tmp_path, task, run_id="j1", resume=True)
    assert second["final_status"] == "PASSED"
    assert [n for n, _ in ran] == ["code", "reviewer", "code", "reviewer"]
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


def _two_node_task() -> dict:
    return {"goal": "g", "nodes": [_agent("a", "WRITE a.txt=A"), _agent("b", "WRITE b.txt=B", ["a"])]}


def _crash(root: Path, run_id: str, node: str) -> None:
    """A process that died mid-run: the record stays RUNNING with `node` in flight, no run_finished is logged,
    and nothing holds the run lock (the lock file may remain; its flock is released)."""
    runroot = root / ".ego/state/runs" / run_id
    record = json.loads((runroot / "record.json").read_text())
    record["final_status"] = "RUNNING"
    record["node_status"][node] = "RUNNING"
    record["results"].pop(node, None)
    (runroot / "record.json").write_text(json.dumps(record))
    lines = [line for line in (runroot / "progress.jsonl").read_text().splitlines() if '"run_finished"' not in line]
    (runroot / "progress.jsonl").write_text("\n".join(lines) + "\n")


def test_unlocked_running_record_reports_interrupted_with_next_action(tmp_path: Path, monkeypatch):
    _agent_driver(monkeypatch)
    execute(tmp_path, _two_node_task(), run_id="c1")
    _crash(tmp_path, "c1", "b")
    st = status(tmp_path, "c1")
    assert st["final_status"] == "INTERRUPTED"
    assert st["node_status"] == {"a": "PASSED", "b": "RUNNING"}
    out = compact_result(st, task_file="plan.json")
    assert out["final_status"] == "INTERRUPTED"
    assert out["next_action"].startswith("ego work resume c1 --root . --task plan.json")
    assert "--trust-cli" in out["next_action"] and "--apply" not in out["next_action"]


def test_watch_stops_with_run_stopped_for_an_interrupted_run(tmp_path: Path, monkeypatch):
    _agent_driver(monkeypatch)
    execute(tmp_path, _two_node_task(), run_id="c2")
    _crash(tmp_path, "c2", "b")
    lines: list[str] = []
    watch(tmp_path, "c2", write=lines.append, poll=0.01)
    last = json.loads(lines[-1])
    assert last["event"] == "run_stopped" and last["final_status"] == "INTERRUPTED"
    assert all(json.loads(line)["event"] != "run_finished" for line in lines)


def test_resume_skips_completed_nodes_and_reruns_the_interrupted_one(tmp_path: Path, monkeypatch):
    ran = _agent_driver(monkeypatch)
    execute(tmp_path, _two_node_task(), run_id="c3")
    _crash(tmp_path, "c3", "b")
    result = execute(tmp_path, _two_node_task(), run_id="c3", resume=True)
    assert result["final_status"] == "PASSED"
    assert [n for n, _ in ran] == ["a", "b", "b"]  # `a` had PASSED before the crash: not run again


def test_held_run_lock_reports_running_and_refuses_a_concurrent_resume(tmp_path: Path, monkeypatch):
    ran = _agent_driver(monkeypatch)
    execute(tmp_path, _two_node_task(), run_id="c4")
    _crash(tmp_path, "c4", "b")
    with exclusive_lock(tmp_path / ".ego/locks/c4.lock", blocking=False):  # the live process
        assert status(tmp_path, "c4")["final_status"] == "RUNNING"
        before = len(ran)
        with pytest.raises(EgoError) as refused:
            execute(tmp_path, _two_node_task(), run_id="c4", resume=True)
        assert refused.value.code == "RUN_ACTIVE"
    assert len(ran) == before  # the refused resume executed no node
    assert status(tmp_path, "c4")["final_status"] == "INTERRUPTED"  # once the lock is released


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def test_worktree_run_receives_gitignored_project_state(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text("CLAUDE.md\n.claude/\nnode_modules/\n.ego/memory/\n.ego/state/\n__pycache__/\n")
    (root / "hello.txt").write_text("initial\n")
    (root / "noop.py").write_text("pass\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")

    # Gitignored project state that `git worktree add` does not check out.
    (root / "CLAUDE.md").write_text("instructions\n")
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "rules" / "engineering.md").write_text("rule\n")
    (root / ".claude" / "node_modules").mkdir()
    (root / ".claude" / "node_modules" / "junk.js").write_text("x\n")
    (root / ".ego" / "memory").mkdir(parents=True)
    (root / ".ego" / "memory" / "fact.md").write_text("fact\n")
    (root / ".ego" / "state" / "runs" / "old").mkdir(parents=True)
    (root / ".ego" / "state" / "runs" / "old" / "record.json").write_text("{}\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "noop.cpython-314.pyc").write_bytes(b"\0")

    task = {"nodes": [{"id": "write", "kind": "code", "argv": ["python3", "noop.py"],
                       "write_allowlist": ["result.txt"]}]}
    result = execute(root, task, worktree=True, cleanup_worktree=False, run_id="hostcfg")

    staging = root / ".ego" / "state" / "runs" / "hostcfg" / "staging"
    assert result["worktree"] is True
    assert (staging / "CLAUDE.md").read_text() == "instructions\n"
    assert (staging / ".claude" / "rules" / "engineering.md").read_text() == "rule\n"
    assert (staging / ".ego" / "memory" / "fact.md").read_text() == "fact\n"
    assert (staging / ".claude" / "node_modules" / "junk.js").exists()
    assert not (staging / ".ego" / "state" / "runs" / "old").exists()
    assert not (staging / "__pycache__").exists()


def test_worktree_run_excludes_state_when_ego_dir_ignored_wholesale(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text(".ego/\n")
    (root / "hello.txt").write_text("initial\n")
    (root / "noop.py").write_text("pass\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")

    (root / ".ego" / "memory").mkdir(parents=True)
    (root / ".ego" / "memory" / "fact.md").write_text("fact\n")
    (root / ".ego" / "state" / "runs" / "old").mkdir(parents=True)
    (root / ".ego" / "state" / "runs" / "old" / "record.json").write_text("{}\n")
    (root / ".ego" / "locks").mkdir(parents=True)
    (root / ".ego" / "locks" / "x.lock").write_text("lock\n")
    (root / ".ego" / "backups").mkdir(parents=True)
    (root / ".ego" / "backups" / "b.txt").write_text("backup\n")

    task = {"nodes": [{"id": "write", "kind": "code", "argv": ["python3", "noop.py"],
                       "write_allowlist": ["result.txt"]}]}
    result = execute(root, task, worktree=True, cleanup_worktree=False, run_id="wholesale")

    staging = root / ".ego" / "state" / "runs" / "wholesale" / "staging"
    assert result["worktree"] is True
    assert (staging / ".ego" / "memory" / "fact.md").read_text() == "fact\n"
    assert not (staging / ".ego" / "state" / "runs" / "old").exists()
    assert not (staging / ".ego" / "locks").exists()
    assert not (staging / ".ego" / "backups").exists()


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
    r = run_worker(host, "DO IT", tmp_path, 600, agent=agent, threshold=1000, segment_turns=2, tag="n-1-x", project_root=tmp_path / "main",
                   max_turns=kw.pop("max_turns", 10), read_only=read_only, on_event=lambda **f: events.append(f), **kw)
    return r, calls, events


@pytest.mark.parametrize("host,read_only", [("claude", False), ("codex", False), ("claude", True)])
def test_every_worker_is_told_the_project_root_alter_knows_it_by(tmp_path, monkeypatch, host, read_only):
    # Arrange / Act
    _, calls, _ = drive(tmp_path, monkeypatch, [result(stop="success")], tokens=500, host=host, read_only=read_only)
    # Assert
    assert calls[0]["env"]["EGO_ALTER_PROJECT_ROOT"] == str((tmp_path / "main").resolve())


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


def test_the_checkpoint_stays_on_disk_until_the_worker_finishes(tmp_path, monkeypatch):
    handoff = tmp_path / ".ego/memory/handoffs/t1.json"

    def save(cwd, k):
        save_checkpoint(cwd, "t1", goal="g", next_steps=["finish B"], run=k["env"]["EGO_RUN_ID"])
        return result(stop="success", sid="s1", turns=1)

    def fresh_session(cwd, k):
        assert handoff.exists()  # the resumed session is still running: the file is dropped only at the end
        return result(stop="success", sid="s2")

    r, _, _ = drive(tmp_path, monkeypatch, [result(), save, fresh_session], tokens=5000)
    assert r.status == "PASS" and r.rotations == 1
    assert not handoff.exists()


def test_a_revision_already_resumed_is_not_offered_again_on_the_next_rotation(tmp_path, monkeypatch):
    def save(cwd, k):
        save_checkpoint(cwd, "t1", goal="g", next_steps=["finish B"], run=k["env"]["EGO_RUN_ID"])
        return result(stop="success", sid="s1", turns=1)

    def save_nothing(cwd, k):
        return result(stop="success", sid="s2", turns=1)

    r, calls, _ = drive(tmp_path, monkeypatch, [result(), save, result(sid="s2"), save_nothing], tokens=5000)
    assert r.status == "FAIL" and r.reason == "NO_CHECKPOINT" and r.rotations == 1
    assert len(calls) == 4
    assert not list((tmp_path / ".ego/memory/handoffs").glob("*.json"))


def test_handoffs_are_dropped_even_when_the_worker_crashes(tmp_path, monkeypatch):
    def save(cwd, k):
        save_checkpoint(cwd, "t1", goal="g", run=k["env"]["EGO_RUN_ID"])
        return result(stop="success", sid="s1", turns=1)

    def crash(cwd, k):
        raise RuntimeError("host crashed")

    with pytest.raises(RuntimeError):
        drive(tmp_path, monkeypatch, [result(), save, crash], tokens=5000)
    assert not list((tmp_path / ".ego/memory/handoffs").glob("*.json"))


def test_other_hosts_and_read_only_nodes_run_once(tmp_path, monkeypatch):
    for host, ro in (("codex", False), ("claude", True)):
        r, calls, _ = drive(tmp_path, monkeypatch, [result(stop="error_max_turns")], tokens=5000, host=host, read_only=ro)
        assert len(calls) == 1 and "resume_session" not in calls[0] and calls[0]["max_turns"] == 10


def test_turn_budget_is_shared_across_segments(tmp_path, monkeypatch):
    r, calls, _ = drive(tmp_path, monkeypatch, [result()], tokens=500, max_turns=4)
    assert r.status == "FAIL" and r.reason == "MAX_TURNS" and len(calls) == 2


def test_token_cap_stops_the_run_before_the_next_attempt(tmp_path: Path, monkeypatch):
    save_config(tmp_path, replace(default_config(), max_run_tokens=1000))
    (tmp_path / "fail.py").write_text("import sys\nsys.exit(1)\n")
    calls = []

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        calls.append(prompt)
        return RunResult("PASS", "ok", "", 0, 0.0, host, usage={"input_tokens": 800, "output_tokens": 300})

    monkeypatch.setattr(work, "run_agent", fake_agent)
    result = execute(tmp_path, {"goal": "g", "nodes": [
        {"id": "impl", "kind": "agent", "prompt": "do it", "host": "claude", "write_allowlist": ["src/**"],
         "checks": [["python3", "fail.py"]], "max_attempts": 3}]}, host="claude", trust_cli=True)
    assert result["final_status"] == "BUDGET_EXHAUSTED" and len(calls) == 1


def test_cache_read_tokens_do_not_count_toward_the_token_cap(tmp_path: Path, monkeypatch):
    save_config(tmp_path, replace(default_config(), max_run_tokens=1000))
    monkeypatch.setattr(work, "run_agent", lambda host, *a, **kw: RunResult(
        "PASS", "ok", "", 0, 0.0, host, usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 100000}))
    result = execute(tmp_path, {"goal": "g", "nodes": [
        {"id": "impl", "kind": "agent", "prompt": "do it", "host": "claude", "write_allowlist": ["src/**"]}]},
        host="claude", trust_cli=True)
    assert result["final_status"] == "PASSED"


def test_there_is_no_whole_run_time_limit(tmp_path: Path, monkeypatch):
    save_config(tmp_path, replace(default_config(), timeout_seconds=5))
    clock = iter(range(0, 10**6, 1000))
    monkeypatch.setattr(work.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(work, "run_agent", lambda host, *a, **kw: RunResult("PASS", "ok", "", 0, 0.0, host))
    result = execute(tmp_path, {"goal": "g", "nodes": [
        {"id": "first", "kind": "agent", "prompt": "a", "host": "claude", "write_allowlist": ["src/**"]},
        {"id": "second", "kind": "agent", "prompt": "b", "host": "claude", "write_allowlist": ["src/**"], "depends_on": ["first"]}]},
        host="claude", trust_cli=True)
    assert result["final_status"] == "PASSED"


def test_tuning_override_in_project_defaults_limits_feedback_length(tmp_path: Path, monkeypatch):
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "defaults.toml").write_text("[tuning]\nmax_feedback_chars = 300\n")
    (tmp_path / "noisy.py").write_text("import sys\nprint('x'*20000)\nprint('REAL FAILURE AT END')\nsys.exit(1)\n")
    monkeypatch.setattr(work, "run_agent", lambda host, *a, **kw: RunResult("PASS", "ok", "", 0, 0.0, host))
    result = execute(tmp_path, {"goal": "g", "nodes": [
        {"id": "impl", "kind": "agent", "prompt": "do it", "host": "claude", "write_allowlist": ["src/**"],
         "checks": [["python3", "noisy.py"]], "max_attempts": 1}]}, host="claude", trust_cli=True)
    feedback = result["results"]["impl"]["attempts"][0]["feedback"]
    assert len(feedback) <= 300 and "REAL FAILURE AT END" in feedback


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


def test_retry_keeps_the_previous_attempts_edits(tmp_path: Path, monkeypatch):
    # Arrange: the first attempt writes only the feature; the retry writes only what the check still misses.
    writes = iter(["src/feature.py", "src/fix.py"])

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        rel = next(writes)
        (cwd / rel).parent.mkdir(parents=True, exist_ok=True)
        (cwd / rel).write_text("x")
        return RunResult("PASS", "ok", "", 0, 0.0, host, cost_usd=0.01)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    (tmp_path / "both_files_exist.py").write_text("import pathlib,sys\nsys.exit(0 if all(pathlib.Path(p).exists() for p in ('src/feature.py','src/fix.py')) else 1)\n")
    both_files_exist = ["python3", "both_files_exist.py"]

    # Act
    result = execute(tmp_path, {"goal": "g", "nodes": [{"id": "impl", "kind": "agent", "prompt": "do it", "host": "claude", "write_allowlist": ["src/**"], "checks": [both_files_exist], "max_attempts": 2}]}, host="claude", trust_cli=True, apply=True, approve=True)

    # Assert
    assert result["final_status"] == "PASSED"
    assert sorted(result["changed_files"]) == ["src/feature.py", "src/fix.py"]


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


def _fake_claude(monkeypatch, tmp_path: Path, flags: str):
    folder = tmp_path / "vendor-bin"
    folder.mkdir()
    script = folder / "claude"
    script.write_text("#!/usr/bin/env python3\nimport sys\n"
                      "print('claude 9.9.9' if '--version' in sys.argv else " + repr(flags) + ")\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("EGO_CACHE_DIR", str(tmp_path / "cache"))


def test_lean_worker_drops_inherited_mcp_and_carries_turn_limit(monkeypatch, tmp_path: Path):
    _fake_claude(monkeypatch, tmp_path, "--output-format --max-turns --permission-mode --strict-mcp-config --mcp-config auto")
    argv = hosts.build_command("claude", "p", read_only=False, max_turns=30, lean=True, use_stdin=True)
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv
    assert argv[argv.index("--max-turns") + 1] == "30"
    inherit = hosts.build_command("claude", "p", read_only=False, use_stdin=True)
    assert "--strict-mcp-config" not in inherit


def test_lean_worker_with_alter_loads_only_requested_mcp(monkeypatch, tmp_path: Path):
    _fake_claude(monkeypatch, tmp_path, "--output-format --max-turns --permission-mode --strict-mcp-config --mcp-config auto")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "alter": {"type": "stdio", "command": "node", "args": ["alter.js"]},
            "other": {"type": "stdio", "command": "node", "args": ["other.js"]},
        }
    }))
    monkeypatch.setenv("HOME", str(home))
    argv = hosts.build_command("claude", "p", read_only=False, lean=True, mcp_servers=["alter"], cwd=tmp_path, use_stdin=True)
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" in argv
    loaded = json.loads(argv[argv.index("--mcp-config") + 1])
    assert list(loaded["mcpServers"].keys()) == ["alter"]


def test_lean_flags_are_skipped_when_the_installed_cli_lacks_them(monkeypatch, tmp_path: Path):
    _fake_claude(monkeypatch, tmp_path, "--output-format --max-turns --permission-mode auto")
    argv = hosts.build_command("claude", "p", read_only=False, lean=True, use_stdin=True)
    assert "--strict-mcp-config" not in argv


def test_managed_worker_gets_configured_turns_not_the_planner_default(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        seen.update(kw)
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("clear_suggest_tokens = 0\n")  # no segmenting: one worker call gets the whole budget
    spec = {"goal": "g", "nodes": [{"id": "impl", "kind": "agent", "prompt": "p", "host": "claude", "write_allowlist": ["src/**"]}]}
    execute(tmp_path, spec, host="claude", trust_cli=True)
    assert seen["max_turns"] == default_config().max_turns == 30 and seen["lean"] is True
    assert seen["mcp_servers"] == ["alter"]
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


def test_natural_language_goal_finds_candidate_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/timeout_validation.py").write_text("def validate(value):\n    return value > 0\n")
    (tmp_path / "src/other.py").write_text("def unrelated():\n    pass\n")
    found = text_fallback(tmp_path, "Add timeout input validation and reject negative values")
    assert found["strategy"] == "local_term_rank"
    assert found["matches"][0]["path"] == "src/timeout_validation.py"
    assert all(m["path"] != "src/other.py" for m in found["matches"])


def _writing_agent(monkeypatch) -> list[str]:
    """Fake worker: `WRITE <file>=<text>` in a node prompt writes that file; returns the node ids that ran."""
    import re
    ran: list[str] = []

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        for name, text in re.findall(r"WRITE (\S+?)=(\S+)", prompt):
            (Path(cwd) / name).write_text(text)
        ran.append(re.search(r"NODE (\S+)", prompt).group(1))
        return RunResult("PASS", "ok", "", 0, 0.0, host, cost_usd=0.0)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    return ran


def _wagent(nid: str, write: str, deps=(), checks=None) -> dict:
    node = {"id": nid, "kind": "agent", "prompt": f"NODE {nid} WRITE {write}", "write_allowlist": ["*.txt"],
            "depends_on": list(deps), "max_attempts": 1}
    if checks:
        node["checks"] = checks
    return node


def test_seeded_run_skips_verified_agent_nodes_and_reruns_only_the_failed_one(tmp_path: Path, monkeypatch):
    ran = _writing_agent(monkeypatch)
    (tmp_path / "check_b.py").write_text("from pathlib import Path\nimport sys\nsys.exit(Path('b.txt').read_text()!='ok')\n")
    (tmp_path / "noop.py").write_text("print('ok')\n")
    gate = {"id": "gate", "kind": "gate", "depends_on": ["a"], "write_allowlist": ["**"], "argv": ["python3", "noop.py"]}
    first = {"goal": "g", "nodes": [_wagent("a", "a.txt=A"), gate,
                                    _wagent("b", "b.txt=bad", ["gate"], [["python3", "check_b.py"]])]}
    r1 = execute(tmp_path, first, extra_allow=["*.py"])
    assert r1["final_status"] == "FAILED" and ran == ["a", "b"]
    second = {"goal": "restated goal", "nodes": [_wagent("a", "a.txt=A"), gate,
                                                 _wagent("b", "b.txt=ok", ["gate"], [["python3", "check_b.py"]])]}
    r2 = execute(tmp_path, second, seed_from=r1["run_id"])
    assert r2["final_status"] == "PASSED" and ran == ["a", "b", "b"]
    assert r2["carried"] == ["a"] and r2["results"]["a"]["carried_from"] == r1["run_id"]
    assert r2["changed_files"] == ["a.txt", "b.txt"]
    progress = (tmp_path / ".ego/state/runs" / r2["run_id"] / "progress.jsonl").read_text()
    assert '"nodes_carried"' in progress
    # A changed ancestor invalidates every node below it.
    third = {"goal": "g", "nodes": [_wagent("a", "a.txt=A2"), gate,
                                    _wagent("b", "b.txt=ok", ["gate"], [["python3", "check_b.py"]])]}
    r3 = execute(tmp_path, third, seed_from=r2["run_id"])
    assert r3["final_status"] == "PASSED" and "carried" not in r3 and ran[3:] == ["a", "b"]


def test_node_fingerprint_covers_ancestors_and_shared_settings_but_not_the_goal():
    base = {"goal": "g", "nodes": [_wagent("a", "a.txt=A"), _wagent("b", "b.txt=B", ["a"]), _wagent("c", "c.txt=C")]}
    fp = work.node_fingerprints(base)
    assert work.node_fingerprints({**base, "goal": "reworded"}) == fp
    changed = work.node_fingerprints({**base, "nodes": [_wagent("a", "a.txt=X")] + base["nodes"][1:]})
    assert changed["a"] != fp["a"] and changed["b"] != fp["b"] and changed["c"] == fp["c"]
    assert work.node_fingerprints({**base, "instructions": "be careful"})["c"] != fp["c"]


def test_planner_uses_the_configured_host_model(tmp_path: Path, monkeypatch):
    from ego.usecases import planner
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


REAL = work.run_agent


@pytest.fixture
def writing_agent(monkeypatch):
    """The mock host never edits; [[WRITE:path]] in a prompt makes it write that file (offline stand-in for a worker)."""
    def fake(host, prompt, cwd, *a, **k):
        m = re.search(r"\[\[WRITE:(.*?)\]\]", prompt)
        if m:
            (cwd / m.group(1)).parent.mkdir(parents=True, exist_ok=True)
            (cwd / m.group(1)).write_text("x")
        return REAL(host, prompt, cwd, *a, **k)
    monkeypatch.setattr(work, "run_agent", fake)


def node(i, **kw):
    return {"id": i, "prompt": "p", "write_allowlist": ["src/*"], **kw}


def test_inputs_outputs_reach_prompts(tmp_path):
    task = {"goal": "g", "nodes": [node("a", inputs=["src/a.py"], outputs=["src/a.py::run"]),
                                   node("b", depends_on=["a"])]}
    validate_task(task)
    assert "## Inputs" in build_task_prompt(task, task["nodes"][0], tmp_path)
    assert "- src/a.py::run" in build_task_prompt(task, task["nodes"][0], tmp_path)
    assert "outputs: src/a.py::run" in build_task_prompt(task, task["nodes"][1], tmp_path)


def test_outputs_must_be_in_write_allowlist_and_inputs_bounded():
    with pytest.raises(EgoError, match="outside write_allowlist"):
        validate_task({"nodes": [node("a", outputs=["docs/x.md"])]})
    with pytest.raises(EgoError, match="INVALID_TASK|must be"):
        validate_task({"nodes": [node("a", inputs=["f"] * 7)]})


def test_tester_may_only_write_tests(project, writing_agent):
    ok = {"nodes": [{"id": "t", "role": "tester", "prompt": "[[WRITE:tests/t.py]]", "write_allowlist": ["tests/*"]}]}
    assert execute(project, ok)["final_status"] == "PASSED"
    # Even with a wide allowlist set by the coder side, the role itself limits writes.
    bad = {"nodes": [{"id": "t", "role": "tester", "prompt": "[[WRITE:tests/../src/x.py]]", "write_allowlist": ["tests/*"], "max_attempts": 1}]}
    assert execute(project, bad)["final_status"] == "FAILED"


def test_reviewer_cannot_write(project, writing_agent):
    task = {"nodes": [node("c", role="coder", prompt="p"),
                      {"id": "g", "kind": "gate", "depends_on": ["c"], "argv": ["python3", "--version"]},
                      {"id": "j", "role": "reviewer", "depends_on": ["g"], "reviews": "c",
                       "prompt": "[[WRITE:src/j.py]]VERDICT: APPROVE"}]}
    # Judge write: the node itself must not carry an allowlist, so the write is refused by role.
    result = execute(project, task)
    assert result["node_status"]["j"] == "FAILED"


def reviewed(verdict_text, **jkw):
    return {"nodes": [node("c", role="coder", prompt="[[WRITE:src/a.py]]ok"),
                      {"id": "g", "kind": "gate", "depends_on": ["c"], "argv": ["python3", "--version"]},
                      {"id": "j", "role": "reviewer", "depends_on": ["g"], "reviews": "c",
                       "prompt": "[[MOCK:" + verdict_text + "]]", **jkw}]}


def test_reviewer_approve_passes(project, writing_agent):
    assert execute(project, reviewed("VERDICT: APPROVE"))["final_status"] == "PASSED"


def test_reviewer_reject_without_evidence_is_approved(project, writing_agent):
    assert execute(project, reviewed("- looks wrong\nVERDICT: REJECT"))["final_status"] == "PASSED"


def test_reviewer_reject_with_evidence_retries_coder_locally_then_fails(project, writing_agent):
    result = execute(project, reviewed("- src/a.py:1: spec omitted\nVERDICT: REJECT"))
    assert result["final_status"] == "FAILED"
    assert result["rounds"] == {"j": 1}
    assert len(result["results"]["c"]["attempts"]) == 1 and result["node_status"]["c"] == "PASSED"
    again = execute(project, reviewed("- src/a.py:1: spec omitted\nVERDICT: REJECT", max_rounds=0))
    assert "j" not in again["rounds"] or again["rounds"]["j"] == 0


def test_reviewer_needs_reviews_and_a_gate():
    base = reviewed("VERDICT: APPROVE")
    no_reviews = {"nodes": [dict(n) for n in base["nodes"]]}
    del no_reviews["nodes"][2]["reviews"]
    with pytest.raises(EgoError, match="reviews"):
        validate_task(no_reviews)
    no_gate = {"nodes": [base["nodes"][0], {**base["nodes"][2], "depends_on": ["c"]}]}
    with pytest.raises(EgoError, match="gate or code"):
        validate_task(no_gate)


def test_failed_gate_means_reviewer_never_runs(project, writing_agent):
    task = reviewed("VERDICT: APPROVE")
    (project / "fail.py").write_text("raise SystemExit(1)\n")
    task["nodes"][1]["argv"] = ["python3", "fail.py"]
    result = execute(project, task)
    assert result["final_status"] == "FAILED" and "j" not in result["node_status"]


def test_gate_failure_in_reviewer_loop_sends_coder_back(project, writing_agent):
    task = reviewed("VERDICT: APPROVE")
    (project / "fail.py").write_text("raise SystemExit(1)\n")
    task["nodes"][1]["argv"] = ["python3", "fail.py"]
    result = execute(project, task)
    assert result["final_status"] == "FAILED" and result["rounds"] == {"j": 1}


def test_identical_retry_is_a_stall_before_max_rounds(project, writing_agent):
    result = execute(project, reviewed("- src/a.py:1: spec omitted\nVERDICT: REJECT"))
    assert result["rounds"] == {"j": 1} and result["exhausted"] == {"j": "fail"}


def test_reviewer_unavailable_is_retried(project, monkeypatch):
    seen = []
    def fake(host, prompt, cwd, *a, **k):
        if "## Reviewer rules" in prompt:
            seen.append(prompt)
            if len(seen) == 1:
                return RunResult("UNAVAILABLE", "", "host down", 127, 0, host)
        return REAL(host, prompt, cwd, *a, **k)
    monkeypatch.setattr(work, "run_agent", fake)
    assert execute(project, reviewed("VERDICT: APPROVE"))["final_status"] == "PASSED"
    assert len(seen) == 2


def test_overlapping_reviewer_loops_are_rejected():
    nodes = [node("c", role="coder", prompt="p"),
             {"id": "g", "kind": "gate", "depends_on": ["c"], "argv": ["python3", "--version"]},
             {"id": "r1", "role": "reviewer", "depends_on": ["g"], "reviews": "c", "prompt": "p"},
             {"id": "r2", "role": "reviewer", "depends_on": ["g"], "reviews": "c", "prompt": "p"}]
    with pytest.raises(EgoError, match="overlap"):
        validate_task({"nodes": nodes})


def test_parallel_conflict_is_not_applied(project):
    (project/"write1.py").write_text("from pathlib import Path\nPath('shared.txt').write_text('one')\n")
    (project/"write2.py").write_text("from pathlib import Path\nPath('shared.txt').write_text('two')\n")
    spec={"nodes":[{"id":"a","kind":"code","argv":["python3","write1.py"],"write_allowlist":["shared.txt"]},
                   {"id":"b","kind":"code","argv":["python3","write2.py"],"write_allowlist":["shared.txt"]}]}
    with pytest.raises(EgoError):execute(project,spec,host="mock",apply=True,approve=True)
    assert not (project/"shared.txt").exists()


def test_build_task_prompt_inlines_rules_alter_rules_and_project_root(tmp_path: Path):
    # Arrange
    project_root = tmp_path / "repo"
    worker_dir = tmp_path / "worker"
    (worker_dir / ".agents/rules").mkdir(parents=True)
    (worker_dir / ".claude/rules").mkdir(parents=True)
    (worker_dir / ".agents/rules/shared.md").write_text("# Shared\nUse DRY.")
    (worker_dir / ".claude/rules/shared.md").write_text("# Shared\nUse DRY.")
    (worker_dir / ".claude/rules/claude_only.md").write_text("# Claude\nCheck diff.")
    spec = {"goal": "Fix parser", "knowledge": "alter"}
    node_spec = {"id": "n1", "prompt": "Update parse()"}

    # Act
    prompt = build_task_prompt(
        spec,
        node_spec,
        worker_dir,
        project_root=project_root,
        alter_rules=["Always respond in Japanese"],
        alter_context="alter: 過去ナレッジ:\n- Parser note: Keep backward compat [n:1]",
    )

    # Assert
    assert "## Project rules" in prompt
    assert prompt.count("Use DRY.") == 1
    assert "### .claude/rules/claude_only.md\n# Claude\nCheck diff." in prompt
    assert "## Project AI usage rules (alter)\n- Always respond in Japanese" in prompt
    assert f'cwd="{project_root.resolve()}"' in prompt
    assert "Parser note: Keep backward compat" in prompt


def test_managed_execute_passes_alter_context_and_disables_duplicate_worker_hooks(tmp_path: Path, monkeypatch):
    # Arrange
    from ego.usecases import session
    save_config(tmp_path, replace(default_config(), knowledge="alter", clear_suggest_tokens=0))
    (tmp_path / ".agents/rules").mkdir(parents=True)
    (tmp_path / ".agents/rules/engineering.md").write_text("Prefer existing patterns.")
    seen: list[dict] = []

    monkeypatch.setattr(session, "alter_project_rules", lambda root: ["Respond in Japanese"])
    monkeypatch.setattr(
        session,
        "alter_prompt_context",
        lambda root, query: f"context for: {query}" if "Implement cache" in query else "",
    )

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        seen.append({"host": host, "prompt": prompt, "env": kw.get("env") or {}})
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    spec = {
        "goal": "Add cache",
        "nodes": [
            {
                "id": "impl",
                "kind": "agent",
                "host": "claude",
                "intent": "Speed up lookups",
                "prompt": "Implement cache",
                "write_allowlist": ["src/**"],
            }
        ],
    }

    # Act
    res = work.execute(tmp_path, spec, host="claude", trust_cli=True)

    # Assert
    assert res["final_status"] == "PASSED"
    assert len(seen) == 1
    prompt = seen[0]["prompt"]
    assert "Prefer existing patterns." in prompt
    assert "- Respond in Japanese" in prompt
    assert "context for: Speed up lookups Implement cache" in prompt
    assert f'cwd="{tmp_path.resolve()}"' in prompt
    env = seen[0]["env"]
    assert env["EGO_ALTER_PROJECT_ROOT"] == str(tmp_path.resolve())
    assert env["EGO_ALTER_SESSION_RULES"] == "0"
    assert env["EGO_ALTER_PROMPT_CONTEXT"] == "0"


def test_select_mcp_servers_and_build_command_resolve_project_root_from_isolated_cwd(tmp_path: Path, monkeypatch):
    # Arrange
    _fake_claude(monkeypatch, tmp_path, "--output-format --max-turns --permission-mode --strict-mcp-config --mcp-config auto")
    home = tmp_path / "home"
    home.mkdir()
    project_root = tmp_path / "main-repo"
    project_root.mkdir()
    isolated_cwd = tmp_path / "ego-node-tmp"
    isolated_cwd.mkdir()
    (home / ".claude.json").write_text(json.dumps({
        "projects": {
            str(project_root.resolve()): {
                "mcpServers": {
                    "alter": {"type": "stdio", "command": "node", "args": ["project-alter.js"]},
                }
            }
        }
    }))
    monkeypatch.setenv("HOME", str(home))

    # Act
    selected = hosts.select_mcp_servers(isolated_cwd, ["alter"], host="claude", project_root=project_root)
    argv = hosts.build_command(
        "claude", "p", read_only=False, lean=True, mcp_servers=["alter"],
        cwd=isolated_cwd, project_root=project_root, use_stdin=True,
    )

    # Assert
    assert selected == {"alter": {"type": "stdio", "command": "node", "args": ["project-alter.js"]}}
    assert "--mcp-config" in argv
    loaded = json.loads(argv[argv.index("--mcp-config") + 1])
    assert loaded["mcpServers"]["alter"]["args"] == ["project-alter.js"]


def _init_git_repo(repo: Path, files: dict[str, str]) -> None:
    import subprocess
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


def test_copy_worktree_extras_copies_ignored_files_independently(tmp_path: Path):
    from ego.shared.util import copy_worktree_extras
    src = tmp_path / "src"
    src.mkdir()
    _init_git_repo(src, {".gitignore": "node_modules/\n"})
    (src / "node_modules" / "pkg").mkdir(parents=True)
    (src / "node_modules" / "pkg" / "index.js").write_text("original")
    dst = tmp_path / "dst"
    dst.mkdir()

    copy_worktree_extras(src, dst)
    copied = dst / "node_modules" / "pkg" / "index.js"
    assert copied.read_text() == "original"

    copied.write_text("changed")

    assert (src / "node_modules" / "pkg" / "index.js").read_text() == "original"


def test_node_runs_in_git_worktree_and_merges_non_overlapping_same_file_edits(tmp_path: Path):
    import subprocess
    from unittest.mock import patch
    from ego.usecases import work
    from ego.domain.config import default_config, save_config

    # Arrange
    lines = [f"line {i}" for i in range(1, 11)]
    _init_git_repo(tmp_path, {"shared.txt": "\n".join(lines) + "\n"})
    save_config(tmp_path, replace(default_config(), max_parallel=2))
    node_has_git: dict[str, bool] = {}

    def fake_non_overlapping(host, prompt, cwd, timeout=180, **kw):
        p = Path(cwd) / "shared.txt"
        content = p.read_text().splitlines()
        if "NODE:top" in prompt:
            node_has_git["top"] = (Path(cwd) / ".git").exists()
            content[0] = "TOP_EDITED"
        else:
            node_has_git["bottom"] = (Path(cwd) / ".git").exists()
            content[-1] = "BOTTOM_EDITED"
        p.write_text("\n".join(content) + "\n")
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    task_ok = {
        "goal": "parallel non-overlapping edits",
        "nodes": [
            {"id": "top", "kind": "agent", "intent": "NODE:top", "prompt": "NODE:top", "depends_on": [], "write_allowlist": ["**"]},
            {"id": "bottom", "kind": "agent", "intent": "NODE:bottom", "prompt": "NODE:bottom", "depends_on": [], "write_allowlist": ["**"]},
        ],
    }

    # Act
    with patch("ego.usecases.work.run_agent", side_effect=fake_non_overlapping):
        res_ok = work._execute(tmp_path, task_ok, work.Progress(tmp_path / "progress.jsonl"), worktree=True, apply=False, cleanup_worktree=False)

    # Assert
    assert res_ok["final_status"] == "PASSED"
    assert node_has_git == {"top": True, "bottom": True}
    staging_ok = tmp_path / ".ego" / "state" / "runs" / res_ok["run_id"] / "staging"
    merged = (staging_ok / "shared.txt").read_text().splitlines()
    assert merged[0] == "TOP_EDITED"
    assert merged[-1] == "BOTTOM_EDITED"
    branches = subprocess.run(["git", "branch", "--list", "ego-node-*"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert branches.stdout.strip() == ""
    worktrees = subprocess.run(["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "ego-node-" not in worktrees.stdout

    # Arrange (conflicting same-line edits)
    def fake_conflicting(host, prompt, cwd, timeout=180, **kw):
        p = Path(cwd) / "shared.txt"
        content = p.read_text().splitlines()
        content[0] = "CONFLICT_A" if "NODE:c1" in prompt else "CONFLICT_B"
        p.write_text("\n".join(content) + "\n")
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    task_conflict = {
        "goal": "parallel conflicting edits",
        "nodes": [
            {"id": "c1", "kind": "agent", "intent": "NODE:c1", "prompt": "NODE:c1", "depends_on": [], "write_allowlist": ["**"]},
            {"id": "c2", "kind": "agent", "intent": "NODE:c2", "prompt": "NODE:c2", "depends_on": [], "write_allowlist": ["**"]},
        ],
    }

    # Act & Assert
    import pytest
    from ego.shared.util import EgoError
    with patch("ego.usecases.work.run_agent", side_effect=fake_conflicting):
        with pytest.raises(EgoError) as exc_info:
            work._execute(tmp_path, task_conflict, work.Progress(tmp_path / "progress.jsonl"), worktree=True, apply=False, cleanup_worktree=False)
    assert exc_info.value.code == "MERGE_CONFLICT"


def test_seeded_run_preserves_git_worktree_across_from_rerun(tmp_path: Path, monkeypatch):
    from ego.usecases import work

    # Arrange
    _init_git_repo(tmp_path, {"base.txt": "base\n"})
    task = {
        "goal": "seed_from worktree preservation",
        "nodes": [
            {"id": "a", "kind": "agent", "intent": "create a", "prompt": "PROMPT_TOKEN_A", "depends_on": [], "write_allowlist": ["**"]},
            {"id": "b", "kind": "agent", "intent": "create b", "prompt": "PROMPT_TOKEN_B", "depends_on": ["a"], "write_allowlist": ["**"]},
        ],
    }

    def fake_first_run(host, prompt, cwd, timeout=180, **kw):
        if "PROMPT_TOKEN_B" in prompt:
            return RunResult("FAIL", "", "boom", 1, 0.0, host)
        (Path(cwd) / "a.txt").write_text("from a\n")
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    monkeypatch.setattr(work, "run_agent", fake_first_run)
    res1 = work._execute(tmp_path, task, work.Progress(tmp_path / "progress.jsonl"), worktree=True, apply=False, cleanup_worktree=False)
    assert res1["final_status"] == "FAILED"

    def fake_second_run(host, prompt, cwd, timeout=180, **kw):
        (Path(cwd) / "b.txt").write_text("from b\n")
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    monkeypatch.setattr(work, "run_agent", fake_second_run)

    # Act
    res2 = work._execute(tmp_path, task, work.Progress(tmp_path / "progress.jsonl"), worktree=True, seed_from=res1["run_id"], apply=False, cleanup_worktree=False)

    # Assert
    assert res2["final_status"] == "PASSED"
    assert res2["worktree"] is True
    assert "a" in res2["carried"]
    staging = tmp_path / ".ego" / "state" / "runs" / res2["run_id"] / "staging"
    assert (staging / "a.txt").read_text() == "from a\n"
    assert (staging / "b.txt").read_text() == "from b\n"
    seed = tmp_path / ".ego" / "state" / "runs" / res1["run_id"]
    assert not (seed / "staging").exists()
    assert (seed / "record.json").exists()


def test_dynamic_dag_scheduler_starts_dependent_node_before_slow_sibling_finishes(tmp_path: Path, monkeypatch):
    import threading
    from ego.usecases import work
    from ego.domain.config import default_config, save_config

    # Arrange
    save_config(tmp_path, replace(default_config(), max_parallel=2))
    slow_started = threading.Event()
    allow_slow_finish = threading.Event()
    events: list[str] = []
    lock = threading.Lock()

    def record(ev: str) -> None:
        with lock:
            events.append(ev)

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        if "PROMPT_TOKEN_AFTER" in prompt:
            assert slow_started.wait(timeout=5)
            record("after_fast:end")
            allow_slow_finish.set()
        elif "PROMPT_TOKEN_SLOW" in prompt:
            slow_started.set()
            assert allow_slow_finish.wait(timeout=5)
            record("slow:end")
        elif "PROMPT_TOKEN_FAST" in prompt:
            record("fast:end")
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    monkeypatch.setattr(work, "run_agent", fake_agent)
    task = {
        "goal": "dynamic dag scheduling",
        "nodes": [
            {"id": "fast", "kind": "agent", "intent": "fast", "prompt": "PROMPT_TOKEN_FAST", "depends_on": [], "write_allowlist": ["**"]},
            {"id": "slow", "kind": "agent", "intent": "slow", "prompt": "PROMPT_TOKEN_SLOW", "depends_on": [], "write_allowlist": ["**"]},
            {"id": "after_fast", "kind": "agent", "intent": "after", "prompt": "PROMPT_TOKEN_AFTER", "depends_on": ["fast"], "write_allowlist": ["**"]},
        ],
    }

    # Act
    res = work._execute(tmp_path, task, work.Progress(tmp_path / "progress.jsonl"), worktree=False, apply=False)

    # Assert
    assert res["final_status"] == "PASSED"
    assert events.index("after_fast:end") < events.index("slow:end")


def test_worktree_without_branch_applies_edits(tmp_path: Path):
    # Arrange
    (tmp_path / "w.py").write_text("from pathlib import Path\nPath('note.txt').write_text('after')\n")
    spec = {"goal": "g", "nodes": [{"id": "n1", "kind": "code", "argv": ["python3", "w.py"], "write_allowlist": ["note.txt"]}]}

    # Act
    res = execute(tmp_path, spec, worktree=True, apply=True, approve=True)

    # Assert
    assert res["final_status"] == "PASSED"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after"


def _replan_project(root: Path) -> None:
    (root / "check.py").write_text("from pathlib import Path\nimport sys\nsys.exit(0 if Path('a.txt').exists() else 1)\n")
    (root / "make_a.py").write_text("from pathlib import Path\nPath('a.txt').write_text('A')\n")


def _code_node(nid: str, script: str) -> dict:
    return {"id": nid, "kind": "code", "argv": ["python3", script], "write_allowlist": ["a.txt"]}


def test_failed_run_replans_once_and_the_second_run_passes(tmp_path: Path, monkeypatch):
    from ego.usecases import replan
    _replan_project(tmp_path)
    monkeypatch.setattr(replan, "plan_work", lambda *a, **kw: {"task": {"goal": "g", "nodes": [_code_node("make-a", "make_a.py")]}})
    spec = {"goal": "g", "on_exhausted": "replan", "max_replans": 1, "nodes": [_code_node("check-a", "check.py")]}
    result = replan.run_with_replans(tmp_path, spec, run_id="r-a", host="mock", trust_cli=False)
    assert result["final_status"] == "PASSED" and len(result["replans"]) == 1


def test_zero_max_replans_does_not_call_the_planner(tmp_path: Path, monkeypatch):
    from ego.usecases import replan
    _replan_project(tmp_path)
    monkeypatch.setattr(replan, "plan_work", lambda *a, **kw: pytest.fail("planner must not run"))
    spec = {"goal": "g", "on_exhausted": "replan", "max_replans": 0, "nodes": [_code_node("check-a", "check.py")]}
    result = replan.run_with_replans(tmp_path, spec, run_id="r-b", host="mock", trust_cli=False)
    assert result["final_status"] == "FAILED" and result["replans"] == []


def test_replan_writing_outside_the_allowlist_is_refused(tmp_path: Path, monkeypatch):
    from ego.usecases import replan
    _replan_project(tmp_path)
    outside = {"id": "make-a", "kind": "code", "argv": ["python3", "make_a.py"], "write_allowlist": ["secret.txt"]}
    monkeypatch.setattr(replan, "plan_work", lambda *a, **kw: {"task": {"goal": "g", "nodes": [outside]}})
    spec = {"goal": "g", "on_exhausted": "replan", "max_replans": 1, "nodes": [_code_node("check-a", "check.py")]}
    result = replan.run_with_replans(tmp_path, spec, run_id="r-c", host="mock", trust_cli=False)
    assert result["final_status"] == "REPLAN_OUT_OF_SCOPE" and len(result["replans"]) == 1


def test_same_failure_twice_stops_as_stalled(tmp_path: Path, monkeypatch):
    from ego.usecases import replan
    _replan_project(tmp_path)
    (tmp_path / ".ego").mkdir(exist_ok=True)
    (tmp_path / ".ego/config.toml").write_text("max_replans = 2\n")
    monkeypatch.setattr(replan, "plan_work", lambda *a, **kw: {"task": {"goal": "g", "nodes": [_code_node("check-a", "check.py")]}})
    spec = {"goal": "g", "on_exhausted": "replan", "max_replans": 2, "nodes": [_code_node("check-a", "check.py")]}
    result = replan.run_with_replans(tmp_path, spec, run_id="r-d", host="mock", trust_cli=False)
    assert result["final_status"] == "STALLED" and len(result["replans"]) == 1


def test_planner_tokens_are_charged_to_the_run_budget(tmp_path: Path, monkeypatch):
    from ego.usecases import replan
    _replan_project(tmp_path)
    (tmp_path / ".ego").mkdir(exist_ok=True)
    (tmp_path / ".ego/config.toml").write_text("max_run_tokens = 1000\n")
    usage = {"input_tokens": 2000, "output_tokens": 0}
    monkeypatch.setattr(replan, "plan_work", lambda *a, **kw: {"task": {"goal": "g", "nodes": [_code_node("make-a", "make_a.py")]}, "planner_usage": usage})
    spec = {"goal": "g", "on_exhausted": "replan", "max_replans": 1, "nodes": [_code_node("check-a", "check.py")]}
    result = replan.run_with_replans(tmp_path, spec, run_id="r-e", host="mock", trust_cli=False)
    assert result["final_status"] == "BUDGET_EXHAUSTED"


def test_has_parallel_writers_only_for_unordered_writer_pairs():
    def node(nid, deps=(), kind="agent", role=None):
        n = {"id": nid, "kind": kind, "depends_on": list(deps)}
        if role:
            n["role"] = role
        return n

    # A serial chain of writers never runs two writers at once.
    assert work._has_parallel_writers([node("a"), node("b", ["a"]), node("c", ["b"])]) is False
    # Two independent writers can run at the same time.
    assert work._has_parallel_writers([node("a"), node("b")]) is True
    # A reviewer and a gate never write, so they do not count as writers.
    assert work._has_parallel_writers([node("a"), node("r", ["a"], role="reviewer"), node("g", kind="gate")]) is False
    # d depends only on a, so it can run alongside b and its descendant c.
    assert work._has_parallel_writers([node("a"), node("b", ["a"]), node("c", ["b"]), node("d", ["a"])]) is True


def test_auto_isolation_uses_worktree_only_for_parallel_writers(tmp_path: Path):
    from unittest.mock import patch
    assert default_config().isolation == "auto"
    _init_git_repo(tmp_path, {"seed.txt": "seed\n"})

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        node_id = re.search(r"NODE:(\w+)", prompt).group(1)
        (Path(cwd) / f"{node_id}.txt").write_text("changed\n")
        return RunResult("PASS", "ok", "", 0, 0.0, host)

    def writer(nid, deps=()):
        return {"id": nid, "kind": "agent", "intent": f"NODE:{nid}", "prompt": f"NODE:{nid}", "depends_on": list(deps), "write_allowlist": ["**"]}

    serial = {"goal": "serial writers", "nodes": [writer("s1"), writer("s2", ["s1"])]}
    parallel = {"goal": "parallel writers", "nodes": [writer("p1"), writer("p2")]}

    with patch("ego.usecases.work.run_agent", side_effect=fake_agent):
        serial_res = work._execute(tmp_path, serial, work.Progress(tmp_path / "progress.jsonl"), apply=False, cleanup_worktree=False)
        parallel_res = work._execute(tmp_path, parallel, work.Progress(tmp_path / "progress.jsonl"), apply=False, cleanup_worktree=False)

    assert serial_res["final_status"] == "PASSED" and serial_res["worktree"] is False
    assert parallel_res["final_status"] == "PASSED" and parallel_res["worktree"] is True
