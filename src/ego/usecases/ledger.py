"""Usage ledger: one C2 usage row per model call, appended as JSON lines under `.ego/state`.

Rows are written by the work run (planner = overhead, node attempts = agent) and read back
by `ego usage summary`. Pricing lives in domain/pricing.py; this module only stores and aggregates.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..domain.usage import CATEGORIES, cache_hit_rate

LEDGER = Path(".ego/state/usage-ledger.jsonl")
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens")


def append_rows(root: Path, rows: list[dict]) -> None:
    """Append rows as JSON lines. Fail-open: an unwritable ledger must never stop a run."""
    if not rows:
        return
    path = root / LEDGER
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def _parse_row(line: str, cutoff: datetime) -> dict | None:
    """The row when the line is a well-formed usage row inside the window; None when outside it. Raises ValueError when malformed."""
    row = json.loads(line)
    if not isinstance(row, dict) or row.get("category") not in CATEGORIES:
        raise ValueError("not a usage row")
    cost = row.get("cost_usd")
    if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool)):
        raise ValueError("cost_usd is not a number")
    try:
        at = datetime.fromisoformat(str(row.get("at")))
    except ValueError:
        raise ValueError("at is not a timestamp")
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return row if at >= cutoff else None


def summarize(root: Path, days: int) -> dict:
    """Totals for the last `days` days, split by category and caller; malformed lines are counted, not fatal."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_category = {name: {"calls": 0, "tokens": 0, "cost_usd": 0.0, "unpriced_calls": 0} for name in CATEGORIES}
    by_caller: dict[str, dict] = {}
    totals = {field: 0 for field in _TOKEN_FIELDS}
    total_cost = 0.0
    unpriced = 0
    skipped = 0
    path = root / LEDGER
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
            skipped = 1
        for line in lines:
            if not line.strip():
                continue
            try:
                row = _parse_row(line, cutoff)
            except ValueError:
                skipped += 1
                continue
            if row is None:
                continue
            tokens = sum(int(row.get(field) or 0) for field in _TOKEN_FIELDS)
            for field in _TOKEN_FIELDS:
                totals[field] += int(row.get(field) or 0)
            cost = row.get("cost_usd")
            category = by_category[row["category"]]
            category["calls"] += 1
            category["tokens"] += tokens
            caller = by_caller.setdefault(str(row.get("caller")), {"calls": 0, "tokens": 0, "cost_usd": 0.0, "unpriced_calls": 0})
            caller["calls"] += 1
            caller["tokens"] += tokens
            if cost is None:
                unpriced += 1
                category["unpriced_calls"] += 1
                caller["unpriced_calls"] += 1
            else:
                total_cost += float(cost)
                category["cost_usd"] += float(cost)
                caller["cost_usd"] += float(cost)
    for bucket in [*by_category.values(), *by_caller.values()]:
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    return {
        "days": days,
        "by_category": by_category,
        "by_caller": by_caller,
        "total_cost_usd": round(total_cost, 6),
        "unpriced_calls": unpriced,
        "cache_hit_rate": cache_hit_rate(totals),
        "skipped_lines": skipped,
    }
