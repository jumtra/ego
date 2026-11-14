"""The shipped no-pip entrypoint and included demo must run as documented."""
from pathlib import Path
import json
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'run_ego.py'
DEMO = ROOT / 'examples' / 'demo_project'


def call(*argv):
    p = subprocess.run([sys.executable, str(ENTRY), *map(str, argv)], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, (p.stdout, p.stderr)
    return json.loads(p.stdout) if p.stdout.startswith('{') else p.stdout


def test_entrypoint_without_install():
    assert 'ego 0.0.14' in call('--version')
    data = call('optimize', 'status')
    assert data['mode'] == 'off'
    assert data['fast_path_extra_model_calls'] == 0


def test_packaged_demo_offline(tmp_path):
    project = tmp_path / 'project'
    shutil.copytree(DEMO, project)
    init = call('init', '--root', project, '--apply')
    assert init['status'] == 'APPLIED'
    work = call('work', 'run', '--root', project, '--task', project / 'work-task.json', '--host', 'mock', '--apply', '--approve')
    assert work['final_status'] == 'PASSED'
    assert (project / 'result.txt').read_text() == 'OK\n'
    ev = call('eval', 'run', '--root', project, '--suite', project / 'eval-suite.json', '--host', 'mock')
    assert ev['all_passed'] is True


def test_harness_squared_full_offline_nonpromoting(tmp_path):
    project = tmp_path / 'project'
    shutil.copytree(DEMO, project)
    call('init', '--root', project, '--apply')
    call('optimize','configure','--root',project,'--mode','shadow','--profile','full','--max-candidates','3','--max-probe-runs','6','--apply')
    result = call('optimize','full','--root',project,'--fixture',project / 'eval-suite.json', '--host','mock','--rounds','1','--execute','--spend-approval','human:offline-lab-test')
    assert result['status'] == 'COMPLETED_WITHOUT_PROMOTION'
    assert len(result['rounds']) == 1
