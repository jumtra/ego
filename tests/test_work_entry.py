from pathlib import Path

from ego.config import Config
from ego.install import apply_install
from ego.templates import skill_md
from ego.util import sha256, write_json


def test_work_entry_delegates_managed_execution_and_keeps_approvals():
    work = skill_md('work')
    assert 'seh work plan' in work
    assert 'seh work run' in work
    assert 'seh work resume' in work
    assert 'Do not recursively invoke /work' in work
    assert 'Never infer approval' in work
    assert 'seh memory search' in work
    assert 'seh memory add' in work


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
