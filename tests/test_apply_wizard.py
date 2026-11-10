"""Interactive CWD apply is optional, safe, and scriptable."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys

from ego.config import load_config
from ego.apply_wizard import should_prompt

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
    assert cfg.memory == "file"
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
