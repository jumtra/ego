"""Atomic persistence, prompt-tail selection, and alter feedback for EnvFacts."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..domain.config import load_config
from ..domain.fact_lifecycle import Fact
from ..domain.settings import LifecycleSettings, load_settings
from ..shared.util import EgoError
from .memory import MCPStdio, _alter_mcp_spec

_STORE_REL = Path(".ego") / "state" / "envfacts.json"
_PENDING_REL = Path(".ego") / "state" / "alter-feedback-pending.jsonl"
_REPLACE_RETRIES = 5
_REPLACE_SLEEP_S = 0.05


def _store_path(root: Path) -> Path:
    return Path(root) / _STORE_REL


def _pending_path(root: Path) -> Path:
    return Path(root) / _PENDING_REL


def load(root: Path) -> dict[str, Fact]:
    """Load persisted EnvFacts from .ego/state/envfacts.json."""
    path = _store_path(root)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    facts: dict[str, Fact] = {}
    for fid, item in raw.items():
        if not isinstance(item, dict):
            continue
        fact_key = item.get("key")
        category = item.get("category")
        if not isinstance(fact_key, str) or not isinstance(category, str):
            continue
        fact_id_val = str(item.get("id") or fid)
        confirmations = [str(s) for s in (item.get("confirmations") or []) if isinstance(s, str)]
        contradictions = int(item.get("contradictions") or 0)
        state = str(item.get("state") or "candidate")
        facts[fact_id_val] = Fact(
            id=fact_id_val,
            key=fact_key,
            category=category,
            state=state,
            confirmations=confirmations,
            contradictions=contradictions,
        )
    return facts


def save(root: Path, facts: dict[str, Fact]) -> None:
    """Atomically write EnvFacts to .ego/state/envfacts.json with PermissionError retries."""
    path = _store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({fid: asdict(fact) for fid, fact in facts.items()}, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".envfacts-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
        for attempt in range(_REPLACE_RETRIES + 1):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt >= _REPLACE_RETRIES:
                    raise
                time.sleep(_REPLACE_SLEEP_S)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def forget(root: Path, fact_id_str: str) -> Fact | None:
    """Mark a fact as 'forgotten' and persist; return updated Fact or None if missing."""
    facts = load(root)
    fact = facts.get(fact_id_str)
    if fact is None:
        return None
    updated = Fact(
        id=fact.id,
        key=fact.key,
        category=fact.category,
        state="forgotten",
        confirmations=list(fact.confirmations),
        contradictions=fact.contradictions,
    )
    facts[fact_id_str] = updated
    save(root, facts)
    return updated


def active_facts(root: Path, s: LifecycleSettings) -> list[str]:
    """Return '<category>: <key>' lines for active facts capped by item count and total chars."""
    facts = load(root)
    lines: list[str] = []
    total_chars = 0
    for fact in facts.values():
        if fact.state != "active":
            continue
        if len(lines) >= s.max_injected_facts:
            break
        line = f"{fact.category}: {fact.key}"
        if total_chars + len(line) > s.max_injected_chars:
            break
        lines.append(line)
        total_chars += len(line)
    return lines


def _send_feedback(root: Path, payload: dict[str, Any]) -> Any:
    spec = _alter_mcp_spec(root)
    if spec is None:
        raise EgoError("ALTER_NOT_CONFIGURED", "alter MCP not configured", 5)
    cmd, env = spec
    return MCPStdio(cmd, env=env).call_tool("alter_knowledge_feedback", payload)


def _flush_pending(root: Path) -> None:
    pending_path = _pending_path(root)
    if not pending_path.is_file():
        return
    try:
        raw_lines = pending_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    still_pending: list[str] = []
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            _send_feedback(root, payload)
        except Exception:
            still_pending.append(json.dumps(payload, ensure_ascii=False))
    if still_pending:
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text("\n".join(still_pending) + "\n", encoding="utf-8")
    else:
        try:
            pending_path.unlink()
        except OSError:
            pass


def notify_alter(root: Path, fact: Fact, kind: str) -> None:
    """Send contradiction/forget feedback to alter MCP, queuing failures to pending JSONL."""
    if load_config(root).knowledge != "alter" or not load_settings(root).lifecycle.alter_feedback:
        return
    _flush_pending(root)
    payload: dict[str, Any] = {
        "cwd": str(root),
        "kind": kind,
        "text": f"{fact.category}: {fact.key}",
        "evidence": [f"ego envfact {fact.id} contradictions={fact.contradictions}"],
    }
    try:
        _send_feedback(root, payload)
    except Exception:
        pending_path = _pending_path(root)
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        with pending_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
