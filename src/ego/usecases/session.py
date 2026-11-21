"""Small, durable session handoffs; no model call, daemon or transcript storage.

Interactive hosts own their conversation state.  We save a bounded, factual
checkpoint and return the appropriate /clear instruction, never pretend we
can remotely clear an already-running terminal or send a slash command to a
non-interactive CLI.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..shared.util import EgoError, alter_project_root, exclusive_lock, is_locked, now, read_json, redact, safe_path, write_json
from ..shared.util import RUN_SESSION_ENV, _id, detect_host, handoff_relpath
from ..domain.config import load_config
from ..domain.task_routing import resume_prompt
from ..domain.session_schema import (
    CHECKPOINT_KINDS,
    DEFAULT_SESSION_SCHEMA_LIMITS,
    SCHEMA,
    SessionSchemaLimits,
    validate_checkpoint_schema,
)

HOST_CLEAR = {"claude": "/clear", "codex": "/clear", "opencode": "/new", "agy": "/clear"}
# How each CLI opens a NEW interactive session that starts with a prompt (checked against each CLI's --help).
HOST_LAUNCH = {"claude": ("claude",), "codex": ("codex",), "opencode": ("opencode", "--prompt"),
               "agy": ("agy", "--prompt-interactive")}
MAX_LIST_GOAL_CHARS = 120


def _notes(values: list[str] | tuple[str, ...] | None, *, limits: SessionSchemaLimits = DEFAULT_SESSION_SCHEMA_LIMITS) -> list[str]:
    """Clean and bound notes mechanically: the newest max_notes survive, so a longer list never fails."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise EgoError("INVALID_HANDOFF", "Notes must be a list", 2)
    if not all(isinstance(x, str) for x in values):
        raise EgoError("INVALID_HANDOFF", "Notes must be strings", 2)
    cleaned = [redact(x.strip())[:limits.max_item_chars] for x in values if x.strip()]
    return list(dict.fromkeys(cleaned))[-limits.max_notes:]


def _merge_notes(prev_items: list[str], new_items: list[str], *, limits: SessionSchemaLimits = DEFAULT_SESSION_SCHEMA_LIMITS) -> list[str]:
    """Keep earlier revisions' notes (e.g. past decisions) when a checkpoint is saved again."""
    return list(dict.fromkeys([*prev_items, *new_items]))[-limits.max_notes:]


_PLAN_KINDS = ("plan_ready", "plan_resume")


def _infer_kind(root: Path, task_id: str, status: str, prev_open: dict[str, Any] | None) -> str:
    """kind が省略されたときの自動判定。task JSON と run の有無で計画フェーズを見分ける。"""
    if status == "COMPLETED":
        return "completed"
    prev_kind = (prev_open.get("kind") if prev_open else None) or "ongoing"
    task_file = root / ".ego" / "state" / "tasks" / f"{task_id}.json"
    runs_dir = root / ".ego" / "state" / "runs"
    pattern = re.compile(rf"^run-{re.escape(task_id)}(-\d+)?$")
    has_run = runs_dir.is_dir() and any(pattern.match(p.name) for p in runs_dir.iterdir())
    if task_file.is_file() and not has_run:
        return "plan_ready"
    if prev_kind in _PLAN_KINDS:
        return "ongoing" if has_run else "plan_resume"
    return prev_kind


def _run_lock_held(root: Path, task_id: str) -> bool:
    """True while a live run process holds its OS flock (a RUNNING record without it is interrupted)."""
    return is_locked(safe_path(root.resolve(), f".ego/locks/{_id(task_id)}.lock"))


def _guard_run_active(root: Path, task_id: str) -> None:
    record = load_checkpoint(root, task_id)
    if record is not None and record.get("source") == "managed" and _run_lock_held(root, task_id):
        raise EgoError("RUN_ACTIVE", f"Run {task_id} is currently active in another process", 3)


def load_checkpoint(root: Path, task_id: str, *, limits: SessionSchemaLimits | None = None) -> dict[str, Any] | None:
    root = root.resolve()
    path = safe_path(root, handoff_relpath(task_id))
    record = read_json(path)
    if record is None:
        return None
    eff_limits = limits or load_config(root).session_schema_limits()
    validate_checkpoint_schema(record, limits=eff_limits)
    if record.get("task_id") != task_id:
        raise EgoError("INVALID_HANDOFF", "Checkpoint task ID mismatch", 2)
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
    kind: str | None = None,
    source: str = "interactive",
    host: str | None = None,
    run: str | None = None,
    limits: SessionSchemaLimits | None = None,
) -> dict[str, Any]:
    """Atomically replace, do not append to an ever-growing history.

    Inputs are explicit facts/artifact pointers, not unverified model claims.
    Redaction is best-effort; callers must not pass secrets or raw logs.
    """
    _id(task_id)
    if host is not None and host not in HOST_CLEAR:
        raise EgoError("UNKNOWN_HOST", f"Unsupported interactive host: {host}", 2)
    if kind is not None and kind not in CHECKPOINT_KINDS:
        raise EgoError("INVALID_HANDOFF", f"Unsupported checkpoint kind: {kind}", 2)
    root = root.resolve()
    eff_limits = limits or load_config(root).session_schema_limits()
    path = safe_path(root, handoff_relpath(task_id))
    lock = safe_path(root, f".ego/locks/handoff-{task_id}.lock")
    with exclusive_lock(lock):
        prev = load_checkpoint(root, task_id, limits=eff_limits)
        # A re-save of an open task keeps what earlier saves recorded; a COMPLETED one starts fresh.
        prev_open = prev if prev is not None and prev.get("status") != "COMPLETED" else None

        def history(key: str, values) -> list[str]:
            new = _notes(values, limits=eff_limits)
            return _merge_notes(prev_open.get(key) or [], new, limits=eff_limits) if prev_open else new

        def inherit(key: str, values, statuses: tuple[str, ...]) -> list[str]:
            if values is None and prev_open is not None and status in statuses:
                return list(prev_open.get(key) or [])
            return _notes(values, limits=eff_limits)

        resolved_kind = kind or _infer_kind(root, task_id, status, prev_open)
        record = {
            "schema_version": SCHEMA,
            "task_id": task_id,
            "status": status,
            "kind": resolved_kind,
            "goal": redact(goal.strip())[:eff_limits.max_goal_chars] if isinstance(goal, str) else goal,
            "completed": history("completed", completed),
            "next_steps": inherit("next_steps", next_steps, ("IN_PROGRESS", "BLOCKED")),
            "decisions": history("decisions", decisions),
            "blockers": inherit("blockers", blockers, ("BLOCKED",)),
            "evidence": history("evidence", evidence),
            "source": source,
            "created_at": prev.get("created_at", now()) if prev else now(),
            "updated_at": now(),
            "revision": int(prev.get("revision", 0)) + 1 if prev else 1,
        }
        # Which CLI wrote it: lets another CLI see whose work it is picking up.
        host = host or (prev.get("host") if prev else None)
        if host in HOST_CLEAR:
            record["host"] = host
        if run:
            record["run"] = str(run)[:64]
        validate_checkpoint_schema(record, limits=eff_limits)
        write_json(path, record)
    return {**record, "path": handoff_relpath(task_id)}


RUN_ID_ENV = "EGO_RUN_ID"  # set by ego run: which wrapper a checkpoint belongs to
ADVICE_STATE = ".ego/state/session_advice.json"
# Revisions already resumed after /clear, per task: stored beside the per-session entries in ADVICE_STATE.
CONSUMED_KEY = "_consumed"
CLEAR_PENDING = ".ego/state/clear_pending.json"
PENDING_TTL = 1800
FRESH_CHECKPOINT = 1800
RESUGGEST_GROWTH = 1.25
# The host cannot submit a prompt after /clear: the context is added silently and waits for a message.
AFTER_CLEAR = "再開コンテキストは自動で読み込まれるので、/clear 後に「続けて」と送ってください。"


def _tail_objects(transcript: Path, tail_bytes: int) -> list[dict[str, Any]]:
    try:
        with transcript.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            lines = f.read().decode("utf8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _codex_token_event(transcript: Path, tail_bytes: int) -> dict[str, Any] | None:
    """The latest `token_count` event of a Codex rollout: its prompt size and the host-reported usage limits."""
    for obj in reversed(_tail_objects(transcript, tail_bytes)):
        payload = obj.get("payload")
        if obj.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
            return payload
    return None


def context_tokens(transcript: Path, tail_bytes: int = 512 * 1024, host: str = "claude") -> int | None:
    """Measured size of the main conversation: the last request's prompt tokens.

    Read from the host transcript tail (Claude: input + cache read + cache creation of
    the latest main-chain assistant message; Codex: the latest token_count event, whose input
    includes cached tokens; AGY: input + cache read of the latest planner response).
    None when it cannot be measured; callers must then stay silent rather than guess.
    """
    if host == "codex":
        event = _codex_token_event(transcript, tail_bytes)
        try:
            total = int(event["info"]["last_token_usage"]["input_tokens"]) if event else 0
        except (KeyError, TypeError, ValueError):
            total = 0
        return total or None
    for obj in reversed(_tail_objects(transcript, tail_bytes)):
        if host == "agy":
            if obj.get("type") == "PLANNER_RESPONSE" and obj.get("source") == "MODEL":
                total = int(obj.get("input_tokens") or 0) + int(obj.get("cache_read_tokens") or 0)
                if total > 0:
                    return total
            continue
        if obj.get("type") != "assistant" or obj.get("isSidechain"):
            continue
        usage = obj.get("message", {}).get("usage") if isinstance(obj.get("message"), dict) else None
        if isinstance(usage, dict):
            total = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if total > 0:
                return total
    return None


def opencode_context_tokens(session_id: str | None) -> int | None:
    """Measured context size of an OpenCode session, read from its local SQLite store."""
    if not session_id or not all(c.isalnum() or c in "-_" for c in session_id):
        return None
    db = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "opencode.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute(
                "select data from message where session_id=? order by time_created desc limit 20",
                (session_id,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    for (data,) in rows:
        try:
            message = json.loads(data)
        except (ValueError, TypeError):
            continue
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tokens = message.get("tokens") if isinstance(message.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        try:
            total = int(tokens.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            return total
    return None


def codex_limits(transcript: Path, tail_bytes: int = 512 * 1024) -> dict[str, Any]:
    """Codex reports its usage limits in the rollout: {five_hour|seven_day: {used_percentage, resets_at}}."""
    event = _codex_token_event(transcript, tail_bytes)
    limits = event.get("rate_limits") if isinstance(event, dict) else None
    out: dict[str, Any] = {}
    for slot in ("primary", "secondary"):
        window = limits.get(slot) if isinstance(limits, dict) else None
        if not isinstance(window, dict):
            continue
        try:
            minutes, used, resets = float(window["window_minutes"]), float(window["used_percent"]), float(window["resets_at"])
        except (TypeError, KeyError, ValueError):
            continue
        out["five_hour" if minutes <= 360 else "seven_day"] = {"used_percentage": used, "resets_at": resets}
    return out


def _age_seconds(stamp: str) -> float:
    try:
        return max(0.0, time.time() - dt.datetime.fromisoformat(stamp).timestamp())
    except (TypeError, ValueError):
        return float("inf")


def latest_open_checkpoint(root: Path, max_age: int = FRESH_CHECKPOINT,
                           exclude: set[str] | frozenset[str] = frozenset(), run: str | None = None,
                           consumed: dict[str, int] | None = None) -> dict[str, Any] | None:
    best = None
    for path in (root / ".ego" / "memory" / "handoffs").glob("*.json"):
        record = read_json(path)
        if (not isinstance(record, dict) or record.get("schema_version") != SCHEMA or record.get("status") == "COMPLETED"
                or record.get("source") != "interactive" or record.get("task_id") in exclude or _age_seconds(record.get("updated_at", "")) > max_age
                or (run is not None and record.get("run") != run)):
            continue
        # Already resumed after /clear: that revision was consumed, so it must not be offered again.
        if consumed and record.get("task_id") in consumed and int(record.get("revision") or 0) <= int(consumed[record["task_id"]]):
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
        except (EgoError, TypeError, ValueError):
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


AUTO_RESUME_FILES = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json",
                     "agy": ".agents/hooks.json", "opencode": ".opencode/plugins/ego.ts"}


def auto_resume_installed(root: Path, host: str = "claude") -> bool:
    """Whether `host` has ego's session-start hook, which injects a saved checkpoint after a clear."""
    try:
        return "hook session-start" in (root / AUTO_RESUME_FILES[host]).read_text(encoding="utf8") or \
            (host == "opencode" and "session-start" in (root / AUTO_RESUME_FILES[host]).read_text(encoding="utf8"))
    except (OSError, KeyError):
        return False


def arm_auto_resume(root: Path, task_id: str, status: str | None = None) -> bool:
    """Interactive checkpoint saved: the next SessionStart(clear) injects it, when auto-resume is installed.

    Without this, a manual "save checkpoint" then /clear never arms the pending resume
    (only the Stop-hook suggestion and `session resume` did), so nothing was injected.
    """
    root = root.resolve()
    if os.environ.get("EGO_RUN") or not any(auto_resume_installed(root, h) for h in AUTO_RESUME_FILES):  # under `ego run` the wrapper types the resume prompt
        return False
    if status == "COMPLETED":  # nothing left to resume; do not leave it competing with live tasks
        _disarm_clear_pending(root, task_id)
        return False
    _mark_clear_pending(root, task_id, os.environ.get("CLAUDE_CODE_SESSION_ID"))
    return True


def clear_advice(root: Path, payload: dict[str, Any], threshold: int, host: str = "claude", info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stop-hook output that enforces the configured context limit (clear_suggest_tokens).

    On reaching the limit, and again at each growth step, a session without a current
    checkpoint is given one short extra turn (decision "block") to save one: only the
    conversation knows what to save. Every later turn above the limit reminds the user
    to /clear. It never clears anything and never blocks twice in a row.
    """
    if threshold <= 0 or not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("context_tokens"), int):  # a host whose plugin measures it (OpenCode)
        tokens = payload["context_tokens"]
    elif isinstance(payload.get("transcript_path"), str):
        tokens = context_tokens(Path(payload["transcript_path"]).expanduser(), host=host)
    else:
        return {}
    session = str(payload.get("session_id") or "unknown")[:80]
    root = root.resolve()
    state_path = safe_path(root, ADVICE_STATE)
    # Every session's Stop hook shares this file: serialize the read-modify-write.
    with exclusive_lock(safe_path(root, ".ego/locks/session-advice.lock")):
        state = read_json(state_path, {})
        state = state if isinstance(state, dict) else {}
        entry = state.get(session) if isinstance(state.get(session), dict) else {}
        last = entry.get("tokens")
        if tokens is None or tokens < threshold:
            if last is not None and tokens is not None:  # compacted or cleared: enforce again at the next crossing
                state.pop(session, None); write_json(state_path, state)
            return {}
        step = last is None or tokens >= last * RESUGGEST_GROWTH
        # A checkpoint another live session saved says nothing about this conversation:
        # offering it here would have the user clear unsaved work.
        foreign = {t for t, e in _pending_tasks(root).items() if e.get("session") and e["session"] != session}
        # At a growth step the checkpoint must postdate the previous step: the work since is not in it.
        max_age = min(FRESH_CHECKPOINT, time.time() - float(entry.get("at") or 0)) if step and last is not None else FRESH_CHECKPOINT
        consumed = state.get(CONSUMED_KEY) if isinstance(state.get(CONSUMED_KEY), dict) else None
        checkpoint = latest_open_checkpoint(root, max_age, exclude=foreign, consumed=consumed)
        # The extra turn this hook asked for (or another hook's) is ending: never block it again.
        save = step and checkpoint is None and not payload.get("stop_hook_active")
        if step and (checkpoint is not None or save):
            state[session] = {"tokens": tokens, "at": time.time()}
            for old in sorted((k for k in state if k != CONSUMED_KEY), key=lambda k: state[k].get("at", 0))[:-20]:
                state.pop(old)
            write_json(state_path, state)
    size = f"約 {tokens // 1000}k トークン（しきい値 {threshold // 1000}k）"
    if checkpoint:
        _mark_clear_pending(root, checkpoint["task_id"], session if session != "unknown" else None)
        if info is not None:
            info["once"] = True  # said once per growth step, not on every later turn
        return {"systemMessage": f"ego: コンテキストは{size}です。チェックポイント {checkpoint['task_id']}（{checkpoint['status']}）は保存済みです。"
                                 f"{HOST_CLEAR.get(host, '/clear')} を実行してください。" + AFTER_CLEAR}
    if save:
        return {"systemMessage": f"ego: コンテキストが{size}に達しました。チェックポイントを自動保存します。保存後に /clear を実行してください。",
                "decision": "block",
                "reason": f"ego: this conversation's context is {tokens} tokens, at or above this project's limit "
                          f"(clear_suggest_tokens = {threshold}). Do no further task work in this turn. Save this conversation's state with one "
                          "`ego session checkpoint --task-id <id> --goal ... --completed ... --next ... --evidence ... --decision ... --blocker ... "
                          "--status <IN_PROGRESS|VERIFIED|BLOCKED|COMPLETED> --kind <ongoing|plan_ready|plan_resume|completed> --root .` (facts only; include uncommitted changes and "
                          "known failures; reuse the task ID if one was already checkpointed). Then tell the user, briefly and in "
                          "their language, that the checkpoint is saved and that they should run /clear and then send "
                          "\"続けて\" to resume from it. Then stop."}
    return {"systemMessage": f"ego: コンテキストは{size}です。「チェックポイントを保存して」と依頼し、"
                             "その後 /clear を実行してください。" + AFTER_CLEAR}


USAGE_STATE = ".ego/state/usage_limits.json"


def usage_state_path(host: str) -> str:
    """Where a host's last usage reading lives. One file per host: a Claude session must never act on AGY's quota (or the reverse)."""
    return USAGE_STATE if host == "claude" else USAGE_STATE.replace(".json", f".{host}.json")


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


def _agy_reset(window: dict[str, Any]) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(window["reset_time"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return time.time() + float(window["reset_in_seconds"])
    except (KeyError, TypeError, ValueError):
        return None


def agy_limits(payload: dict[str, Any]) -> dict[str, Any]:
    """AGY reports `quota.<bucket>.{remaining_fraction, reset_time}` in its status line input.

    A bucket counts only when it is the one the session's model draws from: Gemini models use the
    `gemini-*` buckets (plus `gemini-argon-wtu-weekly` for an Argon model), every other model the `3p-*`
    ones. Without a model name nothing is guessed. Returns {five_hour|seven_day: {used_percentage, resets_at}}."""
    quota = payload.get("quota") if isinstance(payload, dict) else None
    model = payload.get("model") if isinstance(payload, dict) else None
    name = str((model.get("display_name") or model.get("id") or "") if isinstance(model, dict) else model or "").lower()
    if not isinstance(quota, dict) or not name:
        return {}
    if "gemini" in name:
        slots = {"five_hour": ("gemini-5h",), "seven_day": ("gemini-weekly", *(("gemini-argon-wtu-weekly",) if "argon" in name else ()))}
    else:
        slots = {"five_hour": ("3p-5h",), "seven_day": ("3p-weekly",)}
    out: dict[str, Any] = {}
    for slot, buckets in slots.items():
        for bucket in buckets:
            window = quota.get(bucket)
            if not isinstance(window, dict):
                continue
            try:
                used = (1.0 - float(window["remaining_fraction"])) * 100
            except (KeyError, TypeError, ValueError):
                continue
            resets = _agy_reset(window)
            if resets is None or not 0 <= used <= 100:
                continue
            if slot not in out or used > out[slot]["used_percentage"]:
                out[slot] = {"used_percentage": round(used, 2), "resets_at": resets}
    return out


def record_usage(payload: dict[str, Any], fallback_root: Path | None = None) -> str:
    """Status line command: keep the host-reported usage limits for the Stop hook; return the line to show.

    Claude Code reports `rate_limits` only here (subscription plans, after the first
    response); AGY reports `quota` here too. Nothing is estimated: without those fields
    the line is empty and nothing is stored.
    """
    if not isinstance(payload, dict):
        return ""
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    root = None
    for candidate in (workspace.get("project_dir"), payload.get("cwd"), fallback_root):
        if candidate and (Path(str(candidate)) / ".ego").is_dir():
            root = Path(str(candidate)).resolve()
            break
    host = "claude"
    reported = payload.get("rate_limits")
    if reported is None and "quota" in payload:
        host, reported = "agy", agy_limits(payload)
    windows = _windows(reported)
    if not windows:
        return ""
    if root is not None:
        path = safe_path(root, usage_state_path(host))
        limits = {key: {"used_percentage": used, "resets_at": resets} for key, _, _, used, resets in windows}
        prev = read_json(path)
        # The status line refreshes constantly: rewrite only on change, or to keep the reading fresh.
        if not isinstance(prev, dict) or prev.get("rate_limits") != limits or time.time() - float(prev.get("at") or 0) > 60:
            write_json(path, {"at": time.time(), "rate_limits": limits})
    return "ego · " + " · ".join(f"{short} {used:.0f}%" for _, short, _, used, _ in windows)


HOST_NAME = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode", "agy": "AGY"}


def usage_advice(root: Path, payload: dict[str, Any], percent: int, hosts: list[str] | tuple[str, ...] = (),
                 host: str = "claude", limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stop-hook output when the usage limit is nearly spent: have the session checkpoint itself, then propose a switch.

    Only the conversation knows what to save, so this asks for one more short turn
    (decision "block"), once per session and usage window. It never clears or launches anything.
    """
    if percent <= 0 or not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return {}
    root = root.resolve()
    if limits is None:
        state = read_json(safe_path(root, usage_state_path(host)))
        if not isinstance(state, dict) or time.time() - float(state.get("at") or 0) > USAGE_FRESH:
            return {}  # no recent measurement: stay silent rather than guess
        limits = state.get("rate_limits")
    windows = [w for w in _windows(limits) if w[3] >= percent]
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
    others = [h for h in hosts if h in HOST_LAUNCH and h != host and shutil.which(HOST_LAUNCH[h][0])]
    reset = dt.datetime.fromtimestamp(resets).strftime("%m/%d %H:%M")
    size = f"{label}の使用率が {used:.0f}%（しきい値 {percent}%、リセット {reset}）"
    switch = (f"Then run `ego session handoff <task-id> --to <cli> --root .` for the installed alternative(s) "
              f"({', '.join(others)}) and show the user each `launch_command` so they can continue there. "
              if others else
              "No other configured CLI is installed, so tell the user the checkpoint ID and that "
              f"`ego session resume <task-id>` continues it after the limit resets ({reset}). ")
    return {"systemMessage": f"ego: {size}に達しました。チェックポイントを自動保存し、切り替え先を提案します。",
            "decision": "block",
            "reason": f"ego: the {HOST_NAME.get(host, host)} usage limit is nearly spent ({key} {used:.0f}% used, resets {reset}). "
                      "Do no further task work now. Save this conversation's state with one "
                      "`ego session checkpoint --task-id <id> --goal ... --completed ... --next ... --evidence ... --decision ... --blocker ... "
                      "--status <IN_PROGRESS|VERIFIED|BLOCKED|COMPLETED> --kind <ongoing|plan_ready|plan_resume|completed> --root .` (facts only; include uncommitted changes and "
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
    except EgoError:
        return {}
    if record is None:
        return {}
    with exclusive_lock(safe_path(root, ".ego/locks/session-advice.lock")):
        state = read_json(safe_path(root, ADVICE_STATE), {})
        state = state if isinstance(state, dict) else {}
        state.setdefault(CONSUMED_KEY, {})[record["task_id"]] = int(record.get("revision", 1))
        write_json(safe_path(root, ADVICE_STATE), state)
    # additionalContext is silent and starts no turn, so also tell the user what to do next.
    limits = load_config(root).session_schema_limits()
    return {"systemMessage": f"ego: チェックポイント {record['task_id']} の再開コンテキストを読み込みました。「続けて」と送ると再開します。",
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": resume_prompt(record, limits=limits)}}


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
        raise EgoError("UNKNOWN_HOST", f"Unsupported interactive host: {from_host}", 2)
    if task_id:
        _guard_run_active(root, _id(task_id))
        return _id(task_id)
    items = list_checkpoints(root)
    for item in items:
        if item["source"] == "interactive" and from_host in (None, item["host"]):
            return item["task_id"]
    for item in items:
        if (item["source"] == "managed" and from_host in (None, item["host"])
                and not _run_lock_held(root, item["task_id"])):
            return item["task_id"]
    if from_host:
        raise EgoError("HANDOFF_MISSING", f"No open checkpoint saved by {from_host}. It must be saved there first "
                       f"(`ego session checkpoint ... --host {from_host}`); `ego session list` shows what exists", 3)
    raise EgoError("HANDOFF_MISSING", "No open checkpoint; save one with `ego session checkpoint`", 3)


def pickup(root: Path, task_id: str | None, from_host: str) -> dict[str, Any]:
    """Continue, inside the CLI that is already open, the work another CLI checkpointed. Nothing is cleared."""
    task_id = resolve_task(root, task_id, from_host)
    record = load_checkpoint(root, task_id)
    if record is None:
        raise EgoError("HANDOFF_MISSING", "Save a checkpoint before handing off", 3)
    return {
        "task_id": task_id,
        "from_host": record.get("host"),
        "checkpoint_path": handoff_relpath(task_id),
        "checkpoint_status": record["status"],
        "updated_at": record.get("updated_at"),
        "instructions": "Continue in this session by following resume_prompt. It is the other CLI's saved "
                        "checkpoint, not its conversation: verify against files and git diff before relying on it.",
        "resume_prompt": resume_prompt(record, limits=load_config(root.resolve()).session_schema_limits()),
    }


def handoff(root: Path, task_id: str | None, to_host: str, from_host: str | None = None) -> dict[str, Any]:
    """Continue a saved checkpoint in another CLI: a new interactive session that opens on the resume prompt.

    The checkpoint is host-neutral project state, so nothing is converted; no
    conversation of the previous CLI is read or carried over.
    """
    if to_host not in HOST_LAUNCH:
        raise EgoError("UNKNOWN_HOST", f"Unsupported interactive host: {to_host}", 2)
    task_id = resolve_task(root, task_id, from_host)
    record = load_checkpoint(root, task_id)
    if record is None:
        raise EgoError("HANDOFF_MISSING", "Save a checkpoint before handing off", 3)
    prompt = resume_prompt(record, limits=load_config(root.resolve()).session_schema_limits())
    host_prefix = HOST_LAUNCH[to_host]
    # Launch through `ego run` so context rotation, overlay, and session analysis stay active in the target CLI.
    argv = ["ego", "run", to_host, "--", *host_prefix[1:], prompt]
    available = shutil.which(host_prefix[0]) is not None
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
                         f"{to_host} via ego run on the resume prompt. Nothing was started by this command."
                         if available else
                         f"{host_prefix[0]} is not installed on PATH. Install it, or open {to_host} manually and send resume_prompt."),
        "resume_prompt": prompt,
    }


def rotation_instructions(root: Path, task_id: str, host: str = "claude") -> dict[str, Any]:
    if host not in HOST_CLEAR:
        raise EgoError("UNKNOWN_HOST", f"Unsupported interactive host: {host}", 2)
    _guard_run_active(root, task_id)
    record = load_checkpoint(root, task_id)
    if record is None:
        raise EgoError("HANDOFF_MISSING", "Save a checkpoint before clearing context", 3)
    auto = auto_resume_installed(root.resolve(), host)
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
                        "No clear operation was executed by this command.",
        "resume_prompt": resume_prompt(record, limits=load_config(root.resolve()).session_schema_limits()),
    }


def managed_checkpoint(root: Path, record: dict[str, Any], *, state: str, task_file: str | None = None) -> dict[str, Any]:
    """Persist one compact checkpoint at a DAG wave boundary or at termination."""
    limits = load_config(root).session_schema_limits()
    max_notes = limits.max_notes
    spec = record.get("spec", {})
    nodes = spec.get("nodes", [])
    statuses = record.get("node_status", {})
    done = [f"{n['id']} ({statuses[n['id']]})" for n in nodes if statuses.get(n["id"]) == "PASSED"]
    remaining = [n["id"] for n in nodes if statuses.get(n["id"]) != "PASSED"]
    blocks = [f"{nid}: {s}" for nid, s in statuses.items() if s not in ("PASSED", "RUNNING")]
    paths: list[str] = []
    for data in record.get("results", {}).values():
        paths.extend(data.get("changed", []))
    paths = sorted(set(paths))[:max_notes]
    host = record.get("host") if record.get("host") in HOST_CLEAR else None
    next_steps = remaining
    if state in ("COMPLETED", "VERIFIED"):
        next_steps = []
    elif state in ("IN_PROGRESS", "BLOCKED"):
        task_ref = record.get("task_file") or task_file or "<task-file>"
        host_ref = host or "<host>"
        resume_cmd = f"ego work resume {record['run_id']} --root . --task {task_ref} --host {host_ref} --trust-cli"
        next_steps = ([resume_cmd, *remaining] if record.get("run_id") else remaining)
    return save_checkpoint(
        root, record["run_id"], goal=spec.get("goal") or "Finish the declared DAG task",
        completed=done[:max_notes], next_steps=next_steps[:max_notes], blockers=blocks[:max_notes],
        evidence=[f"run: {record['run_id']}", *[f"file: {path}" for path in paths]][:max_notes],
        status=state, source="managed", host=host, limits=limits,
    )


def completion_message(record: dict[str, Any]) -> str:
    state = record.get("final_status")
    if state == "INTERRUPTED":
        return "中断（再開可能）：実行が途中で中断されました。ego work resume で完了済みノードを引き継いで再開できます。"
    if state == "BUDGET_EXHAUSTED":
        return "中断（トークン上限）：1回の実行のトークン上限（max_run_tokens）に達しました。ego work resume で残りのノードを新しい上限枠で再開できます（必要なら --max-run-tokens で上書き）。"
    if state == "PASSED" and record.get("applied"):
        return "完了：変更を適用し、指定された検証に合格しました。"
    if state == "PASSED":
        return "検証完了（未適用）：作業用コピーで検証済みです。変更はまだ元のプロジェクトへ適用していません。"
    if state == "COMPARISON_READY":
        return "比較実行完了（採用待ち）：比較結果を確認して採用する候補を選んでください。"
    return f"未完了：実行状態は {state or 'UNKNOWN'} です。チェックポイントから再開してください。"


def bind_run_session(root: Path, payload: dict[str, Any]) -> bool:
    """Under `ego run`: record which host session this wrapper's terminal holds, for its context probe."""
    target = os.environ.get(RUN_SESSION_ENV)
    if not target or not os.environ.get("EGO_RUN") or not isinstance(payload, dict):
        return False
    path = Path(target)
    if path.resolve().parent != (root.resolve() / ".ego" / "state" / "run").resolve():
        return False  # only the wrapper's own file under this project's run state
    session, transcript = payload.get("session_id"), payload.get("transcript_path")
    if not isinstance(session, str) or not session:
        return False
    write_json(path, {"session_id": session[:200],
                      "transcript_path": transcript if isinstance(transcript, str) else None, "at": time.time()})
    return True


def project_root(root: Path, *candidates: Any) -> Path:
    """The project (the directory holding .ego) for a hook run from a host that does not say where it is."""
    for start in (root, *(Path(str(c)) for c in candidates if c)):
        try:
            start = start.resolve()
        except OSError:
            continue
        for directory in (start, *start.parents):
            if (directory / ".ego").is_dir():
                return directory
    return root.resolve()


def normalize_payload(host: str, obj: Any) -> dict[str, Any]:
    """A host's hook input in the field names the advice functions use (Claude's)."""
    if not isinstance(obj, dict):
        return {}
    if host == "agy":
        return {"session_id": obj.get("conversationId"), "transcript_path": obj.get("transcriptPath"),
                "stop_hook_active": obj.get("continuationActive") is True or int(obj.get("executionNum") or 0) > 0, "source": "clear",
                "cwd": obj.get("cwd")}
    return obj


ALTER_SESSION_MANDATE = (
    "ego: knowledge = \"alter\" is required on this host. Before planning or editing, call `alter_bootstrap` "
    "with cwd set to the repo root, then call `alter_ai_usage` (cwd = repo root) and follow its rules when `source` is `project` "
    "(ignore `common_fallback`); use `alter_task_context` / `alter_search` for domain terms."
)


# Used when the hook already injected the project rules: the model need not call `alter_ai_usage` again.
ALTER_SESSION_MANDATE_INJECTED = (
    "ego: knowledge = \"alter\" is required on this host. Before planning or editing, call `alter_bootstrap` "
    "with cwd set to the repo root; use `alter_task_context` / `alter_search` for domain terms. "
    "The project AI usage rules from `alter_ai_usage` are already injected below; do not call it again."
)


ALTER_UNREACHABLE = ("ego ERROR: alter に接続できません（{reason}）。knowledge = \"alter\" ですが、ナレッジと AI 利用ルールを取得できていません。"
                     "最初にこのエラーをユーザーへ伝え、alter MCP の設定（ホストの MCP 設定または EGO_ALTER_MCP_COMMAND）を確認してもらってください。")


def _alter_call(root: Path, tool: str, args: dict[str, Any], timeout: int) -> tuple[Any, str]:
    """(payload, error): error is "" on success; no MCP configured, a timeout or a bad reply is a non-empty reason."""
    try:
        from ..adapters.memory import MCPStdio, _alter_mcp_spec
        spec = _alter_mcp_spec(root)
        if spec is None:
            return None, "alter MCP server is not configured for this host"
        cmd, env = spec
        result = MCPStdio(cmd, timeout=timeout, env=env).call_tool(tool, args)
        payload: Any = result
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            payload = json.loads(result["content"][0]["text"])
        return payload, ""
    except Exception as exc:
        return None, f"{tool}: {str(exc)[:160] or type(exc).__name__}"


def alter_status(root: Path, timeout: int = 8) -> tuple[list[str], str]:
    """(rules, error) for this project from alter (`alter_ai_usage`).

    Rules are only the project's own (`source == "project"`): alter falls back to the common list when a project has none,
    and common rules must not be applied here. `error` is non-empty when alter cannot be reached, so a hook can say so."""
    if os.environ.get("EGO_ALTER_SESSION_RULES", "1").lower() in ("0", "false", "off", "no"):
        return [], ""
    payload, error = _alter_call(root, "alter_ai_usage", {"cwd": str(alter_project_root(root))}, timeout)
    if error:
        return [], error
    if not isinstance(payload, dict) or payload.get("source") != "project":
        return [], ""
    rules: list[str] = []
    text = payload.get("text")
    for line in text.splitlines() if isinstance(text, str) else []:
        line = line.strip()
        if line.startswith("- ") and line[2:].strip():
            rules.append(" ".join(line[2:].split())[:500])
    return rules[:20], ""


def alter_project_rules(root: Path, timeout: int = 8) -> list[str]:
    """This project's own AI usage rules from alter, as plain lines ([] when none or alter is unreachable)."""
    return alter_status(root, timeout)[0]


def alter_prompt_status(root: Path, prompt: str, timeout: int = 8) -> tuple[str, str]:
    """(context, error) from alter (`alter_task_context`) for the user's prompt.

    Runs mechanically on every prompt so knowledge is looked up without the model having to ask. `context` is "" when
    there is nothing to add; `error` is non-empty only when alter could not be reached."""
    if os.environ.get("EGO_ALTER_PROMPT_CONTEXT", "1").lower() in ("0", "false", "off", "no"):
        return "", ""
    task = " ".join(prompt.split())
    if task.startswith("/"):  # a slash command: search for its arguments, not its name
        task = task.partition(" ")[2].strip()
    if len(task) < 6:
        return "", ""
    payload, error = _alter_call(root, "alter_task_context", {"task": task[:500], "cwd": str(alter_project_root(root))}, timeout)
    if error:
        return "", error
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    lines: list[str] = []
    for item in evidence[:5] if isinstance(evidence, list) else []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        excerpt = " ".join(str(item.get("excerpt") or "").replace("#", " ").split())[:240]
        ref = str(item.get("source_ref") or item.get("id") or "")[:80]
        lines.append(f"- {item['title']}: {excerpt} [{ref}]")
    if not lines:
        return "", ""
    return "alter: 過去ナレッジ（このプロンプトに関連。未検証の根拠として扱い、コードで確認する）:\n" + "\n".join(lines), ""


def alter_prompt_context(root: Path, prompt: str, timeout: int = 8) -> str:
    """The alter context block for the prompt; "" when none or alter is unreachable."""
    return alter_prompt_status(root, prompt, timeout)[0]


def new_transcript_prompt(root: Path, payload: dict[str, Any], host: str = "agy") -> str:
    """The latest user prompt in the host transcript, once; "" when it was already handed out or cannot be read.

    AGY has no prompt-submit event and its payload carries no prompt text: the `PreInvocation` hook fires before
    every model call (also between tool calls), so the prompt is read from the transcript and each one is
    returned a single time per session."""
    transcript, sid = payload.get("transcript_path"), str(payload.get("session_id") or "")
    if not isinstance(transcript, str) or not sid:
        return ""
    from ..adapters.memory import _extract_turn_from_obj
    turn = next((t for t in (_extract_turn_from_obj(o) for o in reversed(_tail_objects(Path(transcript).expanduser(), 256 * 1024)))
                 if t and t.get("role") == "user"), None)
    if turn is None:
        return ""
    key = str(turn.get("id") or turn.get("sequence") or hashlib.sha1(turn["text"].encode("utf8")).hexdigest())
    root = root.resolve()
    path = safe_path(root, ".ego/state/alter_prompts.json")
    with exclusive_lock(safe_path(root, ".ego/locks/alter-prompts.lock")):
        seen = read_json(path, {})
        seen = seen if isinstance(seen, dict) else {}
        if seen.get(sid) == key:
            return ""
        seen.pop(sid, None)
        seen[sid] = key
        write_json(path, dict(list(seen.items())[-50:]))  # bounded: the latest sessions only
    return str(turn["text"])


def with_alter_mandate(out: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Add the alter-first requirement (and alter-managed project rules) to a session-start output, keeping any checkpoint advice already there."""
    merged = dict(out or {})
    hook = dict(merged.get("hookSpecificOutput") or {})
    hook.setdefault("hookEventName", "SessionStart")
    previous = hook.get("additionalContext") or ""
    mandate = ALTER_SESSION_MANDATE
    rules, error = alter_status(root) if root is not None else ([], "")
    if rules:
        mandate = ALTER_SESSION_MANDATE_INJECTED
        mandate += "\nPJ AI利用ルール（alter 管理。必ず適用する）:\n" + "\n".join(f"- {rule}" for rule in rules)
    if error:  # alter is the knowledge source here: say it is down, to the user and to the model
        message = ALTER_UNREACHABLE.format(reason=error)
        mandate += "\n" + message
        merged["systemMessage"] = message
    hook["additionalContext"] = f"{previous}\n{mandate}" if previous else mandate
    merged["hookSpecificOutput"] = hook
    return merged


def adapt_output(host: str, event: str, out: dict[str, Any], once: bool = False) -> dict[str, Any]:
    """The advice in the output format `host` reads for `event` (`stop`, `session-start` or `user-prompt`)."""
    if host == "agy" and event == "user-prompt":  # PreInvocation: a persistent system message before the model call
        context = (out.get("hookSpecificOutput") or {}).get("additionalContext")
        return {"injectSteps": [{"systemMessage": {"systemMessage": context}}]} if context else {}
    if host == "agy":
        if event == "stop":
            if out.get("decision") == "block":
                return {"decision": "continue", "reason": out.get("reason", "")}
            if once and out.get("systemMessage"):  # AGY has no message-to-user channel: have the agent relay it, once
                return {"decision": "continue", "reason": f"ego: Tell the user, briefly and in their language: {out['systemMessage']} Then stop."}
            return {}
        context = (out.get("hookSpecificOutput") or {}).get("additionalContext")
        # Only a restored checkpoint is worth announcing; the alter requirement alone is silent context.
        # The mandate comes first only when there is no checkpoint advice before it (see with_alter_mandate).
        has_error = "ego ERROR:" in (context or "")  # an alter error carries its own "tell the user" instruction
        note = "" if has_error or (context or "").startswith((ALTER_SESSION_MANDATE, ALTER_SESSION_MANDATE_INJECTED)) else "\nTell the user briefly that this checkpoint was loaded."
        return {"injectSteps": [{"systemMessage": {"systemMessage": f"{context}{note}"}}]} if context else {}
    return out


def _save_request(tokens: int, threshold: int) -> str:
    return (f"ego: this conversation's context is {tokens} tokens, at or above this project's limit ({threshold}). "
            "Do no further task work in this turn. Save this conversation's state with one "
            "`ego session checkpoint --task-id <id> --goal ... --completed ... --next ... --evidence ... --decision ... --blocker ... "
            "--status <IN_PROGRESS|VERIFIED|BLOCKED> --kind <ongoing|plan_ready|plan_resume|completed> --root .` (facts only; include uncommitted changes and known "
            "failures; reuse the task ID if one was already checkpointed). Then stop: ego clears this session and "
            "resumes it from that checkpoint automatically.")
