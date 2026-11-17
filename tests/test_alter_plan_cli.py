from __future__ import annotations
from pathlib import Path

from ego.adapters.alter_plan import task_to_spec
from ego.cli import main
from ego.shared.util import read_json, write_json
from ego.usecases import alter_plan


class FakeAlterPlanClient:
    plan = {}

    def __init__(self, root: Path, timeout: int = 30):
        self.root = root

    def scope(self) -> str:
        return 'personal'

    def create(self, spec: dict, scope: str) -> dict:
        return {'plan_id': 'p1', 'node_tasks': {'impl': 't1'}, 'state': 'draft'}

    def get(self, plan_id: str) -> dict:
        return dict(FakeAlterPlanClient.plan)

    def claim(self, plan_id: str, run_id: str, spec_hash: str) -> dict:
        return {'state': 'taken'}


def _task() -> dict:
    return {'goal': 'g', 'done': 'd', 'instructions': '', 'nodes': [
        {'id': 'impl', 'kind': 'agent', 'role': 'coder', 'prompt': 'do it', 'intent': 'it works',
         'requirement': {'who': 'user', 'what': 'login', 'when': 'always', 'where': 'web', 'why': 'access'}, 'steps': ['add handler'],'write_allowlist': ['src/a.py'], 'done_when': [],
         'checks': [['pytest', '-q']], 'depends_on': []}]}


def _write_task(tmp_path: Path) -> Path:
    path = tmp_path / 'demo.json'
    write_json(path, _task())
    return path


def test_push_writes_plan_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(alter_plan, 'AlterPlanClient', FakeAlterPlanClient)
    task = _write_task(tmp_path)
    assert main(['work', 'alter-plan', 'push', '--task', str(task), '--root', str(tmp_path)]) == 0
    assert (tmp_path / '.ego' / 'state' / 'alter_plans' / 'demo.json').exists()


def test_pull_of_draft_plan_exits_nonzero_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(alter_plan, 'AlterPlanClient', FakeAlterPlanClient)
    FakeAlterPlanClient.plan = {'plan_id': 'p1', 'state': 'draft', 'spec': task_to_spec(_task(), 'demo'),
                                'spec_hash': 'h1', 'runs': [], 'errors': []}
    out = tmp_path / 'task.json'
    assert main(['work', 'alter-plan', 'pull', 'p1', '--out', str(out), '--run-id', 'r1', '--root', str(tmp_path)]) != 0
    assert not out.exists()


def test_pull_of_ready_plan_writes_task_file(tmp_path, monkeypatch):
    monkeypatch.setattr(alter_plan, 'AlterPlanClient', FakeAlterPlanClient)
    FakeAlterPlanClient.plan = {'plan_id': 'p1', 'state': 'ready', 'spec': task_to_spec(_task(), 'demo'),
                                'spec_hash': 'h1', 'runs': [], 'errors': []}
    out = tmp_path / 'task.json'
    assert main(['work', 'alter-plan', 'pull', 'p1', '--out', str(out), '--run-id', 'r1', '--root', str(tmp_path)]) == 0
    assert read_json(out)['source'] == {'alter_plan': {'plan_id': 'p1', 'spec_hash': 'h1', 'slug': 'demo'}}
