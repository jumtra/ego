import subprocess
from pathlib import Path

from ego.work import execute


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def test_worktree_run_receives_gitignored_project_state(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text("CLAUDE.md\n.claude/\nnode_modules/\n.ego/memory/\n.ego/state/\n__pycache__/\n")
    (root / "hello.txt").write_text("initial\n")
    (root / "noop.py").write_text("pass\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")

    # Gitignored project state that `git worktree add` does not check out.
    (root / "CLAUDE.md").write_text("instructions\n")
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "rules" / "engineering.md").write_text("rule\n")
    (root / ".claude" / "node_modules").mkdir()
    (root / ".claude" / "node_modules" / "junk.js").write_text("x\n")
    (root / ".ego" / "memory").mkdir(parents=True)
    (root / ".ego" / "memory" / "fact.md").write_text("fact\n")
    (root / ".ego" / "state" / "runs" / "old").mkdir(parents=True)
    (root / ".ego" / "state" / "runs" / "old" / "record.json").write_text("{}\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "noop.cpython-314.pyc").write_bytes(b"\0")

    task = {"nodes": [{"id": "write", "kind": "code", "argv": ["python3", "noop.py"],
                       "write_allowlist": ["result.txt"]}]}
    result = execute(root, task, worktree=True, cleanup_worktree=False, run_id="hostcfg")

    staging = root / ".ego" / "state" / "runs" / "hostcfg" / "staging"
    assert result["worktree"] is True
    assert (staging / "CLAUDE.md").read_text() == "instructions\n"
    assert (staging / ".claude" / "rules" / "engineering.md").read_text() == "rule\n"
    assert (staging / ".ego" / "memory" / "fact.md").read_text() == "fact\n"
    assert (staging / ".claude" / "node_modules" / "junk.js").exists()
    assert not (staging / ".ego" / "state" / "runs" / "old").exists()
    assert not (staging / "__pycache__").exists()
