"""Outer goal loop: plan -> execute -> evaluate -> replan until a measurable goal holds.

Context discipline is the point of this module:

* Every iteration is planned and executed by fresh host invocations. Nothing
  is carried in a conversation; state lives in a small ledger file.
* The replan input is bounded and deterministic: failed node ids, clipped
  verifier output and changed paths. Older iterations collapse to one line
  each. No transcript, no LLM summary.
* Evaluation is the caller's ``done_checks`` run as a final gate in the
  staged copy, so "done" is a command exit status, not a model's opinion.
* Verified work is carried forward by seeding the next run from the previous
  staging tree; edits of failed nodes were never merged there.
"""
from __future__ import annotations
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .context import clip_output
from .policy import check_command
from .session import save_checkpoint
from .usage import summary_line, tokens_of
from .util import EgoError, filehash, ident, now, read_json, safe_path, sha256, json_text, write_json

MAX_ITERATIONS_CAP = 10
MAX_HISTORY_CHARS = 2600
DETAILED_HISTORY = 2
DONE_NODE = "loop-done-check"
SUCCESS = ("VERIFIED", "COMPLETED")
Planner = Callable[[str, str | None, Path], dict]


def _loop_id(value: str) -> str:
    if not isinstance(value, str) or not value or not all(c.isalnum() or c in "-_" for c in value):
        raise EgoError("INVALID_ID", str(value), 2)
    return value


def ledger_path(root: Path, loop_id: str) -> Path:
    return root / ".ego" / "state" / "loops" / _loop_id(loop_id) / "ledger.json"


def with_done_gate(spec: dict, done_checks: list[list[str]]) -> dict:
    """Append the goal's checks as a final gate depending on every planned node."""
    nodes = [dict(n) for n in spec.get("nodes", [])]
    if any(n.get("id") == DONE_NODE for n in nodes):
        raise EgoError("INVALID_TASK", f"Node id {DONE_NODE} is reserved", 2)
    if len(nodes) == 1 and nodes[0].get("kind", "agent") == "agent" and nodes[0].get("checks") == done_checks:
        return {**spec, "nodes": nodes}  # the only node is already verified by exactly these checks
    gate = {"id": DONE_NODE, "kind": "gate", "depends_on": [n["id"] for n in nodes],
            "argv": done_checks[0], "checks": done_checks[1:]}
    return {**spec, "nodes": nodes + [gate]}


def history_text(iterations: list[dict]) -> str | None:
    """Bounded replan input. Recent iterations in detail, older ones masked to a line."""
    if not iterations:
        return None
    lines: list[str] = []
    old, recent = iterations[:-DETAILED_HISTORY], iterations[-DETAILED_HISTORY:]
    for it in old:
        lines.append(f"Iteration {it['n']}: {it['status']}" +
                     (f"; failed {', '.join(f['node'] for f in it.get('failures', []))}" if it.get("failures") else ""))
    for it in recent:
        lines.append(f"Iteration {it['n']}: {it['status']}")
        if it.get("passed"):
            lines.append("  verified nodes: " + ", ".join(it["passed"][:12]))
        for failure in it.get("failures", []):
            where = "goal check" if failure["node"] == DONE_NODE else f"node {failure['node']}"
            lines.append(f"  failed {where}:\n" + clip_output(failure["feedback"], 700))
    changed = recent[-1].get("changed_files", [])
    if changed:
        lines.append("Files changed so far: " + ", ".join(changed[:20]))
    return clip_output("\n".join(lines), MAX_HISTORY_CHARS)


def _signature(root: Path, result: dict, failures: list[dict]) -> str:
    """Identical staged content and identical failure text means no progress."""
    staging = root / ".ego" / "state" / "runs" / result["run_id"] / "staging"
    files = {rel: filehash(safe_path(staging, rel)) for rel in result.get("changed_files", [])}
    return sha256(json_text({"files": files, "failures": [[f["node"] == DONE_NODE, f["feedback"][-400:]] for f in failures]}))


def _failures(result: dict) -> list[dict]:
    out = []
    for nid, data in result.get("results", {}).items():
        if result.get("node_status", {}).get(nid) == "PASSED":
            continue
        last = (data.get("attempts") or [{}])[-1]
        text = last.get("feedback") or last.get("execution", {}).get("stderr") or data.get("output") or "no output"
        out.append({"node": nid, "feedback": clip_output(str(text), 800)})
    return out


def add_usage(total: dict, *, plan_tokens: int | None = None, work: dict | None = None) -> None:
    """Accumulate one planner call (plan_tokens) or one run's usage_summary (work) into the loop total."""
    if plan_tokens is not None:
        total["planned"] = True
        total["plan_tokens"] += plan_tokens
    if work:
        total["work_tokens"] += work.get("work_tokens", 0)
        for name, count in (work.get("by_task") or {}).items():
            total["by_task"][name] = total["by_task"].get(name, 0) + count


def _summary(ledger: dict) -> dict:
    messages = {
        "COMPLETED": "完了：ゴールの検証に合格し、変更を適用しました。",
        "VERIFIED": "検証完了（未適用）：ゴールの検証に合格しました。変更はまだ元のプロジェクトへ適用していません。",
    }
    out = {k: ledger[k] for k in ("loop_id", "status", "goal", "done_checks", "cost_usd", "cost_known") if k in ledger}
    out["iterations"] = [{k: it[k] for k in ("n", "status", "run_id", "failures", "cost_usd") if k in it}
                         for it in ledger["iterations"]]
    last = ledger["iterations"][-1] if ledger["iterations"] else {}
    usage = ledger.get("usage") or {"planned": False, "plan_tokens": 0, "work_tokens": 0, "by_task": {}}
    out["usage"] = usage
    out["usage_text"] = summary_line(usage)
    out["changed_files"] = last.get("changed_files", [])
    if last.get("run_id"):
        out["run_id"] = last["run_id"]
    out["message"] = messages.get(ledger["status"], f"未完了：ループは {ledger['status']} で停止しました。")
    out["ledger_path"] = f".ego/state/loops/{ledger['loop_id']}/ledger.json"
    return out


def run_loop(root: Path, goal: str, *, done_checks: list[list[str]], host: str = "mock",
             scope: list[str] | None = None, max_iterations: int = 3, max_budget_usd: float | None = None,
             max_wall_seconds: int | None = None, trust_cli: bool = False, execute_planner: bool = False,
             apply: bool = False, approve: bool = False, autonomy: str = "auto", isolated: bool = False,
             planner: Planner | None = None, loop_id: str | None = None, plan_first: bool = False,
             task: dict | None = None) -> dict[str, Any]:
    """task: an approved plan run as the first iteration; later iterations replan from the bounded history."""
    from .work import execute, validate_task
    root = root.resolve()
    cfg = load_config(root)
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 6000:
        raise EgoError("INVALID_GOAL", "Missing/oversized goal", 2)
    if not done_checks or any(not isinstance(c, list) or not c for c in done_checks):
        raise EgoError("DONE_CHECK_REQUIRED", "A loop needs at least one measurable check command (argv array)", 2)
    for argv in done_checks:
        check_command(argv, cfg.allow_commands, cfg.policy_mode)
    if not 1 <= max_iterations <= MAX_ITERATIONS_CAP:
        raise EgoError("INVALID_LOOP", f"max_iterations must be between 1 and {MAX_ITERATIONS_CAP}", 2)
    if max_budget_usd is not None and max_budget_usd <= 0:
        raise EgoError("INVALID_LOOP", "max_budget_usd must be positive", 2)
    if apply and not approve:
        raise EgoError("APPROVAL_REQUIRED", "--apply requires --approve")
    scope = scope or ["src/**", "tests/**"]
    seeded: dict | None = None
    if task is not None:
        if plan_first:
            raise EgoError("INVALID_LOOP", "--task already is the first plan; do not combine it with --plan-first", 2)
        if not isinstance(task, dict):
            raise EgoError("INVALID_TASK", "Task must be a JSON object", 2)
        seed_task = dict(task)
        # A task file written by `ego work plan --out` carries what that plan cost.
        seed_usage = seed_task.pop("planner_usage", None)
        if cfg.models and "models" not in seed_task.get("defaults", {}):
            seed_task["defaults"] = {**seed_task.get("defaults", {}), "models": dict(cfg.models)}
        # Reject a broken approved plan before the ledger exists; replanning it would silently replace it.
        validate_task(seed_task)
        for node in seed_task["nodes"]:
            for argv in ([node["argv"]] if node.get("argv") else []) + list(node.get("checks") or []):
                check_command(argv, cfg.allow_commands, cfg.policy_mode)
        seeded = {"task": seed_task, **({"planner_usage": seed_usage} if isinstance(seed_usage, dict) else {})}
    if planner is None:
        from .planner import plan_work

        def planner(goal_text: str, history: str | None, workdir: Path) -> dict:
            if history is None and not plan_first:
                # First try without a planning call: one worker, repaired against the
                # done-checks. Decomposition is paid for only once a direct attempt failed.
                task = {"goal": goal_text, "nodes": [{"id": "implement", "kind": "agent", "prompt": goal_text,
                                                      "write_allowlist": scope, "checks": done_checks}]}
                if cfg.models:
                    task["defaults"] = {"models": dict(cfg.models)}
                return {"task": task}
            return plan_work(root, goal_text, host, scope, execute=execute_planner, history=history, workdir=workdir)

    loop_id = _loop_id(loop_id or ident("loop"))
    path = ledger_path(root, loop_id)
    if path.exists():
        raise EgoError("LOOP_EXISTS", loop_id)
    ledger: dict[str, Any] = {"loop_id": loop_id, "goal": goal.strip(), "done_checks": done_checks, "scope": scope,
                              "host": host, "created_at": now(), "status": "RUNNING", "iterations": [],
                              "seeded_from_task": seeded is not None,
                              "cost_usd": 0.0, "cost_known": True,
                              "usage": {"planned": False, "plan_tokens": 0, "work_tokens": 0, "by_task": {}},
                              "limits": {"max_iterations": max_iterations, "max_budget_usd": max_budget_usd,
                                         "max_wall_seconds": max_wall_seconds}}
    write_json(path, ledger)
    start = time.monotonic()
    seed: str | None = None
    last_signature: str | None = None

    def spend(cost: Any, calls: int) -> None:
        if isinstance(cost, (int, float)):
            ledger["cost_usd"] = round(ledger["cost_usd"] + float(cost), 4)
        elif calls:
            ledger["cost_known"] = False

    for n in range(1, max_iterations + 1):
        if (path.parent / "cancel.flag").exists():
            ledger["status"] = "CANCELLED"; break
        if max_wall_seconds is not None and time.monotonic() - start > max_wall_seconds:
            ledger["status"] = "TIMEOUT"; break
        remaining = None
        if max_budget_usd is not None:
            remaining = max_budget_usd - ledger["cost_usd"]
            if remaining <= 0:
                ledger["status"] = "BUDGET_EXHAUSTED"; break
        entry: dict[str, Any] = {"n": n, "started_at": now()}
        workdir = root / ".ego" / "state" / "runs" / seed / "staging" if seed else root
        try:
            plan = seeded if n == 1 and seeded is not None else \
                planner(ledger["goal"], history_text(ledger["iterations"]), workdir)
            spec = plan.get("task", plan)
            planner_usage = plan.get("planner_usage") if isinstance(plan, dict) else None
            if planner_usage is not None:
                spend(planner_usage.get("cost_usd"), 1)
                add_usage(ledger["usage"], plan_tokens=tokens_of(planner_usage))
            spec = with_done_gate(spec, done_checks)
            # Reject a plan with a forbidden command before anything runs, so the planner
            # hears why instead of the loop dying halfway through an iteration.
            for node in spec["nodes"]:
                for argv in ([node["argv"]] if node.get("argv") else []) + list(node.get("checks") or []):
                    check_command(argv, cfg.allow_commands, cfg.policy_mode)
            result = execute(root, spec, host=host, apply=apply, approve=approve, trust_cli=trust_cli,
                             autonomy=autonomy, isolated=isolated, seed_from=seed, extra_allow=scope,
                             budget_usd=remaining, worktree=False)
        except EgoError as exc:
            # A planner call that produced no usable plan was still paid for.
            failed_usage = getattr(exc, "planner_usage", None)
            if failed_usage is not None:
                spend(failed_usage.get("cost_usd"), 1)
                add_usage(ledger["usage"], plan_tokens=tokens_of(failed_usage))
            # A plan that cannot be produced or validated is an iteration outcome, not a crash.
            if exc.code not in ("PLAN_FAILED", "PLAN_INVALID_JSON", "INVALID_TASK", "TASK_CYCLE", "INVALID_ROUTE",
                                "INSTRUCTIONS_TOO_LARGE", "INVALID_FALLBACK",
                                "COMMAND_DENIED", "DANGEROUS_COMMAND", "INVALID_ARGV"):
                ledger["status"] = "ERROR"; ledger["error"] = {"code": exc.code, "message": clip_output(str(exc), 400)}
                write_json(path, ledger)
                raise
            entry.update(status="PLAN_REJECTED", failures=[{"node": "plan", "feedback": clip_output(f"{exc.code}: {exc}", 600)}])
            signature = sha256(entry["failures"][0]["feedback"])
        else:
            usage = result.get("usage", {})
            spend(usage.get("cost_usd", 0.0), 0)
            add_usage(ledger["usage"], work=usage)
            if usage.get("cost_known", True) is False:
                ledger["cost_known"] = False
            failures = _failures(result)
            entry.update(status=result["final_status"], run_id=result["run_id"], failures=failures,
                         passed=[nid for nid, s in result.get("node_status", {}).items() if s == "PASSED"],
                         changed_files=result.get("changed_files", [])[:40], cost_usd=usage.get("cost_usd", 0.0))
            if seed:
                # The new run copied this tree; keep one staging copy per loop, not one per iteration.
                shutil.rmtree(root / ".ego" / "state" / "runs" / seed / "staging", ignore_errors=True)
            seed = result["run_id"]
            signature = _signature(root, result, failures)
            if result["final_status"] == "PASSED":
                entry["finished_at"] = now()
                ledger["iterations"].append(entry)
                ledger["status"] = "COMPLETED" if result.get("applied") else "VERIFIED"
                break
            if result["final_status"] in ("CANCELLED", "BUDGET_EXHAUSTED", "COMPARISON_READY"):
                entry["finished_at"] = now()
                ledger["iterations"].append(entry)
                ledger["status"] = result["final_status"]
                break
        entry["finished_at"] = now()
        ledger["iterations"].append(entry)
        write_json(path, ledger)
        if signature == last_signature:
            ledger["status"] = "STALLED"; break
        last_signature = signature
    else:
        ledger["status"] = "MAX_ITERATIONS"
    ledger["finished_at"] = now()
    write_json(path, ledger)
    last = ledger["iterations"][-1] if ledger["iterations"] else {}
    save_checkpoint(
        root, loop_id, goal=ledger["goal"],
        completed=[f"iteration {it['n']}: {it['status']}" for it in ledger["iterations"]][-8:],
        next_steps=[] if ledger["status"] == "COMPLETED" else
                   ["Review and apply the staged changes"] if ledger["status"] == "VERIFIED" else
                   [f"Loop stopped: {ledger['status']}"],
        blockers=[f"{f['node']}: {f['feedback'][-200:]}" for f in last.get("failures", [])][:8],
        evidence=[f"ledger: .ego/state/loops/{loop_id}/ledger.json", *([f"run: {last['run_id']}"] if last.get("run_id") else [])],
        status="COMPLETED" if ledger["status"] == "COMPLETED" else "VERIFIED" if ledger["status"] == "VERIFIED" else "BLOCKED",
        source="managed")
    return _summary(ledger)


def loop_status(root: Path, loop_id: str) -> dict[str, Any]:
    ledger = read_json(ledger_path(root.resolve(), loop_id))
    if not ledger:
        raise EgoError("LOOP_NOT_FOUND", loop_id, 3)
    return _summary(ledger)


def apply_loop(root: Path, loop_id: str, *, approve: bool = False) -> dict[str, Any]:
    """Apply the staged result of a VERIFIED loop. Runs no node again; STALE_BASE still guards the target."""
    from .work import execute
    root = root.resolve()
    path = ledger_path(root, loop_id)
    ledger = read_json(path)
    if not ledger:
        raise EgoError("LOOP_NOT_FOUND", loop_id, 3)
    if not approve:
        raise EgoError("APPROVAL_REQUIRED", "loop-apply requires --approve")
    if ledger["status"] == "COMPLETED":
        return _summary(ledger)
    if ledger["status"] != "VERIFIED":
        raise EgoError("LOOP_NOT_VERIFIED", f"Loop status is {ledger['status']}; only a VERIFIED loop can be applied", 3)
    run_id = ledger["iterations"][-1]["run_id"]
    record = read_json(root / ".ego" / "state" / "runs" / run_id / "record.json")
    if not record:
        raise EgoError("RUN_NOT_FOUND", run_id, 3)
    result = execute(root, record["spec"], host=ledger.get("host", "mock"), run_id=run_id, resume=True,
                     apply=True, approve=True, extra_allow=ledger.get("scope"), worktree=False)
    if result.get("applied"):
        ledger["status"] = "COMPLETED"
        ledger["applied_at"] = now()
        write_json(path, ledger)
        save_checkpoint(root, loop_id, goal=ledger["goal"], completed=["goal checks passed", "changes applied"],
                        evidence=[f"ledger: .ego/state/loops/{loop_id}/ledger.json", f"run: {run_id}"],
                        status="COMPLETED", source="managed")
    return _summary(ledger)


def cancel_loop(root: Path, loop_id: str) -> dict[str, Any]:
    path = ledger_path(root.resolve(), loop_id)
    if not path.exists():
        raise EgoError("LOOP_NOT_FOUND", loop_id, 3)
    (path.parent / "cancel.flag").touch()
    return {"status": "CANCELLATION_REQUESTED", "loop_id": loop_id,
            "warning": "Stops before the next iteration; the running iteration finishes first"}
