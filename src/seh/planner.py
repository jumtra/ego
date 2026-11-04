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
    # Scope is a proposal only; user must explicitly approve application.
    if scope is None:
        scope=["src/**","tests/**", "test/**", "app/**", "lib/**", "cmd/**", "pkg/**",
               "internal/**", "Sources/**", "Source/**", "Assets/**", "Scripts/**"]
        # Existing root-level source files in Go, Java, C/C++, Swift, C#, etc. are
        # not silently converted into or constrained to Python-style src/ paths.
        from .toolchains import SOURCE_EXTENSIONS
        scope += [p.name for p in root.iterdir() if p.is_file() and p.suffix in SOURCE_EXTENSIONS and not p.is_symlink()]
        scope=list(dict.fromkeys(scope))

    # Model output is treated as a structured proposal, never executed without separate approval.
    if host=="mock":
        obj={"goal":goal,"nodes":[{"id":"implement","kind":"agent","prompt":goal,"write_allowlist":scope,"max_attempts":2}]}
    else:
        if not execute:raise SEHError("EXPLICIT_EXECUTION_REQUIRED","Planning via model requires --execute")
        cfg=load_config(root)
        from .detect import detect
        project_context=detect(root)
        ctx=search(root,goal,cfg.code_intelligence,limit=8)
        prompt=("Return ONLY a valid JSON object with 'goal' and 'nodes'. "
                "Every node: id (alphanumeric), kind in ['agent','code','gate'], depends_on list, "
                "prompt (for agent nodes), write_allowlist (for agent nodes). "
                "Use the fewest nodes and only real dependencies. No unapproved commands. "
                f"Allowed write scope: {json.dumps(scope)}. Goal: {goal}. "
                f"Detected projects and toolchains (do not assume Python): {json.dumps(project_context.get('projects', []))[:4500]}. "
                f"Relevant project evidence: {json.dumps(ctx)[:5000]}")
        response=run_agent(host,prompt,root,timeout=150,read_only=True)
        if response.status!="PASS":raise SEHError("PLAN_FAILED",response.stderr,5)
        try: obj=json.loads(response.output)
        except ValueError as exc:raise SEHError("PLAN_INVALID_JSON",response.output[:300],5) from exc
    validate_task(obj)
    result={"plan_id":ident("task"),"task":obj,"approval_required_before_apply":True}
    return result
