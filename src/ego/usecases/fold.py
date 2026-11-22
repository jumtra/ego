# Ported from microsoft/TokenEconomizer (MIT, 1bfeccd): tokeneconomizer/plugin/fold.py
"""Post-session fold: aggregate usage into the ledger and observe EnvFacts asynchronously."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterator

from ..adapters.envfact_store import load as load_envfacts, notify_alter, save as save_envfacts
from ..adapters.transcript import read_events
from ..domain.envfacts import extract as extract_envfacts
from ..domain.fact_lifecycle import Fact, fact_id, observe as observe_envfact
from ..domain.settings import load_settings
from ..domain.trace_schema import TraceEvent, UsageEvent
from ..domain.usage import usage_row
from . import ledger

_ERROR_LOG = Path(".ego/state/fold-errors.jsonl")
_LOCK_REL = Path(".ego/locks/fold.lock")
_MAX_ERROR_LINES = 50
_MAX_ERROR_CHARS = 300


@contextmanager
def _acquire_fold_lock(lock_path: Path) -> Iterator[bool]:
    """Acquire an exclusive non-blocking lock on ``lock_path``; yield False when busy."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            return

        try:
            import msvcrt
        except ImportError:
            msvcrt = None
        if msvcrt is not None:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            return

        yield True


def _has_valid_json_line(path: Path) -> bool:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                if isinstance(json.loads(line), dict):
                    return True
            except ValueError:
                continue
    return False


def _sum_usage(events: list[TraceEvent]) -> tuple[dict[str, int], str, int]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
    }
    last_model = "unknown"
    count = 0
    for ev in events:
        if not isinstance(ev, UsageEvent):
            continue
        count += 1
        if ev.model:
            last_model = ev.model
        u = ev.usage
        totals["input_tokens"] += int(u.get("input_tokens") or 0)
        totals["output_tokens"] += int(u.get("output_tokens") or 0)
        read_val = u.get("cache_read_tokens")
        if read_val is None:
            read_val = u.get("cache_read_input_tokens")
        totals["cache_read_tokens"] += int(read_val or 0)
        if "cache_write_5m_tokens" in u or "cache_write_1h_tokens" in u:
            totals["cache_write_5m_tokens"] += int(u.get("cache_write_5m_tokens") or 0)
            totals["cache_write_1h_tokens"] += int(u.get("cache_write_1h_tokens") or 0)
        elif isinstance(u.get("cache_creation"), dict) and (
            "ephemeral_5m_input_tokens" in u["cache_creation"]
            or "ephemeral_1h_input_tokens" in u["cache_creation"]
        ):
            cc = u["cache_creation"]
            totals["cache_write_5m_tokens"] += int(cc.get("ephemeral_5m_input_tokens") or 0)
            totals["cache_write_1h_tokens"] += int(cc.get("ephemeral_1h_input_tokens") or 0)
        else:
            creation = u.get("cache_creation_tokens")
            if creation is None:
                creation = u.get("cache_creation_input_tokens")
            totals["cache_write_5m_tokens"] += int(creation or 0)
    return totals, last_model, count


def _record_error(root: Path, session_id: str, exc: Exception) -> None:
    path = root / _ERROR_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "error": str(exc)[:_MAX_ERROR_CHARS],
            },
            ensure_ascii=False,
        )
        lines: list[str] = []
        if path.exists():
            lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        lines.append(entry)
        kept = lines[-_MAX_ERROR_LINES:]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        return


def _observe_session_envfacts(root: Path, session_id: str, events: list[TraceEvent]) -> int:
    lifecycle_settings = load_settings(root).lifecycle
    if not lifecycle_settings.enable_envfacts:
        return 0
    observations = extract_envfacts(events, session_id)
    if not observations:
        return 0
    facts = load_envfacts(root)
    newly_demoted = []
    for obs in observations:
        fid = fact_id(obs.category, obs.key)
        prev = facts.get(fid)
        cur = prev if prev is not None else Fact(id=fid, key=obs.key, category=obs.category)
        nxt = observe_envfact(cur, obs, lifecycle_settings)
        facts[fid] = nxt
        if cur.state != "demoted" and nxt.state == "demoted":
            newly_demoted.append(nxt)
    save_envfacts(root, facts)
    for df in newly_demoted:
        notify_alter(root, df, "contradiction")
    return len(observations)


def run_fold(root: Path | str, session_id: str, transcript: Path | str) -> dict:
    """Fold one finished session into the usage ledger and EnvFacts under a non-blocking lock."""
    root_path = Path(root)
    with _acquire_fold_lock(root_path / _LOCK_REL) as got:
        if not got:
            return {"status": "busy"}
        try:
            tpath = Path(transcript)
            if not tpath.is_file():
                raise FileNotFoundError(f"transcript not found: {tpath}")
            events = read_events(tpath)
            if not events and tpath.stat().st_size > 0 and not _has_valid_json_line(tpath):
                raise ValueError(f"corrupt transcript: {tpath}")
            totals, model, usage_count = _sum_usage(events)
            if usage_count > 0:
                row = usage_row(totals, model=model, category="agent", caller="interactive")
                ledger.append_rows(root_path, [row])
            obs_count = _observe_session_envfacts(root_path, session_id, events)
            return {
                "status": "ok",
                "events": len(events),
                "usage_events": usage_count,
                "observations": obs_count,
            }
        except Exception as exc:
            _record_error(root_path, session_id, exc)
            return {"status": "error"}


def spawn_fold(root: Path | str, session_id: str, transcript: Path | str) -> None:
    """Spawn `python -m ego fold` in a detached process without waiting; fail-open."""
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ego",
                "fold",
                "--root",
                str(root),
                "--session",
                session_id,
                "--transcript",
                str(transcript),
            ],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
