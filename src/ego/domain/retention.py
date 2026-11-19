"""Bounded retention for `.ego/state` and the other growth paths under `.ego/`.

Every category keeps only its newest entries. Pruning is fail-open: an error in one
category is recorded and never stops a run. Unfinished work is never removed: a run
whose record is still RUNNING (resumable) and a run whose lock is held are both kept.
Staging copies are additionally bounded by age and total size; their run's record and progress always stay.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..shared.util import EgoError, atomic_text, exclusive_lock, read_json, write_json

RUNS_KEEP = 30               # run directories (including their staging copies)
STAGING_MAX_AGE_DAYS = 3     # staging copies not updated for this long are removed
STAGING_MAX_BYTES = 3 * 1024**3  # total staging size budget; oldest copies beyond it are removed
ALTER_SESSIONS_KEEP = 200    # per-session ALTER submission digests
ALTER_TURNS_KEEP = 50        # per-session persisted ALTER turn files (analyzer input)
ALTER_TURNS_MAX_AGE_DAYS = 7
EVENTS_KEEP_LINES = 5000     # lines of the append-only event journal
BACKUPS_KEEP = 20            # install snapshots under .ego/backups


def _newest_first(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def remove_staging(root: Path, runroot: Path) -> None:
    """Remove a run's staging copy (and its worktree registration); record.json and progress stay."""
    staging = runroot / "staging"
    if (staging / ".git").exists():
        # A worktree-backed staging is registered in the repository; deregister it first.
        subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(staging)],
                       capture_output=True, text=True, timeout=60, check=False)
    shutil.rmtree(staging, ignore_errors=True)


def _remove_run(root: Path, runroot: Path) -> None:
    remove_staging(root, runroot)
    shutil.rmtree(runroot, ignore_errors=True)


def _worktree_prune(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "worktree", "prune"],
                   capture_output=True, text=True, timeout=60, check=False)


def _dir_bytes(path: Path) -> int:
    # Clones count at full logical size, so the budget errs on the safe side.
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


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
        lock_path = root / ".ego" / "locks" / (runroot.name + ".lock")
        if lock_path.exists():
            try:
                with exclusive_lock(lock_path, blocking=False):
                    _remove_run(root, runroot)
                    lock_path.unlink(missing_ok=True)
                removed += 1
            except EgoError:
                continue  # the run is active in another process
        else:
            _remove_run(root, runroot)
            removed += 1
    if removed:
        _worktree_prune(root)
    return removed


def _prune_stagings(root: Path) -> int:
    """Bound staging copies by age and total size. The newest staging is always kept for size, so an
    immediate resume survives; a held run lock protects its staging, but still counts against the budget.
    Removed staging cannot be resumed, applied or used with --from; records and progress stay."""
    base = root / ".ego" / "state" / "runs"
    if not base.is_dir():
        return 0
    cutoff = time.time() - STAGING_MAX_AGE_DAYS * 86400
    runs = _newest_first([p for p in base.iterdir() if (p / "staging").is_dir()])
    total = 0
    removed = 0
    for i, runroot in enumerate(runs):
        try:
            with exclusive_lock(root / ".ego" / "locks" / (runroot.name + ".lock"), blocking=False):
                size = _dir_bytes(runroot / "staging")
                expired = runroot.stat().st_mtime < cutoff
                over = i > 0 and total + size > STAGING_MAX_BYTES
                if expired or over:
                    remove_staging(root, runroot)
                    removed += 1
                    continue
                total += size
        except EgoError:
            total += _dir_bytes(runroot / "staging")  # active elsewhere: kept, but still uses budget
    if removed:
        _worktree_prune(root)
    return removed


SHARED_LOCKS = frozenset({
    "install.lock",
    "events.lock",
    "memory.lock",
    "alter-analysis.lock",
    "alter-prompts.lock",
    "session-advice.lock",
    "usage-advice.lock",
})


def _prune_run_locks(root: Path) -> int:
    """Remove per-run lock files whose run directory is gone. Run IDs are unique, so no
    other process can reopen such a lock; shared lock names are never touched, because
    deleting a lock another process may be waiting on is unsafe."""
    locks = root / ".ego" / "locks"
    runs = root / ".ego" / "state" / "runs"
    if not locks.is_dir():
        return 0
    removed = 0
    for path in locks.glob("*.lock"):
        if path.name in SHARED_LOCKS:
            continue
        if (runs / path.name[: -len(".lock")]).exists():
            continue
        try:
            with exclusive_lock(path, blocking=False):
                path.unlink(missing_ok=True)  # unlinked while held: no waiter can take an orphaned inode
            removed += 1
        except EgoError:
            continue
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


def _prune_json_dir(base: Path, max_age_days: int, keep: int) -> int:
    """Remove JSON state files older than `max_age_days`, then keep at most `keep` newest."""
    if not base.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    files = [p for p in base.glob("*.json") if p.is_file()]
    fresh: list[Path] = []
    for path in files:
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
        else:
            fresh.append(path)
    for path in _newest_first(fresh)[keep:]:
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _prune_alter_turns(root: Path) -> int:
    """Remove persisted turn files older than ALTER_TURNS_MAX_AGE_DAYS, then keep at most ALTER_TURNS_KEEP newest."""
    return _prune_json_dir(root / ".ego" / "state" / "alter_turns", ALTER_TURNS_MAX_AGE_DAYS, ALTER_TURNS_KEEP)


def _prune_alter_segments(root: Path) -> int:
    """Remove persisted segment files older than ALTER_TURNS_MAX_AGE_DAYS, then keep at most ALTER_TURNS_KEEP newest."""
    return _prune_json_dir(root / ".ego" / "state" / "alter_segments", ALTER_TURNS_MAX_AGE_DAYS, ALTER_TURNS_KEEP)


def _prune_events(root: Path) -> int:
    path = root / ".ego" / "state" / "events.jsonl"
    if not path.is_file():
        return 0
    with exclusive_lock(root / ".ego" / "locks" / "events.lock"):
        lines = path.read_text(encoding="utf8").splitlines(keepends=True)
        if len(lines) <= EVENTS_KEEP_LINES:
            return 0
        atomic_text(path, "".join(lines[-EVENTS_KEEP_LINES:]))
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
    for name, step in (("runs", _prune_runs), ("stagings", _prune_stagings), ("run_locks", _prune_run_locks),
                       ("alter_sessions", _prune_alter_sessions), ("alter_turns", _prune_alter_turns),
                       ("alter_segments", _prune_alter_segments), ("events", _prune_events),
                       ("backups", _prune_backups)):
        try:
            report[name] = step(root)
        except (OSError, ValueError, EgoError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            report[name] = f"skipped: {type(exc).__name__}"
    return report
