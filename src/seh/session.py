"""Small, durable session handoffs; no model call, daemon or transcript storage.

Interactive hosts own their conversation state.  We save a bounded, factual
checkpoint and return the appropriate /clear instruction, never pretend we
can remotely clear an already-running terminal or send a slash command to a
non-interactive CLI.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .util import SEHError, exclusive_lock, now, read_json, redact, safe_path, write_json

SCHEMA = 1
MAX_ITEM_CHARS = 350
MAX_GOAL_CHARS = 800
MAX_NOTES = 8
MAX_PROMPT_CHARS = 2400
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


def resume_prompt(checkpoint: dict[str, Any]) -> str:
    """Load a succinct and self-contained context, not the whole conversation."""
    task_id = _id(checkpoint["task_id"])
    lines = [
        f"Resume task {task_id} from the durable checkpoint, NOT the previous chat transcript.",
        f"Read {handoff_relpath(task_id)} and inspect actual files/git diff before acting.",
        f"Objective: {checkpoint['goal']}",
        "Checkpoint status: " + checkpoint["status"],
    ]
    for title, key in (("Verified progress", "completed"), ("Next steps", "next_steps"),
                       ("Decisions", "decisions"), ("Blockers", "blockers"), ("Evidence pointers", "evidence")):
        if checkpoint.get(key):
            lines.append(f"{title}: " + "; ".join(str(v) for v in checkpoint[key][:MAX_NOTES]))
    lines.append("Use file and test evidence as truth; do not assume this checkpoint proves completion."
                 " Run the narrowest relevant checks and clearly state COMPLETED, BLOCKED or IN_PROGRESS.")
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


def rotation_instructions(root: Path, task_id: str, host: str = "claude") -> dict[str, Any]:
    if host not in HOST_CLEAR:
        raise SEHError("UNKNOWN_HOST", f"Unsupported interactive host: {host}", 2)
    record = load_checkpoint(root, task_id)
    if record is None:
        raise SEHError("HANDOFF_MISSING", "Save a checkpoint before clearing context", 3)
    return {
        "task_id": task_id,
        "checkpoint_path": handoff_relpath(task_id),
        "checkpoint_status": record["status"],
        "clear_command": HOST_CLEAR[host],
        "clear_is_manual": True,
        "instructions": "Run clear_command in the IDLE interactive host only, then send resume_prompt. "
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
