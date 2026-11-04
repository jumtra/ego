"""Bounded DAG execution: file-level isolation, verified repairs, checkpoint/resume and opt-in apply."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import copy
import json
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
from .config import load_config
from .policy import check_write
from .runner import run_agent, run_checked_command
from .util import SEHError, ident, now, read_json, write_json, snapshot, simple_copy_tree, filehash, safe_path, exclusive_lock
from .task_routing import validate_task_context, resolve_routes, build_task_prompt

KINDS={"worker","agent","code","gate","splitter"}

def validate_task(spec: dict) -> list[dict]:
    if not isinstance(spec,dict) or not isinstance(spec.get("nodes"),list) or not spec["nodes"]:
        raise SEHError("INVALID_TASK", "Task must contain non-empty nodes array", 2)
    nodes=spec["nodes"]
    ids=[n.get("id") for n in nodes if isinstance(n,dict)]
    if len(ids)!=len(nodes) or any(not isinstance(x,str) or not x.replace("-","").replace("_","").isalnum() for x in ids) or len(set(ids))!=len(ids):
        raise SEHError("INVALID_TASK", "Invalid/duplicate node ids", 2)
    amap={n["id"]:n for n in nodes}
    for n in nodes:
        if n.get("kind","agent") not in KINDS: raise SEHError("INVALID_TASK", "Unknown kind", 2)
        if not isinstance(n.get("depends_on",[]),list) or any(d not in amap for d in n.get("depends_on",[])):
            raise SEHError("INVALID_TASK", "Missing dependency", 2)
        if n.get("kind","agent") in ("code","gate") and not (n.get("argv") or n.get("checks")):
            raise SEHError("INVALID_TASK", "Code/gate nodes need argv or checks", 2)
        if n.get("kind","agent") not in ("code","gate") and not isinstance(n.get("prompt"),str):
            raise SEHError("INVALID_TASK", "Agent nodes need prompt", 2)
        if not isinstance(n.get("write_allowlist",[]),list):raise SEHError("INVALID_TASK", "Invalid allowlist", 2)
        if n.get("kind","agent") not in ("gate",) and not n.get("write_allowlist"):
            # Explicitly require scope declarations to avoid broad accidental merges.
            raise SEHError("INVALID_TASK", f"write_allowlist required for {n['id']}",2)
    temp=set();done=set()
    def visit(nid):
        if nid in temp:raise SEHError("TASK_CYCLE",nid,2)
        if nid in done:return
        temp.add(nid)
        for d in amap[nid].get("depends_on",[]):visit(d)
        temp.remove(nid);done.add(nid)
    for nid in ids:visit(nid)
    validate_task_context(spec, nodes)
    return nodes


def _changed(before:dict[str,str],after:dict[str,str])->list[str]:
    return sorted({p for p in before.keys()|after.keys() if before.get(p)!=after.get(p)})


def _run_node(node:dict, spec:dict, staging:Path, default_host:str, cfg, timeout:int, *, feedback:str="", trust_cli:bool=False,
              autonomy: str="auto", isolated:bool=False)->dict:
    """A task node sees a private copy; fallback never reuses failed edits.

    Compare runs have their own isolated copies and never merge any candidate.
    """
    start=time.monotonic()
    kind=node.get("kind","agent")
    if kind in ("code","gate"):
        strategy,routes,fallback_on="single",[],frozenset()
    else:
        strategy,routes,fallback_on=resolve_routes(node,spec,default_host)
    attempts=[]
    max_attempts=min(int(node.get("max_attempts",cfg.max_attempts)),cfg.max_attempts)
    outputs=[]
    def attempt_one(chosen_host:str, chosen_model:str|None, iteration:int) -> dict:
        nonlocal feedback
        with tempfile.TemporaryDirectory(prefix="seh-node-") as tmp:
            work=Path(tmp)/"src"
            simple_copy_tree(staging,work)
            before=snapshot(work)
            if kind in ("code","gate"):
                r=run_checked_command(node.get("argv") or node["checks"][0],work,timeout=timeout,allow=cfg.allow_commands)
            else:
                if chosen_host!="mock" and not trust_cli:
                    raise SEHError("UNTRUSTED_AGENT_EXEC", "Real CLI agents require --trust-cli",3)
                prompt=build_task_prompt(spec,node,work,feedback=feedback)
                try:
                    r=run_agent(chosen_host,prompt,work,timeout,model=chosen_model,read_only=kind=="splitter",
                                autonomy=autonomy,isolated=isolated)
                except SEHError as exc:
                    if exc.code not in ("CLI_INCOMPATIBLE","HOST_UNAVAILABLE"):
                        raise
                    from .runner import RunResult
                    r=RunResult("UNAVAILABLE","",str(exc),127,0,chosen_host,reason=exc.code,model=chosen_model)
            checks=[]
            if r.status=="PASS":
                for argv in node.get("checks",[]):
                    cr=run_checked_command(argv,work,timeout=min(timeout,120),allow=cfg.allow_commands)
                    checks.append(cr.as_dict())
                    if cr.status!="PASS":feedback=cr.output+" "+cr.stderr;break
            ok=(r.status=="PASS" and all(c["status"]=="PASS" for c in checks))
            changed=_changed(before,snapshot(work))
            try:
                for rel in changed:check_write(work,rel,node.get("write_allowlist",[]))
                if kind=="gate" and changed:raise SEHError("GATE_WRITE","A gate may not modify source")
            except SEHError as exc:
                ok=False;feedback=str(exc)
            record={"attempt":iteration,"host":chosen_host,"model":chosen_model or "native-default",
                    "status":"PASS" if ok else "FAIL", "execution":r.as_dict(),"checks":checks,
                    "changed":changed,"feedback":feedback}
            edits={}
            if ok:
                for rel in changed:
                    path=safe_path(work,rel)
                    edits[rel]=path.read_bytes() if path.exists() else None
            return {"ok":ok,"record":record,"edits":edits,"output":r.output,"execution_status":r.status}

    if strategy=="compare":
        results=[]
        for route in routes:
            res=attempt_one(route.host,route.model,1)
            attempts.append(res["record"])
            outputs.append({"host":route.host,"model":route.model or "native-default",
                            "status":res["record"]["status"],"changed":res["record"]["changed"]})
        return {"id":node["id"],"status":"PASSED","attempts":attempts,"edits":{},
                "elapsed":round(time.monotonic()-start,4),"comparison":outputs,
                "review_required":True,"output":"Compare finished; no candidate automatically selected"}

    route_pos=0
    success=False;edits={};last_output=""
    for iteration in range(1,max_attempts+len(routes)+1):
        chosen=routes[route_pos] if routes else None
        r=attempt_one(chosen.host if chosen else "local",chosen.model if chosen else None,iteration)
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
    return {"id":node["id"],"status":"PASSED" if success else "FAILED", "attempts":attempts,
            "edits":edits,"elapsed":round(time.monotonic()-start,4),"output":last_output}


def execute(root:Path, spec:dict, *, host:str="mock",apply:bool=False,approve:bool=False,
            trust_cli:bool=False,run_id:str|None=None,resume:bool=False,autonomy:str="auto",isolated:bool=False)->dict:
    root=root.resolve();cfg=load_config(root)
    nodes=validate_task(spec)
    if apply and not approve:raise SEHError("APPROVAL_REQUIRED", "--apply requires --approve")
    if any(r.host=="agy" for n in nodes if n.get("kind","agent") not in ("gate","code") for r in resolve_routes(n,spec,host)[1]):
        raise SEHError("HOST_POLICY_BLOCKED", "AGY managed execution is disabled",3)
    if autonomy=="bypass" and not isolated:
        raise SEHError("ISOLATION_REQUIRED","Bypass requires explicit external sandbox attestation",3)
    if any(resolve_routes(n,spec,host)[0]=="compare" for n in nodes if n.get("kind","agent") not in ("gate","code")) and apply:
        raise SEHError("COMPARE_NO_APPLY","Compare never applies unreviewed changes",3)
    run_id=run_id or ident("run")
    if not all(c.isalnum() or c in "-_" for c in run_id):raise SEHError("INVALID_ID",run_id,2)
    runroot=root/".harness"/"state"/"runs"/run_id
    staging=runroot/"staging"
    record_path=runroot/"record.json"
    with exclusive_lock(root/".harness"/"locks"/(run_id+".lock")):
        if resume:
            record=read_json(record_path)
            if not record:raise SEHError("RUN_NOT_FOUND",run_id,3)
            if record["spec"]!=spec:raise SEHError("RESUME_MISMATCH","Task specification changed")
            if record.get("final_status")=="PASSED": return {k:v for k,v in record.items() if k!="spec"}
        else:
            if record_path.exists():raise SEHError("RUN_EXISTS",run_id)
            staging.mkdir(parents=True)
            simple_copy_tree(root,staging)
            record={"run_id":run_id,"created_at":now(),"spec":spec,"host":host,"node_status":{},"results":{},"base_snapshot":snapshot(root),"final_status":"RUNNING","applied":False}
            write_json(record_path,record)
        start=time.monotonic()
        pending={n["id"]:n for n in nodes if record["node_status"].get(n["id"])!="PASSED"}
        failed=[]
        while pending:
            if (runroot/"cancel.flag").exists():
                record["final_status"]="CANCELLED";break
            if time.monotonic()-start>cfg.timeout_seconds*len(nodes):
                record["final_status"]="TIMEOUT";break
            ready=[n for n in pending.values() if all(record["node_status"].get(dep)=="PASSED" for dep in n.get("depends_on",[]))]
            if not ready:
                record["final_status"]="BLOCKED_DEPENDENCY";break
            wave=ready[:cfg.max_parallel]
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                results=list(pool.map(lambda n:_run_node(n,spec,staging,host,cfg,min(cfg.timeout_seconds,int(n.get("timeout_seconds",cfg.timeout_seconds))),trust_cli=trust_cli,autonomy=autonomy,isolated=isolated),wave))
            # Atomic wave merge: two nodes may not write the same relative path.
            edit_owners={}
            for result in results:
                for rel in result["edits"]:
                    if rel in edit_owners:raise SEHError("MERGE_CONFLICT",f"{rel} edited by {result['id']} and {edit_owners[rel]}")
                    edit_owners[rel]=result["id"]
            for result in results:
                identifier=result["id"]
                record["node_status"][identifier]=result["status"]
                # No bytes in persisted JSON, only references to changed paths
                record["results"][identifier]={k:v for k,v in result.items() if k!="edits"}
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
            write_json(record_path,record)
            if failed:
                record["final_status"]="FAILED";break
        if not pending and not failed:
            record["final_status"]="COMPARISON_READY" if any(v.get("review_required") for v in record["results"].values()) else "PASSED"
        record["changed_files"]=_changed(record["base_snapshot"],snapshot(staging))
        if record["final_status"]=="PASSED" and apply:
            # Actual workspace may not have drifted since start.
            allowed=set()
            for n in nodes:allowed.update(n.get("write_allowlist",[]))
            for rel in record["changed_files"]:
                check_write(root,rel,list(allowed))
                if filehash(safe_path(root,rel))!=record["base_snapshot"].get(rel):
                    raise SEHError("STALE_BASE",f"Target changed while run executed: {rel}")
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
        return {k:v for k,v in record.items() if k!="spec"}


def status(root:Path,run_id:str)->dict:
    if not run_id.replace("-","").replace("_","").isalnum():raise SEHError("INVALID_ID",run_id,2)
    data=read_json(root/".harness"/"state"/"runs"/run_id/"record.json")
    if not data:raise SEHError("RUN_NOT_FOUND",run_id,3)
    return {k:v for k,v in data.items() if k!="spec"}


def cancel(root:Path,run_id:str)->dict:
    if not run_id.replace("-","").replace("_","").isalnum():raise SEHError("INVALID_ID",run_id,2)
    runroot=root/".harness"/"state"/"runs"/run_id
    if not (runroot/"record.json").exists():raise SEHError("RUN_NOT_FOUND",run_id,3)
    (runroot/"cancel.flag").touch()
    return {"status":"CANCELLATION_REQUESTED","run_id":run_id,
            "warning":"Stops at next workflow boundary; an active tool subprocess may run until its timeout"}
