import json
from pathlib import Path

from ego import planner
from ego.config import Config
from ego.install import apply_install
from ego.runner import RunResult
from ego.templates import skill_md
from ego.util import sha256, write_json


def test_work_entry_delegates_managed_execution_and_keeps_approvals():
    from ego.templates import WORK_REFERENCES
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


def test_standard_work_skill_upgrade_refreshes_all_host_copies(tmp_path: Path):
    from ego.templates import LEGACY_WORK_HASHES
    cfg = Config()
    apply_install(tmp_path, cfg)
    canonical = tmp_path / '.ego/spec/skills/work/SKILL.md'
    old = (Path(__file__).parent / 'fixtures/work-v004.md').read_text()
    assert sha256(old) in LEGACY_WORK_HASHES
    canonical.write_text(old)
    import json
    ownership = json.loads((tmp_path / '.ego/ownership.json').read_text())
    ownership['files']['.ego/spec/skills/work/SKILL.md'] = sha256(old)
    write_json(tmp_path / '.ego/ownership.json', ownership)
    apply_install(tmp_path, cfg)
    assert canonical.read_text() == skill_md('work')
    assert (tmp_path / '.agents/skills/work/SKILL.md').read_text() == skill_md('work')


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
    from ego.templates import WORK_REFERENCES
    plan = skill_md('work-plan')
    assert 'do not plan it again with `ego work plan`' in plan
    assert 'ego work run --root . --task' in plan and '`ego work loop --task`' in plan
    assert 'approved a `work-plan` graph' in WORK_REFERENCES['managed']
    assert '--task <task-file>' in WORK_REFERENCES['loop'] and 'departs from the approved plan' in WORK_REFERENCES['loop']


def test_v0014_work_plan_skill_refreshes(tmp_path: Path):
    from ego.templates import LEGACY_WORK_PLAN_HASHES
    cfg = Config()
    apply_install(tmp_path, cfg)
    canonical = tmp_path / '.ego/spec/skills/work-plan/SKILL.md'
    old = (Path(__file__).parent / 'fixtures/work-plan-v0014.md').read_text()
    assert sha256(old) in LEGACY_WORK_PLAN_HASHES
    assert sha256(skill_md('work-plan')) not in LEGACY_WORK_PLAN_HASHES
    canonical.write_text(old)
    ownership = json.loads((tmp_path / '.ego/ownership.json').read_text())
    ownership['files']['.ego/spec/skills/work-plan/SKILL.md'] = sha256(old)
    write_json(tmp_path / '.ego/ownership.json', ownership)
    apply_install(tmp_path, cfg)
    assert canonical.read_text() == skill_md('work-plan')
    assert (tmp_path / '.claude/skills/work-plan/SKILL.md').read_text() == skill_md('work-plan')


def test_v0015_work_plan_skill_refreshes(tmp_path: Path):
    from ego.templates import LEGACY_WORK_PLAN_HASHES
    cfg = Config()
    apply_install(tmp_path, cfg)
    canonical = tmp_path / '.ego/spec/skills/work-plan/SKILL.md'
    old = (Path(__file__).parent / 'fixtures/work-plan-v0015.md').read_text()
    assert sha256(old) in LEGACY_WORK_PLAN_HASHES
    assert sha256(skill_md('work-plan')) not in LEGACY_WORK_PLAN_HASHES
    canonical.write_text(old)
    ownership = json.loads((tmp_path / '.ego/ownership.json').read_text())
    ownership['files']['.ego/spec/skills/work-plan/SKILL.md'] = sha256(old)
    write_json(tmp_path / '.ego/ownership.json', ownership)
    apply_install(tmp_path, cfg)
    assert canonical.read_text() == skill_md('work-plan')


def test_v0017_work_plan_skill_refreshes_to_rerun_only_corrected_nodes(tmp_path: Path):
    from ego.templates import LEGACY_WORK_PLAN_HASHES, WORK_REFERENCES
    cfg = Config()
    apply_install(tmp_path, cfg)
    canonical = tmp_path / '.ego/spec/skills/work-plan/SKILL.md'
    old = (Path(__file__).parent / 'fixtures/work-plan-v0017.md').read_text()
    assert sha256(old) in LEGACY_WORK_PLAN_HASHES and '--from' not in old
    canonical.write_text(old)
    ownership = json.loads((tmp_path / '.ego/ownership.json').read_text())
    ownership['files']['.ego/spec/skills/work-plan/SKILL.md'] = sha256(old)
    write_json(tmp_path / '.ego/ownership.json', ownership)
    apply_install(tmp_path, cfg)
    assert canonical.read_text() == skill_md('work-plan')
    assert '--from run-<slug>' in skill_md('work-plan') and '--from <failed-run-id>' in WORK_REFERENCES['managed']


def test_v0018_work_plan_skill_refreshes(tmp_path: Path):
    from ego.templates import LEGACY_WORK_PLAN_HASHES
    cfg = Config()
    apply_install(tmp_path, cfg)
    canonical = tmp_path / '.ego/spec/skills/work-plan/SKILL.md'
    old = (Path(__file__).parent / 'fixtures/work-plan-v0018.md').read_text()
    assert sha256(old) in LEGACY_WORK_PLAN_HASHES and 'INTERRUPTED' not in old and 'RUN_ACTIVE' not in old
    canonical.write_text(old)
    ownership = json.loads((tmp_path / '.ego/ownership.json').read_text())
    ownership['files']['.ego/spec/skills/work-plan/SKILL.md'] = sha256(old)
    write_json(tmp_path / '.ego/ownership.json', ownership)
    apply_install(tmp_path, cfg)
    assert canonical.read_text() == skill_md('work-plan')
    assert 'ego session checkpoint --task-id <slug>' in skill_md('work-plan')
    assert 'INTERRUPTED' in skill_md('work-plan') and 'RUN_ACTIVE' in skill_md('work-plan')


def test_work_plan_executes_through_managed_with_progress():
    from ego.templates import WORK_REFERENCES
    plan = skill_md('work-plan')
    for needle in ('Do not edit project files yourself', 'ego work watch', '--run-id', 'next_action', 'Monitor'):
        assert needle in plan
    assert 'ego work watch <id>' in WORK_REFERENCES['managed']


def test_instruction_block_overrides_external_planning():
    from ego.templates import instruction_block
    block = instruction_block(Config())
    assert 'superpowers:brainstorming' in block and 'overrides them' in block


def test_v0012_work_skill_refreshes():
    from ego.templates import LEGACY_WORK_HASHES
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
    from ego.work import execute
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
    from ego import work
    from ego.config import Config, save_config
    from ego.detect import detect, discover_agents
    from ego.task_routing import build_task_prompt, validate_node_notes
    from ego.util import EgoError

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
        {'goal': 'Add alter support', 'memory': 'alter'},
        {'id': 'n1', 'prompt': 'Implement feature', 'agent': 'domain-expert', 'mcp': ['alter']},
        tmp_path,
    )
    assert '## Assigned custom agent' in prompt and 'domain-expert' in prompt
    assert 'Always verify domain terms.' in prompt
    assert '## Required MCP / Knowledge context' in prompt
    assert 'alter_task_context' in prompt

    # work.execute passes lean=False when cfg.memory == 'alter'
    save_config(tmp_path, Config(memory='alter', worker_context='lean'))
    captured_lean = []

    def fake_run_worker(host, p, cwd, timeout=180, **kw):
        captured_lean.append(kw.get('lean'))
        return RunResult('PASS', 'ok', '', 0, 0.0, host)

    monkeypatch.setattr(work, 'run_worker', fake_run_worker)
    res = work.execute(tmp_path, {'goal': 'g', 'nodes': [{'id': 'n1', 'kind': 'agent', 'prompt': 'p', 'write_allowlist': ['src/**']}]}, host='claude', trust_cli=True)
    assert res['final_status'] == 'PASSED'
    assert captured_lean == [False]

    # planner.plan_work passes lean=False and includes custom agents when cfg.memory == 'alter'
    captured_plan = {}

    def fake_plan_agent(host, p, cwd, timeout=180, **kw):
        captured_plan['prompt'] = p
        captured_plan['lean'] = kw.get('lean')
        return RunResult('PASS', json.dumps({'goal': 'g', 'nodes': [{'id': 'n1', 'kind': 'agent', 'prompt': 'p', 'write_allowlist': ['src/**']}]}), '', 0, 0.0, host)

    monkeypatch.setattr(planner, 'run_agent', fake_plan_agent)
    planner.plan_work(tmp_path, 'g', host='claude', execute=True)
    assert captured_plan['lean'] is False
    assert 'domain-expert' in captured_plan['prompt']
    assert 'alter' in captured_plan['prompt']
