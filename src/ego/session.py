"""Small, durable session handoffs; no model call, daemon or transcript storage.

Interactive hosts own their conversation state.  We save a bounded, factual
checkpoint and return the appropriate /clear instruction, never pretend we
can remotely clear an already-running terminal or send a slash command to a
non-interactive CLI.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .util import SEHError, exclusive_lock, now, read_json, redact, safe_path, write_json

SCHEMA = 1
MAX_ITEM_CHARS = 350
MAX_GOAL_CHARS = 800
MAX_NOTES = 8
MAX_PROMPT_CHARS = 1600
HOST_CLEAR = {"claude": "/clear", "codex": "/clear", "opencode": "/new", "agy": None}


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise SEHError("INVALID_TASK_ID", "Use an alphanumeric task ID (max 80 chars) with '-' or '_'", 2)
    return value


def _notes(values: list[str] | tuple[str, ...] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)) or len(values) > MAX_NOTES:
        raise SEHError("INVALID_HANDOFF", f"At most {MAX_NOTES} notes per field", 2)
    if not all(isinstance(x, str) for x in values):
        raise SEHError("INVALID_HANDOFF", "Notes must be strings", 2)
    return [redact(x.strip())[:MAX_ITEM_CHARS] for x in values if x.strip()]


def handoff_relpath(task_id: str) -> str:
    return ".ego/memory/handoffs/" + _id(task_id) + ".json"


def load_checkpoint(root: Path, task_id: str) -> dict[str, Any] | None:
    path = safe_path(root, handoff_relpath(task_id))
    record = read_json(path)
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA or record.get("task_id") != task_id:
        raise SEHError("INVALID_HANDOFF", "Checkpoint schema or task ID mismatch", 2)
    return record


def save_checkpoint(
    root: Path,
    task_id: str,
    *,
    goal: str,
    completed: list[str] | tuple[str, ...] | None = None,
    next_steps: list[str] | tuple[str, ...] | None = None,
    decisions: list[str] | tuple[str, ...] | None = None,
    blockers: list[str] | tuple[str, ...] | None = None,
    evidence: list[str] | tuple[str, ...] | None = None,
    status: str = "IN_PROGRESS",
    source: str = "interactive",
) -> dict[str, Any]:
    """Atomically replace, do not append to an ever-growing history.

    Inputs are explicit facts/artifact pointers, not unverified model claims.
    Redaction is best-effort; callers must not pass secrets or raw logs.
    """
    _id(task_id)
    if status not in {"IN_PROGRESS", "VERIFIED", "COMPLETED", "BLOCKED"}:
        raise SEHError("INVALID_HANDOFF", "Unsupported checkpoint status", 2)
    if source not in {"interactive", "managed"}:
        raise SEHError("INVALID_HANDOFF", "Unsupported source", 2)
    if not isinstance(goal, str) or not goal.strip():
        raise SEHError("INVALID_HANDOFF", "Nonempty goal required", 2)
    root = root.resolve()
    path = safe_path(root, handoff_relpath(task_id))
    lock = safe_path(root, f".ego/locks/handoff-{task_id}.lock")
    with exclusive_lock(lock):
        prev = load_checkpoint(root, task_id)
        record = {
            "schema_version": SCHEMA,
            "task_id": task_id,
            "status": status,
            "goal": redact(goal.strip())[:MAX_GOAL_CHARS],
            "completed": _notes(completed),
            "next_steps": _notes(next_steps),
            "decisions": _notes(decisions),
            "blockers": _notes(blockers),
            "evidence": _notes(evidence),
            "source": source,
            "created_at": prev.get("created_at", now()) if prev else now(),
            "updated_at": now(),
            "revision": int(prev.get("revision", 0)) + 1 if prev else 1,
        }
        write_json(path, record)
    return {**record, "path": handoff_relpath(task_id)}


# Highest-value fields first: when the budget runs out, what to do next survives
# and what is already done (re-derivable from files and git) is dropped.
_PROMPT_FIELDS = (("Next", "next_steps"), ("Blockers", "blockers"), ("Decisions", "decisions"),
                  ("Done", "completed"), ("Evidence", "evidence"))
_PROMPT_TAIL = ("A record, not proof: check files/git diff first, run the narrowest checks, "
                "then state COMPLETED, BLOCKED or IN_PROGRESS.")


def resume_prompt(checkpoint: dict[str, Any], *, limit: int = MAX_PROMPT_CHARS, brief: bool = False) -> str:
    """The handoff as it enters the next context: every character here is paid for again.

    Self-contained, so the reader does not also load the checkpoint file; the
    file is named only when something had to be left out. `brief` drops the
    framing sentences for callers that already supply their own.
    """
    task_id = _id(checkpoint["task_id"])
    head = (f"Task {task_id} [{checkpoint['status']}]" if brief else
            f"Resume task {task_id} [{checkpoint['status']}] from this checkpoint, not the previous chat.")
    pointer = f"More in {handoff_relpath(task_id)}."
    tail = "" if brief else _PROMPT_TAIL
    budget = limit - len(head) - len(pointer) - len(tail) - 2
    goal = "Goal: " + " ".join(str(checkpoint["goal"]).split())
    if len(goal) > budget // 2:
        goal = goal[:max(0, budget // 2)]
    lines = [head, goal]
    budget -= len(goal) + 1
    seen: set[str] = set()
    omitted = False
    for title, key in _PROMPT_FIELDS:
        items: list[str] = []
        for value in (checkpoint.get(key) or [])[:MAX_NOTES]:
            value = " ".join(str(value).split())
            if not value or value in seen:
                continue
            cost = len(value) + 2 + (0 if items else len(title) + 1)
            if cost > budget:
                omitted = True
                continue
            seen.add(value)
            items.append(value)
            budget -= cost
        if items:
            lines.append(f"{title}: " + "; ".join(items))
    if omitted:
        lines.append(pointer)
    if tail:
        lines.append(tail)
    return "\n".join(lines)[:limit]


ADVICE_STATE = ".ego/state/session_advice.json"
CLEAR_PENDING = ".ego/state/clear_pending.json"
PENDING_TTL = 1800
FRESH_CHECKPOINT = 1800
RESUGGEST_GROWTH = 1.25
# The host cannot submit a prompt after /clear: the context is added silently and waits for a message.
AFTER_CLEAR = "再開コンテキストは自動で読み込まれるので、/clear 後に「続けて」と送ってください。"


def context_tokens(transcript: Path, tail_bytes: int = 512 * 1024) -> int | None:
    """Measured size of the main conversation: the last request's prompt tokens.

    Read from the host transcript tail (input + cache read + cache creation of
    the latest main-chain assistant message). None when it cannot be measured;
    callers must then stay silent rather than guess.
    """
    try:
        with transcript.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            lines = f.read().decode("utf8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant" or obj.get("isSidechain"):
            continue
        usage = obj.get("message", {}).get("usage") if isinstance(obj.get("message"), dict) else None
        if isinstance(usage, dict):
            total = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if total > 0:
                return total
    return None


def _age_seconds(stamp: str) -> float:
    try:
        return max(0.0, time.time() - dt.datetime.fromisoformat(stamp).timestamp())
    except (TypeError, ValueError):
        return float("inf")


def latest_open_checkpoint(root: Path, max_age: int = FRESH_CHECKPOINT,
                           exclude: set[str] | frozenset[str] = frozenset()) -> dict[str, Any] | None:
    best = None
    for path in (root / ".ego" / "memory" / "handoffs").glob("*.json"):
        record = read_json(path)
        if (not isinstance(record, dict) or record.get("schema_version") != SCHEMA or record.get("status") == "COMPLETED"
                or record.get("source") != "interactive" or record.get("task_id") in exclude or _age_seconds(record.get("updated_at", "")) > max_age):
            continue
        if best is None or record["updated_at"] > best["updated_at"]:
            best = record
    return best


def _pending_tasks(root: Path) -> dict[str, dict[str, Any]]:
    """Checkpoints awaiting a /clear, per task: several sessions may share one project."""
    raw = read_json(safe_path(root, CLEAR_PENDING))
    if not isinstance(raw, dict):
        return {}
    tasks = raw.get("tasks")
    if not isinstance(tasks, dict):  # single-task file written by an earlier version
        tasks = {raw["task_id"]: {"at": raw.get("at", 0)}} if isinstance(raw.get("task_id"), str) else {}
    fresh = {}
    for task_id, entry in tasks.items():
        try:
            if isinstance(entry, dict) and time.time() - float(entry.get("at", 0)) <= PENDING_TTL:
                fresh[_id(task_id)] = entry
        except (SEHError, TypeError, ValueError):
            continue
    return fresh


def _store_pending(root: Path, tasks: dict[str, dict[str, Any]]) -> None:
    path = safe_path(root, CLEAR_PENDING)
    if tasks:
        write_json(path, {"tasks": tasks})
    else:
        path.unlink(missing_ok=True)


def _pending_lock(root: Path):
    return exclusive_lock(safe_path(root, ".ego/locks/clear-pending.lock"))


def _mark_clear_pending(root: Path, task_id: str, session: str | None = None) -> None:
    _id(task_id)
    with _pending_lock(root):
        tasks = _pending_tasks(root)
        entry: dict[str, Any] = {"at": time.time()}
        # Who armed it: lets another session's Stop hook tell this checkpoint is not its own.
        session = session or (tasks.get(task_id) or {}).get("session")
        if session:
            entry["session"] = str(session)[:80]
        tasks[task_id] = entry
        _store_pending(root, tasks)


def _disarm_clear_pending(root: Path, task_id: str) -> None:
    with _pending_lock(root):
        tasks = _pending_tasks(root)
        if tasks.pop(task_id, None) is not None:
            _store_pending(root, tasks)


def auto_resume_installed(root: Path) -> bool:
    try:
        return "hook session-start" in (root / ".claude" / "settings.json").read_text(encoding="utf8")
    except OSError:
        return False


def arm_auto_resume(root: Path, task_id: str, status: str | None = None) -> bool:
    """Interactive checkpoint saved: the next SessionStart(clear) injects it, when auto-resume is installed.

    Without this, a manual "save checkpoint" then /clear never arms the pending resume
    (only the Stop-hook suggestion and `session resume` did), so nothing was injected.
    """
    root = root.resolve()
    if not auto_resume_installed(root):
        return False
    if status == "COMPLETED":  # nothing left to resume; do not leave it competing with live tasks
        _disarm_clear_pending(root, task_id)
        return False
    _mark_clear_pending(root, task_id, os.environ.get("CLAUDE_CODE_SESSION_ID"))
    return True


def clear_advice(root: Path, payload: dict[str, Any], threshold: int) -> dict[str, Any]:
    """Stop-hook output: tell the user, once per growth step, that a fresh session is due.

    Shown to the user only (systemMessage); it adds nothing to the model's
    context and never clears or blocks anything.
    """
    if threshold <= 0 or not isinstance(payload, dict) or not isinstance(payload.get("transcript_path"), str):
        return {}
    tokens = context_tokens(Path(payload["transcript_path"]).expanduser())
    session = str(payload.get("session_id") or "unknown")[:80]
    root = root.resolve()
    state_path = safe_path(root, ADVICE_STATE)
    # Every session's Stop hook shares this file: serialize the read-modify-write.
    with exclusive_lock(safe_path(root, ".ego/locks/session-advice.lock")):
        state = read_json(state_path, {})
        state = state if isinstance(state, dict) else {}
        last = state.get(session, {}).get("tokens")
        if tokens is None or tokens < threshold:
            if last is not None and tokens is not None:  # compacted or cleared: allow a new suggestion later
                state.pop(session, None); write_json(state_path, state)
            return {}
        if last is not None and last <= tokens < last * RESUGGEST_GROWTH:
            return {}
        state[session] = {"tokens": tokens, "at": time.time()}
        for old in sorted(state, key=lambda k: state[k].get("at", 0))[:-20]:
            state.pop(old)
        write_json(state_path, state)
    size = f"約 {tokens // 1000}k トークン（提案しきい値 {threshold // 1000}k）"
    # A checkpoint another live session saved says nothing about this conversation:
    # offering it here would have the user clear unsaved work.
    foreign = {t for t, e in _pending_tasks(root).items() if e.get("session") and e["session"] != session}
    checkpoint = latest_open_checkpoint(root, exclude=foreign)
    if checkpoint:
        _mark_clear_pending(root, checkpoint["task_id"], session if session != "unknown" else None)
        return {"systemMessage": f"ego: コンテキストは{size}です。チェックポイント {checkpoint['task_id']}（{checkpoint['status']}）は保存済みです。"
                                 "区切りが良ければ /clear を実行してください。" + AFTER_CLEAR}
    return {"systemMessage": f"ego: コンテキストは{size}です。区切りの良いところで「チェックポイントを保存して」と依頼し、"
                             "その後 /clear を実行してください。" + AFTER_CLEAR}


def clear_resume(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """SessionStart(clear) output: inject the pending checkpoint's resume prompt, once."""
    if not isinstance(payload, dict) or payload.get("source") != "clear":
        return {}
    root = root.resolve()
    with _pending_lock(root):
        tasks = _pending_tasks(root)
        if len(tasks) > 1:
            # Parallel sessions: /clear starts a new session ID, so which checkpoint belongs
            # to this terminal cannot be known. Name them; never inject another session's task.
            ids = ", ".join(sorted(tasks))
            return {"systemMessage": f"ego: 再開待ちのチェックポイントが複数あります（{ids}）。並列セッションの取り違えを防ぐため"
                                     "自動読み込みはしません。「<task-id> を続けて」と送ってください。",
                    "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                        f"Checkpoints awaiting resume in this project (parallel sessions): {ids}. Do not guess which is "
                        "this session's. When the user names one, run `ego session resume <task-id> --root .` "
                        "and follow its resume_prompt."}}
        _store_pending(root, {})
    if not tasks:
        return {}
    try:
        record = load_checkpoint(root, next(iter(tasks)))
    except SEHError:
        return {}
    if record is None:
        return {}
    # additionalContext is silent and starts no turn, so also tell the user what to do next.
    return {"systemMessage": f"ego: チェックポイント {record['task_id']} の再開コンテキストを読み込みました。「続けて」と送ると再開します。",
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": resume_prompt(record)}}


def rotation_instructions(root: Path, task_id: str, host: str = "claude") -> dict[str, Any]:
    if host not in HOST_CLEAR:
        raise SEHError("UNKNOWN_HOST", f"Unsupported interactive host: {host}", 2)
    record = load_checkpoint(root, task_id)
    if record is None:
        raise SEHError("HANDOFF_MISSING", "Save a checkpoint before clearing context", 3)
    auto = host == "claude" and auto_resume_installed(root.resolve())
    if auto:
        _mark_clear_pending(root.resolve(), task_id)
    return {
        "auto_resume_after_clear": auto,
        "task_id": task_id,
        "checkpoint_path": handoff_relpath(task_id),
        "checkpoint_status": record["status"],
        "clear_command": HOST_CLEAR[host],
        "clear_is_manual": True,
        "instructions": "Ask the user to run clear_command; the resume prompt is injected automatically "
                        "within 30 minutes. No clear operation was executed by this command."
                        if auto else
                        "Run clear_command in the IDLE interactive host only, then send resume_prompt. "
                        "No clear operation was executed by this command."
                        if HOST_CLEAR[host] else "No verified /clear integration for this host. "
                        "Start a new session manually and send resume_prompt.",
        "resume_prompt": resume_prompt(record),
    }


def managed_checkpoint(root: Path, record: dict[str, Any], *, state: str) -> dict[str, Any]:
    """Persist one compact checkpoint at a DAG wave boundary or at termination."""
    spec = record.get("spec", {})
    nodes = spec.get("nodes", [])
    statuses = record.get("node_status", {})
    done = [f"{n['id']} ({statuses[n['id']]})" for n in nodes if statuses.get(n["id"]) == "PASSED"]
    remaining = [n["id"] for n in nodes if statuses.get(n["id"]) != "PASSED"]
    blocks = [f"{nid}: {s}" for nid, s in statuses.items() if s != "PASSED"]
    paths: list[str] = []
    for data in record.get("results", {}).values():
        paths.extend(data.get("changed", []))
    paths = sorted(set(paths))[:MAX_NOTES]
    if state == "COMPLETED":
        remaining = []
    return save_checkpoint(
        root, record["run_id"], goal=spec.get("goal") or "Finish the declared DAG task",
        completed=done[:MAX_NOTES], next_steps=remaining[:MAX_NOTES], blockers=blocks[:MAX_NOTES],
        evidence=[f"run: {record['run_id']}", *[f"file: {path}" for path in paths]][:MAX_NOTES],
        status=state, source="managed",
    )


def completion_message(record: dict[str, Any]) -> str:
    state = record.get("final_status")
    if state == "PASSED" and record.get("applied"):
        return "完了：変更を適用し、指定された検証に合格しました。"
    if state == "PASSED":
        return "検証完了（未適用）：作業用コピーで検証済みです。変更はまだ元のプロジェクトへ適用していません。"
    if state == "COMPARISON_READY":
        return "比較実行完了（採用待ち）：比較結果を確認して採用する候補を選んでください。"
    return f"未完了：実行状態は {state or 'UNKNOWN'} です。チェックポイントから再開してください。"
