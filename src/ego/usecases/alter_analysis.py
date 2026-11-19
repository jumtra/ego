"""Headless analysis of a recorded alter conversation (alter_analysis = true, knowledge = "alter").

The analysis runs after an `ego run` wrapper ends: the analyzer host runs the alter-session-analyzer skill in a
detached `ego session analyze <sid>` process and attaches the analysis to the recorded conversation id.
Everything here fails open: nothing may raise into, or block, a host hook."""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..shared.util import exclusive_lock, now, read_json, safe_path, write_json

MIN_TURNS = 4
ANALYZER_ENV = "EGO_ALTER_ANALYZER"
ANALYZER_TOOLS = ["Read", "Skill", "ToolSearch", "mcp__alter"]
ANALYZER_MAX_TURNS = 12
MAX_PROMPT_CHARS = 100_000


def _state_path(root: Path) -> Path:
    return root.resolve() / ".ego" / "state" / "alter_analysis.json"


def _load_state(root: Path) -> dict[str, Any]:
    state = read_json(_state_path(root), {})
    sessions = state.get("sessions") if isinstance(state, dict) else None
    return sessions if isinstance(sessions, dict) else {}


def _lock_path(root: Path) -> Path:
    return safe_path(root.resolve(), ".ego/locks/alter-analysis.lock")


def _write_entry(root: Path, sid: str, entry: dict[str, Any]) -> None:
    sessions = _load_state(root)
    sessions[sid] = {**(sessions.get(sid) or {}), **entry, "updated_at": now()}
    write_json(_state_path(root), {"schema_version": 1, "sessions": sessions})


def _save_entry(root: Path, sid: str, entry: dict[str, Any]) -> None:
    with exclusive_lock(_lock_path(root)):
        _write_entry(root, sid, entry)


def _enabled(root: Path) -> bool:
    from ..domain.config import load_config
    cfg = load_config(root)
    return cfg.knowledge == "alter" and bool(cfg.alter_analysis)


def _limit(root: Path) -> int:
    from ..domain.config import load_config
    return int(load_config(root).alter_analysis_timeout_seconds)


def _inside_analyzer() -> bool:
    return os.environ.get(ANALYZER_ENV, "").strip() not in ("", "0")


def _turns_file(root: Path, sid: str) -> dict[str, Any] | None:
    from ..adapters.memory import alter_turns_path
    data = read_json(alter_turns_path(root, sid), None)
    return data if isinstance(data, dict) and isinstance(data.get("turns"), list) else None


def _up_to_date(entry: Any, turn_count: int) -> bool:
    """Analyzed, and the session has not grown since (alter resets the analysis of a conversation that grew).
    An entry written before turn counts were recorded counts as up to date."""
    if not (isinstance(entry, dict) and entry.get("status") == "analyzed"):
        return False
    analyzed = entry.get("turns")
    return not isinstance(analyzed, int) or analyzed >= turn_count


def _spawn_analysis(root: Path, sid: str) -> bool:
    """Start `ego session analyze <sid>` detached; the child is marked so its own hooks do not re-launch."""
    env = {**os.environ, ANALYZER_ENV: "1"}
    subprocess.Popen([sys.executable, "-m", "ego", "session", "analyze", sid], cwd=str(root.resolve()),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=env)
    return True


def launch_analysis(root: Path, sid: str) -> dict[str, Any]:
    """Called when an `ego run` wrapper ends, for its logical session. Never raises."""
    try:
        if _inside_analyzer():
            return {"status": "skipped", "reason": "inside analyzer"}
        if not _enabled(root):
            return {"status": "skipped", "reason": "disabled"}
        sid = str(sid or "").strip()
        if not sid:
            return {"status": "skipped", "reason": "missing session_id"}
        turns = _turns_file(root, sid)
        if turns is None or len(turns["turns"]) < MIN_TURNS:
            return {"status": "skipped", "reason": "not enough turns", "session_id": sid}
        with exclusive_lock(_lock_path(root)):
            entry = _load_state(root).get(sid)
            timeout = _limit(root)
            if _up_to_date(entry, len(turns["turns"])) or (isinstance(entry, dict) and _running(entry, timeout)):
                return {"status": "skipped", "reason": entry.get("status"), "session_id": sid}
            _write_entry(root, sid, {"status": "running"})
            _spawn_analysis(root, sid)
        return {"status": "launched", "session_id": sid}
    except Exception as exc:  # fail open
        return {"status": "error", "reason": str(exc)[:200]}


def _running(entry: dict[str, Any], timeout: int) -> bool:
    """A run that started within its time limit is still running; an older one crashed and may be retried."""
    if entry.get("status") != "running":
        return False
    try:
        started = dt.datetime.fromisoformat(str(entry.get("updated_at", ""))).timestamp()
    except (ValueError, TypeError):
        return False
    return time.time() - started < timeout + 60


ATTACHED_RE = re.compile(r"ALTER_ANALYSIS conversation=(\S+) result=(attached|not_attached)")


def _analyzer_prompt(conversation_id: str, turns: list[dict[str, Any]]) -> str:
    body = json.dumps(turns, ensure_ascii=False)[:MAX_PROMPT_CHARS]
    return (
        "Use the alter-session-analyzer skill to analyze the ALTER conversation whose id is "
        f"`{conversation_id}` and attach the analysis to that conversation. Capture only durable, "
        "owner-attributed knowledge. Do not record a new conversation.\n"
        "The conversation is already recorded: Skip step 1 (`alter_record_conversation`) and go straight "
        f"to the skill's fallback step: call `alter_extract_cognitive` with `conversation_id: \"{conversation_id}\"` "
        "and your `{items, cards, terms}`. Cite turns from the JSON below exactly: `turn_id` = the turn's `id`, "
        "`turn_seq` = its `sequence`, and quote owner (`role: user`) text verbatim.\n\n"
        "When you finish, print exactly one final line and nothing after it:\n"
        f"ALTER_ANALYSIS conversation={conversation_id} result=attached\n"
        "only if alter accepted the analysis for that conversation (for example the analysis was "
        "submitted for owner review, or alter_extract_cognitive succeeded with that conversation_id). "
        "Otherwise print the same line with result=not_attached.\n\n"
        "Recorded turns (redacted, JSON):\n" + body
    )


def _attachment_confirmed(output: str, conversation_id: str) -> bool:
    """True only when the analyzer's last marker names this conversation and reports alter accepted it."""
    found = ATTACHED_RE.findall(output or "")
    if not found:
        return False
    marker_id, result = found[-1]
    return marker_id == conversation_id and result == "attached"


def analyze_session(root: Path, sid: str) -> dict[str, Any]:
    """Run the analyzer host headless on one recorded session. Tracks attempts/status; never raises."""
    try:
        from ..domain.config import load_config
        from ..adapters.hostcompat import inspect_host
        from ..adapters.runner import run_agent
        cfg = load_config(root)
        sid = str(sid or "").strip()
        if not sid:
            return {"status": "skipped", "reason": "missing session_id"}
        if not (cfg.knowledge == "alter" and bool(cfg.alter_analysis)):
            return {"status": "skipped", "reason": "disabled"}
        turns = _turns_file(root, sid)
        if turns is None or len(turns["turns"]) < MIN_TURNS:
            return {"status": "skipped", "reason": "not enough turns", "session_id": sid}
        conversation_id = turns.get("conversation_id")
        if not (isinstance(conversation_id, str) and conversation_id.strip()):
            _save_entry(root, sid, {"status": "failed", "reason": "no conversation id"})
            return {"status": "failed", "reason": "no conversation id", "session_id": sid}
        host = str(turns.get("host") or "")
        info = inspect_host(host)  # reports available/supported; an unknown host raises and is caught below
        if not info.get("available"):
            _save_entry(root, sid, {"status": "failed", "reason": "host unavailable", "host": host})
            return {"status": "failed", "reason": "host unavailable", "session_id": sid}
        if not info.get("supported"):
            _save_entry(root, sid, {"status": "failed", "reason": "host unsupported", "host": host})
            return {"status": "failed", "reason": "host unsupported", "session_id": sid}
        override = cfg.alter_analyzer.get(host) if isinstance(cfg.alter_analyzer, dict) else None
        model = override.get("model") if isinstance(override, dict) else None

        with exclusive_lock(_lock_path(root)):
            attempts = int((_load_state(root).get(sid) or {}).get("attempts") or 0) + 1
            _write_entry(root, sid, {"status": "running", "attempts": attempts, "host": host, "model": model,
                                     "conversation_id": conversation_id})
        env = {**os.environ, ANALYZER_ENV: "1", "EGO_ALTER_AUTO_REVIEW": "0"}  # analyzer must not record itself
        result = run_agent(host, _analyzer_prompt(conversation_id, turns["turns"]), root,
                           timeout=int(cfg.alter_analysis_timeout_seconds), model=model, read_only=False,
                           max_turns=ANALYZER_MAX_TURNS, lean=True, mcp_servers=["alter"], env=env,
                           allowed_tools=ANALYZER_TOOLS)
        # Analyzed only when the run passed AND the analyzer reports alter accepted the analysis for this conversation.
        confirmed = result.status == "PASS" and _attachment_confirmed(result.output, conversation_id)
        status = "analyzed" if confirmed else "failed"
        reason = "" if confirmed else (result.reason or result.status if result.status != "PASS"
                                       else "analysis not confirmed as attached by alter")
        _save_entry(root, sid, {"status": status, "attempts": attempts, "reason": reason, "turns": len(turns["turns"])})
        return {"status": status, "session_id": sid, "conversation_id": conversation_id, "host": host, "attempts": attempts}
    except Exception as exc:  # fail open
        try:
            key = str(sid or "").strip()
            if key:
                _save_entry(root, key, {"status": "failed", "reason": str(exc)[:200]})
        except Exception:
            pass
        return {"status": "error", "reason": str(exc)[:200]}


