"""First-run UX, opt-in apply, ownership, safe removal and rollback."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import pytest

from seh.config import Config
from seh.install import installation_plan, apply_install, uninstallation_plan, apply_uninstall, rollback, MARK_BEGIN, OWNERSHIP
from seh.setup import resolve_setup, setup_command, uninstall_command, status_command
from seh.util import SEHError, filehash

RUNNER = Path(__file__).resolve().parents[1] / 'run_seh.py'


def call(root: Path, *arguments: str):
    return subprocess.run([sys.executable, str(RUNNER), *arguments, '--root', str(root)],
                          capture_output=True, text=True, timeout=20)


def test_preview_then_single_command_setup_is_idempotent(tmp_path):
    (tmp_path/'main.go').write_text('package main\n')
    preview = call(tmp_path, 'setup', '--hosts', 'claude,codex', '--non-interactive')
    assert preview.returncode == 0, preview.stderr
    assert 'Preview only' in preview.stdout
    assert not (tmp_path/'.harness').exists()
    applied = call(tmp_path, 'setup', '--hosts', 'claude,codex', '--yes')
    assert applied.returncode == 0, applied.stderr
    assert 'APPLIED:' in applied.stdout
    assert (tmp_path/'.claude/skills/work/SKILL.md').exists()
    assert (tmp_path/'.agents/skills/work/SKILL.md').exists()
    assert 'Languages: go' in applied.stdout
    repeated = call(tmp_path, 'setup', '--yes')
    assert repeated.returncode == 0, repeated.stderr
    assert 'UNCHANGED:' in repeated.stdout
    state = call(tmp_path, 'status', '--json')
    assert json.loads(state.stdout)['status'] == 'READY'
    assert not (tmp_path/'.harness/optimization.toml').exists()


def test_setup_json_noninteractive_requires_yes(tmp_path):
    p = call(tmp_path, 'setup', '--json', '--hosts', 'all')
    assert p.returncode == 0, p.stderr
    result = json.loads(p.stdout)
    assert result['status'] == 'PREVIEW'
    assert not (tmp_path/'AGENTS.md').exists()
    run = call(tmp_path, 'setup', '--json', '--hosts', 'all', '--yes')
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['status'] == 'APPLIED'
    assert (tmp_path/'AGENTS.md').exists()
    assert (tmp_path/'.opencode/agents/reviewer.md').exists()
    assert (tmp_path/'.codex/agents/reviewer.toml').exists()


def test_existing_user_notes_preserved_across_update_and_uninstall(tmp_path):
    (tmp_path/'CLAUDE.md').write_text('# custom notes\n')
    (tmp_path/'AGENTS.md').write_text('# agent preferences\n')
    assert call(tmp_path,'setup','--yes','--hosts','claude,codex').returncode == 0
    (tmp_path/'CLAUDE.md').write_text((tmp_path/'CLAUDE.md').read_text()+'\n# added later\n')
    assert status_command(tmp_path)['status'] == 'READY'
    assert call(tmp_path,'setup','--yes').returncode == 0
    uninstall = call(tmp_path,'uninstall','--yes','--json')
    assert uninstall.returncode == 0, uninstall.stderr
    assert json.loads(uninstall.stdout)['status'] == 'UNINSTALLED'
    assert MARK_BEGIN not in (tmp_path/'CLAUDE.md').read_text()
    assert '# custom notes' in (tmp_path/'CLAUDE.md').read_text()
    assert '# added later' in (tmp_path/'CLAUDE.md').read_text()
    assert (tmp_path/'AGENTS.md').read_text() == '# agent preferences\n'
    assert not (tmp_path/OWNERSHIP).exists()
    assert not (tmp_path/'.claude/skills/work/SKILL.md').exists()
    assert (tmp_path/'.harness/backups').exists()


def test_refuses_to_replace_user_owned_skill(tmp_path):
    path=tmp_path/'.claude/skills/work/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('user-created',encoding='utf8')
    preview=call(tmp_path,'setup','--hosts','claude','--json')
    assert preview.returncode == 4
    assert json.loads(preview.stdout)['status']=='CONFLICT'
    applied=call(tmp_path,'setup','--hosts','claude','--yes')
    assert applied.returncode == 4
    assert path.read_text()=='user-created'


def test_config_updated_only_if_owned(tmp_path):
    path=tmp_path/'.harness/config.toml'
    path.parent.mkdir(parents=True)
    from seh.config import dump_config
    path.write_text(dump_config(Config()),encoding='utf8')
    # Existing exact configuration is never adopted silently as SEH-owned.
    code, output=setup_command(tmp_path,hosts='all',json_format=True,non_interactive=True)
    assert code['status']=='PREVIEW'
    result,_=setup_command(tmp_path,hosts='all',yes=True,json_format=True)
    assert '.harness/config.toml' not in json.loads((tmp_path/OWNERSHIP).read_text())['files']
    refused=call(tmp_path,'setup','--profile','full','--json','--yes')
    assert refused.returncode == 4
    assert path.read_text()==dump_config(Config())


def test_only_approved_host_instruction_blocks_removed(tmp_path):
    (tmp_path/'CLAUDE.md').write_text('Important project guide\n')
    assert call(tmp_path,'setup','--hosts','claude,codex','--yes').returncode == 0
    updated=call(tmp_path,'setup','--hosts','codex','--yes')
    assert updated.returncode == 0, updated.stderr
    assert MARK_BEGIN not in (tmp_path/'CLAUDE.md').read_text()
    assert 'Important project guide' in (tmp_path/'CLAUDE.md').read_text()
    assert '.harness' in (tmp_path/'AGENTS.md').read_text()
    assert 'CLAUDE.md' not in json.loads((tmp_path/OWNERSHIP).read_text())['files']


def test_setup_detects_host_when_present(tmp_path, monkeypatch):
    import seh.setup as setup
    from seh.hosts import HostCapability
    def fake_capability(host):
        return HostCapability(host, '/tmp/'+host if host in ('codex','opencode') else None,
                              host in ('codex','opencode'),host in ('codex','opencode'),
                              'cli','fake')
    monkeypatch.setattr(setup,'capability',fake_capability)
    cfg, detected, warnings=resolve_setup(tmp_path,profile=None,hosts=None,graph=None,memory=None,scan=False)
    assert cfg.hosts==['codex','opencode']
    assert not warnings


def test_setup_wizard_injected_interactive(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty',lambda:True)
    answers=iter(['claude,codex','minimal','none','file','y'])
    result, code=setup_command(tmp_path,ask=lambda text: next(answers))
    assert code==0
    assert result['status']=='APPLIED'
    assert (tmp_path/'.agents/skills/work/SKILL.md').exists()
    assert not (tmp_path/'.claude/settings.json').exists()


def test_uninstall_preview_and_rollback_restore(tmp_path):
    initial = apply_install(tmp_path,Config(hosts=['claude']))
    preview=call(tmp_path,'uninstall','--json')
    assert json.loads(preview.stdout)['status']=='PREVIEW'
    assert (tmp_path/'.claude').exists()
    removed=apply_uninstall(tmp_path)
    assert removed['status']=='UNINSTALLED'
    assert status_command(tmp_path)['status']=='NOT_INSTALLED'
    assert rollback(tmp_path,removed['snapshot_id'])['status']=='ROLLED_BACK'
    assert status_command(tmp_path)['status']=='READY'
    assert rollback(tmp_path,initial['snapshot_id'])['status']=='ROLLED_BACK'
    assert not (tmp_path/OWNERSHIP).exists()


def test_rollback_rejects_unexpected_postinstall_user_edit(tmp_path):
    applied=apply_install(tmp_path,Config(hosts=['claude']))
    path=tmp_path/'.claude/skills/work/SKILL.md'
    path.write_text('I changed this',encoding='utf8')
    with pytest.raises(SEHError) as exc:rollback(tmp_path,applied['snapshot_id'])
    assert exc.value.code=='ROLLBACK_CONFLICT'
    assert path.read_text()=='I changed this'


def test_uninstall_refuses_managed_block_edit(tmp_path):
    apply_install(tmp_path,Config(hosts=['claude']))
    path=tmp_path/'CLAUDE.md'
    path.write_text(path.read_text().replace('Use existing tools','Use different tools'),encoding='utf8')
    with pytest.raises(SEHError) as exc:apply_uninstall(tmp_path)
    assert exc.value.code=='OWNERSHIP_CONFLICT'
    assert (tmp_path/'.claude/skills/work/SKILL.md').exists()


def test_user_symlink_cannot_be_overwritten(tmp_path):
    outside=tmp_path.parent/'outside.txt'
    outside.write_text('outside')
    (tmp_path/'CLAUDE.md').symlink_to(outside)
    with pytest.raises(SEHError) as exc:installation_plan(tmp_path,Config(hosts=['claude']))
    assert exc.value.code=='SYMLINK_DENIED'
    assert outside.read_text()=='outside'


def test_domain_knowledge_remains_user_data(tmp_path):
    assert call(tmp_path, 'setup', '--yes', '--hosts', 'claude').returncode == 0
    domain = tmp_path / '.harness/knowledge/domain.md'
    domain.write_text('# verified facts\n- Project-specific design\n', encoding='utf8')
    assert call(tmp_path, 'setup', '--yes').returncode == 0
    assert call(tmp_path, 'uninstall', '--yes').returncode == 0
    assert domain.read_text() == '# verified facts\n- Project-specific design\n'
