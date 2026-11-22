"""Replanning inside `ego work run`: a failed run is planned again from its bounded history.

Verified nodes of the failed run are carried into the new plan, so only the remainder reruns.
The planner's output is a proposal: it may not widen the original write allowlists or use a
command the policy denies.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any

from ..domain.config import load_config, resolve_max_run_tokens
from ..domain.policy import check_command
from ..shared.util import EgoError, clip_output, filehash, json_text, safe_path, sha256
from .planner import plan_work
from .work import TokenBudget, execute

DETAILED_HISTORY = 2
REPLAN_KEYS = ("max_replans", "on_exhausted")


def keep_verified(spec: dict, previous: dict, node_status: dict) -> dict:
    """Prepend the previous run's verified agent nodes (and their ancestors), copied verbatim.

    Unchanged definitions keep their fingerprint, so execute() skips them and runs only
    the replanned remainder. A planned node with the same id replaces the verified one
    and runs again; the kept nodes downstream of it then change fingerprint and rerun too.
    """
    planned = {n.get("id") for n in spec.get("nodes", []) if isinstance(n, dict)}
    before = {n["id"]: n for n in previous["nodes"]}
    stack = [nid for nid, n in before.items()
             if node_status.get(nid) == "PASSED" and n.get("kind", "agent") not in ("code", "gate")]
    keep: set[str] = set()
    while stack:
        nid = stack.pop()
        if nid in keep or nid in planned:
            continue
        keep.add(nid)
        stack.extend(before[nid].get("depends_on", []))
    if not keep:
        return spec
    return {**spec, "nodes": [dict(n) for nid, n in before.items() if nid in keep] + list(spec.get("nodes", []))}


def history_text(iterations: list[dict], *, max_chars: int, feedback_chars: int, detailed: int) -> str | None:
    """Bounded replan input. Recent iterations in detail, older ones masked to a line."""
    if not iterations:
        return None
    lines: list[str] = []
    old, recent = iterations[:-detailed], iterations[-detailed:]
    for it in old:
        lines.append(f"Iteration {it['n']}: {it['status']}" +
                     (f"; failed {', '.join(f['node'] for f in it.get('failures', []))}" if it.get("failures") else ""))
    for it in recent:
        lines.append(f"Iteration {it['n']}: {it['status']}")
        if it.get("passed"):
            lines.append("  verified nodes (kept, not re-run): " + ", ".join(it["passed"][:12]))
        for failure in it.get("failures", []):
            stag = failure.get("stagnation")
            stag_line = f"  stagnation: {stag}\n" if stag is not None else ""
            lines.append(f"  failed node {failure['node']}:\n{stag_line}" + clip_output(failure["feedback"], feedback_chars))
    changed = recent[-1].get("changed_files", [])
    if changed:
        lines.append("Files changed so far: " + ", ".join(changed[:20]))
    return clip_output("\n".join(lines), max_chars)


def _signature(root: Path, result: dict, failures: list[dict]) -> str:
    """Identical staged content and identical failure text means no progress."""
    staging = root / ".ego" / "state" / "runs" / result["run_id"] / "staging"
    files = {rel: filehash(safe_path(staging, rel)) for rel in result.get("changed_files", [])}
    return sha256(json_text({"files": files, "failures": [[f["node"], f["feedback"]] for f in failures]}))


def _failures(result: dict, limit: int) -> list[dict]:
    out = []
    for nid, data in result.get("results", {}).items():
        if result.get("node_status", {}).get(nid) == "PASSED":
            continue
        last = (data.get("attempts") or [{}])[-1]
        text = last.get("feedback") or last.get("execution", {}).get("stderr") or data.get("output") or "no output"
        entry: dict[str, Any] = {"node": nid, "feedback": clip_output(str(text), limit)}
        if last.get("stagnation") is not None:
            entry["stagnation"] = last["stagnation"]
        out.append(entry)
    return out


def _entry(n: int, result: dict, limit: int) -> dict[str, Any]:
    return {"n": n, "run_id": result["run_id"], "status": result["final_status"],
            "failures": _failures(result, limit),
            "passed": [nid for nid, s in result.get("node_status", {}).items() if s == "PASSED"],
            "changed_files": result.get("changed_files", [])}


def _replan_wanted(spec: dict, result: dict) -> bool:
    if result.get("final_status") != "FAILED":
        return False
    exhausted = set((result.get("exhausted") or {}).values())
    return "replan" in exhausted or (spec.get("on_exhausted") == "replan" and "fail" not in exhausted)


def _out_of_scope(spec: dict, allowed: set[str], cfg) -> bool:
    for node in spec["nodes"]:
        if any(p not in allowed for p in node.get("write_allowlist", [])):
            return True
        for argv in ([node["argv"]] if node.get("argv") else []) + list(node.get("checks") or []):
            try:
                check_command(argv, cfg.allow_commands, cfg.policy_mode)
            except EgoError:
                return True
    return False


def run_with_replans(root: Path, spec: dict, *, run_id: str | None, host: str | None, trust_cli: bool,
                     max_run_tokens: int | None = None, **kw) -> dict:
    root = root.resolve()
    cfg = load_config(root)
    budget = TokenBudget(resolve_max_run_tokens(cfg, max_run_tokens))
    allowed = {p for n in spec.get("nodes", []) for p in n.get("write_allowlist", [])}
    may_replan = trust_cli or host == "mock"
    result = execute(root, spec, run_id=run_id, host=host, trust_cli=trust_cli, token_budget=budget, **kw)
    base = result["run_id"]
    iterations = [_entry(1, result, cfg.tuning.max_summary_chars)]
    replans: list[dict] = []
    current, seen, n = spec, None, 0
    while may_replan and _replan_wanted(current, result) and n < spec.get("max_replans", 0):
        signature = _signature(root, result, iterations[-1]["failures"])
        if signature == seen:
            result["final_status"] = "STALLED"
            break
        seen = signature
        n += 1
        previous_run = result["run_id"]
        plan = plan_work(root, spec["goal"], host, scope=sorted(allowed), execute=True,
                         history=history_text(iterations, max_chars=cfg.tuning.max_feedback_chars,
                                              feedback_chars=cfg.tuning.max_summary_chars, detailed=DETAILED_HISTORY),
                         workdir=root / ".ego" / "state" / "runs" / previous_run / "staging")
        if plan.get("planner_usage") is not None:
            budget.charge(plan["planner_usage"])
        replanned = keep_verified(plan.get("task", plan), current, result.get("node_status", {}))
        replanned = {**replanned, **{k: spec[k] for k in REPLAN_KEYS if k in spec}}
        if _out_of_scope(replanned, allowed, cfg):
            result["final_status"] = "REPLAN_OUT_OF_SCOPE"
            replans.append({"n": n, "run_id": None, "status": result["final_status"], "failures": []})
            break
        current = replanned
        result = execute(root, current, run_id=f"{base}-r{n}", host=host, trust_cli=trust_cli, token_budget=budget,
                         seed_from=previous_run, **{k: v for k, v in kw.items() if k not in ("resume", "seed_from")})
        iterations.append(_entry(n + 1, result, cfg.tuning.max_summary_chars))
        replans.append({"n": n, "run_id": result["run_id"], "status": result["final_status"],
                        "failures": iterations[-1]["failures"]})
    result["replans"] = replans
    return result
