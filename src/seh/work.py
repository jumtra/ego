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
        if ('argv' in n and n['argv'] is not None) or ('checks' in n and n['checks'] is not None):
            from .toolchains import CHECK_KINDS
            if 'checks' in n and not isinstance(n['checks'], list):
                raise SEHError('INVALID_TASK','checks must be a list',2)
            candidates = ([n['argv']] if n.get('argv') is not None else []) + n.get('checks', [])
            for candidate in candidates:
                if isinstance(candidate,list) and candidate and all(isinstance(x,str) and x for x in candidate):continue
                if isinstance(candidate,dict) and set(candidate)<={'toolchain','project','ecosystem'} and candidate.get('toolchain') in CHECK_KINDS and all(isinstance(candidate.get(k),str) for k in ('project','ecosystem') if k in candidate):continue
                raise SEHError('INVALID_TASK','Invalid command or toolchain check',2)
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
    return nodes


def _check_spec(value: object, work: Path) -> tuple[list[str], Path]:
    """Accept raw argv, or a toolchain selection with a safe subproject cwd."""
    if isinstance(value, list) and value and all(isinstance(s, str) and s for s in value):
        return value, work
    if isinstance(value, dict) and isinstance(value.get('toolchain'), str):
        from .toolchains import resolve_check
        if set(value) - {'toolchain', 'project', 'ecosystem'}:
            raise SEHError('INVALID_TASK', 'Unknown toolchain check fields', 2)
        resolved = resolve_check(work, value['toolchain'], project=value.get('project'),
                                 ecosystem=value.get('ecosystem'))
        return resolved['argv'], Path(resolved['cwd'])
    raise SEHError('INVALID_TASK', 'Expected argv array or toolchain check object', 2)


def _changed(before:dict[str,str],after:dict[str,str])->list[str]:
    return sorted({p for p in before.keys()|after.keys() if before.get(p)!=after.get(p)})


def _run_node(node:dict, staging:Path, host:str, cfg, timeout:int, *, feedback:str="", trust_cli:bool=False)->dict:
    """Each node sees a private copy. No edits to the stable workspace in this function."""
    start=time.monotonic()
    with tempfile.TemporaryDirectory(prefix="seh-unit-") as temp:
        work=Path(temp)/"src"
        simple_copy_tree(staging,work)
        before=snapshot(work)
        kind=node.get("kind","agent")
        attempts=[];success=False
        max_attempts = min(int(node.get("max_attempts",cfg.max_attempts)),cfg.max_attempts)
        last_output=""
        for attempt in range(1,max_attempts+1):
            if kind in ("code","gate"):
                argv,cwd=_check_spec(node.get("argv") or node["checks"][0],work)
                r=run_checked_command(argv,cwd,timeout=timeout,allow=cfg.allow_commands)
            else:
                if host != "mock" and not trust_cli: raise SEHError("UNTRUSTED_AGENT_EXEC", "Real CLI agent may execute arbitrary code; require --trust-cli")
                prompt=node["prompt"] + ("\nVerifier feedback: " + feedback if feedback else "")
                r=run_agent(host,prompt,work,timeout,read_only=kind=="splitter")
            last_output=r.output
            checks=[]
            if r.status=="PASS":
                # A code/gate node using its first check as its primary command
                # must not waste time running the same test twice.
                remaining_checks = node.get('checks', [])
                if kind in ('code','gate') and not node.get('argv'):
                    remaining_checks = remaining_checks[1:]
                for argv in remaining_checks:
                    check_argv,check_cwd=_check_spec(argv,work)
                    cr=run_checked_command(check_argv,check_cwd,timeout=min(timeout,120),allow=cfg.allow_commands)
                    checks.append(cr.as_dict())
                    if cr.status!="PASS":feedback=cr.output+" "+cr.stderr;break
            ok=(r.status=="PASS" and all(c["status"]=="PASS" for c in checks))
            after=snapshot(work)
            changed=_changed(before,after)
            try:
                for rel in changed:check_write(work,rel,node.get("write_allowlist",[]))
                if kind=="gate" and changed:raise SEHError("GATE_WRITE", "A gate may not modify source")
            except SEHError as exc:
                ok=False;feedback=str(exc)
            attempts.append({"attempt":attempt,"status":"PASS" if ok else "FAIL","execution":r.as_dict(),"checks":checks,"changed":changed,"feedback":feedback})
            if ok:
                success=True;break
            if not feedback:feedback=r.stderr or r.output or "Execution failed"
        # Only copy allowed post-change files to staging on PASS. Output record includes bytes for merging.
        edits={}
        if success:
            for rel in _changed(before,snapshot(work)):
                path=safe_path(work,rel)
                edits[rel] = path.read_bytes() if path.exists() else None
        return {"id":node["id"],"status":"PASSED" if success else "FAILED","attempts":attempts,"edits":edits,"elapsed":round(time.monotonic()-start,4),"output":last_output}


def execute(root:Path, spec:dict, *, host:str="mock",apply:bool=False,approve:bool=False,
            trust_cli:bool=False,run_id:str|None=None,resume:bool=False)->dict:
    root=root.resolve();cfg=load_config(root)
    nodes=validate_task(spec)
    if apply and not approve:raise SEHError("APPROVAL_REQUIRED", "--apply requires --approve")
    if host=="agy":raise SEHError("HOST_POLICY_BLOCKED", "AGY is configuration only",3)
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
                results=list(pool.map(lambda n:_run_node(n,staging,host,cfg,min(cfg.timeout_seconds,int(n.get("timeout_seconds",cfg.timeout_seconds))),trust_cli=trust_cli),wave))
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
        if not pending and not failed:record["final_status"]="PASSED"
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
