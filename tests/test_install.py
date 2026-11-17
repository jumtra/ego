"""Install and CLI business flows: apply/status/rollback/uninstall/init/sync, projects, hosts, models, self-update, and core-command smoke."""
from __future__ import annotations
from pathlib import Path
import hashlib
import io
import json
import os
import pytest
import shutil
import subprocess
import sys
import tomllib
import zipfile

from ego import __version__
from ego.adapters import hostcompat
from ego.adapters import selfupdate
from ego.adapters.codeintel import impact, affected_tests, search, health
from ego.adapters.detect import detect
from ego.adapters.hosts import build_command
from ego.adapters.hosts import capability
from ego.adapters.memory import MCPStdio
from ego.adapters.runner import parse_host_response
from ego.cli import _project_target
from ego.cli import main
from ego.domain.config import Config
from ego.domain.config import Config, load_config
from ego.domain.config import Config, load_config, save_config
from ego.domain.config import load_config
from ego.domain.config import load_config, save_config
from ego.domain.task_routing import resolve_routes, build_task_prompt
from ego.shared.util import EgoError
from ego.shared.util import EgoError, filehash
from ego.shared.util import EgoError, read_json, filehash
from ego.shared.util import EgoError, read_json, write_json
from ego.usecases.apply_wizard import should_prompt
from ego.usecases.install import OWNERSHIP, apply_install
from ego.usecases.install import apply_install
from ego.usecases.install import apply_install, installation_plan, rollback
from ego.usecases.install import apply_install, uninstall_project, uninstall_cli
from ego.usecases.planner import plan_work
from ego.usecases.projects import list_projects, project_info, registry_path, scan, sync_all, sync_project, unregister
from ego.usecases.work import execute, validate_task


def test_non_destructive_managed_block(tmp_path):
    root=tmp_path
    (root/"CLAUDE.md").write_text("# My notes\n",encoding="utf8")
    result=apply_install(root,Config())
    assert (root/"CLAUDE.md").read_text().startswith("# My notes")
    rollback(root,result["snapshot_id"])
    assert (root/"CLAUDE.md").read_text()=="# My notes\n"
    assert not (root/".agents/skills/work/SKILL.md").exists()


def test_owned_edit_conflict(project):
    path=project/".claude"/"skills"/"work"/"SKILL.md"
    path.write_text(path.read_text()+"\nCUSTOM",encoding="utf8")
    with pytest.raises(EgoError) as exc:
        apply_install(project,load_config(project))
    assert exc.value.code == "OWNERSHIP_CONFLICT"


def test_config_policy_mode(project):
    cfg = Config(policy_mode="relaxed")
    save_config(project, cfg)
    loaded = load_config(project)
    assert loaded.policy_mode == "relaxed"
    content = (project / ".ego/config.toml").read_text()
    assert 'policy_mode = "relaxed"' in content
    with pytest.raises(EgoError):
        save_config(project, Config(policy_mode="invalid"))


def test_config_models_support(project):
    cfg = Config(models={"claude": "sonnet-3.5", "codex": "gpt-5-codex", "opencode": "auto"})
    save_config(project, cfg)
    loaded = load_config(project)
    assert loaded.models == {"claude": "sonnet-3.5", "codex": "gpt-5-codex", "opencode": "auto"}
    content = (project / ".ego/config.toml").read_text()
    assert 'models = {claude = "sonnet-3.5", codex = "gpt-5-codex", opencode = "auto"}' in content
    assert "# モデル設定の例 (models):" in content

    # Validation failure on invalid host or model type
    with pytest.raises(EgoError):
        save_config(project, Config(models={"invalid_host": "sonnet"}))
    with pytest.raises(EgoError):
        save_config(project, Config(models={"claude": ""}))


def test_config_validate(project):
    bad=Config(max_parallel=99)
    with pytest.raises(EgoError):save_config(project,bad)
    path=project/".ego/config.toml"
    path.write_text("bogus = true\n")
    with pytest.raises(EgoError):load_config(project)


def test_recommended_defaults_and_sync_fallback(tmp_path):
    from ego.cli import main
    root = tmp_path / "fresh"
    root.mkdir()
    apply_install(root, Config())
    loaded = load_config(root)
    assert loaded.usage_switch_percent == 99
    assert loaded.run_yolo is True
    assert set(loaded.models) == {"claude", "codex", "opencode", "agy"}
    for role in ("coder", "tester", "judge"):
        assert set(loaded.roles[role]) == {"claude", "codex", "agy", "opencode"}

    # Existing config.toml without those keys inherits the recommended defaults on sync
    partial = tmp_path / "partial"
    partial.mkdir()
    apply_install(partial, Config())
    (partial / ".ego" / "config.toml").write_text('schema_version = 1\nprofile = "balanced"\n', encoding="utf-8")
    assert main(["sync", "--root", str(partial), "--apply"]) == 0
    synced = load_config(partial)
    assert synced.usage_switch_percent == 99
    assert synced.run_yolo is True
    assert "agy" in synced.models
    assert "agy" in synced.roles["coder"]


ENTRY = Path(__file__).resolve().parents[1] / "run_ego.py"


def cmd(root: Path, *argv: str, answers: str | None = None, env: dict | None = None):
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run([sys.executable, str(ENTRY), *argv],
                          cwd=root, input=answers, capture_output=True,
                          text=True, timeout=25, env=run_env)


def output(proc):
    return json.loads(proc.stdout)


def test_project_apply_lifecycle(tmp_path: Path):
    proj = tmp_path / "my_project"
    proj.mkdir()
    (proj / "main.py").write_text("print('hello')\n")

    # 1. Preview via dry-run without writing
    dry = cmd(proj, "apply", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    dry_data = output(dry)
    assert dry_data["status"] == "CHANGES_PENDING"
    assert not (proj / ".ego").exists()

    # 2. Check fails when changes are pending
    check_pending = cmd(proj, "apply", "--check")
    assert check_pending.returncode == 5

    # 3. Apply non-interactively
    applied = cmd(proj, "apply", "--non-interactive", "--hosts", "claude,codex", "--profile", "minimal")
    assert applied.returncode == 0, applied.stderr
    app_data = output(applied)
    assert app_data["status"] == "APPLIED"
    snapshot_id = app_data["snapshot_id"]
    assert (proj / ".ego" / "config.toml").exists()
    assert (proj / ".claude" / "skills" / "work" / "SKILL.md").exists()

    # 4. Check succeeds when up-to-date
    assert cmd(proj, "apply", "--check").returncode == 0
    status = output(cmd(proj, "status"))
    assert status["status"] == "UP_TO_DATE"
    assert status["installed_version"] == "0.0.35"
    assert status["last_snapshot_id"] == snapshot_id

    # 5. Rollback restores previous state
    rb = cmd(proj, "rollback")
    assert rb.returncode == 0, rb.stderr
    rb_data = output(rb)
    assert rb_data["status"] == "ROLLED_BACK"
    assert rb_data["snapshot_id"] == snapshot_id
    assert not (proj / ".claude" / "skills" / "work" / "SKILL.md").exists()


def test_apply_protects_home_and_root(tmp_path: Path):
    res = cmd(tmp_path, "apply", "--dry-run", env={"HOME": str(tmp_path)})
    assert res.returncode != 0
    assert "INVALID_PROJECT_ROOT" in res.stderr

    class DummyArgs:
        root = str(Path.home())
    with pytest.raises(EgoError) as exc:
        _project_target(DummyArgs())
    assert exc.value.code == "INVALID_PROJECT_ROOT"


def _apply(root: Path, **settings) -> Path:
    root.mkdir(parents=True)
    cfg = Config()
    for key, value in settings.items():
        setattr(cfg, key, value)
    apply_install(root, cfg)
    return root


def _age(root: Path, version: str = "0.0.1") -> None:
    """Make a project look as if an older ego rendered it."""
    data = read_json(root / OWNERSHIP)
    data["installed_version"] = version
    rel = ".claude/rules/testing.md"
    (root / rel).write_text("# Testing\n\nold text\n")
    from ego.shared.util import filehash
    data["files"][rel] = filehash(root / rel)
    write_json(root / OWNERSHIP, data)


def test_apply_registers_the_project_and_reports_its_version(tmp_path: Path):
    root = _apply(tmp_path / "a", clear_suggest_tokens=150000)
    assert str(root.resolve()) in read_json(registry_path())["projects"]
    info = project_info(root.resolve())
    assert info["installed_version"] == __version__ and info["status"] == "UP_TO_DATE"
    assert info["version_differs"] is False and info["clear_suggest_tokens"] == 150000
    _age(root)
    stale = list_projects()[0]
    assert stale["installed_version"] == "0.0.1" and stale["version_differs"] is True and stale["status"] == "CHANGES_PENDING"


def test_sync_all_updates_each_project_with_its_own_settings(tmp_path: Path):
    a = _apply(tmp_path / "a", clear_suggest_tokens=150000, hosts=["claude"])
    b = _apply(tmp_path / "b", policy_mode="relaxed")
    _age(a); _age(b)
    (b / ".claude/rules/engineering.md").write_text("user edit\n")  # not ego's any more: must block, not be overwritten
    preview = sync_all(apply=False)
    assert preview["counts"] == {"CHANGES_PENDING": 1, "BLOCKED": 1}
    assert "old text" in (a / ".claude/rules/testing.md").read_text()
    done = {Path(row["root"]).name: row for row in sync_all(apply=True)["projects"]}
    assert done["a"]["status"] == "APPLIED" and done["a"]["previous_version"] == "0.0.1" and done["a"]["installed_version"] == __version__
    assert "old text" not in (a / ".claude/rules/testing.md").read_text()
    kept = load_config(a)
    assert kept.clear_suggest_tokens == 150000 and kept.hosts == ["claude"]
    assert done["b"]["status"] == "BLOCKED" and (b / ".claude/rules/engineering.md").read_text() == "user edit\n"
    assert load_config(b).policy_mode == "relaxed"


def test_projects_cli_scan_add_remove_and_missing(tmp_path: Path, capsys):
    a = _apply(tmp_path / "work" / "a")
    b = _apply(tmp_path / "work" / "nested" / "b")
    assert unregister(a) and unregister(b)
    assert sorted(scan(tmp_path / "work")) == sorted([str(a.resolve()), str(b.resolve())])
    assert main(["projects", "remove", str(b)]) == 0
    capsys.readouterr()
    assert main(["projects", "add", str(tmp_path)]) == 3  # not an applied project
    capsys.readouterr()
    import shutil
    shutil.rmtree(b)
    assert main(["projects", "add", str(a)]) == 0
    capsys.readouterr()
    write_json(registry_path(), {"schema_version": 1, "projects": {str(a.resolve()): {}, str(b.resolve()): {}}})
    assert main(["projects", "list"]) == 0
    rows = {Path(r["root"]).name: r["status"] for r in json.loads(capsys.readouterr().out)["projects"]}
    assert rows == {"a": "UP_TO_DATE", "b": "MISSING"}
    assert main(["sync", "--all", "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {"UP_TO_DATE": 1, "MISSING": 1}


def test_status_shows_version_difference_and_apply_sets_the_clear_limit(tmp_path: Path, capsys):
    root = _apply(tmp_path / "a")
    assert main(["apply", "--root", str(root), "--non-interactive", "--clear-suggest-tokens", "90000"]) == 0
    capsys.readouterr()
    assert load_config(root).clear_suggest_tokens == 90000
    hooks = json.loads((root / ".claude/settings.json").read_text())["hooks"]
    assert set(hooks) == {"Stop", "SessionStart", "UserPromptSubmit", "SessionEnd"}  # memory = alter adds the prompt lookup and the session-end review
    _age(root, "0.0.2")
    assert main(["status", "--root", str(root)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["installed_version"] == "0.0.2" and status["harness_version"] == __version__ and status["version_differs"] is True


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
                          check=True, capture_output=True, text=True).stdout.strip()


def _release(repo: Path, version: str) -> None:
    (repo / "src/ego").mkdir(parents=True, exist_ok=True)
    (repo / "src/ego/__init__.py").write_text(f'__version__ = "{version}"\n')
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", version)


def test_git_checkout_updates_by_fast_forward_only(tmp_path: Path, monkeypatch):
    origin = tmp_path / "origin"; origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _release(origin, "1.0.0")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    assert selfupdate.git_update(clone, "1.0.0", apply=True, yes=True)["update_available"] is False
    _release(origin, "1.1.0")
    check = selfupdate.git_update(clone, "1.0.0")
    assert check["mode"] == "git" and check["latest"] == "1.1.0" and check["commits_behind"] == 1 and check["update_available"]
    assert "1.0.0" in (clone / "src/ego/__init__.py").read_text()  # a check changes nothing
    monkeypatch.setattr(selfupdate.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(EgoError, match="--yes"):
        selfupdate.git_update(clone, "1.0.0", apply=True)
    done = selfupdate.git_update(clone, "1.0.0", apply=True, yes=True)
    assert done["status"] == "INSTALLED" and done["version"] == "1.1.0" and done["commits_applied"] == 1
    assert "1.1.0" in (clone / "src/ego/__init__.py").read_text()
    # Local commits are never merged or discarded.
    _release(clone, "1.1.1-local")
    _release(origin, "1.2.0")
    with pytest.raises(EgoError, match="local commit"):
        selfupdate.git_update(clone, "1.1.0", apply=True, yes=True)
    assert "1.1.1-local" in (clone / "src/ego/__init__.py").read_text()


def test_update_from_a_checkout_then_syncs_registered_projects(tmp_path: Path, monkeypatch, capsys):
    root = _apply(tmp_path / "a", clear_suggest_tokens=150000)
    _age(root)
    assert selfupdate.source_checkout() is not None and selfupdate.self_argv()[-1].endswith("run_ego.py")
    monkeypatch.setattr(selfupdate, "self_update", lambda *a, **k: {"mode": "git", "update_available": False})
    assert main(["update", "--yes", "--sync-all"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["projects_sync"]["counts"] == {"APPLIED": 1}
    assert read_json(root / OWNERSHIP)["installed_version"] == __version__ and load_config(root).clear_suggest_tokens == 150000
    _age(root)
    assert main(["update", "--yes", "--sync", "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["project_sync"]["status"] == "APPLIED"
    assert main(["update", "--sync", "--root", str(tmp_path)]) == 3


def test_update_typo_is_accepted(tmp_path: Path, monkeypatch, capsys):
    root = _apply(tmp_path / "a", clear_suggest_tokens=150000)
    _age(root)
    monkeypatch.setattr(selfupdate, "self_update", lambda *a, **k: {"mode": "git", "update_available": False})
    assert main(["udpate", "--yes", "--sync-all"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["projects_sync"]["counts"] == {"APPLIED": 1}
    assert "assuming 'update' for typo 'udpate'" in captured.err
    assert main(["--root", str(root), "updae", "--yes", "--sync"]) == 0
    assert json.loads(capsys.readouterr().out)["project_sync"]["status"] == "APPLIED"
    with pytest.raises(SystemExit):
        main(["totallyunknown"])


def test_update_asks_which_pending_configs_to_apply(tmp_path: Path, capsys):
    from ego.cli import _ask_config_sync
    a = _apply(tmp_path / "a", hosts=["claude"])
    b = _apply(tmp_path / "b")
    _age(a); _age(b)
    rows = [r for r in list_projects() if r["status"] == "CHANGES_PENDING"]
    assert len(rows) == 2
    answers = iter(["1\n"])
    assert _ask_config_sync(a, rows, read=lambda: next(answers)) == "project"  # this project only
    assert _ask_config_sync(b, rows, read=lambda: "2\n") == "all"
    assert _ask_config_sync(a, rows, read=lambda: "3\n") == "none"
    assert _ask_config_sync(a, rows, read=lambda: "\n") == "none"              # Enter keeps the safe default
    assert _ask_config_sync(a, rows, read=lambda: "") == "none"                # end of input never applies
    answers = iter(["9\n", "x\n", "2\n"])
    assert _ask_config_sync(a, rows, read=lambda: next(answers)) == "all"      # invalid input re-asks
    other = tmp_path / "elsewhere"; other.mkdir()
    capsys.readouterr()
    assert _ask_config_sync(other, rows, read=lambda: "1\n") == "all"          # an unregistered root gets no 'this project' option
    assert "差分だけ適用" not in capsys.readouterr().err


def test_interactive_update_applies_only_the_chosen_scope(tmp_path: Path, monkeypatch, capsys):
    import io
    a = _apply(tmp_path / "a", clear_suggest_tokens=150000)
    _age(a)
    monkeypatch.setattr(selfupdate, "self_update", lambda *x, **k: {"mode": "git", "update_available": False})
    monkeypatch.setattr(selfupdate.sys, "stdin", io.StringIO("1\n"))
    monkeypatch.setattr(selfupdate.sys, "stdout", type("T", (io.StringIO,), {"isatty": lambda self: True})())
    monkeypatch.setattr(selfupdate.sys.stdin, "isatty", lambda: True, raising=False)
    assert main(["update", "--root", str(a)]) == 0  # the in-terminal prompt picked 1: this project only
    assert read_json(a / OWNERSHIP)["installed_version"] == __version__
    assert load_config(a).clear_suggest_tokens == 150000


ENTRY = Path(__file__).resolve().parents[1] / "run_ego.py"


def cli(root: Path, *argv: str, answers: str | None = None):
    return subprocess.run([sys.executable, str(ENTRY), "apply", *argv],
                          cwd=root, input=answers, capture_output=True,
                          text=True, timeout=25)


def result(proc):
    return json.loads(proc.stdout)


def test_force_wizard_options_and_confirm(tmp_path):
    project = tmp_path / "typescript"
    project.mkdir()
    (project / "package.json").write_text('{"scripts":{"test":"node test.js"}}')
    p = cli(project, "--interactive", answers="1\n1,3\n3\n1\n2\ny\ny\n")
    assert p.returncode == 0, p.stderr
    assert result(p)["status"] == "APPLIED"
    assert "既存設定" in p.stderr
    assert result(p)["tests_detected"] is True
    cfg = load_config(project)
    assert cfg.profile == "minimal"
    assert cfg.hosts == ["claude", "opencode"]
    assert cfg.code_intelligence == "none"
    assert cfg.knowledge == "file"
    assert cfg.runtime_mode == "managed"
    assert cfg.tests
    assert (project / ".claude/skills/work/SKILL.md").exists()
    assert (project / ".opencode/agents/implementer.md").exists()
    assert not (project / ".codex/agents/implementer.toml").exists()
    assert "この設定を適用しますか" in p.stderr
    assert "=== ego" not in p.stdout


def test_cancel_at_selection_without_file_writes(tmp_path):
    p = cli(tmp_path, "--interactive", answers="q\n")
    assert p.returncode == 0
    assert result(p)["status"] == "CANCELLED"
    assert not (tmp_path / ".ego").exists()


def test_decline_confirm_without_file_writes(tmp_path):
    p = cli(tmp_path, "--interactive", answers="\n\n\n\n\nn\nn\n")
    assert p.returncode == 0, p.stderr
    assert result(p)["status"] == "CANCELLED"
    assert not (tmp_path / ".ego").exists()


def test_selection_invalid_then_correct(tmp_path):
    p = cli(tmp_path, "--interactive", answers="99\n2\nnone\n1,2\n3\n1\n1\nn\ny\n")
    assert p.returncode == 0, p.stderr
    assert "無効な選択" in p.stderr
    assert "少なくとも1つ" in p.stderr
    assert load_config(tmp_path).hosts == ["claude", "codex"]


def test_explicit_hosts_remain_authoritative_during_wizard(tmp_path):
    p = cli(tmp_path, "--interactive", "--hosts", "codex,claude", answers="2\n3\n1\n1\nn\ny\n")
    assert p.returncode == 0, p.stderr
    assert load_config(tmp_path).hosts == ["codex", "claude"]
    assert "設定を生成するHost" not in p.stderr


def test_dry_run_wizard_preview_has_no_write(tmp_path):
    p = cli(tmp_path, "--interactive", "--dry-run", answers="1\n1\n3\n1\n1\nn\n")
    assert p.returncode == 0, p.stderr
    assert result(p)["status"] == "CHANGES_PENDING"
    assert not (tmp_path / ".ego").exists()


def test_noninteractive_flags_do_not_prompt_and_reapply(tmp_path):
    p = cli(tmp_path, "--non-interactive", "--hosts", "claude", "--profile", "minimal")
    assert p.returncode == 0, p.stderr
    assert result(p)["status"] == "APPLIED"
    assert not p.stderr
    p2 = cli(tmp_path, "--non-interactive")
    assert result(p2)["status"] == "UP_TO_DATE"
    assert load_config(tmp_path).hosts == ["claude"]


def test_interactive_defaults_preserve_existing_configuration(tmp_path):
    p = cli(tmp_path, "--non-interactive", "--hosts", "opencode", "--profile", "minimal", "--runtime", "managed")
    assert p.returncode == 0
    follow = cli(tmp_path, "--interactive", answers="\n\n\n\n\nn\n")
    assert follow.returncode == 0, follow.stderr
    assert result(follow)["status"] == "UP_TO_DATE"
    assert load_config(tmp_path).hosts == ["opencode"]
    assert load_config(tmp_path).runtime_mode == "managed"


def test_forced_wizard_eof_cancels_cleanly(tmp_path):
    p = cli(tmp_path, "--interactive", answers="")
    assert p.returncode == 0
    assert result(p)["status"] == "CANCELLED"
    assert not (tmp_path / ".ego").exists()


def test_check_and_dry_run_remain_automation_safe(tmp_path):
    p = cli(tmp_path, "--check", answers="1\n")
    assert p.returncode == 5
    assert not p.stderr
    assert "プロファイル" not in p.stdout
    assert not (tmp_path / ".ego").exists()


def test_should_prompt_auto_conditions(monkeypatch):
    class Stream:
        def __init__(self, tty): self.tty = tty
        def isatty(self): return self.tty
    class Args:
        interactive = False
        non_interactive = False
        dry_run = False
        check = False
        profile = None
        hosts = None
        graph = None
        memory = None
        runtime = None
        detect_tests = False
    a = Args()
    monkeypatch.setattr(sys, "stdin", Stream(True))
    monkeypatch.setattr(sys, "stdout", Stream(True))
    assert should_prompt(a) is True
    a.hosts = "claude"
    assert should_prompt(a) is False
    a.hosts = None
    a.check = True
    assert should_prompt(a) is False
    a.check = False
    a.interactive = True
    monkeypatch.setattr(sys, "stdout", Stream(False))
    assert should_prompt(a) is True
    a.interactive = False
    assert should_prompt(a) is False


def fake_hosts(monkeypatch, tmp_path, *, claude_exit=0):
    folder=tmp_path/'vendor-bin'
    folder.mkdir()
    paths={}
    for name in ('claude','codex','opencode'):
        script=folder/name
        script.write_text('#!/usr/bin/env python3\n' + f'''import json,sys
args=sys.argv[1:]
if '--version' in args:
    print('{name} 99.1.0'); sys.exit(0)
if '--help' in args:
    print('--model --effort --output-format --max-turns --permission-mode --dangerously-skip-permissions --json --yolo --dangerously-bypass-approvals-and-sandbox --sandbox --ask-for-approval --format --auto auto')
    sys.exit(0)
if {name!r} == 'claude' and {claude_exit}:
    sys.stderr.write('rate limit: retry later'); sys.exit({claude_exit})
model='native-default'
if '--model' in args: model=args[args.index('--model')+1]
msg='host={name};model='+model+';'+sys.stdin.read(4096)[:30]
if {name!r} == 'claude':
    print(json.dumps({{'type':'result','result':msg}}))
elif {name!r} == 'codex':
    print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':msg}}}}))
else:
    print(json.dumps({{'type':'text','part':{{'text':msg}}}}))
''')
        script.chmod(0o755)
        paths[name]=script
    monkeypatch.setenv('PATH',str(folder)+os.pathsep+os.environ.get('PATH',''))
    monkeypatch.setenv('EGO_CACHE_DIR',str(tmp_path/'cache'))
    return paths


def test_host_probe_invalidates_cache_on_binary_change(monkeypatch, tmp_path):
    paths=fake_hosts(monkeypatch,tmp_path)
    initial=hostcompat.inspect_host('claude')
    assert initial['supported'] and '--model' in initial['flags']
    with paths['claude'].open('a') as f:f.write('\n# changed\n')
    newer=hostcompat.inspect_host('claude')
    assert newer['fingerprint'] != initial['fingerprint']
    assert newer['checked_at'] >= initial['checked_at']


def test_flags_model_and_permission_profiles(monkeypatch,tmp_path):
    fake_hosts(monkeypatch,tmp_path)
    claude=build_command('claude','hello',model='sonnet',read_only=False)
    assert '--model' in claude and 'sonnet' in claude
    assert '--permission-mode' in claude and 'auto' in claude
    claude_effort=build_command('claude','hello',model='sonnet',effort='medium')
    assert '--effort' in claude_effort and 'medium' in claude_effort
    claude_auto=build_command('claude','hello',model='sonnet',effort='auto')
    assert '--effort' not in claude_auto
    codex=build_command('codex','hello',model='gpt-test',read_only=False)
    assert '--sandbox' in codex and 'workspace-write' in codex
    assert '--ask-for-approval' in codex and 'never' in codex
    open_code=build_command('opencode','hello',model='provider/model-x',read_only=False)
    assert '--auto' in open_code and '--model' in open_code
    with pytest.raises(EgoError,match='isolated'):
        build_command('codex','hello',autonomy='bypass')
    assert '--yolo' in build_command('codex','hello',autonomy='bypass',isolated=True)
    assert '--dangerously-skip-permissions' in build_command('claude','hello',autonomy='bypass',isolated=True)


def test_task_model_overrides_and_instruction_passing(tmp_path):
    (tmp_path/'global.md').write_text('Use TypeScript.\n')
    (tmp_path/'feature.md').write_text('Implement validation.\n')
    (tmp_path/'api.ts').write_text('export const ok = true;\n')
    task={'goal':'Build feature', 'instructions':'Do not change API schema', 'instructions_file':'global.md',
          'defaults':{'hosts':['claude','codex'],'strategy':'fallback','models':{'claude':'sonnet','codex':'gpt-a'}},
          'nodes':[{'id':'do','kind':'agent','prompt':'Fix source files','models':{'codex':'gpt-b'},
                    'instructions_file':'feature.md','context_files':['api.ts'],'write_allowlist':['src/*.ts']}]}
    strategy,routes,_=resolve_routes(task['nodes'][0],task)
    assert strategy=='fallback'
    assert [(r.host,r.model) for r in routes]==[('claude','sonnet'),('codex','gpt-b')]
    prompt=build_task_prompt(task,task['nodes'][0],tmp_path)
    assert 'Build feature' in prompt and 'Use TypeScript' in prompt
    assert 'Implement validation' in prompt and 'api.ts' in prompt


def test_config_models_fallback_in_routes():
    # task has no models configured, should inherit from default_models (from config)
    task = {'goal': 'Build feature', 'defaults': {'hosts': ['claude', 'codex'], 'strategy': 'fallback'},
            'nodes': [{'id': 'do', 'kind': 'agent', 'prompt': 'Do work'}]}
    config_models = {'claude': 'sonnet-3.5', 'codex': 'gpt-5-codex'}
    strategy, routes, _ = resolve_routes(task['nodes'][0], task, default_models=config_models)
    assert strategy == 'fallback'
    assert [(r.host, r.model) for r in routes] == [('claude', 'sonnet-3.5'), ('codex', 'gpt-5-codex')]

    # task explicit override beats default_models
    task_override = {'goal': 'Build feature',
                     'defaults': {'hosts': ['claude', 'codex'], 'strategy': 'fallback',
                                  'models': {'claude': 'opus'}},
                     'nodes': [{'id': 'do', 'kind': 'agent', 'prompt': 'Do work'}]}
    _, routes_overridden, _ = resolve_routes(task_override['nodes'][0], task_override, default_models=config_models)
    assert [(r.host, r.model) for r in routes_overridden] == [('claude', 'opus'), ('codex', 'gpt-5-codex')]

    # table configuration with effort support
    config_table = {
        'claude': {'model': 'sonnet', 'effort': 'medium'},
        'codex': {'model': 'gpt-5-codex', 'effort': 'auto'},
    }
    _, routes_table, _ = resolve_routes(task['nodes'][0], task, default_models=config_table)
    assert routes_table[0].host == 'claude'
    assert routes_table[0].model == 'sonnet'
    assert routes_table[0].effort == 'medium'
    assert routes_table[1].host == 'codex'
    assert routes_table[1].model == 'gpt-5-codex'
    assert routes_table[1].effort is None  # 'auto' normalizes to None


def test_fallback_only_on_unavailability(monkeypatch,project,tmp_path):
    fake_hosts(monkeypatch,tmp_path,claude_exit=2)
    spec={'goal':'Report host and model', 'defaults':{'hosts':['claude','codex'], 'strategy':'fallback',
         'models':{'claude':'sonnet','codex':'gpt-example'}},
         'nodes':[{'id':'one','prompt':'Say hi','kind':'agent','write_allowlist':['output.txt'],'max_attempts':1}]}
    result=execute(project,spec,host='claude',trust_cli=True)
    assert result['final_status']=='PASSED'
    attempts=result['results']['one']['attempts']
    assert [a['host'] for a in attempts]==['claude','codex']
    assert attempts[1]['model']=='gpt-example'
    assert 'host=codex' in result['results']['one']['output']


def test_compare_never_merges_or_applies(monkeypatch,project,tmp_path):
    fake_hosts(monkeypatch,tmp_path)
    spec={'defaults':{'hosts':['claude','codex'],'strategy':'compare','models':{'claude':'sonnet','codex':'gpt-z'}},
         'nodes':[{'id':'review','kind':'agent','prompt':'Review','write_allowlist':['review.txt']}]}
    res=execute(project,spec,trust_cli=True)
    assert res['final_status']=='COMPARISON_READY'
    assert res['changed_files']==[]
    assert len(res['results']['review']['comparison'])==2
    with pytest.raises(EgoError,match='Compare'):
        execute(project,spec,trust_cli=True,apply=True,approve=True)


def fake_wheel(version: str) -> bytes:
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr(f'ego-{version}.dist-info/METADATA',
                   'Metadata-Version: 2.1\nName: ego\nVersion: '+version+'\n')
    return buf.getvalue()


def test_selfupdate_digest_and_version(monkeypatch,tmp_path):
    wheel=fake_wheel('1.4.2')
    digest=hashlib.sha256(wheel).hexdigest()
    rel={'tag_name':'v1.4.2','draft':False,'prerelease':False,'html_url':f'https://github.com/{selfupdate.REPO}/releases/tag/v1.4.2',
         'assets':[{'name':'ego-1.4.2-py3-none-any.whl',
                    'browser_download_url':f'https://github.com/{selfupdate.REPO}/releases/download/v1.4.2/x.whl',
                    'digest':'sha256:'+digest}]}
    def download(url,limit,timeout=12):return json.dumps(rel).encode() if url==selfupdate.API else wheel
    monkeypatch.setattr(selfupdate,'_read_url',download)
    info=selfupdate.latest_release('1.4.1')
    assert info['can_install']
    path=selfupdate.fetch_verified_wheel(info,tmp_path/'update.whl')
    assert path.read_bytes()==wheel
    rel['assets'][0]['digest']='sha256:'+'0'*64
    with pytest.raises(EgoError,match='digest mismatch'):
        selfupdate.fetch_verified_wheel(selfupdate.latest_release('1.4.1'),tmp_path/'invalid.whl')
    assert not (tmp_path/'invalid.whl').exists()


def test_update_requires_approval_and_venv(monkeypatch,tmp_path):
    with pytest.raises(EgoError,match='--yes'):
        selfupdate.install_wheel(tmp_path/'x.whl','1.4.2')
    monkeypatch.setattr(selfupdate.sys,'prefix',getattr(selfupdate.sys,'base_prefix',selfupdate.sys.prefix))
    with pytest.raises(EgoError,match='virtual environment'):
        selfupdate.install_wheel(tmp_path/'x.whl','1.4.2',approved=True)


def test_update_installer_falls_back_to_uv_without_pip(monkeypatch,tmp_path):
    import importlib.util
    wheel=tmp_path/'x.whl'
    monkeypatch.setattr(importlib.util,'find_spec',lambda name:object())
    assert selfupdate._installer_argv(wheel)[1:4]==['-m','pip','install']
    monkeypatch.setattr(importlib.util,'find_spec',lambda name:None)
    monkeypatch.setattr(selfupdate.shutil,'which',lambda name:'/opt/uv' if name=='uv' else None)
    argv=selfupdate._installer_argv(wheel)
    assert argv[:5]==['/opt/uv','pip','install','--python',selfupdate.sys.executable] and '--no-deps' in argv and '--no-index' in argv
    monkeypatch.setattr(selfupdate.shutil,'which',lambda name:None)
    with pytest.raises(EgoError,match='neither pip nor uv'):
        selfupdate._installer_argv(wheel)


def test_node_precedence_over_task_and_code_node_under_compare_defaults(project):
    from ego.domain.task_routing import resolve_routes
    spec={"defaults":{"hosts":["claude","codex"],"strategy":"fallback","models":{"codex":"default-codex"}}}
    n={"id":"x","host":"codex","model":"node-codex","prompt":"x","write_allowlist":["x.txt"]}
    strategy,routes,_=resolve_routes(n,spec)
    assert strategy=="single" and routes[0].model=="node-codex"
    (project/"runme.py").write_text("from pathlib import Path\nPath('x.txt').write_text('hi')\n")
    spec={"defaults":{"hosts":["claude","codex"],"strategy":"compare"},
          "nodes":[{"id":"x","kind":"code","argv":["python3","runme.py"],"write_allowlist":["x.txt"]}]}
    result=execute(project,spec)
    assert result["final_status"]=="PASSED"
    assert "x.txt" in result["changed_files"]


def test_cli_flag_removal_fails_closed(monkeypatch,tmp_path):
    paths=fake_hosts(monkeypatch,tmp_path)
    assert '--json' in build_command('codex','hello')
    # Simulate an incompatible vendor update: same executable path, new contents.
    script=paths['codex']
    data=script.read_text()
    data=data.replace('print(\'--model --effort --output-format --max-turns --permission-mode --dangerously-skip-permissions --json --yolo --dangerously-bypass-approvals-and-sandbox --sandbox --ask-for-approval --format --auto auto\')',
                      'print(\'--model --sandbox --ask-for-approval\')')
    assert data!=script.read_text()
    script.write_text(data)
    with pytest.raises(EgoError,match='required CLI switches absent'):
        build_command('codex','hello')


def test_local_wheel_requires_integrity_and_newer_version(tmp_path):
    wheel=fake_wheel('1.4.2')
    path=tmp_path/'ego-1.4.2-py3-none-any.whl'
    path.write_bytes(wheel)
    with pytest.raises(EgoError,match='digest mismatch'):
        selfupdate.update_from_local_wheel(path,'0'*64,'1.4.1',yes=True)
    with pytest.raises(EgoError,match='newer stable'):
        selfupdate.update_from_local_wheel(path,hashlib.sha256(wheel).hexdigest(),'1.4.2',yes=True)


def test_host_models_do_not_refresh_on_task(monkeypatch,tmp_path,project):
    from ego.adapters import models
    def unwanted(*args,**kwargs):raise AssertionError('Work path triggered a model refresh')
    monkeypatch.setattr(models,'refresh_catalog',unwanted)
    spec={'defaults':{'model':'auto'},'nodes':[{'id':'noop','kind':'agent','prompt':'[[MOCK:hello]]',
          'write_allowlist':['result.txt']}]}
    assert execute(project,spec)['final_status']=='PASSED'


def test_bin_wrappers_work_via_symlink(tmp_path):
    import subprocess
    repo_root=Path(__file__).resolve().parent.parent
    bin_dir=tmp_path/'local'/'bin'
    bin_dir.mkdir(parents=True)
    for name in ("ego",):
        link=bin_dir/name
        link.symlink_to(repo_root/'bin'/name)
        proc=subprocess.run([str(link),'--version'],capture_output=True,text=True,timeout=10,check=False)
        assert proc.returncode==0, proc.stderr
        assert 'ego ' in proc.stdout


def test_selfupdate_ssl_context_falls_back_to_system_ca(monkeypatch,tmp_path):
    import ssl
    fake_ca=tmp_path/'cert.pem'
    fake_ca.write_text('-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n')
    loaded=[]
    class DummyCtx:
        def load_verify_locations(self,cafile=None):
            loaded.append(cafile)
    monkeypatch.setattr(ssl,'create_default_context',lambda:DummyCtx())
    monkeypatch.setattr(ssl,'get_default_verify_paths',lambda:type('P',(),{'cafile':None,'capath':None})())
    monkeypatch.setattr(selfupdate,'SYSTEM_CA_BUNDLES',(str(fake_ca),))
    selfupdate._ssl_context()
    assert loaded==[str(fake_ca)]


def test_selfupdate_allows_api_github_com_redirect(monkeypatch):
    class DummyResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def geturl(self):
            return "https://api.github.com/repositories/12345/releases/latest"
        def read(self, limit):
            return b"{}"

    monkeypatch.setattr(selfupdate, "urlopen", lambda req, timeout=12, context=None: DummyResponse())
    data = selfupdate._read_url("https://api.github.com/repos/jumtra/ego/releases/latest", 1024)
    assert data == b"{}"


def test_make_install_creates_symlinks_without_clobbering_existing_file(tmp_path):
    import subprocess
    repo_root=Path(__file__).resolve().parent.parent
    bin_dir=tmp_path/'bin'
    bin_dir.mkdir()
    existing=bin_dir/'other'
    existing.write_text('#!/bin/sh\necho existing\n')
    existing.chmod(0o755)
    proc=subprocess.run(['make','install',f'BINDIR={bin_dir}'],cwd=repo_root,capture_output=True,text=True,timeout=10,check=False)
    assert proc.returncode==0, proc.stderr
    assert (bin_dir/'ego').is_symlink()
    assert not (bin_dir/'harness').exists()
    assert not existing.is_symlink() and existing.read_text()=='#!/bin/sh\necho existing\n'
    ver=subprocess.run([str(bin_dir/'ego'),'--version'],capture_output=True,text=True,timeout=10,check=False)
    assert ver.returncode==0 and 'ego ' in ver.stdout
    clean_bin=tmp_path/'clean_bin'
    proc2=subprocess.run(['make','install',f'BINDIR={clean_bin}'],cwd=repo_root,capture_output=True,text=True,timeout=10,check=False)
    assert proc2.returncode==0, proc2.stderr
    assert (clean_bin/"ego").is_symlink()
    assert not (clean_bin/"seh").exists()
    assert not (clean_bin/'harness').exists()


def test_load_config_accepts_v2_nested_config(tmp_path):
    from ego.domain.config import load_config
    h=tmp_path/'.ego'
    h.mkdir()
    (h/'config.toml').write_text('''schema_version = "2.0"
profile = "standard"

[hosts]
enabled = ["claude", "codex", "opencode", "antigravity"]

[code_intelligence]
provider = "none"

[memory]
provider = "files"

[runtime]
default_mode = "native"
max_concurrency = 2
max_fix_iterations = 3
timeout_sec = 300

[sdk]
mode = "auto"

[authoring]
require_eval = true

[evaluation]
trials = 1

[evolution]
auto_promote = false
''')
    cfg=load_config(tmp_path)
    assert cfg.profile=='balanced'
    assert cfg.hosts==['claude','codex','opencode','agy']
    assert cfg.code_intelligence=='none'
    assert cfg.knowledge=='file'


def test_codex_approve_for_me_and_agy_models_parsing(monkeypatch,tmp_path):
    paths=fake_hosts(monkeypatch,tmp_path)
    script=paths['codex']
    script.write_text(script.read_text().replace('--ask-for-approval','--approve-for-me'))
    cmd=build_command('codex','hello',read_only=False,autonomy='auto')
    assert '--approve-for-me' in cmd
    from ego.adapters.models import parse_agy_models
    parsed=parse_agy_models('gemini-4-argon-medium\tGemini 4 Argon (Medium)\nclaude-opus-5-5-high\tClaude Opus 5.5 (High)\n')
    assert parsed==['claude-opus-5-5-high','gemini-4-argon-medium']


def test_node_notes_reach_worker_and_successor_prompts(tmp_path):
    task={'goal':'Add endpoint','nodes':[
        {'id':'design','prompt':'Write the contract','write_allowlist':['docs/*'],
         'intent':'Both halves need one contract','provides':['docs/plan.md']},
        {'id':'build','prompt':'Implement it','depends_on':['design'],'write_allowlist':['src/*'],
         'intent':'Users need the endpoint','done_when':['GET /x returns 200'],
         'non_goals':['Refactoring the router'],'assumptions':['Auth middleware exists']}]}
    validate_task(task)
    prompt=build_task_prompt(task,task['nodes'][1],tmp_path)
    assert 'Users need the endpoint' in prompt and '- GET /x returns 200' in prompt
    assert '- Refactoring the router' in prompt and '- Auth middleware exists' in prompt
    # The successor learns what the prior stage was for and what it left behind.
    assert '- design: Both halves need one contract; provides: docs/plan.md' in prompt
    first=build_task_prompt(task,task['nodes'][0],tmp_path)
    assert 'Later stages rely on this stage for\n- docs/plan.md' in first and 'Done when' not in first


@pytest.mark.parametrize('notes',[{'intent':''},{'intent':['why']},{'intent':'x'*401},
                                  {'done_when':'passes'},{'done_when':['']},{'provides':['a']*7},
                                  {'requirement':'x'},{'requirement':{}},{'requirement':{'whom':'x'}},
                                  {'steps':[]},{'steps':['s']*11}])
def test_node_notes_are_bounded(notes):
    task={'goal':'g','nodes':[{'id':'a','prompt':'p','write_allowlist':['src/*'],**notes}]}
    with pytest.raises(EgoError,match='INVALID_TASK|must be'):
        validate_task(task)


def test_requirement_and_steps_reach_prompt(tmp_path):
    task={'goal':'Add endpoint','nodes':[
        {'id':'api','prompt':'Add /x','write_allowlist':['src/*'],
         'requirement':{'who':'worker','what':'Add /x','where':'src/api.py','how':'Add a route'},
         'steps':['Add def x() to src/api.py','Return 200']},
        {'id':'docs','prompt':'Document /x','depends_on':['api'],'write_allowlist':['docs/*']}]}
    validate_task(task)
    prompt=build_task_prompt(task,task['nodes'][0],tmp_path)
    assert '## Requirement (5W1H)\n- Who: worker' in prompt and '- Where: src/api.py' in prompt
    assert '1. Add def x() to src/api.py\n2. Return 200' in prompt and 'exactly as written' in prompt
    assert 'what: Add /x' in build_task_prompt(task,task['nodes'][1],tmp_path)


def test_uninstall_project_cleans_owned_files(tmp_path):
    cfg = Config(hosts=["claude"])
    apply_install(tmp_path, cfg)
    
    assert (tmp_path / ".ego").exists()
    assert (tmp_path / ".claude").exists()
    assert (tmp_path / "CLAUDE.md").exists()
    
    res = uninstall_project(tmp_path)
    assert res["status"] == "UNINSTALLED"
    assert not (tmp_path / ".ego").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_uninstall_cli(tmp_path):
    ego = tmp_path / "ego"
    ego.write_text("#!/bin/sh\n")
    res = uninstall_cli(tmp_path)
    assert res["status"] == "UNINSTALLED_CLI"
    assert not ego.exists()


ROOT = Path(__file__).resolve().parents[1]


ENTRY = ROOT / 'run_ego.py'


DEMO = ROOT / 'examples' / 'demo_project'


def call(*argv):
    p = subprocess.run([sys.executable, str(ENTRY), *map(str, argv)], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, (p.stdout, p.stderr)
    return json.loads(p.stdout) if p.stdout.startswith('{') else p.stdout


ROOT=Path(__file__).resolve().parents[1]


def test_code_index_impact_affected_options(tmp_path):
    (tmp_path / '.ego').mkdir()
    (tmp_path / '.ego' / 'config.toml').write_text('code_intelligence = "none"\n', encoding='utf-8')
    entry = ROOT / 'run_ego.py'
    for action in (['code','index'], ['code','impact','Some.symbol'], ['code','affected','tests/test_unit.py']):
        p=subprocess.run([sys.executable,str(entry), *action,'--root',str(tmp_path)],capture_output=True,text=True)
        assert p.returncode == 0, p.stderr
        obj=json.loads(p.stdout)
        assert obj.get('status') in ('NOT_APPLICABLE','UNSUPPORTED'),obj
    p=subprocess.run([sys.executable,str(entry),'code','index','--root',str(tmp_path),'--execute'],capture_output=True,text=True)
    assert p.returncode==0,p.stderr


def test_host_agent_formats(project):
    for role in ("implementer","reviewer","fixer","evolver"):
        assert (project/f".codex/agents/{role}.toml").exists()
        assert (project/f".agents/agents/{role}.md").exists()
        assert (project/f".opencode/agents/{role}.md").exists()
        with (project/f".codex/agents/{role}.toml").open("rb") as f:
            assert tomllib.load(f)["name"]==role
    assert not (project/".claude/settings.json").exists()
    cfg=load_config(project);cfg.profile="full"
    apply_install(project,cfg)
    assert (project/".claude/settings.json").exists()


def test_project_detection(tmp_path):
    (tmp_path/"pyproject.toml").write_text('[project]\nname="x"\n')
    (tmp_path/"tests").mkdir()
    result=detect(tmp_path)
    assert "python" in result["stack"]
    assert ["python3","-m","pytest","-q"] in result["test_commands"]


def test_cli_root_after_action(project):
    from ego.domain.config import Config, save_config
    save_config(project, Config(knowledge="file"))
    cmd=[sys.executable,"-m","ego","memory","add","some note","--root",str(project)]
    env={**os.environ,"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")}
    res=subprocess.run(cmd,capture_output=True,text=True,env=env,timeout=20)
    assert res.returncode==0,res.stderr
    assert json.loads(res.stdout)["text"]=="some note"


def test_cli_fast_does_not_import_optimizer():
    script="import sys; import ego.cli; print('ego.optimize' in sys.modules)"
    env={**os.environ,"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")}
    r=subprocess.run([sys.executable,"-c",script],capture_output=True,text=True,env=env)
    assert r.stdout.strip()=="False"


def test_mock_and_claude_parsing():
    raw='{"type":"result","result":"done","total_cost_usd":0.4,"usage":{"input_tokens":10,"output_tokens":5}}'
    assert parse_host_response("claude",raw)==("done",15,0.4)


# ---------------------------------------------------------------------------
# CLI surface: the 18 core commands parse; removed peripheral commands exit 2
# ---------------------------------------------------------------------------

CORE_COMMANDS = ["apply", "status", "rollback", "uninstall", "init", "sync", "projects", "doctor",
                 "hosts", "models", "update", "work", "state", "memory", "session", "run", "code", "hook"]
REMOVED_COMMANDS = ["tool", "modules", "prompt", "eval", "author", "evolve", "optimize", "migrate",
                    "pr", "bench", "events", "context"]


def _exit_code(argv):
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code


@pytest.mark.parametrize("command", CORE_COMMANDS)
def test_core_command_is_registered_and_shows_help(command, capsys):
    with pytest.raises(SystemExit) as done:
        main([command, "--help"])
    assert done.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("command", REMOVED_COMMANDS)
def test_removed_peripheral_command_fails_with_exit_2(command, capsys):
    assert _exit_code([command]) == 2
    assert "invalid choice" in capsys.readouterr().err


def test_sync_moves_file_memory_to_alter_when_alter_mcp_is_configured(tmp_path: Path, monkeypatch):
    root = _apply(tmp_path / "a", knowledge="file", hosts=["claude"])
    assert "SessionEnd" not in (root / ".claude/settings.json").read_text() if (root / ".claude/settings.json").exists() else True
    monkeypatch.setenv("EGO_ALTER_MCP_COMMAND", "true")
    monkeypatch.setenv("EGO_ALTER_AUTO_REGISTER", "0")
    assert project_info(root.resolve())["status"] == "CHANGES_PENDING"
    row = sync_project(root.resolve(), apply=True)
    assert row["status"] == "APPLIED" and row["knowledge_migrated"] == "file -> alter"
    assert load_config(root).knowledge == "alter"
    assert "SessionEnd" in (root / ".claude/settings.json").read_text()
    assert project_info(root.resolve())["status"] == "UP_TO_DATE"


def test_sync_keeps_file_memory_without_alter_mcp_or_when_opted_out(tmp_path: Path, monkeypatch):
    root = _apply(tmp_path / "a", knowledge="file", hosts=["claude"])
    monkeypatch.delenv("EGO_ALTER_MCP_COMMAND", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    assert project_info(root.resolve())["status"] == "UP_TO_DATE"
    monkeypatch.setenv("EGO_ALTER_MCP_COMMAND", "true")
    monkeypatch.setenv("EGO_SYNC_KEEP_MEMORY", "1")
    assert project_info(root.resolve())["status"] == "UP_TO_DATE"
