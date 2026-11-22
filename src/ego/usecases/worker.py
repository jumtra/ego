"""Managed workers that keep their own context under the project's token limit.

A headless worker cannot be sent `/clear`. Claude runs in segments of a few turns and
measures context between segments; Codex, OpenCode and AGY run with live stream/session
monitoring and interrupt at `clear_suggest_tokens`. On reaching the limit, ego asks the
session to save a checkpoint (falling back to a mechanical workspace summary on
non-Claude hosts if unavailable) and continues in a fresh session.
"""
from __future__ import annotations
import dataclasses
import os
import time
from pathlib import Path
from typing import Any, Callable
from ..adapters.runner import RunResult
from ..adapters.transcript import claude_transcript_path
from ..domain.config import load_config
from ..domain.usage import billable_tokens
from .session import RUN_ID_ENV, context_tokens, latest_open_checkpoint, opencode_context_tokens, resume_prompt
from ..shared.util import ALTER_ROOT_ENV, clip_output, read_json, snapshot

CONTINUE_PROMPT = "Continue the task from where you stopped."
_MANAGED_HOSTS = ("claude", "codex", "opencode", "agy")


def worker_context_tokens(cwd: Path, session_id: str | None) -> int | None:
    """Measured context size of a worker session, read from Claude's own session record."""
    path = claude_transcript_path(cwd, session_id)
    return context_tokens(path) if path is not None else None


def worker_context_tokens_for_host(host: str, cwd: Path, session_id: str | None) -> int | None:
    """Measured context size for any managed worker host from its own local session store."""
    if host == "claude":
        return worker_context_tokens(cwd, session_id)
    if not session_id or not all(c.isalnum() or c in "-_" for c in session_id):
        return None
    if host == "codex":
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        sessions_dir = home / "sessions"
        if not sessions_dir.is_dir():
            return None
        matches: list[tuple[float, Path]] = []
        for path in sessions_dir.glob(f"*/*/*/rollout-*-{session_id}.jsonl"):
            try:
                matches.append((path.stat().st_mtime, path))
            except OSError:
                continue
        if not matches:
            return None
        matches.sort(reverse=True)
        return context_tokens(matches[0][1], host="codex")
    if host == "opencode":
        return opencode_context_tokens(session_id)
    if host == "agy":
        home = Path(os.environ.get("AGY_DATA_DIR") or Path.home() / ".gemini" / "antigravity-cli")
        path = home / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"
        return context_tokens(path, host="agy") if path.is_file() else None
    return None


_HANDOFF_SNAPSHOT_EXCLUDES = (".git", ".ego", "__pycache__", ".pytest_cache", ".venv", "node_modules")


def _workspace_snapshot(cwd: Path) -> dict[str, str]:
    """Hash regular workspace files (excluding VCS/ego/virtualenv/cache dirs) for mechanical handoff diffs."""
    return snapshot(cwd, excludes=_HANDOFF_SNAPSHOT_EXCLUDES) if cwd.is_dir() else {}


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(k for k in (before.keys() | after.keys()) if before.get(k) != after.get(k))


def _mechanical_handoff(
    host: str, tokens: int, threshold: int, changed_files: list[str], last_output: str, max_chars: int
) -> str:
    shown = changed_files[:40]
    files_line = ", ".join(shown) if shown else "(none)"
    if len(changed_files) > 40:
        files_line += f" ... (+{len(changed_files) - 40} more)"
    excerpt = clip_output(last_output.strip(), max(200, max_chars // 2)) if last_output.strip() else "(none)"
    return (
        f"ego: Context rotation fallback ({host}: {tokens} tokens >= {threshold}). "
        "The previous session stopped at the context limit without saving a structured checkpoint; "
        "in-progress edits remain in this working directory. "
        "Treat this note as unverified and inspect the actual files / git diff before continuing.\n"
        f"Changed files in workspace: {files_line}\n"
        f"Last session output excerpt:\n{excerpt}"
    )


def _merge(results: list[RunResult], *, status: str | None = None, reason: str | None = None, rotations: int = 0) -> RunResult:
    last = results[-1]
    usage: dict[str, Any] = {}
    for r in results:
        for key, value in (r.usage or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    costs = [r.cost_usd for r in results]
    output = last.output or next((r.output for r in reversed(results) if r.output), "")
    return dataclasses.replace(
        last, status=status or last.status, output=output, reason=reason if reason is not None else last.reason,
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
               project_root: Path, on_event: Callable[..., None] | None = None,
               token_budget: Any = None, **kw: Any) -> RunResult:
    emit = on_event or (lambda **_: None)
    env = {
        **os.environ,
        ALTER_ROOT_ENV: str(project_root.resolve()),
        "EGO_ALTER_SESSION_RULES": "0",
        "EGO_ALTER_PROMPT_CONTEXT": "0",
    }
    if host not in _MANAGED_HOSTS or read_only or threshold <= 0:
        return agent(host, prompt, cwd, timeout, max_turns=max_turns, read_only=read_only, env=env, **kw)
    from .session import _save_request
    env |= {"EGO_RUN": host, RUN_ID_ENV: tag}
    deadline = time.monotonic() + timeout
    cfg = load_config(project_root)
    results: list[RunResult] = []
    used = rotations = 0
    session: str | None = None
    text = prompt
    consumed: dict[str, int] = {}
    base_snap = _workspace_snapshot(cwd) if host != "claude" else {}
    max_rotations = max(3, min(20, max_turns))

    def out_of_tokens() -> bool:
        # Segments of this worker are not charged to the shared budget until the node returns, so include them here.
        return token_budget is not None and bool(results) and \
            token_budget.exhausted(sum(billable_tokens(r.usage) for r in results))

    def apply_checkpoint(checkpoint: dict[str, Any], tokens: int) -> str:
        consumed[checkpoint["task_id"]] = int(checkpoint.get("revision", 1))
        emit(segment=len(results), tokens=tokens, action="rotate")
        return prompt + "\n\n" + resume_prompt(checkpoint, limits=cfg.session_schema_limits())

    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0 and results:
                return _merge(results, status="TIMEOUT", rotations=rotations)
            if out_of_tokens():
                return _merge(results, status="FAIL", reason="BUDGET_EXHAUSTED", rotations=rotations)
            if host == "claude":
                turns = max(1, min(segment_turns, max_turns - used))
                r = agent(host, text, cwd, max(1, int(left)), max_turns=turns, read_only=False, resume_session=session,
                          env=env, **kw)
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
                if out_of_tokens():
                    return _merge(results, status="FAIL", reason="BUDGET_EXHAUSTED", rotations=rotations)
                saved = agent(host, _save_request(tokens, threshold), cwd, max(1, int(deadline - time.monotonic())), max_turns=cfg.tuning.max_save_turns,
                              read_only=False, resume_session=r.session_id, env=env, **kw)
                results.append(saved)
                checkpoint = latest_open_checkpoint(cwd, run=tag, consumed=consumed)
                if checkpoint is None:
                    return _merge(results, status="FAIL", reason="NO_CHECKPOINT", rotations=rotations)
                session, text = None, apply_checkpoint(checkpoint, tokens)
                rotations += 1
                continue

            probe = lambda sid: worker_context_tokens_for_host(host, cwd, sid)
            call_kw = {**kw, "context_limit": threshold, "context_probe": probe}
            if session is not None:
                call_kw["resume_session"] = session
            r = agent(host, text, cwd, max(1, int(left)), max_turns=max_turns, read_only=False, env=env, **call_kw)
            results.append(r)
            if r.status != "PASS" or r.stop_reason != "context_limit":
                return _merge(results, rotations=rotations)
            if rotations >= max_rotations:
                return _merge(results, status="FAIL", reason="MAX_ROTATIONS", rotations=rotations)
            tokens = r.context_tokens or worker_context_tokens_for_host(host, cwd, r.session_id) or threshold
            if out_of_tokens():
                return _merge(results, status="FAIL", reason="BUDGET_EXHAUSTED", rotations=rotations)
            checkpoint = None
            if r.session_id:
                save_left = deadline - time.monotonic()
                if save_left <= 0:
                    return _merge(results, status="TIMEOUT", rotations=rotations)
                saved = agent(host, _save_request(tokens, threshold), cwd, max(1, int(save_left)), max_turns=cfg.tuning.max_save_turns,
                              read_only=False, resume_session=r.session_id, env=env, **kw)
                results.append(saved)
                checkpoint = latest_open_checkpoint(cwd, run=tag, consumed=consumed)
            if checkpoint is not None:
                session, text = None, apply_checkpoint(checkpoint, tokens)
            else:
                changed = _changed_paths(base_snap, _workspace_snapshot(cwd))
                last_out = r.output or next((x.output for x in reversed(results) if x.output), "")
                fallback = _mechanical_handoff(host, tokens, threshold, changed, last_out, cfg.tuning.max_feedback_chars)
                emit(segment=len(results), tokens=tokens, action="rotate", fallback=True)
                session, text = None, prompt + "\n\n" + fallback
            rotations += 1
    finally:
        _drop_handoffs(cwd, tag)
