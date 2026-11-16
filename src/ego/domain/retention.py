"""Bounded retention for `.ego/state` and the other growth paths under `.ego/`.

Every category keeps only its newest entries. Pruning is fail-open: an error in one
category is recorded and never stops a run. Unfinished work is never removed: a run
whose record is still RUNNING (resumable), a run whose lock is held, and a candidate
referenced by a promotion are all kept.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..shared.util import EgoError, exclusive_lock, read_json, write_json

RUNS_KEEP = 30               # run directories (including their staging copies)
CANDIDATES_KEEP = 200        # candidate and gate records
ALTER_SESSIONS_KEEP = 200    # per-session ALTER submission digests
EVENTS_KEEP_LINES = 5000     # lines of the append-only event journal
BACKUPS_KEEP = 20            # install snapshots under .ego/backups


def _newest_first(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def _remove_run(root: Path, runroot: Path) -> None:
    staging = runroot / "staging"
    if (staging / ".git").exists():
        # A worktree-backed staging is registered in the repository; deregister it first.
        subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(staging)],
                       capture_output=True, text=True, timeout=60, check=False)
    shutil.rmtree(runroot, ignore_errors=True)


def _prune_runs(root: Path) -> int:
    base = root / ".ego" / "state" / "runs"
    if not base.is_dir():
        return 0
    removed = 0
    runs = [p for p in base.iterdir() if p.is_dir()]
    for runroot in _newest_first(runs)[RUNS_KEEP:]:
        record = read_json(runroot / "record.json", {})
        if not isinstance(record, dict) or record.get("final_status") in (None, "RUNNING"):
            continue  # unfinished or unreadable: may still be resumed
        try:
            with exclusive_lock(root / ".ego" / "locks" / (runroot.name + ".lock"), blocking=False):
                _remove_run(root, runroot)
            removed += 1
        except EgoError:
            continue  # the run is active in another process
    if removed:
        subprocess.run(["git", "-C", str(root), "worktree", "prune"],
                       capture_output=True, text=True, timeout=60, check=False)
    return removed


def _prune_run_locks(root: Path) -> int:
    """Remove per-run lock files whose run directory is gone. Run IDs are unique, so no
    other process can reopen such a lock; shared lock names (install, events, memory) are
    never touched, because deleting a lock another process may be waiting on is unsafe."""
    locks = root / ".ego" / "locks"
    runs = root / ".ego" / "state" / "runs"
    if not locks.is_dir():
        return 0
    removed = 0
    for path in locks.glob("run-*.lock"):
        if (runs / path.name[: -len(".lock")]).exists():
            continue
        try:
            with exclusive_lock(path, blocking=False):
                path.unlink(missing_ok=True)  # unlinked while held: no waiter can take an orphaned inode
            removed += 1
        except EgoError:
            continue
    return removed


def _prune_candidates(root: Path) -> int:
    base = root / ".ego" / "state" / "candidates"
    if not base.is_dir():
        return 0
    promoted = {str(p.get("candidate_id")) for p in (read_json(root / ".ego" / "state" / "promotions.json", []) or [])
                if isinstance(p, dict)}
    removed = 0
    for path in _newest_first([p for p in base.glob("*.json") if p.is_file()])[CANDIDATES_KEEP:]:
        if any(cid and cid in path.stem for cid in promoted):
            continue  # a promotion refers to it: keep for rollback and audit
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _prune_alter_sessions(root: Path) -> int:
    path = root / ".ego" / "state" / "alter_sessions.json"
    state = read_json(path, {})
    sessions = state.get("sessions") if isinstance(state, dict) else None
    if not isinstance(sessions, dict) or len(sessions) <= ALTER_SESSIONS_KEEP:
        return 0
    ordered = sorted(sessions.items(), key=lambda kv: str((kv[1] or {}).get("updated_at", "")), reverse=True)
    kept = dict(ordered[:ALTER_SESSIONS_KEEP])
    write_json(path, {"schema_version": state.get("schema_version", 1), "sessions": kept})
    return len(sessions) - len(kept)


def _prune_events(root: Path) -> int:
    path = root / ".ego" / "state" / "events.jsonl"
    if not path.is_file():
        return 0
    with exclusive_lock(root / ".ego" / "locks" / "events.lock"):
        lines = path.read_text(encoding="utf8").splitlines(keepends=True)
        if len(lines) <= EVENTS_KEEP_LINES:
            return 0
        path.write_text("".join(lines[-EVENTS_KEEP_LINES:]), encoding="utf8")
        return len(lines) - EVENTS_KEEP_LINES


def _prune_backups(root: Path) -> int:
    base = root / ".ego" / "backups"
    if not base.is_dir():
        return 0
    snaps = [p for p in base.glob("snapshot-*") if p.is_dir()]
    removed = 0
    for path in _newest_first(snaps)[BACKUPS_KEEP:]:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def prune_state(root: Path) -> dict[str, Any]:
    """Enforce every retention limit once. Returns the number removed per category."""
    root = Path(root).resolve()
    report: dict[str, Any] = {}
    for name, step in (("runs", _prune_runs), ("run_locks", _prune_run_locks), ("candidates", _prune_candidates),
                       ("alter_sessions", _prune_alter_sessions), ("events", _prune_events),
                       ("backups", _prune_backups)):
        try:
            report[name] = step(root)
        except (OSError, ValueError, EgoError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            report[name] = f"skipped: {type(exc).__name__}"
    return report
