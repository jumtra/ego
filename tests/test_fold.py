"""Tests for detached post-session fold (ego.usecases.fold and CLI wiring)."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ego.cli import main
from ego.usecases.fold import run_fold, spawn_fold


def test_run_fold_returns_busy_when_lock_held(tmp_path: Path):
    fcntl = pytest.importorskip("fcntl")
    lock_path = tmp_path / ".ego" / "locks" / "fold.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    with lock_path.open("a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = run_fold(tmp_path, "sess-busy", transcript)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    assert result == {"status": "busy"}


def test_run_fold_corrupt_transcript_writes_error_log_without_raising(tmp_path: Path):
    bad = tmp_path / "corrupt.jsonl"
    bad.write_text("{not valid json\n", encoding="utf-8")

    # Fill 55 errors to verify truncation to last 50 lines as well.
    for i in range(55):
        res = run_fold(tmp_path, f"sess-{i}", bad)
        assert res == {"status": "error"}

    err_log = tmp_path / ".ego" / "state" / "fold-errors.jsonl"
    assert err_log.is_file()
    lines = [ln for ln in err_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 50
    last = json.loads(lines[-1])
    assert last["session_id"] == "sess-54"
    assert "at" in last
    assert len(last["error"]) <= 300


def test_spawn_fold_calls_popen_detached_and_swallows_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    spawn_fold(tmp_path, "sess-1", tmp_path / "t.jsonl")

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[:4] == [sys.executable, "-m", "ego", "fold"]
    assert "--root" in cmd and str(tmp_path) in cmd
    assert "--session" in cmd and "sess-1" in cmd
    assert "--transcript" in cmd
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL

    def failing_popen(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    spawn_fold(tmp_path, "sess-2", tmp_path / "t.jsonl")


def test_run_fold_writes_ledger_and_envfacts(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 30,
                },
                "content": [
                    {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "pytest -q"}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu-1", "is_error": False, "content": "1 passed"},
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 10,
                },
                "content": [],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    res = run_fold(tmp_path, "sess-ok", transcript)
    assert res["status"] == "ok"
    assert res["usage_events"] == 2
    assert res["observations"] >= 1

    ledger_path = tmp_path / ".ego" / "state" / "usage-ledger.jsonl"
    assert ledger_path.is_file()
    ledger_row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[-1])
    assert ledger_row["input_tokens"] == 140
    assert ledger_row["output_tokens"] == 60
    assert ledger_row["cache_read_tokens"] == 200
    assert ledger_row["cache_write_5m_tokens"] == 30
    assert ledger_row["model"] == "claude-opus-4-6"
    assert ledger_row["category"] == "agent"
    assert ledger_row["caller"] == "interactive"

    envfacts_path = tmp_path / ".ego" / "state" / "envfacts.json"
    assert envfacts_path.is_file()


def test_cli_fold_and_session_end_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-sonnet-4-6", "usage": {"input_tokens": 5, "output_tokens": 2}, "content": []}}) + "\n",
        encoding="utf-8",
    )

    rc = main(["fold", "--root", str(tmp_path), "--session", "s-cli", "--transcript", str(transcript)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"

    spawned: list[tuple] = []
    monkeypatch.setattr("ego.usecases.fold.spawn_fold", lambda root, sid, tp: spawned.append((Path(root), sid, tp)))
    payload = json.dumps({"session_id": "s-hook", "transcript_path": str(transcript)})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert main(["hook", "session-end", "--host", "claude", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "{}"
    assert spawned == [(tmp_path, "s-hook", str(transcript))]
