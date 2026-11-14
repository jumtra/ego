"""Managed Claude workers that keep their own context under the project's token limit.

A headless worker cannot be sent `/clear`, so it runs in segments of a few turns.
Between segments ego measures the session's real context size. Below the limit the
same session continues (`--resume`); at or above it the session is asked to save a
checkpoint, and the work continues in a fresh session started from that checkpoint.
The other hosts, and read-only nodes, run once exactly as before.
"""
from __future__ import annotations
import dataclasses
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from .runner import RunResult
from .session import RUN_ID_ENV, context_tokens, latest_open_checkpoint, resume_prompt
from .usage import TOKEN_KEYS
from .util import read_json

CONTINUE_PROMPT = "Continue the task from where you stopped."


def worker_context_tokens(cwd: Path, session_id: str | None) -> int | None:
    """Measured context size of a worker session, read from Claude's own session record."""
    if not session_id or not all(c.isalnum() or c in "-_" for c in session_id):
        return None
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for base in (str(cwd), str(cwd.resolve())):
        path = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", base) / f"{session_id}.jsonl"
        if path.is_file():
            return context_tokens(path)
    return None


def _merge(results: list[RunResult], *, status: str | None = None, reason: str | None = None, rotations: int = 0) -> RunResult:
    last = results[-1]
    usage: dict[str, Any] = {}
    for r in results:
        for key, value in (r.usage or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    costs = [r.cost_usd for r in results]
    return dataclasses.replace(
        last, status=status or last.status, reason=reason if reason is not None else last.reason,
        usage=usage or None, cost_usd=None if any(c is None for c in costs) else round(sum(costs), 6),
        tokens=sum(r.tokens or 0 for r in results) or last.tokens,
        elapsed_seconds=round(sum(r.elapsed_seconds for r in results), 4),
        segments=len(results), rotations=rotations)


def _drop_handoffs(cwd: Path, tag: str) -> None:
    """The saved checkpoint belongs to ego, not to the worker's change set."""
    for path in (cwd / ".ego" / "memory" / "handoffs").glob("*.json"):
        record = read_json(path)
        if isinstance(record, dict) and record.get("run") == tag:
            path.unlink(missing_ok=True)


def run_worker(host: str, prompt: str, cwd: Path, timeout: int, *, agent: Callable[..., RunResult], threshold: int,
               segment_turns: int, tag: str, max_turns: int, read_only: bool,
               on_event: Callable[..., None] | None = None, max_budget_usd: float | None = None, **kw: Any) -> RunResult:
    emit = on_event or (lambda **_: None)
    if host != "claude" or read_only or threshold <= 0:
        return agent(host, prompt, cwd, timeout, max_turns=max_turns, read_only=read_only,
                     max_budget_usd=max_budget_usd, **kw)
    from .ptyrun import _save_request
    env = {**os.environ, "EGO_RUN": "claude", RUN_ID_ENV: tag}
    deadline = time.monotonic() + timeout
    results: list[RunResult] = []
    used = rotations = 0
    session: str | None = None
    text = prompt

    def spent() -> float:
        return sum(r.cost_usd or 0.0 for r in results)

    while True:
        left = deadline - time.monotonic()
        if left <= 0 and results:
            return _merge(results, status="TIMEOUT", rotations=rotations)
        remaining = None
        if max_budget_usd is not None:
            remaining = max_budget_usd - spent()
            if remaining <= 0 and results:
                return _merge(results, status="FAIL", reason="BUDGET_EXHAUSTED", rotations=rotations)
        turns = max(1, min(segment_turns, max_turns - used))
        r = agent(host, text, cwd, max(1, int(left)), max_turns=turns, read_only=False, resume_session=session,
                  env=env, max_budget_usd=remaining, **kw)
        results.append(r)
        used += int((r.usage or {}).get("turns") or turns)
        if r.status != "PASS" or r.stop_reason != "error_max_turns":
            return _merge(results, rotations=rotations)
        if used >= max_turns:
            return _merge(results, status="FAIL", reason="MAX_TURNS", rotations=rotations)
        tokens = worker_context_tokens(cwd, r.session_id)
        if tokens is None or tokens < threshold:
            emit(segment=len(results), tokens=tokens, action="continue")
            session, text = r.session_id, CONTINUE_PROMPT
            continue
        saved = agent(host, _save_request(tokens, threshold), cwd, max(1, int(deadline - time.monotonic())), max_turns=3,
                      read_only=False, resume_session=r.session_id, env=env, max_budget_usd=remaining, **kw)
        results.append(saved)
        checkpoint = latest_open_checkpoint(cwd, run=tag)
        if checkpoint is None:
            return _merge(results, status="FAIL", reason="NO_CHECKPOINT", rotations=rotations)
        _drop_handoffs(cwd, tag)
        emit(segment=len(results), tokens=tokens, action="rotate")
        session, text = None, prompt + "\n\n" + resume_prompt(checkpoint)
        rotations += 1
