"""Bounded DAG execution: file-level isolation, verified repairs, checkpoint/resume and opt-in apply."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import uuid
from typing import Any
from .config import load_config
from .policy import check_write, check_role_write
from .runner import run_agent, run_checked_command
from .worker import run_worker
from .util import (EgoError, ident, now, read_json, write_json, snapshot, simple_copy_tree,
                   filehash, safe_path, exclusive_lock, git_is_repo, git_worktree_add, git_worktree_remove,
                   sha256, json_text)
from .task_routing import validate_task_context, resolve_routes, build_task_prompt
from .session import managed_checkpoint, load_checkpoint, completion_message, handoff_relpath
from .context import clip_output
from .judge import parse_verdict, format_findings
from .usage import TOKEN_KEYS, tokens_of, summary_line

KINDS={"worker","agent","code","gate","splitter"}

class Progress:
    """Append-only per-run event log (`progress.jsonl`), optionally echoed to stderr.

    A calling agent follows a run through these one-line JSON events instead of
    waiting for the final summary; the full record stays on disk.
    """
    def __init__(self,path:Path|None,echo:bool=False):
        self.path,self.echo,self.lock=path,echo,threading.Lock()
        self.seq=0
        if path is not None and path.exists():
            self.seq=sum(1 for line in path.read_text(encoding="utf8").splitlines() if line.strip())

    def emit(self,event:str,**fields)->None:
        if self.path is None:return
        with self.lock:
            self.seq+=1
            line=json.dumps({"seq":self.seq,"at":now(),"event":event,**fields},ensure_ascii=False)
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.path.open("a",encoding="utf8") as f:f.write(line+"\n")
            if self.echo:print(line,file=sys.stderr,flush=True)


def _brief_findings(findings)->list[str]:
    return [clip_output(json.dumps(f,ensure_ascii=False) if not isinstance(f,str) else f,300) for f in (findings or [])[:5]]


def validate_task(spec: dict) -> list[dict]:
    if not isinstance(spec,dict) or not isinstance(spec.get("nodes"),list) or not spec["nodes"]:
        raise EgoError("INVALID_TASK", "Task must contain non-empty nodes array", 2)
    nodes=spec["nodes"]
    ids=[n.get("id") for n in nodes if isinstance(n,dict)]
    if len(ids)!=len(nodes) or any(not isinstance(x,str) or not x.replace("-","").replace("_","").isalnum() for x in ids) or len(set(ids))!=len(ids):
        raise EgoError("INVALID_TASK", "Invalid/duplicate node ids", 2)
    amap={n["id"]:n for n in nodes}
    for n in nodes:
        if n.get("kind","agent") not in KINDS: raise EgoError("INVALID_TASK", "Unknown kind", 2)
        if not isinstance(n.get("depends_on",[]),list) or any(d not in amap for d in n.get("depends_on",[])):
            raise EgoError("INVALID_TASK", "Missing dependency", 2)
        if n.get("kind","agent") in ("code","gate") and not (n.get("argv") or n.get("checks")):
            raise EgoError("INVALID_TASK", "Code/gate nodes need argv or checks", 2)
        if n.get("kind","agent") not in ("code","gate") and not isinstance(n.get("prompt"),str):
            raise EgoError("INVALID_TASK", "Agent nodes need prompt", 2)
        if not isinstance(n.get("write_allowlist",[]),list):raise EgoError("INVALID_TASK", "Invalid allowlist", 2)
        if n.get("role")=="judge" and n.get("write_allowlist"):raise EgoError("INVALID_TASK", f"judge {n['id']} is read-only: no write_allowlist", 2)
        if n.get("role")=="tester" and any(not str(p).startswith("tests/") for p in n.get("write_allowlist",[])):
            raise EgoError("INVALID_TASK", f"tester {n['id']} may only write under tests/", 2)
        if n.get("kind","agent") not in ("gate",) and n.get("role")!="judge" and not n.get("write_allowlist"):
            # Explicitly require scope declarations to avoid broad accidental merges.
            raise EgoError("INVALID_TASK", f"write_allowlist required for {n['id']}",2)
    temp=set();done=set()
    def visit(nid):
        if nid in temp:raise EgoError("TASK_CYCLE",nid,2)
        if nid in done:return
        temp.add(nid)
        for d in amap[nid].get("depends_on",[]):visit(d)
        temp.remove(nid);done.add(nid)
    for nid in ids:visit(nid)
    validate_task_context(spec, nodes)
    for n in nodes:
        if n.get("role")!="judge":continue
        target=n.get("reviews")
        if target not in _ancestors(amap,n["id"]) or amap[target].get("role")!="coder" or amap[target].get("kind","agent") in ("code","gate"):
            raise EgoError("INVALID_TASK", f"judge {n['id']} needs reviews: a coder node it depends on", 2)
        if not any(amap[d].get("kind","agent") in ("gate","code") for d in n.get("depends_on",[])):
            raise EgoError("INVALID_TASK", f"judge {n['id']} must depend on a gate or code node", 2)
        if not isinstance(n.get("max_retries",1),int) or not 0<=n.get("max_retries",1)<=2:
            raise EgoError("INVALID_TASK", f"max_retries of {n['id']} must be 0-2", 2)
    return nodes


def _ancestors(amap:dict, nid:str)->set:
    out=set();stack=list(amap[nid].get("depends_on",[]))
    while stack:
        d=stack.pop()
        if d not in out:out.add(d);stack.extend(amap[d].get("depends_on",[]))
    return out


def node_fingerprints(spec:dict)->dict[str,str]:
    """Per node: a hash of its own definition, the shared task context and every ancestor's fingerprint.

    Equal fingerprints mean the node would get the same prompt, routing and upstream work.
    The goal is left out: a replanned task restates the same objective, and a node is
    accepted by its own checks, not by the goal's wording.
    """
    amap={n["id"]:n for n in spec["nodes"]}
    context={k:spec[k] for k in ("instructions","instructions_file","defaults") if k in spec}
    out:dict[str,str]={}
    def visit(nid:str)->str:
        if nid not in out:
            node=amap[nid]
            out[nid]=sha256(json_text({"node":node,"context":context,"deps":[visit(d) for d in node.get("depends_on",[])]}))
        return out[nid]
    for nid in amap:visit(nid)
    return out


def _carry_verified(nodes:list[dict], fingerprints:dict[str,str], seed_record:dict)->list[str]:
    """Agent nodes the seed run verified with an identical fingerprint; their edits are already in the seeded staging.

    Code and gate nodes are cheap and re-verify the seeded tree, so they always run again.
    """
    before=seed_record.get("fingerprints") or {}
    status=seed_record.get("node_status") or {}
    return [n["id"] for n in nodes if n.get("kind","agent") not in ("code","gate")
            and status.get(n["id"])=="PASSED" and before.get(n["id"])==fingerprints[n["id"]]]


def _between(amap:dict, target:str, judge:str)->set:
    """Nodes to rerun after a rejection: the reviewed coder, everything downstream of it that the judge waits on, and the judge."""
    above=_ancestors(amap,judge)
    return {target,judge}|{x for x in above if target in _ancestors(amap,x)}


def _changed(before:dict[str,str],after:dict[str,str])->list[str]:
    return sorted({p for p in before.keys()|after.keys() if before.get(p)!=after.get(p)})


def _run_node(node:dict, spec:dict, staging:Path, default_host:str, cfg, timeout:int, *, feedback:str="", trust_cli:bool=False,
              autonomy: str="auto", isolated:bool=False, handoff:dict|None=None,
              budget_usd: float|None=None, progress:Progress|None=None)->dict:
    """A task node sees a private copy; fallback never reuses failed edits.

    Compare runs have their own isolated copies and never merge any candidate.
    """
    progress=progress or Progress(None)
    start=time.monotonic()
    kind=node.get("kind","agent")
    if kind in ("code","gate"):
        strategy,routes,fallback_on="single",[],frozenset()
    else:
        strategy,routes,fallback_on=resolve_routes(node,spec,default_host,default_models=cfg.models,default_roles=cfg.roles)
    attempts=[]
    max_attempts=min(int(node.get("max_attempts",cfg.max_attempts)),cfg.max_attempts)
    if node.get("role")=="judge":max_attempts=1  # one audit per run; the DAG decides about retries
    verdict_info={}
    outputs=[]
    def attempt_one(chosen_host:str, chosen_model:str|None, chosen_effort:str|None, iteration:int) -> dict:
        nonlocal feedback
        with tempfile.TemporaryDirectory(prefix="ego-node-") as tmp:
            work=Path(tmp)/"src"
            simple_copy_tree(staging,work)
            before=snapshot(work)
            if kind in ("code","gate"):
                r=run_checked_command(node.get("argv") or node["checks"][0],work,timeout=timeout,allow=cfg.allow_commands)
                # A failing command's own output is the evidence; without it a retry or replan is blind.
                if r.status!="PASS":feedback=clip_output((r.output+"\n"+r.stderr).strip() or f"exit {r.exit_code}",4000)
            else:
                if chosen_host!="mock" and not trust_cli:
                    raise EgoError("UNTRUSTED_AGENT_EXEC", "Real CLI agents require --trust-cli",3)
                prompt=build_task_prompt(spec,node,work,feedback=feedback,handoff=handoff)
                try:
                    r=run_worker(chosen_host,prompt,work,timeout,agent=run_agent,threshold=cfg.clear_suggest_tokens,
                                 segment_turns=cfg.worker_segment_turns,tag=f"{node['id']}-{iteration}-{uuid.uuid4().hex[:8]}",
                                 on_event=lambda **f:progress.emit("worker_segment",node=node["id"],attempt=iteration,**f),
                                 model=chosen_model,effort=chosen_effort,
                                 read_only=kind=="splitter" or node.get("role")=="judge",autonomy=autonomy,isolated=isolated,
                                 max_turns=max(1,min(int(node.get("max_turns",cfg.max_turns)),200)),
                                 lean=cfg.worker_context=="lean",max_budget_usd=budget_usd)
                except EgoError as exc:
                    if exc.code not in ("CLI_INCOMPATIBLE","HOST_UNAVAILABLE"):
                        raise
                    from .runner import RunResult
                    r=RunResult("UNAVAILABLE","",str(exc),127,0,chosen_host,reason=exc.code,model=chosen_model,effort=chosen_effort)
            checks=[]
            if r.status=="PASS":
                for argv in node.get("checks",[]):
                    cr=run_checked_command(argv,work,timeout=min(timeout,120),allow=cfg.allow_commands)
                    checks.append({**cr.as_dict(),"argv":argv})
                    if cr.status!="PASS":feedback=clip_output(cr.output+"\n"+cr.stderr,4000);break
            ok=(r.status=="PASS" and all(c["status"]=="PASS" for c in checks))
            if ok and node.get("role")=="judge":
                verdict_info.update(parse_verdict(r.output))
                if verdict_info["verdict"]=="INVALID":ok=False;feedback="Judge output has no VERDICT line"
                elif verdict_info["verdict"]=="REJECT":ok=False;feedback=format_findings(verdict_info["findings"])
            changed=_changed(before,snapshot(work))
            try:
                for rel in changed:
                    check_role_write(node.get("role"),rel)
                    check_write(work,rel,node.get("write_allowlist",[]))
                if kind=="gate" and changed:raise EgoError("GATE_WRITE","A gate may not modify source")
            except EgoError as exc:
                ok=False;feedback=str(exc)
            record={"attempt":iteration,"host":chosen_host,"model":chosen_model or "native-default",
                    "effort":chosen_effort or "auto",
                    "status":"PASS" if ok else "FAIL",
                    # The record keeps bounded evidence, not up to 1 MB of raw output per attempt.
                    "execution":{**r.as_dict(),"output":clip_output(r.output,20000),"stderr":clip_output(r.stderr,8000)},
                    "checks":[{**c,"output":clip_output(c.get("output",""),8000),"stderr":clip_output(c.get("stderr",""),8000)} for c in checks],
                    "changed":changed,"feedback":feedback}
            edits={}
            if ok:
                for rel in changed:
                    path=safe_path(work,rel)
                    edits[rel]=path.read_bytes() if path.exists() else None
            progress.emit("attempt_finished",node=node["id"],attempt=iteration,host=chosen_host,model=record["model"],
                          status=record["status"],checks=[{"argv":c["argv"],"status":c["status"]} for c in checks],
                          feedback=clip_output(feedback or "",800),changed=changed[:20],
                          tokens=tokens_of(r.usage or {}),segments=getattr(r,"segments",1),rotations=getattr(r,"rotations",0))
            return {"ok":ok,"record":record,"edits":edits,"output":r.output,"execution_status":r.status}

    if strategy=="compare":
        results=[]
        for route in routes:
            res=attempt_one(route.host,route.model,route.effort,1)
            attempts.append(res["record"])
            outputs.append({"host":route.host,"model":route.model or "native-default",
                            "effort":route.effort or "auto",
                            "status":res["record"]["status"],"changed":res["record"]["changed"]})
        return {"id":node["id"],"status":"PASSED","attempts":attempts,"edits":{},
                "elapsed":round(time.monotonic()-start,4),"comparison":outputs,
                "review_required":True,"output":"Compare finished; no candidate automatically selected"}

    route_pos=0
    success=False;edits={};last_output=""
    for iteration in range(1,max_attempts+len(routes)+1):
        chosen=routes[route_pos] if routes else None
        r=attempt_one(chosen.host if chosen else "local",chosen.model if chosen else None,chosen.effort if chosen else None,iteration)
        attempts.append(r["record"])
        last_output=r["output"]
        if r["ok"]:
            success=True;edits=r["edits"];break
        if strategy=="fallback" and r["execution_status"] in fallback_on and route_pos<len(routes)-1:
            route_pos+=1
            feedback="Previous host was unavailable; start from the original task state."
            continue
        # A task evaluation failure must not silently switch vendors.
        if iteration>=max_attempts+route_pos:
            break
        if r["execution_status"] in ("UNAVAILABLE","TIMEOUT","RATE_LIMIT"):
            break
        feedback=r["record"].get("feedback") or r["record"]["execution"].get("stderr") or "Verification failed"
    out={"id":node["id"],"status":"PASSED" if success else "FAILED", "attempts":attempts,
         "edits":edits,"elapsed":round(time.monotonic()-start,4),"output":clip_output(last_output,20000)}
    if verdict_info:out["verdict"]=verdict_info["verdict"];out["findings"]=verdict_info["findings"]
    return out


def usage_summary(record:dict)->dict:
    """Aggregate model traffic of a run, by phase: plan (planner call) and work (per task node).

    cost_known is False when a host reported no cost. by_task maps each node id
    to its own token count, so a reader can see where the tokens went.
    """
    total={"model_calls":0,"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,
           "cache_creation_tokens":0,"cost_usd":0.0,"cost_known":True,"planned":False,"plan_tokens":0,"work_tokens":0,"by_task":{}}
    planner=record.get("planner_usage")
    if isinstance(planner,dict):
        total["model_calls"]+=1
        total["planned"]=True
        total["plan_tokens"]=tokens_of(planner)
        for key in TOKEN_KEYS:total[key]+=int(planner.get(key,0) or 0)
        if isinstance(planner.get("cost_usd"),(int,float)):total["cost_usd"]+=float(planner["cost_usd"])
        else:total["cost_known"]=False
    for nid,result in record.get("results",{}).items():
        for attempt in result.get("attempts",[]):
            ex=attempt.get("execution",{})
            if ex.get("host") in (None,"local","mock"):continue
            total["model_calls"]+=1
            usage=ex.get("usage") or {}
            for key in TOKEN_KEYS:
                total[key]+=int(usage.get(key,0) or 0)
            count=tokens_of(usage)
            total["work_tokens"]+=count
            total["by_task"][nid]=total["by_task"].get(nid,0)+count
            if isinstance(ex.get("cost_usd"),(int,float)):total["cost_usd"]+=float(ex["cost_usd"])
            else:total["cost_known"]=False
    total["cost_usd"]=round(total["cost_usd"],4)
    return total


def next_action(result:dict, task_file:str="<task-file>")->str|None:
    if result.get("final_status")!="PASSED" or result.get("applied") or not result.get("run_id"):return None
    return (f"ego work resume {result['run_id']} --root . --task {task_file} --host {result.get('host','<host>')} "
            "--trust-cli --apply --approve")


def compact_result(result:dict, *, max_files:int=40, task_file:str="<task-file>")->dict:
    """What a calling agent needs to decide its next step, without the record.

    The full record (per-file base snapshot, every attempt and raw worker
    output) stays on disk; printing it would put it in the caller's context.
    """
    failures=[]
    for nid,data in result.get("results",{}).items():
        if result.get("node_status",{}).get(nid)=="PASSED":continue
        last=(data.get("attempts") or [{}])[-1]
        text=last.get("feedback") or last.get("execution",{}).get("stderr") or data.get("output") or ""
        failures.append({"node":nid,"attempts":len(data.get("attempts",[])),"feedback":clip_output(str(text),800)})
    changed=result.get("changed_files",[])
    out={k:result[k] for k in ("run_id","final_status","applied","node_status","message","checkpoint_path","usage","progress") if k in result}
    nodes={}
    for nid,data in result.get("results",{}).items():
        last=(data.get("attempts") or [{}])[-1]
        entry={"status":data.get("status"),"attempts":len(data.get("attempts",[])),
               "checks":[{"argv":c.get("argv"),"status":c.get("status")} for c in last.get("checks",[])]}
        if data.get("verdict"):entry["verdict"]=data["verdict"];entry["findings"]=_brief_findings(data.get("findings"))
        nodes[nid]=entry
    if nodes:out["nodes"]=nodes
    action=next_action(result,task_file)
    if action:out["next_action"]=action
    if "usage" in result:out["usage_text"]=summary_line(result["usage"])
    out["changed_files"]=changed[:max_files]
    if len(changed)>max_files:out["changed_files_omitted"]=len(changed)-max_files
    if failures:out["failures"]=failures
    comparison={nid:data["comparison"] for nid,data in result.get("results",{}).items() if data.get("comparison")}
    if comparison:out["comparison"]=comparison
    if result.get("run_id"):out["record_path"]=f".ego/state/runs/{result['run_id']}/record.json"
    return out


def _execute(root:Path, spec:dict, progress:Progress, *, host:str="mock",apply:bool=False,approve:bool=False,
            trust_cli:bool=False,run_id:str|None=None,resume:bool=False,autonomy:str="auto",isolated:bool=False,
            worktree:bool|None=None,cleanup_worktree:bool|None=None,seed_from:str|None=None,
            extra_allow:list[str]|None=None,budget_usd:float|None=None,planner_usage:dict|None=None)->dict:
    root=root.resolve();cfg=load_config(root)
    use_worktree = worktree if worktree is not None else (cfg.isolation == "worktree")
    seed_dir=None
    if seed_from:
        # Continue from the verified staging of an earlier run: only edits of
        # PASSED nodes were ever merged there, so failed attempts are not inherited.
        if not all(c.isalnum() or c in "-_" for c in seed_from):raise EgoError("INVALID_ID",seed_from,2)
        seed_dir=root/".ego"/"state"/"runs"/seed_from/"staging"
        seed_record=read_json(seed_dir.parent/"record.json")
        if not seed_dir.is_dir() or not seed_record:raise EgoError("SEED_MISSING",f"No staging for run {seed_from}",3)
        use_worktree=False
    should_cleanup = cleanup_worktree if cleanup_worktree is not None else cfg.cleanup_worktree
    nodes=validate_task(spec)
    if apply and not approve:raise EgoError("APPROVAL_REQUIRED", "--apply requires --approve")
    if autonomy=="bypass" and not isolated:
        raise EgoError("ISOLATION_REQUIRED","Bypass requires explicit external sandbox attestation",3)
    if any(resolve_routes(n,spec,host,default_models=cfg.models,default_roles=cfg.roles)[0]=="compare" for n in nodes if n.get("kind","agent") not in ("gate","code")) and apply:
        raise EgoError("COMPARE_NO_APPLY","Compare never applies unreviewed changes",3)
    run_id=run_id or ident("run")
    if not all(c.isalnum() or c in "-_" for c in run_id):raise EgoError("INVALID_ID",run_id,2)
    runroot=root/".ego"/"state"/"runs"/run_id
    staging=runroot/"staging"
    record_path=runroot/"record.json"
    with exclusive_lock(root/".ego"/"locks"/(run_id+".lock")):
        if resume:
            record=read_json(record_path)
            if not record:raise EgoError("RUN_NOT_FOUND",run_id,3)
            if record["spec"]!=spec:raise EgoError("RESUME_MISMATCH","Task specification changed")
            # An explicit resume supersedes an earlier cancel request; otherwise the run could never continue.
            (runroot/"cancel.flag").unlink(missing_ok=True)
            # A verified, unapplied run may be approved and applied on resume
            # without re-running the entire DAG. An already-applied run is inert.
            if record.get("final_status")=="PASSED" and (not apply or record.get("applied")):
                return {**{k:v for k,v in record.items() if k!="spec"}, "message":completion_message(record),
                        "checkpoint_path":handoff_relpath(run_id)}
        else:
            if record_path.exists():raise EgoError("RUN_EXISTS",run_id)
            is_git = git_is_repo(root)
            worktree_created = False
            if use_worktree and is_git:
                branch_name = f"ego-task-{run_id}"
                staging.parent.mkdir(parents=True, exist_ok=True)
                if git_worktree_add(root, staging, commit_or_branch="HEAD", new_branch=branch_name):
                    worktree_created = True
                else:
                    staging.mkdir(parents=True, exist_ok=True)
                    simple_copy_tree(root, staging)
            else:
                staging.mkdir(parents=True)
                simple_copy_tree(seed_dir or root,staging)
            record={"run_id":run_id,"created_at":now(),"spec":spec,"host":host,"node_status":{},"results":{},
                    # Tokens spent by the planning call that produced this task, reported with the run.
                    **({"planner_usage":planner_usage} if planner_usage is not None else {}),
                    # A seeded run keeps the original base: changes are measured against, and
                    # applied over, the project as it was when the first run started.
                    # A worktree starts from HEAD, not from the working tree: measuring it against the
                    # working tree would report (and apply) uncommitted or untracked files as reverted/deleted.
                    "base_snapshot":seed_record["base_snapshot"] if seed_dir else snapshot(staging if worktree_created else root),
                    "final_status":"RUNNING","applied":False,
                    "worktree":worktree_created,
                    "fingerprints":node_fingerprints(spec)}
            if seed_dir:
                carried=_carry_verified(nodes,record["fingerprints"],seed_record)
                for nid in carried:
                    record["node_status"][nid]="PASSED"
                    record["results"][nid]={"id":nid,"status":"PASSED","attempts":[],"carried_from":seed_from}
                if carried:record["carried"]=carried
            write_json(record_path,record)
        # A resume is a fresh host invocation; carry only the most recent
        # durable, bounded summary, not the previous session transcript.
        handoff = load_checkpoint(root, run_id) if resume else None
        progress.emit("run_started",run_id=run_id,nodes=[n["id"] for n in nodes],resume=resume)
        if record.get("carried") and not resume:
            progress.emit("nodes_carried",nodes=record["carried"],from_run=seed_from)
        start=time.monotonic()
        amap={n["id"]:n for n in nodes}
        feedbacks:dict[str,str]={}
        if resume:
            # A failed node reruns with the evidence of its last failure instead of repeating the same attempt blind.
            for nid,data in record["results"].items():
                if nid not in amap or record["node_status"].get(nid)=="PASSED":continue
                last=(data.get("attempts") or [{}])[-1]
                text=last.get("feedback") or last.get("execution",{}).get("stderr") or data.get("output")
                if not text:continue
                node=amap[nid]
                if node.get("role")=="judge":
                    if data.get("verdict")!="REJECT":continue
                    # Rerunning the judge alone would reject the same code: send the reviewed coder back with the findings.
                    feedbacks[node["reviews"]]=clip_output(str(text),4000)
                    for rid in _between(amap,node["reviews"],nid):record["node_status"].pop(rid,None)
                else:
                    feedbacks[nid]=clip_output(str(text),4000)
        pending={n["id"]:n for n in nodes if record["node_status"].get(n["id"])!="PASSED"}
        failed=[]
        retries=record.setdefault("judge_retries",{})
        while pending:
            if (runroot/"cancel.flag").exists():
                record["final_status"]="CANCELLED";break
            if time.monotonic()-start>cfg.timeout_seconds*len(nodes):
                record["final_status"]="TIMEOUT";break
            remaining_budget=None
            if budget_usd is not None:
                remaining_budget=budget_usd-usage_summary(record)["cost_usd"]
                if remaining_budget<=0:
                    record["final_status"]="BUDGET_EXHAUSTED";break
            ready=[n for n in pending.values() if all(record["node_status"].get(dep)=="PASSED" for dep in n.get("depends_on",[]))]
            if not ready:
                record["final_status"]="FAILED" if failed else "BLOCKED_DEPENDENCY";break
            wave=ready[:cfg.max_parallel]
            for n in wave:progress.emit("node_started",node=n["id"],kind=n.get("kind","agent"),role=n.get("role"))
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                results=list(pool.map(lambda n:_run_node(n,spec,staging,host,cfg,min(cfg.timeout_seconds,int(n.get("timeout_seconds",cfg.timeout_seconds))),feedback=feedbacks.pop(n['id'],''),trust_cli=trust_cli,autonomy=autonomy,isolated=isolated,handoff=handoff,budget_usd=remaining_budget,progress=progress),wave))
            # Atomic wave merge: two nodes may not write the same relative path.
            edit_owners={}
            for result in results:
                for rel in result["edits"]:
                    if rel in edit_owners:
                        # Do not leave the record RUNNING forever: nothing of this wave was merged.
                        record["final_status"]="MERGE_CONFLICT";write_json(record_path,record)
                        raise EgoError("MERGE_CONFLICT",f"{rel} edited by {result['id']} and {edit_owners[rel]}")
                    edit_owners[rel]=result["id"]
            for result in results:
                identifier=result["id"]
                record["node_status"][identifier]=result["status"]
                # No bytes in persisted JSON, only references to changed paths
                record["results"][identifier]={k:v for k,v in result.items() if k!="edits"}
                extra={"verdict":result["verdict"],"findings":_brief_findings(result.get("findings"))} if result.get("verdict") else {}
                progress.emit("node_finished",node=identifier,status=result["status"],attempts=len(result["attempts"]),
                              changed=sorted(result["edits"])[:20],**extra)
                if result["status"]!="PASSED":failed.append(identifier)
                else:
                    for rel,data in result["edits"].items():
                        path=safe_path(staging,rel)
                        if data is None:
                            if path.exists():path.unlink()
                        else:
                            from .util import atomic_bytes
                            atomic_bytes(path,data)
                pending.pop(identifier)
            # A rejection with evidence sends only the reviewed coder (and what the judge waits on) back.
            for result in results:
                jid=result["id"]
                jnode=amap[jid]
                if result.get("verdict")!="REJECT" or jid not in failed or retries.get(jid,0)>=jnode.get("max_retries",1):continue
                failed.remove(jid)
                retries[jid]=retries.get(jid,0)+1
                progress.emit("judge_retry",judge=jid,reviews=jnode["reviews"],retry=retries[jid])
                feedbacks[jnode["reviews"]]=format_findings(result["findings"])
                for rid in _between(amap,jnode["reviews"],jid):
                    record["node_status"].pop(rid,None)
                    pending[rid]=amap[rid]
            write_json(record_path,record)
            # Exactly one tiny file overwrite per completed wave. No LLM or
            # per-tool hook, and no transcript or tool logs in long-term memory.
            managed_checkpoint(root,record,state="BLOCKED" if failed else "IN_PROGRESS")
            # A failure stops only its dependents: independent nodes still finish, so a
            # corrected rerun (`--from`) carries them instead of running them for the first time.
        if not pending and not failed:
            record["final_status"]="COMPARISON_READY" if any(v.get("review_required") for v in record["results"].values()) else "PASSED"
        elif failed and record["final_status"]=="RUNNING":
            record["final_status"]="FAILED"
        record["changed_files"]=_changed(record["base_snapshot"],snapshot(staging))
        record["usage"]=usage_summary(record)
        if record["final_status"]=="PASSED" and apply:
            # Actual workspace may not have drifted since start.
            allowed=set(extra_allow or [])
            for n in nodes:allowed.update(n.get("write_allowlist",[]))
            for rel in record["changed_files"]:
                check_write(root,rel,list(allowed))
                if filehash(safe_path(root,rel))!=record["base_snapshot"].get(rel):
                    raise EgoError("STALE_BASE",f"Target changed while run executed: {rel}")
            backups={}
            from .util import atomic_bytes
            try:
                for rel in record["changed_files"]:
                    dst=safe_path(root,rel)
                    backups[rel]=dst.read_bytes() if dst.exists() else None
                    src=safe_path(staging,rel)
                    if src.exists(): atomic_bytes(dst,src.read_bytes())
                    elif dst.exists():dst.unlink()
                record["applied"]=True
            except Exception:
                for rel,b in backups.items():
                    dst=safe_path(root,rel)
                    if b is None:
                        if dst.exists():dst.unlink()
                    else:atomic_bytes(dst,b)
                raise
        write_json(record_path,record)
        # An unfinished run keeps its worktree: resume and `--from` continue from that staging.
        if record.get("worktree") and should_cleanup and record["final_status"] in ("PASSED","COMPARISON_READY"):
            git_worktree_remove(root, staging, force=True)
            record["worktree_cleaned"] = True
            write_json(record_path, record)
        milestone=("COMPLETED" if record["final_status"]=="PASSED" and record["applied"]
                   else "VERIFIED" if record["final_status"] in ("PASSED","COMPARISON_READY")
                   else "BLOCKED")
        handoff_record=managed_checkpoint(root,record,state=milestone)
        result={k:v for k,v in record.items() if k!="spec"}
        result["message"]=completion_message(record)
        result["checkpoint_path"]=handoff_record["path"]
        return result


def execute(root:Path, spec:dict, *, run_id:str|None=None, progress_stderr:bool=False, **kwargs)->dict:
    """Run a task and journal its progress to `.ego/state/runs/<run_id>/progress.jsonl`."""
    run_id=run_id or ident("run")
    if not all(c.isalnum() or c in "-_" for c in run_id):raise EgoError("INVALID_ID",run_id,2)
    runroot=root.resolve()/".ego"/"state"/"runs"/run_id
    progress=Progress(runroot/"progress.jsonl",echo=progress_stderr)
    try:
        result=_execute(root,spec,progress,run_id=run_id,**kwargs)
    except BaseException as exc:
        # Only a run that exists can be followed; a refused start (RUN_EXISTS, bad task) leaves no log.
        if (runroot/"record.json").exists() and not (isinstance(exc,EgoError) and exc.code=="RUN_EXISTS"):
            code=exc.code if isinstance(exc,EgoError) else type(exc).__name__
            progress.emit("run_error",run_id=run_id,code=code,message=clip_output(str(exc),400))
        raise
    progress.emit("run_finished",run_id=run_id,final_status=result["final_status"],applied=result.get("applied",False),
                  changed_files=len(result.get("changed_files",[])),message=result.get("message"))
    return result


def status(root:Path,run_id:str)->dict:
    if not run_id.replace("-","").replace("_","").isalnum():raise EgoError("INVALID_ID",run_id,2)
    data=read_json(root/".ego"/"state"/"runs"/run_id/"record.json")
    if not data:raise EgoError("RUN_NOT_FOUND",run_id,3)
    return {**{k:v for k,v in data.items() if k!="spec"},
            "message":completion_message(data), "checkpoint_path":handoff_relpath(run_id),
            "usage":usage_summary(data),"progress":_progress_tail(root,run_id)}


def _progress_tail(root:Path,run_id:str,n:int=10)->list[dict]:
    path=root/".ego"/"state"/"runs"/run_id/"progress.jsonl"
    if not path.exists():return []
    out=[]
    for line in path.read_text(encoding="utf8").splitlines()[-n:]:
        try:out.append(json.loads(line))
        except ValueError:continue
    return out


def watch(root:Path,run_id:str,*,after:int=0,poll:float=1.0,write=lambda s:print(s,flush=True),start_wait:float=60.0)->int:
    """Print the run's events after `after` as they appear; return the last seq once the run ended."""
    if not run_id.replace("-","").replace("_","").isalnum():raise EgoError("INVALID_ID",run_id,2)
    runroot=root/".ego"/"state"/"runs"/run_id
    path=runroot/"progress.jsonl"
    seen,idle_ended,begun=after,0,time.monotonic()
    while True:
        lines=path.read_text(encoding="utf8").splitlines() if path.exists() else []
        for line in lines:
            try:event=json.loads(line)
            except ValueError:continue
            if event.get("seq",0)<=seen:continue
            write(line);seen=event["seq"]
            if event.get("event") in ("run_finished","run_error"):return seen
        record=read_json(runroot/"record.json") if (runroot/"record.json").exists() else None
        if record is None and not path.exists():
            if time.monotonic()-begun>start_wait:raise EgoError("RUN_NOT_FOUND",run_id,3)
        elif record is not None and record.get("final_status")!="RUNNING":
            idle_ended+=1
            if idle_ended>=2:
                write(json.dumps({"event":"run_stopped","final_status":record.get("final_status")},ensure_ascii=False))
                return seen
        else:idle_ended=0
        time.sleep(poll)


def cancel(root:Path,run_id:str)->dict:
    if not run_id.replace("-","").replace("_","").isalnum():raise EgoError("INVALID_ID",run_id,2)
    runroot=root/".ego"/"state"/"runs"/run_id
    if not (runroot/"record.json").exists():raise EgoError("RUN_NOT_FOUND",run_id,3)
    (runroot/"cancel.flag").touch()
    return {"status":"CANCELLATION_REQUESTED","run_id":run_id,
            "warning":"Stops at next workflow boundary; an active tool subprocess may run until its timeout"}
