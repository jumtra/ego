"""Explicit plan-first workflow decomposition; no unsolicited model calls."""
from __future__ import annotations
import json
from pathlib import Path
from .util import SEHError, ident, write_json
from .runner import run_agent
from .work import validate_task
from .codeintel import search
from .config import load_config


def plan_work(root:Path,goal:str,host:str="mock",scope:list[str]|None=None,*,execute:bool=False)->dict:
    if not goal.strip() or len(goal)>6000:raise SEHError("INVALID_GOAL","Missing/oversized goal",2)
    scope=scope or ["src/**","tests/**"]
    # Model output is treated as a structured proposal, never executed without separate approval.
    if host=="mock":
        obj={"goal":goal,"nodes":[{"id":"implement","kind":"agent","prompt":goal,"write_allowlist":scope,"max_attempts":2}]}
    else:
        if not execute:raise SEHError("EXPLICIT_EXECUTION_REQUIRED","Planning via model requires --execute")
        cfg=load_config(root)
        ctx=search(root,goal,cfg.code_intelligence,limit=8)
        prompt=("Return ONLY a valid JSON object with 'goal' and 'nodes'. "
                "Every node: id (alphanumeric), kind in ['agent','code','gate'], depends_on list, "
                "prompt (for agent nodes), write_allowlist (for agent nodes). "
                "Every code/gate node MUST include argv as a nonempty command argument array, "
                "or checks as a nonempty list of command argument arrays. "
                "Checks must be argv arrays, never shell strings. "
                "Use focused checks for independent implementation nodes; put integration checks "
                "in a final gate depending on all required implementations. "
                "Return raw JSON without Markdown fences. "
                "Use the fewest nodes and only real dependencies. No unapproved commands. "
                f"Allowed write scope: {json.dumps(scope)}. Goal: {goal}. "
                f"Relevant project evidence: {json.dumps(ctx)[:5000]}")
        response=run_agent(host,prompt,root,timeout=150,read_only=True)
        if response.status!="PASS":raise SEHError("PLAN_FAILED",response.stderr,5)
        try: obj=json.loads(response.output)
        except ValueError as exc:raise SEHError("PLAN_INVALID_JSON",response.output[:300],5) from exc
    cfg = load_config(root)
    if cfg.models and "models" not in obj.get("defaults", {}):
        defaults = dict(obj.get("defaults", {}))
        defaults["models"] = dict(cfg.models)
        obj["defaults"] = defaults
    validate_task(obj)
    result={"plan_id":ident("task"),"task":obj,"approval_required_before_apply":True}
    return result
