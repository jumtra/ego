"""Work flows: run/resume/--from rerun, watch/cancel, parallel staging, worktree isolation, judge gate, worker rotation, loop/planner."""
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
from ego.domain.config import Config
from ego.domain.config import Config, load_config
from ego.domain.config import Config, load_config, save_config
from ego.domain.judge import parse_verdict
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
from ego.usecases.loop import DONE_NODE, history_text, run_loop
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
    apply_install(tmp_path, Config())
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
    assert 'ego work run --root . --task' in plan and '`ego work loop --task`' in plan
    assert 'approved a `work-plan` graph' in WORK_REFERENCES['managed']
    assert '--task <task-file>' in WORK_REFERENCES['loop'] and 'departs from the approved plan' in WORK_REFERENCES['loop']


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
    cfg = Config()
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
    assert result['task']['nodes'][0]['steps'] == ['Edit src/ego/x.py function f: return 1']


def test_custom_agent_discovery_task_routing_and_non_lean_alter(tmp_path: Path, monkeypatch):
    import pytest
    from ego.usecases import work
    from ego.domain.config import Config, save_config
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
    save_config(tmp_path, Config(knowledge='alter', worker_context='lean'))
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
    from ego.domain.config import Config
    apply_install(root, Config())
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True, check=True)
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


def test_lean_worker_drops_inherited_mcp_and_carries_turn_and_budget_limits(monkeypatch, tmp_path: Path):
    _fake_claude(monkeypatch, tmp_path, "--output-format --max-turns --permission-mode --strict-mcp-config --mcp-config --max-budget-usd auto")
    argv = hosts.build_command("claude", "p", read_only=False, max_turns=30, lean=True, max_budget_usd=1.5, use_stdin=True)
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv
    assert argv[argv.index("--max-turns") + 1] == "30"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.50"
    inherit = hosts.build_command("claude", "p", read_only=False, use_stdin=True)
    assert "--strict-mcp-config" not in inherit and "--max-budget-usd" not in inherit


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
    argv = hosts.build_command("claude", "p", read_only=False, lean=True, max_budget_usd=1.0, use_stdin=True)
    assert "--strict-mcp-config" not in argv and "--max-budget-usd" not in argv


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
    assert seen["max_turns"] == Config().max_turns == 30 and seen["lean"] is True
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
    gate = {"id": "gate", "kind": "gate", "depends_on": ["a"], "argv": ["python3", "noop.py"]}
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


def test_loop_replan_keeps_verified_agent_nodes_without_running_them_again(tmp_path: Path, monkeypatch):
    _loop_project(tmp_path)
    ran = _writing_agent(monkeypatch)
    histories = []

    def planner(goal, history, workdir):
        histories.append(history)
        return {"goal": goal, "nodes": [_wagent("make-a", "a.txt=A") if history is None else _wagent("make-b", "b.txt=B")]}

    result = run_loop(tmp_path, "produce a and b", done_checks=[["python3", "check.py"]], planner=planner,
                      scope=["*.txt"], max_iterations=3)
    assert result["status"] == "VERIFIED"
    assert ran == ["make-a", "make-b"]
    assert "verified nodes (kept, not re-run): make-a" in histories[1]
    record = json.loads((tmp_path / ".ego/state/runs" / result["iterations"][1]["run_id"] / "record.json").read_text())
    assert record["carried"] == ["make-a"] and [n["id"] for n in record["spec"]["nodes"]] == ["make-a", "make-b", DONE_NODE]


def test_loop_planner_may_redo_a_verified_node_by_reusing_its_id(tmp_path: Path, monkeypatch):
    _loop_project(tmp_path)
    ran = _writing_agent(monkeypatch)

    def planner(goal, history, workdir):
        if history is None:
            return {"goal": goal, "nodes": [_wagent("make-a", "a.txt=A")]}
        return {"goal": goal, "nodes": [_wagent("make-a", "a.txt=A WRITE b.txt=B")]}

    result = run_loop(tmp_path, "produce a and b", done_checks=[["python3", "check.py"]], planner=planner,
                      scope=["*.txt"], max_iterations=3)
    assert result["status"] == "VERIFIED" and ran == ["make-a", "make-a"]


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
    from ego.usecases.loop import apply_loop
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


def test_first_iteration_skips_the_planner_and_replans_only_after_failure(tmp_path: Path, monkeypatch):
    from ego.usecases import planner as planner_module
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


def test_judge_cannot_write(project, writing_agent):
    task = {"nodes": [node("c", role="coder", prompt="p"),
                      {"id": "g", "kind": "gate", "depends_on": ["c"], "argv": ["python3", "--version"]},
                      {"id": "j", "role": "judge", "depends_on": ["g"], "reviews": "c",
                       "prompt": "[[WRITE:src/j.py]]VERDICT: APPROVE"}]}
    # Judge write: the node itself must not carry an allowlist, so the write is refused by role.
    result = execute(project, task)
    assert result["node_status"]["j"] == "FAILED"


def judged(verdict_text, **jkw):
    return {"nodes": [node("c", role="coder", prompt="[[WRITE:src/a.py]]ok"),
                      {"id": "g", "kind": "gate", "depends_on": ["c"], "argv": ["python3", "--version"]},
                      {"id": "j", "role": "judge", "depends_on": ["g"], "reviews": "c",
                       "prompt": "[[MOCK:" + verdict_text + "]]", **jkw}]}


def test_judge_approve_passes(project, writing_agent):
    assert execute(project, judged("VERDICT: APPROVE"))["final_status"] == "PASSED"


def test_judge_reject_without_evidence_is_approved(project, writing_agent):
    assert execute(project, judged("- looks wrong\nVERDICT: REJECT"))["final_status"] == "PASSED"


def test_judge_reject_with_evidence_retries_coder_locally_then_fails(project, writing_agent):
    result = execute(project, judged("- src/a.py:1: spec omitted\nVERDICT: REJECT"))
    assert result["final_status"] == "FAILED"
    assert result["judge_retries"] == {"j": 1}
    assert len(result["results"]["c"]["attempts"]) == 1 and result["node_status"]["c"] == "PASSED"
    again = execute(project, judged("- src/a.py:1: spec omitted\nVERDICT: REJECT", max_retries=0))
    assert "j" not in again["judge_retries"] or again["judge_retries"]["j"] == 0


def test_judge_needs_reviews_and_a_gate():
    base = judged("VERDICT: APPROVE")
    no_reviews = {"nodes": [dict(n) for n in base["nodes"]]}
    del no_reviews["nodes"][2]["reviews"]
    with pytest.raises(EgoError, match="reviews"):
        validate_task(no_reviews)
    no_gate = {"nodes": [base["nodes"][0], {**base["nodes"][2], "depends_on": ["c"]}]}
    with pytest.raises(EgoError, match="gate or code"):
        validate_task(no_gate)


def test_failed_gate_means_judge_never_runs(project, writing_agent):
    task = judged("VERDICT: APPROVE")
    (project / "fail.py").write_text("raise SystemExit(1)\n")
    task["nodes"][1]["argv"] = ["python3", "fail.py"]
    result = execute(project, task)
    assert result["final_status"] == "FAILED" and "j" not in result["node_status"]


def test_parallel_conflict_is_not_applied(project):
    (project/"write1.py").write_text("from pathlib import Path\nPath('shared.txt').write_text('one')\n")
    (project/"write2.py").write_text("from pathlib import Path\nPath('shared.txt').write_text('two')\n")
    spec={"nodes":[{"id":"a","kind":"code","argv":["python3","write1.py"],"write_allowlist":["shared.txt"]},
                   {"id":"b","kind":"code","argv":["python3","write2.py"],"write_allowlist":["shared.txt"]}]}
    with pytest.raises(EgoError):execute(project,spec,host="mock",apply=True,approve=True)
    assert not (project/"shared.txt").exists()
