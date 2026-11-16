"""Mechanical token accounting: host usage in, per-phase numbers and one readable line out.

A token count is the sum of input, output, prompt-cache read and prompt-cache
creation tokens. Cache reads are included on purpose: they are what the host
actually processed, so the figure matches the context the work consumed.
"""
from __future__ import annotations

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def tokens_of(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(key) or 0) for key in TOKEN_KEYS)


def fmt_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def usage_text(report: dict) -> str:
    """One line such as: plan 1.2k tokens · work 45.6k tokens (task A 8.1k, task B 2.0k) · total 46.8k tokens.

    `report` may carry plan_tokens, work_tokens and by_task {task id: tokens}; a
    key that is absent is left out of the line, so each command shows only its own phases.
    """
    parts: list[str] = []
    plan = report.get("plan_tokens")
    work = report.get("work_tokens")
    if plan is not None:
        parts.append(f"plan {fmt_tokens(int(plan))} tokens")
    if work is not None:
        text = f"work {fmt_tokens(int(work))} tokens"
        tasks = report.get("by_task") or {}
        if tasks:
            text += " (" + ", ".join(f"task {name} {fmt_tokens(int(count))}" for name, count in tasks.items()) + ")"
        parts.append(text)
    if len(parts) > 1:
        parts.append(f"total {fmt_tokens(int(plan or 0) + int(work or 0))} tokens")
    return " · ".join(parts) if parts else "tokens: no model calls"


def summary_line(usage: dict) -> str:
    """The line for a usage_summary() result; the plan phase appears only when a planner call was made."""
    return usage_text({
        "plan_tokens": usage.get("plan_tokens") if usage.get("planned") else None,
        "work_tokens": usage.get("work_tokens"),
        "by_task": usage.get("by_task"),
    })
