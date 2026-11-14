"""Project application lifecycle: apply, dry-run, check, status, and rollback."""
from __future__ import annotations
import json
from pathlib import Path
import os
import subprocess
import sys

from ego.cli import _project_target
from ego.util import EgoError
import pytest

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
    assert status["installed_version"] == "0.0.15"
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
