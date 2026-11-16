import json
import os
import time
from pathlib import Path

from ego import retention
from ego.util import exclusive_lock
from ego.cli import main


def _touch_old(path: Path, age: int) -> None:
    t = time.time() - age
    os.utime(path, (t, t))


def _make_run(root: Path, name: str, status: str, age: int) -> Path:
    run = root / ".ego" / "state" / "runs" / name
    (run / "staging").mkdir(parents=True)
    rec = run / "record.json"
    rec.write_text(json.dumps({"final_status": status}))
    _touch_old(rec, age)
    _touch_old(run, age)
    return run


def test_runs_keep_newest_finished_and_never_unfinished(tmp_path):
    for i in range(retention.RUNS_KEEP + 5):
        _make_run(tmp_path, f"run-{i:03d}", "PASSED", age=1000 + i)
    unfinished = _make_run(tmp_path, "run-unfinished", "RUNNING", age=99999)
    report = retention.prune_state(tmp_path)
    left = {p.name for p in (tmp_path / ".ego" / "state" / "runs").iterdir()}
    assert report["runs"] == 5
    assert "run-unfinished" in left and unfinished.exists()
    assert len(left) == retention.RUNS_KEEP + 1
    assert "run-000" in left and "run-034" not in left


def test_held_run_lock_protects_run(tmp_path):
    for i in range(retention.RUNS_KEEP + 1):
        _make_run(tmp_path, f"run-{i:03d}", "PASSED", age=1000 + i)
    oldest = "run-030"
    with exclusive_lock(tmp_path / ".ego" / "locks" / (oldest + ".lock"), blocking=False):
        retention.prune_state(tmp_path)
    assert (tmp_path / ".ego" / "state" / "runs" / oldest).exists()


def test_orphan_run_lock_removed(tmp_path):
    locks = tmp_path / ".ego" / "locks"
    locks.mkdir(parents=True)
    (locks / "run-gone.lock").write_text("")
    (locks / "install.lock").write_text("")
    retention.prune_state(tmp_path)
    assert not (locks / "run-gone.lock").exists()
    assert (locks / "install.lock").exists()


def test_candidates_keep_newest_and_promoted(tmp_path):
    cands = tmp_path / ".ego" / "state" / "candidates"
    cands.mkdir(parents=True)
    for i in range(retention.CANDIDATES_KEEP + 3):
        p = cands / f"cand-{i:04d}.json"
        p.write_text("{}")
        _touch_old(p, 10000 + i)
    # Larger index = older (see _touch_old). The oldest three are 202, 201, 200.
    promoted = cands / "cand-0202.json"  # among the oldest, but referenced by a promotion
    (tmp_path / ".ego" / "state" / "promotions.json").write_text(json.dumps([{"candidate_id": "cand-0202"}]))
    report = retention.prune_state(tmp_path)
    assert report["candidates"] == 2
    assert promoted.exists()
    assert not (cands / "cand-0200.json").exists() and not (cands / "cand-0201.json").exists()


def test_alter_sessions_bounded(tmp_path):
    path = tmp_path / ".ego" / "state" / "alter_sessions.json"
    path.parent.mkdir(parents=True)
    sessions = {f"s{i:04d}": {"updated_at": f"2026-01-{i:04d}"} for i in range(retention.ALTER_SESSIONS_KEEP + 10)}
    path.write_text(json.dumps({"schema_version": 1, "sessions": sessions}))
    retention.prune_state(tmp_path)
    kept = json.loads(path.read_text())["sessions"]
    assert len(kept) == retention.ALTER_SESSIONS_KEEP
    assert "s0000" not in kept and f"s{retention.ALTER_SESSIONS_KEEP + 9:04d}" in kept


def test_events_rotated_to_tail(tmp_path):
    path = tmp_path / ".ego" / "state" / "events.jsonl"
    path.parent.mkdir(parents=True)
    n = retention.EVENTS_KEEP_LINES + 100
    path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(n)))
    retention.prune_state(tmp_path)
    lines = path.read_text().splitlines()
    assert len(lines) == retention.EVENTS_KEEP_LINES
    assert json.loads(lines[0])["i"] == 100


def test_backups_keep_newest(tmp_path):
    base = tmp_path / ".ego" / "backups"
    for i in range(retention.BACKUPS_KEEP + 2):
        d = base / f"snapshot-{i:03d}"
        d.mkdir(parents=True)
        _touch_old(d, 10000 + i)
    retention.prune_state(tmp_path)
    left = {p.name for p in base.glob("snapshot-*")}
    assert len(left) == retention.BACKUPS_KEEP
    assert "snapshot-000" in left and "snapshot-021" not in left


def test_state_prune_cli(tmp_path, capsys):
    assert main(["state", "prune", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert '"runs": 0' in out
