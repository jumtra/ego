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
import shlex
import shutil
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
# How each CLI opens a NEW interactive session that starts with a prompt (checked against each CLI's --help).
HOST_LAUNCH = {"claude": ("claude",), "codex": ("codex",), "opencode": ("opencode", "--prompt"),
               "agy": ("agy", "--prompt-interactive")}
MAX_LIST_GOAL_CHARS = 120


# What each CLI exports to the commands it runs (names taken from the CLIs themselves).
HOST_ENV = {"claude": ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID"), "codex": ("CODEX_THREAD_ID", "CODEX_CI"),
            "opencode": ("OPENCODE",), "agy": ("ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID")}


def detect_host() -> str | None:
    """The CLI this process runs under, when exactly one says so. None rather than a guess."""
    found = [host for host, names in HOST_ENV.items() if any(os.environ.get(n) for n in names)]
    return found[0] if len(found) == 1 else None


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
    host: str | None = None,
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
    if host is not None and host not in HOST_CLEAR:
        raise SEHError("UNKNOWN_HOST", f"Unsupported interactive host: {host}", 2)
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
        # Which CLI wrote it: lets another CLI see whose work it is picking up.
        host = host or (prev.get("host") if prev else None)
        if host in HOST_CLEAR:
            record["host"] = host
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


USAGE_STATE = ".ego/state/usage_limits.json"
USAGE_ADVICE_STATE = ".ego/state/usage_advice.json"
USAGE_FRESH = 900
_WINDOWS = (("five_hour", "5h", "5時間枠"), ("seven_day", "7d", "7日枠"))


def _windows(limits: Any) -> list[tuple[str, str, str, float, float]]:
    """(key, short, label, used %, resets_at) for each usage window still running."""
    out = []
    for key, short, label in _WINDOWS:
        window = limits.get(key) if isinstance(limits, dict) else None
        try:
            used, resets = float(window["used_percentage"]), float(window["resets_at"])
        except (TypeError, KeyError, ValueError):
            continue
        if 0 <= used <= 100 and resets > time.time():
            out.append((key, short, label, used, resets))
    return out


def record_usage(payload: dict[str, Any], fallback_root: Path | None = None) -> str:
    """Status line command: keep the host-reported usage limits for the Stop hook; return the line to show.

    Claude Code reports `rate_limits` only here (subscription plans, after the first
    response). Nothing is estimated: without that field the line is empty and nothing is stored.
    """
    if not isinstance(payload, dict):
        return ""
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    root = None
    for candidate in (workspace.get("project_dir"), payload.get("cwd"), fallback_root):
        if candidate and (Path(str(candidate)) / ".ego").is_dir():
            root = Path(str(candidate)).resolve()
            break
    windows = _windows(payload.get("rate_limits"))
    if not windows:
        return ""
    if root is not None:
        path = safe_path(root, USAGE_STATE)
        limits = {key: {"used_percentage": used, "resets_at": resets} for key, _, _, used, resets in windows}
        prev = read_json(path)
        # The status line refreshes constantly: rewrite only on change, or to keep the reading fresh.
        if not isinstance(prev, dict) or prev.get("rate_limits") != limits or time.time() - float(prev.get("at") or 0) > 60:
            write_json(path, {"at": time.time(), "rate_limits": limits})
    return "ego · " + " · ".join(f"{short} {used:.0f}%" for _, short, _, used, _ in windows)


def usage_advice(root: Path, payload: dict[str, Any], percent: int, hosts: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """Stop-hook output when the usage limit is nearly spent: have the session checkpoint itself, then propose a switch.

    Only the conversation knows what to save, so this asks for one more short turn
    (decision "block"), once per session and usage window. It never clears or launches anything.
    """
    if percent <= 0 or not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return {}
    root = root.resolve()
    state = read_json(safe_path(root, USAGE_STATE))
    if not isinstance(state, dict) or time.time() - float(state.get("at") or 0) > USAGE_FRESH:
        return {}  # no recent measurement: stay silent rather than guess
    windows = [w for w in _windows(state.get("rate_limits")) if w[3] >= percent]
    if not windows:
        return {}
    key, _, label, used, resets = max(windows, key=lambda w: w[3])
    session = str(payload.get("session_id") or "unknown")[:80]
    mark = f"{key}:{int(resets)}"
    advice_path = safe_path(root, USAGE_ADVICE_STATE)
    with exclusive_lock(safe_path(root, ".ego/locks/usage-advice.lock")):
        seen = read_json(advice_path, {})
        seen = seen if isinstance(seen, dict) else {}
        if seen.get(session, {}).get("mark") == mark:
            return {}
        seen[session] = {"mark": mark, "at": time.time()}
        for old in sorted(seen, key=lambda k: seen[k].get("at", 0))[:-20]:
            seen.pop(old)
        write_json(advice_path, seen)
    others = [h for h in hosts if h in HOST_LAUNCH and h != "claude" and shutil.which(HOST_LAUNCH[h][0])]
    reset = dt.datetime.fromtimestamp(resets).strftime("%m/%d %H:%M")
    size = f"{label}の使用率が {used:.0f}%（しきい値 {percent}%、リセット {reset}）"
    switch = (f"Then run `ego session handoff <task-id> --to <cli> --root .` for the installed alternative(s) "
              f"({', '.join(others)}) and show the user each `launch_command` so they can continue there. "
              if others else
              "No other configured CLI is installed, so tell the user the checkpoint ID and that "
              f"`ego session resume <task-id>` continues it after the limit resets ({reset}). ")
    return {"systemMessage": f"ego: {size}に達しました。チェックポイントを自動保存し、切り替え先を提案します。",
            "decision": "block",
            "reason": f"ego: the Claude usage limit is nearly spent ({key} {used:.0f}% used, resets {reset}). "
                      "Do no further task work now. Save this conversation's state with one "
                      "`ego session checkpoint --task-id <id> --goal ... --completed ... --next ... --evidence ... "
                      "--status <IN_PROGRESS|VERIFIED|BLOCKED|COMPLETED> --root .` (facts only; include uncommitted changes and "
                      "known failures; reuse the task ID if one was already checkpointed). " + switch +
                      "Reply to the user briefly in their language, then stop."}


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


def list_checkpoints(root: Path, include_completed: bool = False) -> list[dict[str, Any]]:
    """Bounded summaries, newest first: how a session in any CLI finds what another one saved."""
    found = []
    for path in (root.resolve() / ".ego" / "memory" / "handoffs").glob("*.json"):
        record = read_json(path)
        if (not isinstance(record, dict) or record.get("schema_version") != SCHEMA or record.get("task_id") != path.stem
                or (record.get("status") == "COMPLETED" and not include_completed)):
            continue
        found.append({"task_id": record["task_id"], "status": record.get("status"), "host": record.get("host"),
                      "source": record.get("source"), "updated_at": str(record.get("updated_at", "")),
                      "goal": " ".join(str(record.get("goal", "")).split())[:MAX_LIST_GOAL_CHARS]})
    return sorted(found, key=lambda r: r["updated_at"], reverse=True)


def resolve_task(root: Path, task_id: str | None, from_host: str | None = None) -> str:
    """An explicit ID, or the most recently saved open interactive checkpoint (of one CLI, with from_host)."""
    if from_host is not None and from_host not in HOST_CLEAR:
        raise SEHError("UNKNOWN_HOST", f"Unsupported interactive host: {from_host}", 2)
    if task_id:
        return _id(task_id)
    for item in list_checkpoints(root):
        if item["source"] == "interactive" and from_host in (None, item["host"]):
            return item["task_id"]
    if from_host:
        raise SEHError("HANDOFF_MISSING", f"No open checkpoint saved by {from_host}. It must be saved there first "
                       f"(`ego session checkpoint ... --host {from_host}`); `ego session list` shows what exists", 3)
    raise SEHError("HANDOFF_MISSING", "No open checkpoint; save one with `ego session checkpoint`", 3)


def pickup(root: Path, task_id: str | None, from_host: str) -> dict[str, Any]:
    """Continue, inside the CLI that is already open, the work another CLI checkpointed. Nothing is cleared."""
    task_id = resolve_task(root, task_id, from_host)
    record = load_checkpoint(root, task_id)
    if record is None:
        raise SEHError("HANDOFF_MISSING", "Save a checkpoint before handing off", 3)
    return {
        "task_id": task_id,
        "from_host": record.get("host"),
        "checkpoint_path": handoff_relpath(task_id),
        "checkpoint_status": record["status"],
        "updated_at": record.get("updated_at"),
        "instructions": "Continue in this session by following resume_prompt. It is the other CLI's saved "
                        "checkpoint, not its conversation: verify against files and git diff before relying on it.",
        "resume_prompt": resume_prompt(record),
    }


def handoff(root: Path, task_id: str | None, to_host: str, from_host: str | None = None) -> dict[str, Any]:
    """Continue a saved checkpoint in another CLI: a new interactive session that opens on the resume prompt.

    The checkpoint is host-neutral project state, so nothing is converted; no
    conversation of the previous CLI is read or carried over.
    """
    if to_host not in HOST_LAUNCH:
        raise SEHError("UNKNOWN_HOST", f"Unsupported interactive host: {to_host}", 2)
    task_id = resolve_task(root, task_id, from_host)
    record = load_checkpoint(root, task_id)
    if record is None:
        raise SEHError("HANDOFF_MISSING", "Save a checkpoint before handing off", 3)
    prompt = resume_prompt(record)
    argv = [*HOST_LAUNCH[to_host], prompt]
    available = shutil.which(argv[0]) is not None
    return {
        "task_id": task_id,
        "from_host": record.get("host"),
        "to_host": to_host,
        "checkpoint_path": handoff_relpath(task_id),
        "checkpoint_status": record["status"],
        "host_available": available,
        "launch_argv": argv,
        "launch_command": shlex.join(argv),
        "launched": False,
        "instructions": ("Run launch_command in a terminal at the project root (or rerun with --launch there) to open "
                         f"{to_host} on the resume prompt. Nothing was started by this command."
                         if available else
                         f"{argv[0]} is not installed on PATH. Install it, or open {to_host} manually and send resume_prompt."),
        "resume_prompt": prompt,
    }


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
