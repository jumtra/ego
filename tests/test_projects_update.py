"""Applied-project registry, bulk sync with inherited settings, and git-checkout self-update."""
import json
import subprocess
from pathlib import Path

import pytest

from ego import __version__, selfupdate
from ego.cli import main
from ego.config import Config, load_config
from ego.install import OWNERSHIP, apply_install
from ego.projects import list_projects, project_info, registry_path, scan, sync_all, unregister
from ego.util import EgoError, read_json, write_json


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
    from ego.util import filehash
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
    assert set(hooks) == {"Stop", "SessionStart"}
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
