from pathlib import Path
import json
import pytest

from ego.usecases.work import execute


class RecordingClient:
    calls: list = []
    fail: bool = False
    constructed: int = 0

    def __init__(self, root: Path, timeout: int = 30):
        type(self).constructed += 1

    def report(self, plan_id: str, run_id: str, state: str, nodes: dict):
        if type(self).fail:
            raise RuntimeError('409 conflict')
        type(self).calls.append((plan_id, run_id, state, nodes))
        return {'ok': True}


@pytest.fixture
def client(monkeypatch):
    RecordingClient.calls = []
    RecordingClient.fail = False
    RecordingClient.constructed = 0
    monkeypatch.setattr('ego.adapters.alter_plan.AlterPlanClient', RecordingClient)
    return RecordingClient


def _spec(tmp_path: Path, with_plan: bool) -> dict:
    (tmp_path / 'check.py').write_text('pass\n')
    spec = {'goal': 'Report progress', 'nodes': [
        {'id': 'validate', 'kind': 'code', 'argv': ['python3', 'check.py'], 'write_allowlist': ['result.txt']}
    ]}
    if with_plan:
        spec['source'] = {'alter_plan': {'plan_id': 'plan-1'}}
    return spec


def test_alter_plan_run_reports_running_then_passed_per_node(tmp_path: Path, client):
    result = execute(tmp_path, _spec(tmp_path, True), run_id='run-report', apply=True, approve=True)

    assert result['final_status'] == 'PASSED'
    states = [call[2] for call in client.calls]
    assert states[0] == 'running'
    assert states[-1] == 'passed'
    assert set(states[:-1]) == {'running'}
    plan_id, run_id, _, nodes = client.calls[-1]
    assert (plan_id, run_id) == ('plan-1', 'run-report')
    assert nodes['validate']['status'] == 'PASSED'


def test_alter_outage_never_changes_run_result_and_leaves_pending_marker(tmp_path: Path, client):
    client.fail = True

    result = execute(tmp_path, _spec(tmp_path, True), run_id='run-outage', apply=True, approve=True)

    assert result['final_status'] == 'PASSED'
    marker = tmp_path / '.ego' / 'state' / 'runs' / 'run-outage' / 'alter_sync.json'
    data = json.loads(marker.read_text(encoding='utf8'))
    assert data['pending'] is True
    assert data['state'] == 'passed'


def test_task_without_alter_plan_makes_no_client_calls(tmp_path: Path, client):
    result = execute(tmp_path, _spec(tmp_path, False), run_id='run-plain', apply=True, approve=True)

    assert result['final_status'] == 'PASSED'
    assert client.constructed == 0
    assert client.calls == []
