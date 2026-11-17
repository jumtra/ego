from __future__ import annotations
from pathlib import Path

import pytest

from ego.usecases import alter_plan
from ego.shared.util import EgoError


class FakeAlterPlanClient:
    plan = {}
    claims = []

    def __init__(self, root: Path, timeout: int = 30):
        self.root = root

    def get(self, plan_id: str) -> dict:
        return dict(FakeAlterPlanClient.plan)

    def claim(self, plan_id: str, run_id: str, spec_hash: str) -> dict:
        FakeAlterPlanClient.claims.append((plan_id, run_id, spec_hash))
        return {'state': 'taken'}


def _spec() -> dict:
    return {'slug': 'demo', 'goal': 'g', 'done': 'd', 'instructions': '',
            'extra': {}, 'nodes': [{'node_id': 'impl', 'prompt': 'do it', 'depends_on': [], 'intent': '',
                                    'requirement': {}, 'steps': [], 'where': ['src/a.py'], 'done_when': [],
                                    'checks': ['pytest -q'], 'non_goals': [], 'assumptions': [], 'provides': [],
                                    'extra': {}}]}


@pytest.fixture
def fake(monkeypatch):
    FakeAlterPlanClient.claims = []
    FakeAlterPlanClient.plan = {'plan_id': 'p1', 'state': 'draft', 'spec': _spec(), 'spec_hash': 'h1', 'runs': [], 'errors': []}
    monkeypatch.setattr(alter_plan, 'AlterPlanClient', FakeAlterPlanClient)
    return FakeAlterPlanClient


def test_pull_of_draft_is_refused_and_writes_nothing(fake, tmp_path):
    out = tmp_path / 'task.json'
    with pytest.raises(EgoError) as exc:
        alter_plan.pull(tmp_path, 'p1', str(out), 'r1')
    assert exc.value.code == 'ALTER_PLAN_NOT_READY'
    assert not out.exists()
    assert fake.claims == []


def test_pull_of_ready_plan_writes_task_and_claims_with_spec_hash(fake, tmp_path):
    fake.plan['state'] = 'ready'
    out = tmp_path / 'task.json'
    result = alter_plan.pull(tmp_path, 'p1', str(out), 'r1')
    task = alter_plan.read_json(out)
    assert task['source'] == {'alter_plan': {'plan_id': 'p1', 'spec_hash': 'h1', 'slug': 'demo'}}
    assert fake.claims == [('p1', 'r1', 'h1')]
    assert result == {'task_file': str(out), 'run_id': 'r1', 'from_run': None, 'nodes': 1}


def test_pull_after_failed_last_run_returns_from_run(fake, tmp_path):
    fake.plan.update(state='ready', runs=[{'run_id': 'old', 'state': 'failed', 'at': 'x'}])
    result = alter_plan.pull(tmp_path, 'p1', str(tmp_path / 'task.json'), 'r2')
    assert result['from_run'] == 'old'


def test_wait_returns_when_plan_becomes_ready(fake, tmp_path, monkeypatch):
    states = iter(['draft', 'ready'])
    events = []

    class Seq(FakeAlterPlanClient):
        def get(self, plan_id):
            return {'plan_id': plan_id, 'state': next(states)}

    monkeypatch.setattr(alter_plan, 'AlterPlanClient', Seq)
    result = alter_plan.wait(tmp_path, 'p1', interval=0, write=events.append, sleep=lambda s: None)
    assert result['state'] == 'ready'
    assert [('"draft"' in e, '"ready"' in e) for e in events] == [(True, False), (False, True)]


def test_report_payload_maps_failed_run_to_failed_state():
    record = {'final_status': 'FAILED', 'results': {'impl': {'status': 'FAILED', 'attempts': [
        {'checks': [{'status': 'FAIL', 'output': 'boom'}]}]}}}
    state, nodes = alter_plan.report_payload(record)
    assert state == 'failed'
    assert nodes == {'impl': {'status': 'FAILED', 'attempts': 1, 'failure': 'boom'}}
