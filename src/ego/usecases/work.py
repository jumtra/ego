"""Bounded DAG execution: file-level isolation, verified repairs, checkpoint/resume and opt-in apply."""
from __future__ import annotations
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import uuid
from ..domain.config import Config, load_config, resolve_max_run_tokens
from ..domain.defaults import package_defaults
from ..domain.policy import check_write, check_role_write
from ..adapters.runner import run_agent, run_checked_command
from .worker import run_worker
from .overlay import overlaid, instruction_target
from ..shared.util import (EgoError, ident, now, read_json, write_json, snapshot, simple_copy_tree,
                   filehash, safe_path, exclusive_lock, git_is_repo, git_head, git_worktree_add, git_worktree_remove, git_branch_delete,
                   copy_worktree_extras, git_commit_paths, git_merge_branch, sha256, json_text, is_locked, atomic_bytes)
from ..domain.task_routing import validate_task_context, resolve_routes, build_task_prompt
from ..domain.task_schema import PlanSchemaLimits, validate_task_schema
from .session import managed_checkpoint, load_checkpoint, completion_message, handoff_relpath
from ..shared.util import clip_output
from ..domain.review import parse_verdict, format_findings
from ..domain.usage import CACHE_TIER_KEYS, CATEGORIES, TOKEN_KEYS, billable_tokens, tokens_of, summary_line, usage_row
from ..domain.pricing import cost_usd as price_cost
from ..domain.settings import load_settings
from ..domain.envfacts import extract as extract_envfacts
from ..domain.fact_lifecycle import Fact, fact_id, observe as observe_envfact
from ..adapters.envfact_store import active_facts, load as load_envfacts, notify_alter, save as save_envfacts
from ..adapters.transcript import claude_transcript_path, read_events
from . import ledger

# Serializes copies out of and writes into a run's staging tree across parallel node threads.
_STAGING_LOCK=threading.Lock()

class TokenBudget:
    """Per-invocation billable token cap (0 = unlimited), shared by parallel node threads."""
    def __init__(self,limit:int,spent:int=0):
        self.limit,self.spent=limit,spent
        self.lock=threading.Lock()

    def charge(self,usage)->None:
        count=billable_tokens(usage)
        with self.lock:self.spent+=count

    def remaining(self)->int|None:
        if self.limit<=0:return None
        with self.lock:return self.limit-self.spent

    def exhausted(self,extra:int=0)->bool:
        if self.limit<=0:return False
        with self.lock:return self.limit-self.spent-extra<=0

class Progress:
    """Append-only per-run event log (`progress.jsonl`), optionally echoed to stderr.

    A calling agent follows a run through these one-line JSON events instead of
    waiting for the final summary; the full record stays on disk.
    """
    def __init__(self,path:Path|None,echo:bool=False,on_event=None):
        self.path,self.echo,self.lock=path,echo,threading.Lock()
        self.on_event=on_event
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
        if self.on_event:
            try: self.on_event(event,fields)
            except Exception: pass


def _brief_findings(findings,limit:int)->list[str]:
    return [clip_output(json.dumps(f,ensure_ascii=False) if not isinstance(f,str) else f,limit) for f in (findings or [])[:5]]


def validate_task(spec: dict, *, limits: PlanSchemaLimits | None = None, caps: Config | None = None) -> list[dict]:
    validate_task_schema(spec, limits=limits)
    nodes=spec["nodes"]
    amap={n["id"]:n for n in nodes}
    for n in nodes:
        if any(d not in amap for d in (n.get("depends_on") or [])):
            raise EgoError("INVALID_TASK", "Missing dependency", 2)
    temp=set();done=set()
    def visit(nid):
        if nid in temp:raise EgoError("TASK_CYCLE",nid,2)
        if nid in done:return
        temp.add(nid)
        for d in (amap[nid].get("depends_on") or []):visit(d)
        temp.remove(nid);done.add(nid)
    for nid in amap:visit(nid)
    validate_task_context(spec, nodes, validated=True, limits=limits)
    if caps is not None:
        for n in nodes:
            for key in ("max_attempts","max_rounds","max_turns"):
                if n.get(key) is not None and n[key]>getattr(caps,key):
                    raise EgoError("INVALID_TASK", f"{key} of {n['id']} exceeds config {key}={getattr(caps,key)}", 2)
        if spec.get("max_replans") is not None and spec["max_replans"]>caps.max_replans:
            raise EgoError("INVALID_TASK", f"max_replans exceeds config max_replans={caps.max_replans}", 2)
    for n in nodes:
        if n.get("role")!="reviewer":
            if n.get("on_exhausted") is not None:raise EgoError("INVALID_TASK", f"on_exhausted of {n['id']} is only allowed on reviewer nodes", 2)
            continue
        target=n.get("reviews")
        if target not in _ancestors(amap,n["id"]) or amap[target].get("role")!="coder" or amap[target].get("kind","agent") in ("code","gate"):
            raise EgoError("INVALID_TASK", f"reviewer {n['id']} needs reviews: a coder node it depends on", 2)
        if not any(amap[d].get("kind","agent") in ("gate","code") for d in (n.get("depends_on") or [])):
            raise EgoError("INVALID_TASK", f"reviewer {n['id']} must depend on a gate or code node", 2)
        if n.get("on_exhausted")=="replan" and not spec.get("max_replans"):
            raise EgoError("INVALID_TASK", f"on_exhausted replan of {n['id']} needs max_replans >= 1", 2)
    reviewers=[n for n in nodes if n.get("role")=="reviewer"]
    for i,a in enumerate(reviewers):
        for b in reviewers[i+1:]:
            if _between(amap,a["reviews"],a["id"]) & _between(amap,b["reviews"],b["id"]):
                raise EgoError("INVALID_TASK", f"loops of {a['id']} and {b['id']} overlap", 2)
    return nodes


def _ancestors(amap:dict, nid:str)->set:
    out=set();stack=list(amap[nid].get("depends_on") or [])
    while stack:
        d=stack.pop()
        if d not in out:out.add(d);stack.extend(amap[d].get("depends_on") or [])
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


def _between(amap:dict, target:str, reviewer:str)->set:
    """Nodes to rerun after a rejection: the reviewed coder, everything downstream of it that the reviewer waits on, and the reviewer."""
    above=_ancestors(amap,reviewer)
    return {target,reviewer}|{x for x in above if target in _ancestors(amap,x)}


def _loop_of(amap:dict, nid:str)->dict|None:
    """The reviewer whose loop contains nid: the reviewer itself or a node between its coder and it."""
    for r in amap.values():
        if r.get("role")=="reviewer" and (nid==r["id"] or nid in _between(amap,r["reviews"],r["id"])):return r
    return None


def _failure_text(data:dict)->str:
    last=(data.get("attempts") or [{}])[-1]
    return str(last.get("feedback") or last.get("execution",{}).get("stderr") or data.get("output") or "")


def _changed(before:dict[str,str],after:dict[str,str])->list[str]:
    return sorted({p for p in before.keys()|after.keys() if before.get(p)!=after.get(p)})


def _effective_status(root:Path, record:dict)->str:
    """A RUNNING record whose run lock is free belongs to a process that died: it was interrupted."""
    if record.get("final_status")=="RUNNING" and not is_locked(root.resolve()/".ego"/"locks"/f"{record['run_id']}.lock"):
        return "INTERRUPTED"
    return str(record.get("final_status") or "UNKNOWN")


def _is_writer(node:dict)->bool:
    """Nodes that may change files. gate, splitter and reviewer never write, so they never get a private worktree."""
    return node.get("kind","agent") not in ("gate","splitter") and node.get("role")!="reviewer"


def _has_parallel_writers(nodes:list[dict])->bool:
    """True when two writer nodes can run at the same time: neither depends on the other, directly or transitively.

    Only then does the 3-way merge of worktree isolation matter. Serial writers are safe in copy mode.
    """
    amap={n["id"]:n for n in nodes}
    ancestors:dict[str,set[str]]={}
    def ancestors_of(nid:str)->set[str]:
        if nid not in ancestors:
            acc:set[str]=set()
            for dep in amap[nid].get("depends_on") or []:
                acc|={dep}|ancestors_of(dep)
            ancestors[nid]=acc
        return ancestors[nid]
    writers=[n["id"] for n in nodes if _is_writer(n)]
    return any(a not in ancestors_of(b) and b not in ancestors_of(a)
               for i,a in enumerate(writers) for b in writers[i+1:])


def _worktree_mode(cfg, nodes:list[dict], worktree:bool|None)->bool:
    """An explicit worktree flag wins. `auto` isolates a run only when two writer nodes can run in parallel."""
    if worktree is not None:return worktree
    if cfg.isolation=="auto":return _has_parallel_writers(nodes)
    return cfg.isolation=="worktree"


def _apply_defaults_override(spec:dict, cli_defaults:dict|None)->dict:
    """Merge CLI runtime defaults (`--hosts`/`--strategy`/`--model`/`--models`) without mutating persisted spec."""
    if not cli_defaults:
        return spec
    merged_defaults={**(spec.get("defaults") or {}),**cli_defaults}
    return {**spec,"defaults":merged_defaults}


def _strip_flat_role_models(roles_map:dict|None)->dict:
    if not isinstance(roles_map,dict):
        return {}
    return {
        rk:({k:v for k,v in rv.items() if k!="model"} if isinstance(rv,dict) else rv)
        for rk,rv in roles_map.items()
    }


def _resolve_node_routes(node:dict, spec:dict, default_host:str, cfg, route_override:dict|None=None):
    """Resolve routes for an agent node, applying explicit runtime overrides (e.g. on resume) over node/role defaults."""
    if not route_override:
        return resolve_routes(node,spec,default_host,default_models=cfg.models,default_roles=cfg.roles)
    target_nodes=route_override.get("nodes")
    if target_nodes is not None and node.get("id") not in target_nodes:
        return resolve_routes(node,spec,default_host,default_models=cfg.models,default_roles=cfg.roles)
    eff_node=dict(node)
    eff_spec=spec
    eff_default_roles=cfg.roles
    host_changed=bool(route_override.get("hosts") or route_override.get("host"))
    if route_override.get("hosts"):
        eff_node.pop("host",None)
        eff_node["hosts"]=list(route_override["hosts"])
        if not route_override.get("strategy"):
            eff_node["strategy"]="fallback" if len(eff_node["hosts"])>1 else "single"
    elif route_override.get("host"):
        eff_node.pop("hosts",None)
        eff_node["host"]=route_override["host"]
        if not route_override.get("strategy"):
            eff_node["strategy"]="single"
    if route_override.get("strategy"):
        eff_node["strategy"]=route_override["strategy"]
    if route_override.get("models"):
        eff_node.pop("model",None)
        eff_node["models"]=dict(route_override["models"])
    elif route_override.get("model"):
        eff_node.pop("models",None)
        eff_node["model"]=route_override["model"]
    elif host_changed:
        # Switching CLI without pinning a model drops the previous host's flat model pins so the new host uses its own model.
        eff_node.pop("model",None)
        eff_node.pop("models",None)
        if isinstance(spec.get("defaults"),dict):
            eff_defaults={k:v for k,v in spec["defaults"].items() if k!="model"}
            if isinstance(eff_defaults.get("roles"),dict):
                eff_defaults["roles"]=_strip_flat_role_models(eff_defaults["roles"])
            eff_spec={**spec,"defaults":eff_defaults}
        eff_default_roles=_strip_flat_role_models(cfg.roles)
    return resolve_routes(eff_node,eff_spec,route_override.get("host") or default_host,
                          default_models=cfg.models,default_roles=eff_default_roles)


def _partial_dir(runroot:Path, node_id:str)->Path:
    return safe_path(runroot,f"partial/{node_id}")


def _clear_partial(runroot:Path, node_id:str)->None:
    pdir=_partial_dir(runroot,node_id)
    if pdir.exists():
        shutil.rmtree(pdir,ignore_errors=True)


def _is_handoff_artifact(rel:str)->bool:
    return rel==".ego/memory/handoffs" or rel.startswith(".ego/memory/handoffs/")


def _snapshot_allowed_partial(work:Path, before:dict[str,str], node:dict, *, exclude_paths:set[str]|None=None)->tuple[bool,dict[str,bytes|None],dict[str,str|None]]:
    """Capture policy-allowed edits vs `before` with base hashes, excluding runtime overlay/handoff paths."""
    excluded=set(exclude_paths or ())
    def _filter(snap:dict[str,str])->dict[str,str]:
        return {k:v for k,v in snap.items() if k not in excluded and not _is_handoff_artifact(k)}
    changed=_changed(_filter(before),_filter(snapshot(work)))
    if not changed:
        return True,{},{}
    kind=node.get("kind","agent")
    try:
        for rel in changed:
            check_role_write(node.get("role"),rel)
            check_write(work,rel,node.get("write_allowlist",[]))
        if kind=="gate":
            raise EgoError("GATE_WRITE","A gate may not modify source")
    except EgoError:
        return False,{},{}
    edits:dict[str,bytes|None]={}
    base_hashes:dict[str,str|None]={}
    for rel in changed:
        path=safe_path(work,rel)
        edits[rel]=path.read_bytes() if path.exists() else None
        base_hashes[rel]=before.get(rel)
    return True,edits,base_hashes


def _save_partial(runroot:Path, node_id:str, edits:dict[str,bytes|None], meta:dict)->None:
    """Persist allowed in-progress edits of a failed/interrupted node outside staging so resume can continue from them."""
    _clear_partial(runroot,node_id)
    if not edits and not meta.get("output"):
        return
    pdir=_partial_dir(runroot,node_id)
    files_dir=pdir/"files"
    modified:list[str]=[]
    deleted:list[str]=[]
    for rel,data in sorted(edits.items()):
        if data is None:
            deleted.append(rel)
        else:
            atomic_bytes(safe_path(files_dir,rel),data)
            modified.append(rel)
    write_json(pdir/"manifest.json",{
        "node_id":node_id,
        "saved_at":now(),
        "modified":modified,
        "deleted":deleted,
        **meta,
    })


def _load_partial(runroot:Path, node_id:str, *, staging:Path|None=None)->tuple[dict[str,bytes|None], dict|None, list[str]]:
    pdir=_partial_dir(runroot,node_id)
    manifest=read_json(pdir/"manifest.json")
    if not isinstance(manifest,dict):
        return {},None,[]
    files_dir=pdir/"files"
    base_hashes=manifest.get("base_hashes") if isinstance(manifest.get("base_hashes"),dict) else {}
    edits:dict[str,bytes|None]={}
    conflicts:list[str]=[]

    def _matches_base(rel:str)->bool:
        if staging is None or rel not in base_hashes:
            return True
        cur_path=safe_path(staging,rel)
        cur_hash=filehash(cur_path) if cur_path.is_file() else None
        return cur_hash==base_hashes[rel]

    for rel in manifest.get("modified") or []:
        if not _matches_base(rel):
            conflicts.append(rel)
            continue
        path=safe_path(files_dir,rel)
        if path.is_file():
            edits[rel]=path.read_bytes()
    for rel in manifest.get("deleted") or []:
        if not _matches_base(rel):
            conflicts.append(rel)
            continue
        edits[rel]=None
    return edits,manifest,conflicts


def _handoff_feedback(prev_host:str, prev_model:str|None, exec_status:str, changed:list[str], prev_output:str, limit:int, *, conflicts:list[str]|None=None)->str:
    model_label=prev_model or "native-default"
    if changed:
        head=(f"Previous attempt ({prev_host}/{model_label}) stopped with {exec_status}. "
              f"Its allowed edits are already present in the workspace ({', '.join(changed[:20])}). "
              "Inspect the current workspace and finish the remaining work without redoing completed edits.")
    else:
        head=(f"Previous attempt ({prev_host}/{model_label}) stopped with {exec_status} before modifying any files. "
              "Continue from the current workspace state.")
    parts=[head]
    if conflicts:
        parts.append("Skipped stale partial edits because staging changed since the previous attempt: "
                     + ", ".join(conflicts[:20])
                     + ". Re-apply any needed changes on top of the latest files.")
    summary=prev_output.strip()
    if summary:
        parts.append(f"Last progress output:\n{summary}")
    return clip_output("\n\n".join(parts),limit)


def _run_node(node:dict, spec:dict, staging:Path, default_host:str, cfg, timeout:int, *, feedback:str="", trust_cli:bool=False,
              autonomy: str="auto", isolated:bool=False, handoff:dict|None=None,
              token_budget:TokenBudget|None=None, progress:Progress|None=None, project_root:Path,
              alter_rules:list[str]|None=None, use_worktree: bool = False, run_id: str = "run",
              initial_carry:dict[str,bytes|None]|None=None, route_override:dict|None=None,
              runroot:Path|None=None)->dict:
    """A task node sees a private copy; retries, fallbacks and resumes continue from the previous attempt's allowed edits.

    Compare runs have their own isolated copies and never merge any candidate.
    """
    progress=progress or Progress(None)
    start=time.monotonic()
    kind=node.get("kind","agent")
    if kind in ("code","gate"):
        strategy,routes,fallback_on="single",[],frozenset()
    else:
        strategy,routes,fallback_on=_resolve_node_routes(node,spec,default_host,cfg,route_override=route_override)
    attempts=[]
    max_attempts=int(node.get("max_attempts",cfg.max_attempts))
    verdict_info={}
    outputs=[]
    def attempt_one(chosen_host:str, chosen_model:str|None, chosen_effort:str|None, iteration:int, carry:dict|None=None) -> dict:
        nonlocal feedback
        with tempfile.TemporaryDirectory(prefix="ego-node-") as tmp:
            work=Path(tmp)/"src"
            node_wt=False
            node_branch=""
            keep_branch=False
            try:
                with _STAGING_LOCK:
                    if use_worktree and _is_writer(node):
                        base_commit=git_head(staging)
                        cand_branch=f"ego-node-{run_id}-{node['id']}-{iteration}-{uuid.uuid4().hex[:6]}"
                        if base_commit and git_worktree_add(staging,work,commit_or_branch=base_commit,new_branch=cand_branch):
                            copy_worktree_extras(staging,work)
                            node_wt=True
                            node_branch=cand_branch
                        else:
                            simple_copy_tree(staging,work)
                    else:
                        simple_copy_tree(staging,work)
                before=snapshot(work)
                # A retry continues from the previous attempt's edits; a fresh copy lets a partial fix pass without them.
                for rel,data in (carry or {}).items():
                    path=safe_path(work,rel)
                    if data is None:
                        if path.exists():path.unlink()
                    else:atomic_bytes(path,data)
                if kind in ("code","gate"):
                    r=run_checked_command(node.get("argv") or node["checks"][0],work,timeout=timeout,allow=cfg.allow_commands)
                    # A failing command's own output is the evidence; without it a retry or replan is blind.
                    if r.status!="PASS":feedback=clip_output((r.output+"\n"+r.stderr).strip() or f"exit {r.exit_code}",cfg.tuning.max_feedback_chars)
                else:
                    if chosen_host!="mock" and not trust_cli:
                        raise EgoError("UNTRUSTED_AGENT_EXEC", "Real CLI agents require --trust-cli",3)
                    alter_ctx = ""
                    if chosen_host != "mock" and cfg.knowledge == "alter":
                        from .session import alter_prompt_context
                        node_query = " ".join(x for x in (str(node.get("intent") or "").strip(), str(node.get("prompt") or "").strip()) if x)
                        alter_ctx = alter_prompt_context(project_root, node_query)
                    prompt=build_task_prompt(spec,node,work,feedback=feedback,handoff=handoff,
                                             project_root=project_root,alter_rules=alter_rules,alter_context=alter_ctx,
                                             feedback_limit=cfg.tuning.max_feedback_chars,handoff_limit=cfg.tuning.max_handoff_chars)
                    try:
                        lifecycle_settings=load_settings(project_root).lifecycle
                        if lifecycle_settings.enable_envfacts:
                            lines=active_facts(project_root,lifecycle_settings)
                            if lines:
                                prompt+="\n\n## Observed environment facts\n"+"\n".join("- "+l for l in lines)
                    except Exception:
                        pass
                    mcp_servers=list(node.get("mcp") or [])
                    if cfg.knowledge=="alter" and "alter" not in mcp_servers:
                        mcp_servers.append("alter")
                    def _on_worker_event(**f):
                        progress.emit("worker_segment",node=node["id"],attempt=iteration,**f)
                        if runroot is not None and kind not in ("code","gate"):
                            try:
                                ok_p,seg_edits,seg_hashes=_snapshot_allowed_partial(
                                    work,before,node,exclude_paths={instruction_target(chosen_host)[0]}
                                )
                                if ok_p and seg_edits:
                                    _save_partial(runroot,node["id"],seg_edits,{
                                        "host":chosen_host,
                                        "model":chosen_model or "native-default",
                                        "execution_status":"IN_PROGRESS",
                                        "base_hashes":seg_hashes,
                                    })
                            except Exception:
                                pass
                    # `ego apply` no longer writes the managed block, so the worker's private copy gets it for its session.
                    try:
                        with overlaid(work,chosen_host,cfg):
                            try:
                                r=run_worker(chosen_host,prompt,work,timeout,agent=run_agent,project_root=project_root,threshold=cfg.clear_suggest_tokens,
                                             segment_turns=cfg.worker_segment_turns,tag=f"{node['id']}-{iteration}-{uuid.uuid4().hex[:8]}",
                                             on_event=_on_worker_event,
                                             model=chosen_model,effort=chosen_effort,
                                             read_only=kind=="splitter" or node.get("role")=="reviewer",autonomy=autonomy,isolated=isolated,
                                             max_turns=int(node.get("max_turns",cfg.max_turns)),
                                             lean=cfg.worker_context=="lean",mcp_servers=mcp_servers or None,token_budget=token_budget)
                            except EgoError as exc:
                                if exc.code not in ("CLI_INCOMPATIBLE","HOST_UNAVAILABLE"):
                                    raise
                                from ..adapters.runner import RunResult
                                r=RunResult("UNAVAILABLE","",str(exc),127,0,chosen_host,reason=exc.code,model=chosen_model,effort=chosen_effort)
                    except BaseException:
                        if runroot is not None and kind not in ("code","gate") and work.is_dir():
                            try:
                                ok_p,int_edits,int_hashes=_snapshot_allowed_partial(work,before,node)
                                if ok_p and int_edits:
                                    _save_partial(runroot,node["id"],int_edits,{
                                        "host":chosen_host,
                                        "model":chosen_model or "native-default",
                                        "execution_status":"INTERRUPTED",
                                        "base_hashes":int_hashes,
                                    })
                            except Exception:
                                pass
                        raise
                    try:
                        lifecycle_settings=load_settings(project_root).lifecycle
                        if chosen_host=="claude" and r.session_id and lifecycle_settings.enable_envfacts:
                            tpath=claude_transcript_path(work,r.session_id)
                            if tpath is not None:
                                observations=extract_envfacts(read_events(tpath),r.session_id)
                                if observations:
                                    facts=load_envfacts(project_root)
                                    newly_demoted=[]
                                    for obs in observations:
                                        fid=fact_id(obs.category,obs.key)
                                        prev=facts.get(fid)
                                        cur=prev if prev is not None else Fact(id=fid,key=obs.key,category=obs.category)
                                        nxt=observe_envfact(cur,obs,lifecycle_settings)
                                        facts[fid]=nxt
                                        if cur.state!="demoted" and nxt.state=="demoted":
                                            newly_demoted.append(nxt)
                                    save_envfacts(project_root,facts)
                                    for df in newly_demoted:
                                        notify_alter(project_root,df,"contradiction")
                    except Exception:
                        pass
                if token_budget is not None:token_budget.charge(r.usage)
                checks=[]
                if r.status=="PASS":
                    for argv in node.get("checks",[]):
                        cr=run_checked_command(argv,work,timeout=timeout,allow=cfg.allow_commands)
                        checks.append({**cr.as_dict(),"argv":argv})
                        if cr.status!="PASS":feedback=clip_output(cr.output+"\n"+cr.stderr,cfg.tuning.max_feedback_chars);break
                ok=(r.status=="PASS" and all(c["status"]=="PASS" for c in checks))
                if ok and node.get("role")=="reviewer":
                    verdict_info.update(parse_verdict(r.output))
                    if verdict_info["verdict"]=="INVALID":ok=False;feedback="Reviewer output has no VERDICT line"
                    elif verdict_info["verdict"]=="REJECT":ok=False;feedback=format_findings(verdict_info["findings"],cfg.tuning.max_feedback_chars)
                changed=_changed(before,snapshot(work))
                writes_ok=True
                try:
                    for rel in changed:
                        check_role_write(node.get("role"),rel)
                        check_write(work,rel,node.get("write_allowlist",[]))
                    if kind=="gate" and changed:raise EgoError("GATE_WRITE","A gate may not modify source")
                except EgoError as exc:
                    ok=False;writes_ok=False;feedback=str(exc)
                record={"attempt":iteration,"host":chosen_host,"model":chosen_model or "native-default",
                        "effort":chosen_effort or "auto",
                        "status":"PASS" if ok else "FAIL",
                        # The record keeps bounded evidence, not up to 1 MB of raw output per attempt.
                        "execution":{**r.as_dict(),"output":clip_output(r.output,cfg.tuning.max_record_chars),"stderr":clip_output(r.stderr,cfg.tuning.max_record_chars)},
                        "checks":[{**c,"output":clip_output(c.get("output",""),cfg.tuning.max_record_chars),"stderr":clip_output(c.get("stderr",""),cfg.tuning.max_record_chars)} for c in checks],
                        "changed":changed,"feedback":feedback}
                edits={}
                base_hashes={}
                if writes_ok:
                    for rel in changed:
                        path=safe_path(work,rel)
                        edits[rel]=path.read_bytes() if path.exists() else None
                        base_hashes[rel]=before.get(rel)
                if ok and node_wt and changed and strategy!="compare":
                    with _STAGING_LOCK:
                        keep_branch=git_commit_paths(work,changed,f"ego node {node['id']} attempt {iteration}")
                progress.emit("attempt_finished",node=node["id"],attempt=iteration,host=chosen_host,model=record["model"],
                              status=record["status"],checks=[{"argv":c["argv"],"status":c["status"]} for c in checks],
                              feedback=clip_output(feedback or "",cfg.tuning.max_summary_chars),changed=changed[:20],
                              tokens=tokens_of(r.usage or {}),segments=getattr(r,"segments",1),rotations=getattr(r,"rotations",0))
                return {"ok":ok,"record":record,"edits":edits,"base_hashes":base_hashes,"writes_ok":writes_ok,"output":r.output,"execution_status":r.status,
                        "branch":node_branch if keep_branch else None}
            finally:
                if node_wt:
                    with _STAGING_LOCK:
                        git_worktree_remove(staging,work,force=True)
                        if not keep_branch and node_branch:
                            git_branch_delete(staging,node_branch,force=True)

    if strategy=="compare":
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
    success=False;edits={};last_output="";winning_branch=None
    carry=dict(initial_carry) if (initial_carry and kind not in ("code","gate")) else {}
    latest_partial=dict(carry)
    latest_meta:dict={}
    budget_exhausted=False
    for iteration in range(1,max_attempts+len(routes)+1):
        if token_budget is not None and token_budget.exhausted():
            budget_exhausted=True;break
        chosen=routes[route_pos] if routes else None
        r=attempt_one(chosen.host if chosen else "local",chosen.model if chosen else None,chosen.effort if chosen else None,iteration,carry)
        attempts.append(r["record"])
        last_output=r["output"]
        if r["ok"]:
            success=True;edits=r["edits"];winning_branch=r.get("branch");break
        is_budget_hit=r["record"]["execution"].get("reason")=="BUDGET_EXHAUSTED"
        if r["writes_ok"] and kind not in ("code","gate"):
            latest_partial=dict(r["edits"])
            latest_meta={
                "host":chosen.host if chosen else "local",
                "model":(chosen.model if chosen else None) or "native-default",
                "execution_status":"BUDGET_EXHAUSTED" if is_budget_hit else r["execution_status"],
                "output":clip_output(r["output"],cfg.tuning.max_feedback_chars),
                "base_hashes":r.get("base_hashes") or {},
            }
        if is_budget_hit:
            budget_exhausted=True;break
        if strategy=="fallback" and r["execution_status"] in fallback_on and route_pos<len(routes)-1:
            route_pos+=1
            carry=dict(latest_partial)
            feedback=_handoff_feedback(
                chosen.host if chosen else "local",
                chosen.model if chosen else None,
                r["execution_status"],
                sorted(carry.keys()),
                r["output"],
                cfg.tuning.max_feedback_chars,
            )
            continue
        if node.get("role")=="reviewer":
            # A reviewer's verdict is final; only an unavailable or timed-out host is worth another attempt.
            if r["execution_status"] not in ("UNAVAILABLE","TIMEOUT","RATE_LIMIT"):break
            continue
        # A task evaluation failure must not silently switch vendors.
        if iteration>=max_attempts+route_pos:
            break
        if r["execution_status"] in ("UNAVAILABLE","TIMEOUT","RATE_LIMIT"):
            break
        carry=dict(latest_partial) if kind not in ("code","gate") else {}
        feedback=r["record"].get("feedback") or r["record"]["execution"].get("stderr") or "Verification failed"
    out={"id":node["id"],"status":"PASSED" if success else "FAILED", "attempts":attempts,
         "edits":edits,"elapsed":round(time.monotonic()-start,4),"output":clip_output(last_output,cfg.tuning.max_record_chars),
         "branch":winning_branch}
    if not success and kind not in ("code","gate") and (latest_partial or latest_meta.get("output")):
        out["partial_edits"]=latest_partial
        out["partial_meta"]=latest_meta
    if verdict_info:out["verdict"]=verdict_info["verdict"];out["findings"]=verdict_info["findings"]
    if budget_exhausted:out["budget_exhausted"]=True
    return out


def _run_model(usage:dict,requested:object)->str:
    """The model that produced the usage: the one Claude reported, else the one requested. Empty when neither is known."""
    return str(usage.get("model") or requested or "")


def usage_summary(record:dict)->dict:
    """Aggregate model traffic of a run, by phase: plan (planner call) and work (per task node).

    cost_known is False when a host reported no cost. by_task maps each node id
    to its own token count, so a reader can see where the tokens went.
    by_category splits the run into agent bodies (node attempts) and overhead (planner).
    estimated_cost_usd uses the host cost when given, else the shared price table; it is None
    when any call has no known price, because an unknown price is never guessed.
    """
    total={"model_calls":0,"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,
           "cache_creation_tokens":0,"cache_write_5m_tokens":0,"cache_write_1h_tokens":0,
           "cost_usd":0.0,"cost_known":True,"planned":False,"plan_tokens":0,"work_tokens":0,"by_task":{},
           "by_category":{name:{"tokens":0,"cost_usd":0.0} for name in CATEGORIES},"estimated_cost_usd":0.0}
    unpriced={name:0 for name in CATEGORIES}
    def _book(category:str,usage:dict,host_cost:object,model:str)->None:
        bucket=total["by_category"][category]
        bucket["tokens"]+=tokens_of(usage)
        cost=float(host_cost) if isinstance(host_cost,(int,float)) and not isinstance(host_cost,bool) else price_cost(usage,model)
        if cost is None:unpriced[category]+=1
        else:bucket["cost_usd"]+=cost
    planner=record.get("planner_usage")
    if isinstance(planner,dict):
        total["model_calls"]+=1
        total["planned"]=True
        total["plan_tokens"]=tokens_of(planner)
        for key in TOKEN_KEYS:total[key]+=int(planner.get(key,0) or 0)
        for key in CACHE_TIER_KEYS:total[key]+=int(planner.get(key,0) or 0)
        if isinstance(planner.get("cost_usd"),(int,float)):total["cost_usd"]+=float(planner["cost_usd"])
        else:total["cost_known"]=False
        _book("overhead",planner,planner.get("cost_usd"),_run_model(planner,None))
    for nid,result in record.get("results",{}).items():
        for attempt in result.get("attempts",[]):
            ex=attempt.get("execution",{})
            if ex.get("host") in (None,"local","mock"):continue
            total["model_calls"]+=1
            usage=ex.get("usage") or {}
            for key in TOKEN_KEYS:
                total[key]+=int(usage.get(key,0) or 0)
            for key in CACHE_TIER_KEYS:
                total[key]+=int(usage.get(key,0) or 0)
            count=tokens_of(usage)
            total["work_tokens"]+=count
            total["by_task"][nid]=total["by_task"].get(nid,0)+count
            if isinstance(ex.get("cost_usd"),(int,float)):total["cost_usd"]+=float(ex["cost_usd"])
            else:total["cost_known"]=False
            _book("agent",usage,ex.get("cost_usd"),_run_model(usage,ex.get("model")))
    total["cost_usd"]=round(total["cost_usd"],4)
    for name,bucket in total["by_category"].items():
        bucket["cost_usd"]=None if unpriced[name] else round(bucket["cost_usd"],6)
    total["estimated_cost_usd"]=None if any(unpriced.values()) else round(sum(b["cost_usd"] for b in total["by_category"].values()),6)
    return total


def _write_ledger(root:Path,record:dict,run_id:str)->None:
    """Append this run's model calls to the usage ledger, once per call. Fail-open: the run never stops for accounting."""
    try:
        written=record.setdefault("_ledger_written",[])
        rows=[]
        planner=record.get("planner_usage")
        if isinstance(planner,dict) and "planner" not in written:
            rows.append(usage_row(planner,model=_run_model(planner,None),category="overhead",caller="planner",run_id=run_id))
            written.append("planner")
        for nid,result in record.get("results",{}).items():
            for index,attempt in enumerate(result.get("attempts",[])):
                key=f"{nid}#{index}"
                ex=attempt.get("execution",{})
                usage=ex.get("usage") or {}
                if ex.get("host") in (None,"local","mock") or key in written or not usage:continue
                rows.append(usage_row(usage,model=_run_model(usage,ex.get("model")),category="agent",caller=f"node:{nid}",run_id=run_id,node=nid))
                written.append(key)
        ledger.append_rows(root,rows)
    except Exception:
        pass


def next_action(result:dict, task_file:str="<task-file>")->str|None:
    if not result.get("run_id"):return None
    task_ref=(task_file if task_file!="<task-file>" else None) or result.get("task_file") or "<task-file>"
    if result.get("final_status") in ("INTERRUPTED","BUDGET_EXHAUSTED"):
        return f"ego work resume {result['run_id']} --root . --task {task_ref} --host {result.get('host','<host>')} --trust-cli"
    if result.get("final_status")=="FAILED":
        for nid,data in (result.get("results") or {}).items():
            if (result.get("node_status") or {}).get(nid)=="PASSED":continue
            last=(data.get("attempts") or [{}])[-1]
            if (last.get("execution") or {}).get("status") in ("RATE_LIMIT","TIMEOUT","UNAVAILABLE"):
                return f"ego work resume {result['run_id']} --root . --task {task_ref} --host <other-host> --trust-cli"
        return None
    if result.get("final_status")!="PASSED" or result.get("applied"):return None
    return (f"ego work resume {result['run_id']} --root . --task {task_ref} --host {result.get('host','<host>')} "
            "--trust-cli --apply --approve")


def compact_result(result:dict, *, summary_limit:int|None=None, max_files:int=40, task_file:str="<task-file>")->dict:
    """What a calling agent needs to decide its next step, without the record.

    The full record (per-file base snapshot, every attempt and raw worker
    output) stays on disk; printing it would put it in the caller's context.
    """
    if summary_limit is None:summary_limit=package_defaults()["tuning"]["max_summary_chars"]
    failures=[]
    for nid,data in result.get("results",{}).items():
        if result.get("node_status",{}).get(nid)=="PASSED":continue
        last=(data.get("attempts") or [{}])[-1]
        text=last.get("feedback") or last.get("execution",{}).get("stderr") or data.get("output") or ""
        failures.append({"node":nid,"attempts":len(data.get("attempts",[])),"feedback":clip_output(str(text),summary_limit)})
    changed=result.get("changed_files",[])
    out={k:result[k] for k in ("run_id","final_status","applied","node_status","message","checkpoint_path","usage","progress") if k in result}
    nodes={}
    for nid,data in result.get("results",{}).items():
        last=(data.get("attempts") or [{}])[-1]
        entry={"status":data.get("status"),"attempts":len(data.get("attempts",[])),
               "checks":[{"argv":c.get("argv"),"status":c.get("status")} for c in last.get("checks",[])]}
        if data.get("partial_files"):entry["partial_files"]=data["partial_files"][:20]
        if data.get("verdict"):entry["verdict"]=data["verdict"];entry["findings"]=_brief_findings(data.get("findings"),summary_limit)
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


def _execute(root:Path, spec:dict, progress:Progress, *, host:str|None="mock",apply:bool=False,approve:bool=False,
            trust_cli:bool=False,run_id:str|None=None,resume:bool=False,autonomy:str="auto",isolated:bool=False,
            worktree:bool|None=None,cleanup_worktree:bool|None=None,seed_from:str|None=None,
            extra_allow:list[str]|None=None,token_budget:TokenBudget|None=None,planner_usage:dict|None=None,
            task_file:str|None=None,route_override:dict|None=None,defaults_override:dict|None=None,
            max_run_tokens:int|None=None)->dict:
    root=root.resolve();cfg=load_config(root)
    from ..domain.retention import prune_state
    prune_state(root)  # bounded state: fail-open, before this run adds to it
    if cfg.knowledge == "alter":
        from ..adapters.memory import ensure_alter_project_quietly
        ensure_alter_project_quietly(root)
    seed_dir=None
    if seed_from:
        # Continue from the verified staging of an earlier run: only edits of
        # PASSED nodes were ever merged there, so failed attempts are not inherited.
        if not all(c.isalnum() or c in "-_" for c in seed_from):raise EgoError("INVALID_ID",seed_from,2)
        seed_dir=root/".ego"/"state"/"runs"/seed_from/"staging"
        seed_record=read_json(seed_dir.parent/"record.json")
        if not seed_dir.is_dir() or not seed_record:raise EgoError("SEED_MISSING",f"No staging for run {seed_from}",3)
    should_cleanup = cleanup_worktree if cleanup_worktree is not None else cfg.cleanup_worktree
    if resume:
        validate_task_schema(spec, limits=cfg.plan_schema_limits())
        nodes=spec["nodes"]
        cli_defaults=None
        routing_spec=spec
    else:
        cli_defaults=dict(defaults_override) if defaults_override else None
        routing_spec=_apply_defaults_override(spec, cli_defaults)
        nodes=validate_task(routing_spec, limits=cfg.plan_schema_limits(), caps=cfg)
    use_worktree=_worktree_mode(cfg,nodes,worktree)
    if seed_from:
        use_worktree=use_worktree and bool(seed_record.get("worktree")) and git_is_repo(seed_dir)
    if apply and not approve:raise EgoError("APPROVAL_REQUIRED", "--apply requires --approve")
    if autonomy=="bypass" and not isolated:
        raise EgoError("ISOLATION_REQUIRED","Bypass requires explicit external sandbox attestation",3)
    run_id=run_id or ident("run")
    if not all(c.isalnum() or c in "-_" for c in run_id):raise EgoError("INVALID_ID",run_id,2)
    runroot=root/".ego"/"state"/"runs"/run_id
    staging=runroot/"staging"
    record_path=runroot/"record.json"
    with exclusive_lock(root/".ego"/"locks"/(run_id+".lock"),blocking=False,busy_code="RUN_ACTIVE"):
        if resume:
            record=read_json(record_path)
            if not record:raise EgoError("RUN_NOT_FOUND",run_id,3)
            if record["spec"]!=spec:raise EgoError("RESUME_MISMATCH","Task specification changed")
            cli_defaults=record.get("cli_defaults") if isinstance(record.get("cli_defaults"),dict) else None
            routing_spec=_apply_defaults_override(spec, cli_defaults)
            nodes=validate_task(routing_spec, limits=cfg.plan_schema_limits(), caps=cfg)
            node_ids={n["id"] for n in nodes}
            target_nodes=route_override.get("nodes") if route_override else None
            if target_nodes:
                missing=[nid for nid in target_nodes if nid not in node_ids]
                if missing:
                    raise EgoError("INVALID_NODE",f"Unknown node id(s) in --node: {', '.join(missing)}",2)
            base_host=record.get("host") or "mock"
            if route_override and not target_nodes:
                override_host=route_override.get("host") or ((route_override.get("hosts") or [None])[0])
                effective_host=override_host or host or base_host
            elif target_nodes:
                effective_host=base_host
            else:
                effective_host=host or base_host
            if any(_resolve_node_routes(n,routing_spec,effective_host,cfg,route_override=route_override)[0]=="compare" for n in nodes if n.get("kind","agent") not in ("gate","code")) and apply:
                raise EgoError("COMPARE_NO_APPLY","Compare never applies unreviewed changes",3)
            # An explicit resume supersedes an earlier cancel request; otherwise the run could never continue.
            (runroot/"cancel.flag").unlink(missing_ok=True)
            # A verified, unapplied run may be approved and applied on resume
            # without re-running the entire DAG. An already-applied run is inert.
            if record.get("final_status")=="PASSED" and (not apply or record.get("applied")):
                return {**{k:v for k,v in record.items() if k!="spec"}, "message":completion_message(record),
                        "checkpoint_path":handoff_relpath(run_id)}
            # Applying or resuming an interrupted run requires the staged copy; missing staging would snapshot as all-deleted.
            if not staging.is_dir():
                raise EgoError("STAGING_MISSING", f"No staging directory for run {run_id}", 3)
            if task_file:record["task_file"]=str(task_file)
            if route_override and not target_nodes and (route_override.get("host") or route_override.get("hosts")):
                record["host"]=effective_host
            # Nodes that were mid-flight when the process died are not done: they run again.
            for nid in [k for k,v in record["node_status"].items() if v=="RUNNING"]:record["node_status"].pop(nid)
            record["final_status"]="RUNNING"
            write_json(record_path,record)
            managed_checkpoint(root,record,state="IN_PROGRESS",task_file=record.get("task_file"))
        else:
            effective_host=host or "mock"
            if any(_resolve_node_routes(n,routing_spec,effective_host,cfg,route_override=route_override)[0]=="compare" for n in nodes if n.get("kind","agent") not in ("gate","code")) and apply:
                raise EgoError("COMPARE_NO_APPLY","Compare never applies unreviewed changes",3)
            if record_path.exists():raise EgoError("RUN_EXISTS",run_id)
            is_git = git_is_repo(root)
            worktree_created = False
            if use_worktree and is_git:
                branch_name = f"ego-task-{run_id}"
                commit_ref = (git_head(seed_dir) if seed_dir else None) or "HEAD"
                staging.parent.mkdir(parents=True, exist_ok=True)
                if git_worktree_add(root, staging, commit_or_branch=commit_ref, new_branch=branch_name):
                    worktree_created = True
                    copy_worktree_extras(seed_dir if seed_dir else root, staging)
                    # File knowledge may be untracked/uncommitted, so a checkout alone can miss it.
                    kdir = root / ".ego" / "knowledge"
                    if kdir.is_dir():
                        shutil.copytree(kdir, staging / ".ego" / "knowledge", dirs_exist_ok=True, symlinks=False)
                else:
                    staging.mkdir(parents=True, exist_ok=True)
                    simple_copy_tree(seed_dir or root, staging)
            else:
                staging.mkdir(parents=True)
                simple_copy_tree(seed_dir or root,staging)
            record={"run_id":run_id,"created_at":now(),"spec":spec,"host":effective_host,"node_status":{},"results":{},
                    **({"cli_defaults":cli_defaults} if cli_defaults else {}),
                    # Tokens spent by the planning call that produced this task, reported with the run.
                    **({"planner_usage":planner_usage} if planner_usage is not None else {}),
                    # A seeded run keeps the original base: changes are measured against, and
                    # applied over, the project as it was when the first run started.
                    # A worktree starts from HEAD, not from the working tree: measuring it against the
                    # working tree would report (and apply) uncommitted or untracked files as reverted/deleted.
                    "base_snapshot":seed_record["base_snapshot"] if seed_dir else snapshot(staging if worktree_created else root),
                    "final_status":"RUNNING","applied":False,
                    "worktree":worktree_created,
                    "fingerprints":node_fingerprints(routing_spec)}
            if seed_dir:
                carried=_carry_verified(nodes,record["fingerprints"],seed_record)
                for nid in carried:
                    record["node_status"][nid]="PASSED"
                    record["results"][nid]={"id":nid,"status":"PASSED","attempts":[],"carried_from":seed_from}
                if carried:record["carried"]=carried
            if task_file:record["task_file"]=str(task_file)
            write_json(record_path,record)
            managed_checkpoint(root,record,state="IN_PROGRESS",task_file=record.get("task_file"))
            if seed_dir:
                # The new staging now holds the seed's verified work: keep one staging per rerun chain.
                from ..domain.retention import remove_staging
                try:
                    with exclusive_lock(root/".ego"/"locks"/(seed_from+".lock"),blocking=False):
                        remove_staging(root,seed_dir.parent)
                except EgoError:
                    pass  # the seed run is active elsewhere; retention removes it later
        # A resume is a fresh host invocation; carry only the most recent
        # durable, bounded summary, not the previous session transcript.
        handoff = load_checkpoint(root, run_id) if resume else None
        progress.emit("run_started",run_id=run_id,nodes=[n["id"] for n in nodes],resume=resume)
        if record.get("carried") and not resume:
            progress.emit("nodes_carried",nodes=record["carried"],from_run=seed_from)
        start=time.monotonic()
        amap={n["id"]:n for n in nodes}
        feedbacks:dict[str,str]={}
        initial_carries:dict[str,dict[str,bytes|None]]={}
        alter_rules: list[str] = []
        has_real_agent = any(
            r.host != "mock"
            for n in nodes if n.get("kind", "agent") not in ("gate", "code")
            for r in _resolve_node_routes(n, routing_spec, effective_host, cfg, route_override=route_override)[1]
        )
        if cfg.knowledge == "alter" and has_real_agent:
            from .session import alter_project_rules
            alter_rules = alter_project_rules(root)
        if resume:
            # A failed node reruns with the evidence of its last failure instead of repeating the same attempt blind.
            for nid,data in record["results"].items():
                if nid not in amap or record["node_status"].get(nid)=="PASSED":continue
                text=_failure_text(data)
                if not text:continue
                node=amap[nid]
                loop=_loop_of(amap,nid)
                if node.get("role")=="reviewer":
                    if data.get("verdict")!="REJECT":continue
                    # Rerunning the reviewer alone would reject the same code: send the reviewed coder back with the findings.
                    feedbacks[node["reviews"]]=clip_output(str(text),cfg.tuning.max_feedback_chars)
                    for rid in _between(amap,node["reviews"],nid):record["node_status"].pop(rid,None)
                elif loop is not None and nid!=loop["reviews"]:
                    feedbacks[loop["reviews"]]=clip_output(str(text),cfg.tuning.max_feedback_chars)
                    for rid in _between(amap,loop["reviews"],loop["id"]):record["node_status"].pop(rid,None)
                else:
                    feedbacks[nid]=clip_output(str(text),cfg.tuning.max_feedback_chars)
            # Restore persisted in-progress edits of unfinished nodes so a resumed run (including on another CLI/model) continues where it stopped.
            for nid,node in amap.items():
                if record["node_status"].get(nid)=="PASSED" or node.get("kind","agent") in ("code","gate"):
                    continue
                p_edits,p_meta,p_conflicts=_load_partial(runroot,nid,staging=staging)
                if p_edits:
                    initial_carries[nid]=p_edits
                if p_meta or p_conflicts:
                    meta_obj=p_meta or {}
                    handoff_note=_handoff_feedback(
                        str(meta_obj.get("host") or effective_host),
                        meta_obj.get("model"),
                        str(meta_obj.get("execution_status") or "FAIL"),
                        sorted(p_edits.keys()),
                        str(meta_obj.get("output") or ""),
                        cfg.tuning.max_feedback_chars,
                        conflicts=p_conflicts,
                    )
                    prev_fb=feedbacks.get(nid,"")
                    combined=f"{handoff_note}\n\n{prev_fb}".strip() if prev_fb and prev_fb not in handoff_note else handoff_note
                    feedbacks[nid]=clip_output(combined,cfg.tuning.max_feedback_chars)
        if token_budget is None:token_budget=TokenBudget(resolve_max_run_tokens(cfg,max_run_tokens))
        pending={n["id"]:n for n in nodes if record["node_status"].get(n["id"])!="PASSED"}
        failed=[]
        rounds=record.setdefault("rounds",{})
        round_sig=record.setdefault("round_sig",{})
        exhausted=record.setdefault("exhausted",{})
        # Prompt-only knowledge hint; record['spec'] stays the user's spec so resume comparisons are unchanged.
        prompt_spec=({**routing_spec,'knowledge':cfg.knowledge} if 'knowledge' not in routing_spec else routing_spec)
        max_workers=max(1,cfg.max_parallel)
        in_flight:dict={}
        running_ids:set[str]=set()
        staging_seq=0
        version_by_path:dict[str,tuple[int,str]]={}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            try:
                while pending or in_flight:
                    if (runroot/"cancel.flag").exists():
                        record["final_status"]="CANCELLED";break
                    if token_budget.exhausted():
                        record["final_status"]="BUDGET_EXHAUSTED";break
                    ready=[n for n in pending.values() if n["id"] not in running_ids and all(record["node_status"].get(dep)=="PASSED" for dep in n.get("depends_on",[]))]
                    if not ready and not in_flight:
                        record["final_status"]="FAILED" if failed else "BLOCKED_DEPENDENCY";break
                    slots=max_workers-len(in_flight)
                    to_start=ready[:slots] if slots>0 else []
                    if to_start:
                        for n in to_start:
                            record["node_status"][n["id"]]="RUNNING"
                            running_ids.add(n["id"])
                            progress.emit("node_started",node=n["id"],kind=n.get("kind","agent"),role=n.get("role"))
                        write_json(record_path,record)
                        for n in to_start:
                            nid=n["id"]
                            fut=pool.submit(_run_node,n,prompt_spec,staging,effective_host,cfg,min(cfg.timeout_seconds,int(n.get("timeout_seconds",cfg.timeout_seconds))),
                                            feedback=feedbacks.pop(nid,''),trust_cli=trust_cli,autonomy=autonomy,isolated=isolated,
                                            handoff=handoff,token_budget=token_budget,progress=progress,project_root=root,
                                            alter_rules=alter_rules,use_worktree=bool(record.get("worktree")),run_id=run_id,
                                            initial_carry=initial_carries.pop(nid,None),route_override=route_override,runroot=runroot)
                            in_flight[fut]=(nid,staging_seq)
                    done,_=wait(in_flight.keys(),return_when=FIRST_COMPLETED)
                    # Each node is persisted as soon as it finishes: a crash or cancel loses only the unfinished nodes.
                    for fut in done:
                        nid,start_seq=in_flight.pop(fut)
                        running_ids.discard(nid)
                        result=fut.result()
                        identifier=result["id"]
                        if result.get("budget_exhausted"):record["final_status"]="BUDGET_EXHAUSTED"
                        if result["status"]=="PASSED":
                            branch=result.get("branch") if record.get("worktree") else None
                            if branch:
                                with _STAGING_LOCK:
                                    merged,merge_out=git_merge_branch(staging,branch,changed_paths=list(result["edits"].keys()),message=f"ego merge {identifier}")
                                    git_branch_delete(staging,branch,force=True)
                                if not merged:
                                    record["final_status"]="MERGE_CONFLICT";write_json(record_path,record)
                                    managed_checkpoint(root,record,state="BLOCKED",task_file=record.get("task_file"))
                                    raise EgoError("MERGE_CONFLICT",f"Git merge conflict for {identifier}: {clip_output(merge_out,cfg.tuning.max_summary_chars)}")
                                staging_seq+=1
                            elif result["edits"]:
                                for rel in result["edits"]:
                                    prev_seq,prev_owner=version_by_path.get(rel,(0,""))
                                    if prev_seq>start_seq:
                                        record["final_status"]="MERGE_CONFLICT";write_json(record_path,record)
                                        managed_checkpoint(root,record,state="BLOCKED",task_file=record.get("task_file"))
                                        raise EgoError("MERGE_CONFLICT",f"{rel} edited by {identifier} and {prev_owner}")
                                staging_seq+=1
                                with _STAGING_LOCK:
                                    for rel,data in result["edits"].items():
                                        path=safe_path(staging,rel)
                                        if data is None:
                                            if path.exists():path.unlink()
                                        else:atomic_bytes(path,data)
                                        version_by_path[rel]=(staging_seq,identifier)
                        record["node_status"][identifier]=result["status"]
                        if result["status"]=="PASSED":
                            _clear_partial(runroot,identifier)
                            persisted_partial:list[str]=[]
                        else:
                            partial_edits=result.get("partial_edits") or {}
                            partial_meta=result.get("partial_meta") or {}
                            _save_partial(runroot,identifier,partial_edits,partial_meta)
                            persisted_partial=sorted(partial_edits.keys())
                        # No bytes in persisted JSON, only references to changed paths
                        node_record={k:v for k,v in result.items() if k not in ("edits","partial_edits","partial_meta")}
                        if persisted_partial:
                            node_record["partial_files"]=persisted_partial
                        record["results"][identifier]=node_record
                        if result["status"]!="PASSED":failed.append(identifier)
                        pending.pop(identifier)
                        # A rejection, or a gate/code failure inside a reviewer's loop, sends only the reviewed coder back.
                        loop=_loop_of(amap,identifier)
                        is_reject=identifier in failed and loop is not None and identifier==loop["id"] and result.get("verdict")=="REJECT"
                        is_gate=identifier in failed and loop is not None and identifier not in (loop["id"],loop["reviews"])
                        if is_reject or is_gate:
                            coder=loop["reviews"]
                            text=format_findings(result["findings"],cfg.tuning.max_feedback_chars) if is_reject else clip_output(_failure_text(result),cfg.tuning.max_feedback_chars)
                            if rounds.get(loop["id"],0)<loop.get("max_rounds",cfg.max_rounds):
                                # The staging tree after the coder's merge: a retry that leaves it and the feedback unchanged is a stall.
                                sig=sha256(json_text({"files":snapshot(staging),"feedback":text[-400:]}))
                                if round_sig.get(loop["id"])==sig:
                                    exhausted[loop["id"]]="fail"
                                    progress.emit("loop_stalled",reviewer=loop["id"])
                                else:
                                    round_sig[loop["id"]]=sig
                                    failed.remove(identifier)
                                    rounds[loop["id"]]=rounds.get(loop["id"],0)+1
                                    progress.emit("review_round",reviewer=loop["id"],reviews=coder,round=rounds[loop["id"]],cause="reject" if is_reject else "gate")
                                    feedbacks[coder]=text
                                    for rid in _between(amap,coder,loop["id"]):
                                        record["node_status"].pop(rid,None)
                                        pending[rid]=amap[rid]
                            else:
                                exhausted[loop["id"]]=loop.get("on_exhausted") or "fail"
                        # Persist this node now: staging, record.json and the handoff checkpoint.
                        with _STAGING_LOCK:
                            record["changed_files"]=_changed(record["base_snapshot"],snapshot(staging))
                            record["usage"]=usage_summary(record)
                            _write_ledger(root,record,run_id)
                            write_json(record_path,record)
                            cp=managed_checkpoint(root,record,state="BLOCKED" if failed else "IN_PROGRESS",task_file=record.get("task_file"))
                        extra={"verdict":result["verdict"],"findings":_brief_findings(result.get("findings"),cfg.tuning.max_summary_chars)} if result.get("verdict") else {}
                        progress.emit("node_finished",node=identifier,status=result["status"],attempts=len(result["attempts"]),
                                      changed=sorted(result["edits"])[:20],checkpoint_revision=cp["revision"],**extra)
                    # A failure stops only its dependents: independent nodes still finish, so a
                    # corrected rerun (`--from`) carries them instead of running them for the first time.
            finally:
                for rem_fut in list(in_flight.keys()):
                    in_flight.pop(rem_fut)
                    try:
                        rem_res=rem_fut.result()
                        if record.get("worktree") and rem_res.get("branch"):
                            with _STAGING_LOCK:
                                git_branch_delete(staging,rem_res["branch"],force=True)
                    except Exception:
                        pass
        if not pending and not failed:
            record["final_status"]="COMPARISON_READY" if any(v.get("review_required") for v in record["results"].values()) else "PASSED"
        elif failed and record["final_status"]=="RUNNING":
            record["final_status"]="FAILED"
        record["changed_files"]=_changed(record["base_snapshot"],snapshot(staging))
        record["usage"]=usage_summary(record)
        _write_ledger(root,record,run_id)
        if record["final_status"]=="PASSED" and apply:
            # Actual workspace may not have drifted since start.
            allowed=set(extra_allow or [])
            for n in nodes:allowed.update(n.get("write_allowlist",[]))
            for rel in record["changed_files"]:
                check_write(root,rel,list(allowed))
                if filehash(safe_path(root,rel))!=record["base_snapshot"].get(rel):
                    raise EgoError("STALE_BASE",f"Target changed while run executed: {rel}")
            backups={}
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
        # An unfinished or unapplied run keeps its worktree: resume, `--apply` and `--from` need that staging.
        if record.get("worktree") and should_cleanup and (
            (record["final_status"] == "PASSED" and record.get("applied"))
            or record["final_status"] == "COMPARISON_READY"
        ):
            git_worktree_remove(root, staging, force=True)
            # The staging branch is only a vehicle; drop it unless it holds unmerged commits.
            record["branch_deleted"] = git_branch_delete(root, f"ego-task-{run_id}", force=bool(record.get("applied")))
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


def _alter_reporter(root:Path,ref:dict,run_id:str,runroot:Path):
    """Build a progress callback that mirrors node results to the alter plan; alter outages never fail the run."""
    nodes:dict={}
    failures:dict={}
    summary_limit=load_config(root).tuning.max_summary_chars
    client=None
    marker=runroot/"alter_sync.json"
    def callback(event:str,fields:dict)->None:
        nonlocal client
        node=fields.get("node")
        if event=="node_started":
            nodes[node]={"status":"RUNNING","attempts":0,"failure":""}
        elif event=="node_finished":
            failed=fields["status"]!="PASSED"
            nodes[node]={"status":fields["status"],"attempts":fields["attempts"],"failure":failures.get(node,"") if failed else ""}
        elif event=="attempt_finished" and fields.get("status")!="PASS":
            failures[node]=clip_output(fields.get("feedback") or "",summary_limit)
        if event in ("run_started","node_started","node_finished","attempt_finished"):state="running"
        elif event=="run_finished":state="passed" if fields["final_status"]=="PASSED" else "failed"
        elif event=="run_error":state="failed"
        else:return
        try:
            if client is None:
                from ..adapters.alter_plan import AlterPlanClient
                client=AlterPlanClient(root)
            client.report(ref["plan_id"],run_id,state,dict(nodes))
        except Exception as exc:
            write_json(marker,{"pending":True,"state":state,"error":str(exc)[:300],"at":now()})
            return
        marker.unlink(missing_ok=True)
    return callback


def execute(root:Path, spec:dict, *, run_id:str|None=None, progress_stderr:bool=False, **kwargs)->dict:
    """Run a task and journal its progress to `.ego/state/runs/<run_id>/progress.jsonl`."""
    run_id=run_id or ident("run")
    if not all(c.isalnum() or c in "-_" for c in run_id):raise EgoError("INVALID_ID",run_id,2)
    runroot=root.resolve()/".ego"/"state"/"runs"/run_id
    progress=Progress(runroot/"progress.jsonl",echo=progress_stderr)
    ref=(spec.get("source") or {}).get("alter_plan")
    if ref:progress.on_event=_alter_reporter(root,ref,run_id,runroot)
    try:
        result=_execute(root,spec,progress,run_id=run_id,**kwargs)
    except BaseException as exc:
        # Only a run that exists can be followed; a refused start (RUN_EXISTS, bad task) leaves no log.
        # A refused start (RUN_EXISTS, RUN_ACTIVE) must not write run_error into the log of the run that is live.
        if (runroot/"record.json").exists() and not (isinstance(exc,EgoError) and exc.code in ("RUN_EXISTS","RUN_ACTIVE")):
            code=exc.code if isinstance(exc,EgoError) else type(exc).__name__
            progress.emit("run_error",run_id=run_id,code=code,message=clip_output(str(exc),load_config(root.resolve()).tuning.max_summary_chars))
        raise
    progress.emit("run_finished",run_id=run_id,final_status=result["final_status"],applied=result.get("applied",False),
                  changed_files=len(result.get("changed_files",[])),message=result.get("message"))
    return result


def status(root:Path,run_id:str)->dict:
    if not run_id.replace("-","").replace("_","").isalnum():raise EgoError("INVALID_ID",run_id,2)
    data=read_json(root/".ego"/"state"/"runs"/run_id/"record.json")
    if not data:raise EgoError("RUN_NOT_FOUND",run_id,3)
    data={**data,"final_status":_effective_status(root,data)}
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
        elif record is not None and _effective_status(root,record)!="RUNNING":
            idle_ended+=1
            if idle_ended>=2:
                write(json.dumps({"event":"run_stopped","final_status":_effective_status(root,record)},ensure_ascii=False))
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
