"""CLI-first entrypoint."""
from __future__ import annotations
import argparse
import difflib
import json
import os
from pathlib import Path
import shutil
import sys
from . import __version__
from .shared.util import EgoError, read_json, write_json

def _print(data):print(json.dumps(data,indent=2,ensure_ascii=False,default=str))
def _root(args):return Path(getattr(args,"root", ".")).resolve()

def _project_target(args) -> Path:
    target = Path(getattr(args, "root", None) or Path.cwd()).resolve()
    if target in (Path.home(), Path("/")):
        raise EgoError("INVALID_PROJECT_ROOT", f"Cannot apply harness directly to {target}", 2)
    return target

def _cmd_apply(args):
    """CWD-first installation; guided TTY choices or deterministic non-interactive use."""
    from .domain.config import load_config
    from .usecases.install import installation_plan, apply_install
    root = _project_target(args)
    cfg = load_config(root)
    if args.hosts is not None: cfg.hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if args.profile is not None: cfg.profile = args.profile
    if args.graph is not None: cfg.code_intelligence = args.graph
    if args.knowledge is not None: cfg.knowledge = args.knowledge
    if args.runtime is not None: cfg.runtime_mode = args.runtime
    if getattr(args, "policy", None) is not None: cfg.policy_mode = args.policy
    if getattr(args, "clear_suggest_tokens", None) is not None: cfg.clear_suggest_tokens = args.clear_suggest_tokens
    if getattr(args, "models", None) is not None:
        try:
            parsed = json.loads(args.models)
            if not isinstance(parsed, dict): raise ValueError("must be JSON object")
            cfg.models = parsed
        except Exception as exc:
            raise EgoError("INVALID_MODEL", f"models must be a valid JSON object: {exc}", 2)
    if getattr(args, "roles", None) is not None:
        try:
            parsed = json.loads(args.roles)
            if not isinstance(parsed, dict): raise ValueError("must be JSON object")
            cfg.roles = parsed
        except Exception as exc:
            raise EgoError("INVALID_CONFIG", f"roles must be a valid JSON object: {exc}", 2)
    # No extra import or stdin inspection on scripted apply/check/dry-run.
    from .usecases.apply_wizard import should_prompt, select_options, confirm_apply, WizardCancelled
    interactive = should_prompt(args)
    detect_tests = args.detect_tests
    if interactive:
        try:
            detect_tests |= select_options(cfg, args, root)
        except WizardCancelled:
            _print({"status": "CANCELLED", "root": str(root), "changed_count": 0})
            return 0
    cfg.validate()
    if detect_tests and not cfg.tests:
        from .adapters.detect import detect
        cfg.tests = detect(root)["test_commands"]
    plan = installation_plan(root, cfg)
    changes = [c for c in plan["changes"] if c["action"] != "unchanged"]
    status = "BLOCKED" if plan["conflicts"] else "UP_TO_DATE" if not changes else "CHANGES_PENDING"
    from .adapters.hostcompat import host_versions as _host_versions
    host_versions = _host_versions(cfg.hosts)
    summary = {"status": status, "root": str(root), "harness_version": __version__,
               "profile": cfg.profile, "policy_mode": cfg.policy_mode, "hosts": cfg.hosts, "models": cfg.models, "roles": cfg.roles,
               "code_intelligence": cfg.code_intelligence, "knowledge": cfg.knowledge,
               "runtime_mode": cfg.runtime_mode, "tests_detected": bool(detect_tests),
               "changes": changes, "conflicts": plan["conflicts"],
               "changed_count": len(changes), "host_versions": host_versions}
    if args.dry_run or args.check or plan["conflicts"]:
        if getattr(args, "install_alter_skills", False) and not args.check and not plan["conflicts"]:
            from .adapters.memory import install_alter_skills
            summary["alter_skills"] = install_alter_skills(root, clients=cfg.hosts, scope="global", dry_run=True)
        _print(summary)
        return 5 if args.check and (changes or plan["conflicts"]) else 0
    if not changes:
        if getattr(args, "install_alter_skills", False):
            from .adapters.memory import install_alter_skills
            summary["alter_skills"] = install_alter_skills(root, clients=cfg.hosts, scope="global")
        _print(summary)
        return 0
    if interactive:
        try:
            if not confirm_apply(cfg, changes, root, host_versions):
                _print({**summary, "status": "CANCELLED"})
                return 0
        except WizardCancelled:
            _print({**summary, "status": "CANCELLED"})
            return 0
    result = apply_install(root, cfg)
    out = {**summary, "status": result["status"], "snapshot_id": result["snapshot_id"]}
    if getattr(args, "install_alter_skills", False):
        from .adapters.memory import install_alter_skills
        out["alter_skills"] = install_alter_skills(root, clients=cfg.hosts, scope="global")
    _print(out)
    return 0

def _cmd_status(args):
    from .domain.config import load_config
    from .usecases.install import installation_plan, OWNERSHIP
    root = _project_target(args)
    ownership_path = root / OWNERSHIP
    ownership = read_json(ownership_path, {})
    cfg = load_config(root)
    plan = installation_plan(root, cfg)
    changes = [c for c in plan["changes"] if c["action"] != "unchanged"]
    status = "BLOCKED" if plan["conflicts"] else "UP_TO_DATE" if not changes else "CHANGES_PENDING"
    last_snapshot_id = ownership.get("last_snapshot_id")
    if not last_snapshot_id:
        backups = root / ".ego" / "backups"
        if backups.exists():
            snaps = sorted([p.name for p in backups.glob("snapshot-*") if p.is_dir()])
            if snaps:
                last_snapshot_id = snaps[-1]
    installed_version = ownership.get("installed_version") or ownership.get("version") or __version__
    _print({
        "status": status,
        "root": str(root),
        "installed_version": installed_version,
        "harness_version": __version__,
        "version_differs": installed_version != __version__,
        "last_snapshot_id": last_snapshot_id,
        "changes": changes,
        "conflicts": plan["conflicts"],
        "changed_count": len(changes),
    })
    return 0

def _cmd_rollback(args):
    from .usecases.install import rollback, OWNERSHIP
    root = _project_target(args)
    snapshot = getattr(args, "snapshot", None)
    if not snapshot:
        ownership = read_json(root / OWNERSHIP, {})
        snapshot = ownership.get("last_snapshot_id")
        if not snapshot:
            backups = root / ".ego" / "backups"
            if backups.exists():
                snaps = sorted([p.name for p in backups.glob("snapshot-*") if p.is_dir()])
                if snaps:
                    snapshot = snaps[-1]
        if not snapshot:
            raise EgoError("NO_SNAPSHOTS", "No snapshots available to rollback", 3)
    result = rollback(root, snapshot)
    _print(result)
    return 0

def _cmd_uninstall(args):
    from .usecases.install import uninstall_project, uninstall_cli
    if getattr(args, "cli", False):
        _print(uninstall_cli())
        return 0
    root = _project_target(args)
    if not getattr(args, "yes", False):
        try:
            ans = input(f"Are you sure you want to uninstall ego from '{root.name}'? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            _print({"status": "CANCELLED"})
            return 0
    _print(uninstall_project(root))
    return 0

def _cmd_init(args):
    from .domain.config import load_config
    from .usecases.install import installation_plan,apply_install
    root=_project_target(args)
    cfg=load_config(root)
    if args.profile:cfg.profile=args.profile
    if args.hosts:cfg.hosts=[h.strip() for h in args.hosts.split(",") if h.strip()]
    if args.graph:cfg.code_intelligence=args.graph
    if args.knowledge:cfg.knowledge=args.knowledge
    cfg.validate()
    from .adapters.detect import detect
    detection=detect(root)
    if args.detect_tests and not cfg.tests:
        cfg.tests=detection["test_commands"]
    result=apply_install(root,cfg) if args.apply else installation_plan(root,cfg)
    if not args.apply:result.pop("wanted",None)
    result["detection"]=detection
    from .adapters.hostcompat import host_versions
    result["host_versions"]=host_versions(cfg.hosts)
    _print(result);return 0

def _cmd_sync(args):
    if args.all:
        from .usecases.projects import sync_all
        result=sync_all(apply=args.apply)
        _print(result)
        return 5 if any(k in result["counts"] for k in ("ERROR","BLOCKED")) else 0
    from .domain.config import load_config
    from .usecases.install import installation_plan,apply_install
    cfg=load_config(_root(args))
    result=apply_install(_root(args),cfg) if args.apply else installation_plan(_root(args),cfg)
    if not args.apply:result.pop("wanted",None)
    from .adapters.hostcompat import host_versions
    result["host_versions"]=host_versions(cfg.hosts)
    _print(result);return 0

def _cmd_projects(args):
    from .usecases.projects import list_projects,register,unregister,scan,project_info,registry_path
    if args.action=="list":
        _print({"harness_version":__version__,"registry":str(registry_path()),"projects":list_projects()});return 0
    if args.action=="scan":
        _print({"registered":scan(Path(args.dir),args.depth)});return 0
    results=[]
    for raw in args.paths:
        path=Path(raw).resolve()
        if args.action=="add":
            info=project_info(path)
            if info["status"] in ("MISSING","NOT_APPLIED"):raise EgoError("PROJECT_NOT_INITIALIZED",f"{path}: no ego installation (.ego/ownership.json)",3)
            register(path);results.append(info)
        else:results.append({"root":str(path),"removed":unregister(path)})
    _print(results);return 0

def _cmd_doctor(args):
    from .domain.config import load_config
    from .adapters.hosts import capability
    from .adapters.codeintel import health
    from .adapters.memory import memory_health, knowledge_health
    from .domain.policy import sandbox_diagnostics
    cfg=load_config(_root(args))
    results={"version":__version__,"hosts":{x:capability(x).__dict__ for x in cfg.hosts},
             "code_intelligence":health(cfg.code_intelligence),"knowledge":cfg.knowledge,
             "sandbox":sandbox_diagnostics(),
             "memory_health":memory_health(_root(args)),"knowledge_health":knowledge_health(_root(args),cfg.knowledge)}
    _print(results);return 0



def _cmd_work(args):
    from .usecases.work import status,cancel,compact_result,watch
    root=_root(args)
    # Default output is a compact summary: the full record holds a per-file
    # snapshot and raw worker output, which would land in the caller's context.
    from .domain.config import load_config
    summary_limit=load_config(root).tuning.max_summary_chars
    shape=(lambda r:r) if getattr(args,"verbose",False) else (lambda r:compact_result(r,summary_limit=summary_limit,task_file=getattr(args,"task",None) or "<task-file>"))
    if args.action=="status":_print(shape(status(root,args.run_id)));return 0
    if args.action=="watch":watch(root,args.run_id,after=args.after);return 0
    if args.action=="cancel":_print(cancel(root,args.run_id));return 0
    if args.action=="alter-plan":
        from .usecases.alter_plan import push,wait,pull,sync
        if args.plan_action=="push":result=push(root,args.task,args.slug)
        elif args.plan_action=="wait":result=wait(root,args.plan_id,args.interval,args.timeout)
        elif args.plan_action=="pull":result=pull(root,args.plan_id,args.out,args.run_id)
        else:result=sync(root,args.run_id,args.task)
        _print(result);return 0
    if args.action=="plan":
        from .usecases.planner import plan_work
        result=plan_work(root,args.goal,args.host,[s.strip() for s in args.scope.split(",") if s.strip()] if args.scope else None,execute=args.execute)
        if args.out:
            # The task file carries the planning cost, so a later `work run` reports the plan phase too.
            task=dict(result["task"])
            if result.get("planner_usage") is not None:task["planner_usage"]=result["planner_usage"]
            write_json(Path(args.out),task)
        _print(result);return 0
    if args.action=="native":
        _print({"mode":"native","status":"USE_HOST_DIRECTLY","additional_llm_calls":0,
                "instructions":"Use /work in the host, or ego work run for bounded graph execution"});return 0
    task=read_json(Path(args.task))
    if not task:raise EgoError("TASK_MISSING",str(args.task),3)
    # Planning cost is reporting data, not part of the task specification (resume compares specs).
    planner_usage=task.pop("planner_usage",None)
    route_override=None
    defaults_override=None
    if args.action=="resume":
        override={}
        if args.host:override["host"]=args.host
        if args.hosts:override["hosts"]=[h.strip() for h in args.hosts.split(",") if h.strip()]
        if args.strategy:override["strategy"]=args.strategy
        if args.model:override["model"]=args.model
        if args.models:
            try:override["models"]=json.loads(args.models)
            except json.JSONDecodeError as exc:raise EgoError("INVALID_MODEL",str(exc),2) from exc
        if getattr(args,"nodes",None):
            nodes=[nid.strip() for raw in args.nodes for nid in raw.split(",") if nid.strip()]
            if nodes:override["nodes"]=nodes
        if override:route_override=override
    elif any(getattr(args,attr,None) for attr in ("hosts","strategy","model","models")):
        defaults_override={}
        if args.hosts:defaults_override["hosts"]=[h.strip() for h in args.hosts.split(",") if h.strip()]
        if args.strategy:defaults_override["strategy"]=args.strategy
        if args.model:defaults_override["model"]=args.model
        if args.models:
            try:defaults_override["models"]=json.loads(args.models)
            except json.JSONDecodeError as exc:raise EgoError("INVALID_MODEL",str(exc),2) from exc
    from .usecases.replan import run_with_replans
    result=run_with_replans(root,task,host=args.host,apply=args.apply,approve=args.approve,trust_cli=args.trust_cli,
                   run_id=args.run_id,resume=args.action=="resume",progress_stderr=True,
                   autonomy=args.autonomy,isolated=args.attest_external_sandbox,
                   worktree=args.worktree,cleanup_worktree=args.cleanup_worktree,planner_usage=planner_usage,
                   seed_from=getattr(args,"seed_from",None),task_file=args.task,
                   route_override=route_override,defaults_override=defaults_override,
                   max_run_tokens=args.max_run_tokens)
    _print(shape(result));return 0 if result["final_status"] in ("PASSED","COMPARISON_READY") else 5






def _cmd_state(args):
    from .domain.retention import prune_state
    _print(prune_state(_root(args)));return 0


def _cmd_usage(args):
    from .usecases import ledger
    _print(ledger.summarize(_root(args), args.days));return 0


def _cmd_memory(args):
    from .domain.config import load_config
    from .adapters.memory import FileMemory, install_alter_skills
    root=_root(args);cfg=load_config(root)
    if args.action=="install-skills":
        clients=[c.strip() for c in args.clients.split(",") if c.strip()] if args.clients else cfg.hosts
        _print(install_alter_skills(root,clients=clients,scope=args.scope,agents=not args.no_agents,dry_run=args.dry_run))
        return 0
    p=FileMemory(root)  # process memory (facts about the work itself) is always local; project knowledge is `ego knowledge`
    if args.action=="add":_print(p.add(args.text,tags=args.tags or [],evidence=args.evidence or []));return 0
    if args.action=="search":_print(p.search(args.query));return 0
    if args.action=="list":_print(p.list());return 0
    if args.action=="delete":_print({"deleted":p.delete(args.id)});return 0

def _cmd_knowledge(args):
    """Project knowledge for `knowledge = "file"`: an index plus one file per topic. With alter the knowledge lives there."""
    from dataclasses import asdict
    from .adapters import envfact_store
    from .domain.config import load_config
    from .adapters.knowledge import KnowledgeStore
    root=_root(args)
    if args.action=="facts":
        _print([asdict(f) for f in envfact_store.load(root).values()]);return 0
    if args.action=="forget":
        f=envfact_store.forget(root,args.id)
        if f is None:
            raise EgoError("NOT_FOUND",args.id,1)
        envfact_store.notify_alter(root,f,"forget")
        _print(asdict(f));return 0
    if load_config(root).knowledge=="alter":
        raise EgoError("KNOWLEDGE_IS_ALTER","knowledge = \"alter\": project knowledge lives in alter. Use the alter MCP tools (alter_task_context / alter_search / alter_get_note); set knowledge = \"file\" to use .ego/knowledge",2)
    store=KnowledgeStore(root)
    if args.action=="index":print(store.index_text(),end="");return 0
    if args.action=="search":_print(store.search(" ".join(args.query),limit=args.limit));return 0
    if args.action=="get":_print([store.get(slug) for slug in args.slug]);return 0
    if args.action=="add":_print(store.add(args.text,topic=args.topic,summary=args.summary,keywords=args.keyword));return 0

def _cmd_session(args):
    from .usecases.session import (save_checkpoint, load_checkpoint, rotation_instructions, arm_auto_resume,
                          detect_host, list_checkpoints, resolve_task, handoff, pickup, RUN_ID_ENV)
    root = _root(args)
    if args.action == "checkpoint":
        saved_record = save_checkpoint(
            root, args.task_id, goal=args.goal, completed=args.completed,
            next_steps=args.next_steps, decisions=args.decision,
            blockers=args.blocker, evidence=args.evidence,
            status=args.status, kind=args.kind, source="interactive", host=args.host or detect_host(),
            run=os.environ.get(RUN_ID_ENV),
        )
        try:
            from .domain.config import load_config
            if load_config(root).knowledge == "alter":
                from .adapters.memory import submit_alter_checkpoint_review
                submit_alter_checkpoint_review(root, saved_record)
            else:
                from .adapters.knowledge import record_checkpoint_decisions
                record_checkpoint_decisions(root, saved_record)
        except Exception:
            pass
        # Acknowledge only: the caller just supplied these notes, echoing them back costs context twice.
        result = {"task_id": saved_record["task_id"], "status": saved_record["status"], "revision": saved_record["revision"],
                  "path": saved_record["path"], "auto_resume_after_clear": arm_auto_resume(root, args.task_id, args.status)}
    elif args.action == "list":
        result = list_checkpoints(root, include_completed=args.all)
    elif args.action == "status":
        result = load_checkpoint(root, args.task_id)
        if result is None: raise EgoError("HANDOFF_MISSING", args.task_id, 3)
    elif args.action == "handoff":
        result = handoff(root, args.task_id, args.to, args.from_host)
        if args.launch:
            # Replaces this process with the target CLI: only meaningful from a real terminal.
            if not result["host_available"]:
                raise EgoError("HOST_UNAVAILABLE", result["instructions"], 3)
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise EgoError("LAUNCH_NEEDS_TTY", "--launch opens an interactive CLI and needs a terminal; "
                               "run launch_command yourself: " + result["launch_command"], 3)
            os.chdir(root)
            exec_argv = result["launch_argv"]
            if exec_argv[0] == "ego" and shutil.which("ego") is None:
                exec_argv = [sys.executable, "-m", "ego", *exec_argv[1:]]
            os.execvp(exec_argv[0], exec_argv)
    elif args.action == "analyze":
        from .usecases.alter_analysis import analyze_session
        result = analyze_session(root, args.session_id)
    elif args.from_host:
        result = pickup(root, args.task_id, args.from_host)
    else:
        result = rotation_instructions(root, resolve_task(root, args.task_id), args.host or detect_host() or "claude")
    _print(result)
    return 0


def _cmd_run(args):
    from .domain.config import load_config
    from .usecases.ptyrun import run_host
    root=_root(args)
    cfg=load_config(root)
    threshold=args.clear_tokens if args.clear_tokens is not None else cfg.clear_suggest_tokens
    return run_host(args.host,args.args,root,threshold,yolo=cfg.run_yolo if args.yolo is None else args.yolo)


def _cmd_code(args):
    from .adapters.codeintel import health,search,symbol,index,impact,affected_tests
    from .domain.config import load_config
    provider=load_config(_root(args)).code_intelligence
    res={"doctor":lambda:health(provider),"search":lambda:search(_root(args),args.query,provider),
         "symbol":lambda:symbol(_root(args),args.name,provider),"index":lambda:index(_root(args),provider,execute=args.execute),
         "impact":lambda:impact(_root(args),args.name,provider),"affected":lambda:affected_tests(_root(args),args.files,provider)}[args.action]()
    _print(res);return 0


def _cmd_fold(args):
    from .usecases.fold import run_fold
    _print(run_fold(_root(args), args.session, args.transcript))
    return 0


def _cmd_tools(args):
    from .usecases.toolprune import apply, propose, restore
    root=_root(args)
    if args.action=="propose":
        _print(propose(root))
        return 0
    if args.action=="apply":
        candidates=propose(root)
        if not args.yes:
            _print(candidates)
            return 0
        _print(apply(root,candidates,args.host))
        return 0
    if args.action=="restore":
        _print(restore(root,args.tool))
        return 0
    return 0


def _cmd_hook(args):
    from .domain.policy import check_hook_payload
    from .domain.config import load_config
    root=_root(args)
    raw=sys.stdin.read(1024*1024)
    if os.environ.get("EGO_ALTER_ANALYZER","").strip() not in ("","0") and args.event in ("stop","session-start","session-end","user-prompt"):
        print("{}");return 0
    if args.event=="context-probe":
        # Status line of a host started by `ego run`: report its context size to the wrapper.
        try:
            from .usecases.ptyrun import STATE_ENV,record_context
            from .usecases.session import record_usage
            payload=json.loads(raw)
            line=record_context(payload,Path(os.environ[STATE_ENV]))
            # The same input carries AGY's quota: keep it for the Stop hook and show it beside the context size.
            usage=record_usage(payload,root).removeprefix("ego · ")
            print(f"{line} · {usage}" if usage else line)
        except (ValueError,OSError,EgoError,KeyError,TypeError):print("")
        return 0
    if args.event=="statusline":
        # Prints the status line itself (plain text); any problem means an empty line.
        try:
            from .usecases.session import record_usage
            print(record_usage(json.loads(raw),root))
        except (ValueError,OSError,EgoError,KeyError,TypeError):print("")
        return 0
    if args.event=="user-prompt":
        # Looks up alter knowledge for the prompt; any problem means "say nothing".
        try:
            from .adapters.memory import use_host
            from .usecases.session import ALTER_UNREACHABLE,adapt_output,alter_prompt_status,new_transcript_prompt,normalize_payload,project_root
            obj=normalize_payload(args.host,json.loads(raw))
            root=project_root(root,obj.get("cwd"))
            prompt=str(obj.get("prompt") or "")
            if args.host=="agy":prompt=new_transcript_prompt(root,obj)  # AGY sends no prompt: read it from the transcript, once
            error=""
            with use_host(args.host):
                text,error=alter_prompt_status(root,prompt) if load_config(root).knowledge=="alter" else ("","")
            if error:text=ALTER_UNREACHABLE.format(reason=error)  # alter is the knowledge source: an unreachable alter is an error, not silence
        except (ValueError,OSError,EgoError,KeyError,TypeError):text="";error=""
        out={"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":text}} if text else {}
        if error:out["systemMessage"]=text
        print(json.dumps(adapt_output(args.host,"user-prompt",out),ensure_ascii=False));return 0
    if args.event=="session-end":
        # Kept so hooks installed by older versions exit cleanly; the session ends with the ego run wrapper.
        print("{}")
        try:
            obj=json.loads(raw)
            sid=obj.get("session_id") if isinstance(obj,dict) else None
            tpath=obj.get("transcript_path") if isinstance(obj,dict) else None
            if isinstance(sid,str) and sid and isinstance(tpath,str) and tpath:
                from .usecases.fold import spawn_fold
                spawn_fold(root,sid,tpath)
        except Exception:
            pass
        return 0
    if args.event in ("stop","session-start"):
        # Advisory hooks never fail the host turn: any problem means "say nothing".
        host=args.host
        try:
            from .usecases.session import adapt_output,bind_run_session,clear_advice,clear_resume,codex_limits,normalize_payload,project_root,usage_advice
            obj=normalize_payload(host,json.loads(raw))
            root=project_root(root,obj.get("cwd"))
            info={}
            if args.event=="stop":
                bind_run_session(root,obj)
                cfg=load_config(root)
                if cfg.knowledge == "alter":
                    try:
                        from .adapters.memory import resolve_alter_session, submit_alter_session_review
                        transcript = obj.get("transcript_path") if isinstance(obj.get("transcript_path"), str) else None
                        alter_sid, segment_id = resolve_alter_session(str(obj.get("session_id") or ""), transcript=transcript)
                        submit_alter_session_review(
                            root,
                            session_id=alter_sid,
                            host=host,
                            transcript=transcript,
                            messages=obj.get("messages") if isinstance(obj.get("messages"), list) else None,
                            keep_turns=cfg.alter_analysis,
                            segment_id=segment_id,
                        )
                    except Exception:
                        pass
                    if host == "claude":
                        try:  # alter holds project knowledge: Claude's auto-memory would be a second context
                            from .adapters.memory import migrate_host_memory
                            migrate_host_memory(root)
                        except Exception:
                            pass
                # A nearly spent usage limit outranks a large context: there may be no later turn to save in.
                # Under `ego run` the wrapper saves, clears and resumes: the hooks must not ask for the same again.
                wrapped=bool(os.environ.get("EGO_RUN"))
                # Codex reports its usage limits in the session record; Claude's and AGY's come from the status line
                # (usage_advice reads what it recorded). OpenCode reports none, so there usage_switch_percent has nothing to act on.
                limits=None
                if host=="codex":limits=codex_limits(Path(obj["transcript_path"]).expanduser()) if isinstance(obj.get("transcript_path"),str) else {}
                elif host=="opencode":limits={}
                out=usage_advice(root,obj,cfg.usage_switch_percent,cfg.hosts,host=host,limits=limits) or ({} if wrapped else clear_advice(root,obj,cfg.clear_suggest_tokens,host=host,info=info))
            else:
                out={} if os.environ.get("EGO_RUN") or not load_config(root).auto_resume else clear_resume(root,obj)  # the hook is also installed for alter alone
                # Inside `ego run` the checkpoint resume stands down, but the alter requirement is not a resume: keep it.
                if load_config(root).knowledge=="alter":
                    from .adapters.memory import use_host
                    from .usecases.session import with_alter_mandate
                    with use_host(host):
                        out=with_alter_mandate(out, root)
                # Bound last: ego run's resume prompt waits for this, so it must follow the SessionStart work.
                bind_run_session(root,obj)
            out=adapt_output(host,args.event,out,once=bool(info.get("once")))
        except (ValueError,OSError,EgoError,KeyError,TypeError):out={}
        print(json.dumps(out,ensure_ascii=False));return 0
    cfg=load_config(root)
    try:obj=json.loads(raw)
    except ValueError:
        _print({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Malformed event JSON"}});return 0
    result=check_hook_payload(args.event,obj,policy_mode=cfg.policy_mode)
    if result["decision"]=="deny":
        _print({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":result["reason"]}})
    else:_print({})
    return 0






def _cmd_hosts(args):
    from .adapters.hostcompat import list_host_capabilities, inspect_host
    if args.host:
        _print(inspect_host(args.host, refresh=args.action=="refresh"))
    else:
        _print(list_host_capabilities(refresh=args.action=="refresh"))
    return 0


def _cmd_models(args):
    from .adapters.models import list_models,refresh_catalog
    root=_root(args)
    if args.action=="refresh":
        _print(refresh_catalog(root,hosts=[args.host] if args.host else None))
    else:
        _print(list_models(root,host=args.host,refresh_if_stale=not args.no_refresh))
    return 0


def _cmd_update(args):
    from .adapters.selfupdate import self_update, update_from_local_wheel, self_argv
    project=_root(args)
    if args.sync and not (project/".ego"/"config.toml").exists():
        raise EgoError("PROJECT_NOT_INITIALIZED","--sync requires existing .ego/config.toml",3)
    if args.wheel:
        if not args.sha256:
            raise EgoError("DIGEST_REQUIRED","--wheel requires --sha256",2)
        result=update_from_local_wheel(Path(args.wheel),args.sha256,__version__,yes=args.yes)
    else:
        if args.sha256:
            raise EgoError("INVALID_UPDATE","--sha256 requires --wheel",2)
        result=self_update(__version__,apply=not args.check,yes=args.yes)
    mode="all" if args.sync_all else "project" if args.sync else None
    if mode is None and not args.check and sys.stdin.isatty() and sys.stdout.isatty():
        # Interactive: ask which pending project configs to re-render. Read by the code now on disk.
        pending=_pending_projects(self_argv())
        if pending:
            mode=_ask_config_sync(project,pending)
            if mode=="none":mode=None
    if mode and not args.check:
        import subprocess
        # Projects are synced whether or not a newer version was just installed: they may lag the CLI.
        # A new process runs the code now on disk; no stale templates.
        target=["--all"] if mode=="all" else ["--root",str(project)]
        p=subprocess.run([*self_argv(),"sync",*target,"--apply"],capture_output=True,text=True,timeout=300)
        try:synced=json.loads(p.stdout)
        except ValueError:synced=None
        if synced is None or (p.returncode and mode!="all"):
            raise EgoError("PROJECT_SYNC_FAILED",p.stderr[-1600:] or p.stdout[-1600:],3)
        if mode=="all":result["projects_sync"]=synced
        else:
            synced.pop("wanted",None);result["project_sync"]=synced
        _print(result)
        return 5 if p.returncode else 0
    _print(result)
    return 0


def _pending_projects(argv: list[str]) -> list[dict]:
    """Registered projects whose generated config is out of date (or blocked by a user edit)."""
    import subprocess
    p=subprocess.run([*argv,"projects","list"],capture_output=True,text=True,timeout=120)
    try:rows=json.loads(p.stdout)["projects"]
    except (ValueError,KeyError,TypeError):return []
    return [r for r in rows if isinstance(r,dict) and r.get("status") in ("CHANGES_PENDING","BLOCKED")]


def _ask_config_sync(root: Path, pending: list[dict], *, read=None) -> str:
    """Choose the scope of the config re-render after an update: 'project', 'all' or 'none' (default)."""
    read=read or sys.stdin.readline
    here=str(root.resolve())
    options=[]
    current=[r for r in pending if r.get("root")==here and r.get("status")=="CHANGES_PENDING"]
    if current:
        options.append(("project",f"差分だけ適用: このプロジェクトのみ ({here})。変更されたファイルだけ書き換えます"))
    options.append(("all",f"全て適用: 登録済みの {len(pending)} 件すべて。競合のあるプロジェクトは変更せずスキップします"))
    options.append(("none","適用しない: 後で `ego sync --all --apply` で適用できます"))
    sys.stderr.write("\n=== ego 設定の適用 ===\n適用待ちのプロジェクト:\n")
    for r in pending:
        conflicts=r.get("conflicts") or []
        note=f" 競合: {', '.join(conflicts)}" if conflicts else ""
        sys.stderr.write(f"  - {r.get('root')} [{r.get('status')}] 変更 {r.get('changed_count',0)} 件{note}\n")
    for index,(_,label) in enumerate(options,1):sys.stderr.write(f"  {index}. {label}\n")
    default=len(options)
    while True:
        sys.stderr.write(f"番号を入力 [Enter: {default} 適用しない]: ");sys.stderr.flush()
        try:answer=read().strip()
        except (OSError,ValueError):return "none"
        if not answer or answer.lower() in ("q","quit"):return "none"
        if answer.isdecimal() and 1<=int(answer)<=len(options):return options[int(answer)-1][0]
        sys.stderr.write("無効な選択です。\n")


def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="ego",description="Standalone CLI-first ego harness")
    p.add_argument("--version",action="version",version=f"ego {__version__}")
    sub=p.add_subparsers(dest="command",required=True)
    def common(sp):sp.add_argument("--root",default=".")
    sp=sub.add_parser("apply",help="Apply or refresh harness installation for project")
    sp.add_argument("--root",help="Project root (default CWD)")
    sp.add_argument("--dry-run",action="store_true")
    sp.add_argument("--check",action="store_true")
    sp.add_argument("--hosts")
    sp.add_argument("--profile",choices=["minimal","balanced","full"])
    sp.add_argument("--graph",choices=["codegraph","graphify","none"])
    sp.add_argument("--knowledge","--memory",dest="knowledge",choices=["file","alter"],help="Where project knowledge lives: file (.ego/knowledge) or alter (alter MCP); --memory is a deprecated alias")
    sp.add_argument("--install-alter-skills",action="store_true",help="Install external alter-skills from github:jumtra/alter-skills")
    sp.add_argument("--detect-tests",action="store_true",help="Detect candidate test commands without running tests")
    sp.add_argument("--runtime",choices=["native","managed"],help="Default runtime; per-task runtime remains configurable")
    sp.add_argument("--policy",choices=["strict","relaxed"],help="Policy mode (strict: safe; relaxed: default, loose mode allowing git push and edits)")
    sp.add_argument("--models",help="Default models JSON mapping, e.g. '{\"claude\":\"sonnet\"}'")
    sp.add_argument("--roles",help="Per-role host/model/effort JSON, e.g. '{\"reviewer\":{\"host\":\"agy\",\"effort\":\"high\"}}'")
    sp.add_argument("--clear-suggest-tokens",type=int,metavar="N",help="At N context tokens the Stop hook (Claude Code, Codex, AGY, OpenCode plugin) has the session checkpoint and suggests /clear (0 = off)")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--interactive",action="store_true",help="Always ask for choices, even when stdin is redirected")
    mode.add_argument("--non-interactive",action="store_true",help="Never prompt (for scripts and CI)")
    sp.set_defaults(func=_cmd_apply)
    sp=sub.add_parser("status",help="Show CWD project harness drift/installation")
    sp.add_argument("--root",help="Project root (default CWD)")
    sp.set_defaults(func=_cmd_status)
    sp=sub.add_parser("rollback",help="Roll back files applied to this project")
    sp.add_argument("--root",help="Project root (default CWD)")
    sp.add_argument("snapshot",nargs="?",help="Optional snapshot ID to rollback to")
    sp.set_defaults(func=_cmd_rollback)
    sp=sub.add_parser("uninstall",help="Uninstall ego files from project or remove CLI")
    common(sp)
    sp.add_argument("--cli",action="store_true",help="Uninstall global ego CLI symlinks (~/.local/bin)")
    sp.add_argument("--yes","-y",action="store_true",help="Confirm uninstall without prompt")
    sp.set_defaults(func=_cmd_uninstall)
    init=sub.add_parser("init",help="Preview/create minimal portable harness (dry-run default)");common(init)
    init.add_argument("--profile",choices=["minimal","balanced","full"])
    init.add_argument("--hosts",help="Comma-separated claude,codex,opencode,agy")
    init.add_argument("--graph",choices=["codegraph","graphify","none"])
    init.add_argument("--knowledge","--memory",dest="knowledge",choices=["file","alter"],help="Where project knowledge lives: file (.ego/knowledge) or alter (alter MCP); --memory is a deprecated alias")
    init.add_argument("--detect-tests",action="store_true",help="Add verified test command candidates to config")
    init.add_argument("--apply",action="store_true");init.set_defaults(func=_cmd_init)
    sp=sub.add_parser("sync",help="Re-render host configurations");common(sp);sp.add_argument("--apply",action="store_true")
    sp.add_argument("--all",action="store_true",help="Every registered project (see 'ego projects list'), each with its own saved config");sp.set_defaults(func=_cmd_sync)
    sp=sub.add_parser("projects",help="Projects ego is applied to: installed version and drift of each")
    s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("list")
    r=s.add_parser("add",help="Register already-applied projects");r.add_argument("paths",nargs="+")
    r=s.add_parser("remove",help="Forget projects (their files are untouched)");r.add_argument("paths",nargs="+")
    r=s.add_parser("scan",help="Register every applied project found under a directory");r.add_argument("dir");r.add_argument("--depth",type=int,default=4)
    sp.set_defaults(func=_cmd_projects)
    sp=sub.add_parser("doctor");common(sp);sp.set_defaults(func=_cmd_doctor)
    sp=sub.add_parser("hosts",help="Inspect installed host CLI versions and supported flags");common(sp)
    s=sp.add_subparsers(dest="action",required=True)
    for name in ("inspect","refresh"):
        q=s.add_parser(name);q.add_argument("--host",choices=["claude","codex","opencode","agy"])
    sp.set_defaults(func=_cmd_hosts)
    sp=sub.add_parser("models",help="List or refresh host model catalogs; no refresh during Fast work");common(sp)
    s=sp.add_subparsers(dest="action",required=True)
    q=s.add_parser("list");q.add_argument("--host",choices=["claude","codex","opencode","agy"]);q.add_argument("--no-refresh",action="store_true")
    q=s.add_parser("refresh");q.add_argument("--host",choices=["claude","codex","opencode","agy"])
    sp.set_defaults(func=_cmd_models)
    sp=sub.add_parser("update",help="Update ego itself: fast-forward a git checkout, or install the verified latest GitHub Release wheel");common(sp)
    sp.add_argument("--check",action="store_true",help="Check availability only; do not install")
    sp.add_argument("--yes",action="store_true",help="Approve verified update in non-interactive mode")
    sp.add_argument("--wheel",help="Offline wheel upgrade from an explicitly trusted local file")
    sp.add_argument("--sha256",help="Expected SHA256 digest for --wheel")
    sp.add_argument("--sync",action="store_true",help="Then re-render the --root project with its saved config")
    sp.add_argument("--sync-all",action="store_true",help="Then re-render every registered project, each with its own saved config")
    sp.set_defaults(func=_cmd_update)
    sp=sub.add_parser("work");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("native");r=s.add_parser("status");r.add_argument("run_id");r.add_argument("--verbose",action="store_true",help="Print the full run record")
    r=s.add_parser("cancel");r.add_argument("run_id",help="Run ID")
    r=s.add_parser("plan");r.add_argument("--goal",required=True);r.add_argument("--host",default="mock");r.add_argument("--scope");r.add_argument("--out");r.add_argument("--execute",action="store_true")
    r=s.add_parser("alter-plan",help="Hand a plan to alter for node editing, then pull the approved plan");ap=r.add_subparsers(dest="plan_action",required=True)
    ac=ap.add_parser("push");ac.add_argument("--task",required=True);ac.add_argument("--slug")
    ac=ap.add_parser("wait");ac.add_argument("plan_id");ac.add_argument("--interval",type=float,default=5);ac.add_argument("--timeout",type=float,default=0)
    ac=ap.add_parser("pull");ac.add_argument("plan_id");ac.add_argument("--out",required=True);ac.add_argument("--run-id",required=True)
    ac=ap.add_parser("sync");ac.add_argument("run_id");ac.add_argument("--task",required=True)
    r=s.add_parser("watch",help="Stream a run's progress events (JSON lines) until it ends");r.add_argument("run_id");r.add_argument("--after",type=int,default=0)
    for name in ("run","resume"):
        r=s.add_parser(name);r.add_argument("--task",required=True)
        r.add_argument("--host",choices=["mock","claude","codex","opencode","agy"],default="mock" if name=="run" else None)
        r.add_argument("--apply",action="store_true");r.add_argument("--approve",action="store_true");r.add_argument("--trust-cli",action="store_true")
        r.add_argument("--hosts", help="Comma-separated task default hosts; per-node settings take precedence")
        r.add_argument("--strategy",choices=["single","fallback","compare"])
        r.add_argument("--model", help="Single-host explicit model ID; omit for native host default")
        r.add_argument("--models",help='JSON object of host to model overrides, e.g. {"claude":"sonnet"}')
        r.add_argument("--autonomy",choices=["auto","safe","bypass"],default="auto")
        r.add_argument("--attest-external-sandbox",action="store_true",help="Assert externally hardened OS sandbox; needed for bypass mode")
        r.add_argument("--worktree",action="store_true",default=None,help="Execute task in an isolated Git worktree")
        r.add_argument("--keep-worktree",dest="cleanup_worktree",action="store_false",default=None,help="Keep the Git worktree after task completion")
        r.add_argument("--max-run-tokens",type=int,default=None,
                       help="Override max_run_tokens for this invocation (0 = unlimited, or >= 1000)")
        r.add_argument("--verbose",action="store_true",help="Print the full run record instead of the compact summary")
        if name=="resume":
            r.add_argument("run_id")
            r.add_argument("--node",action="append",dest="nodes",help="Limit resume routing overrides (--host/--model/...) to these node IDs (repeatable or comma-separated)")
        else:
            r.add_argument("--run-id",help="Fixed run ID, so `ego work watch <id>` can follow the run")
            r.add_argument("--from",dest="seed_from",metavar="RUN_ID",
                           help="Continue a failed run with a corrected task: start from its verified changes and skip its verified, unchanged agent nodes")
    sp.set_defaults(func=_cmd_work)
    sp=sub.add_parser("state",help="Enforce retention limits on .ego/state and related growth paths");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("prune",help="Remove entries beyond the retention limits now (also runs automatically)").set_defaults(func=_cmd_state)
    sp=sub.add_parser("usage",help="Model usage ledger: cost split between agent bodies (agent) and ego control (overhead)");common(sp);s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("summary",help="Totals by category and caller over the last --days days");r.add_argument("--days",type=int,default=7)
    r.set_defaults(func=_cmd_usage)
    sp=sub.add_parser("memory");common(sp);s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("add");r.add_argument("text");r.add_argument("--tags",action="append");r.add_argument("--evidence",action="append")
    r=s.add_parser("search");r.add_argument("query");s.add_parser("list");r=s.add_parser("delete");r.add_argument("id")
    r=s.add_parser("install-skills",help="Install external alter-skills from github:jumtra/alter-skills")
    r.add_argument("--scope",choices=["global","project"],default="global")
    r.add_argument("--clients",help="Comma-separated claude,codex,opencode,agy (default: configured hosts)")
    r.add_argument("--no-agents",action="store_true",help="Skip installing agent definitions")
    r.add_argument("--dry-run",action="store_true")
    sp.set_defaults(func=_cmd_memory)
    sp=sub.add_parser("knowledge",help="Project knowledge (knowledge = \"file\"): an index plus one file per topic");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("index",help="Print .ego/knowledge/index.md: read it first, then open only the topics you need")
    r=s.add_parser("search",help="Topics whose summary or keywords match the words");r.add_argument("query",nargs="+");r.add_argument("--limit",type=int,default=5)
    r=s.add_parser("get",help="Print the topic files");r.add_argument("slug",nargs="+")
    r=s.add_parser("add",help="Record verified knowledge: merged into the matching topic when the index shows one, else a new topic file")
    r.add_argument("text");r.add_argument("--topic",help="Force this topic (slug); created when it does not exist");r.add_argument("--summary",help="One-line topic summary for the index")
    r.add_argument("--keyword",action="append",help="Index keyword for the topic (repeatable)")
    s.add_parser("facts",help="List learned environment facts")
    r=s.add_parser("forget",help="Forget a learned environment fact so it is never injected again");r.add_argument("id")
    sp.set_defaults(func=_cmd_knowledge)
    sp=sub.add_parser("session",help="Small durable task checkpoints and manual interactive session rotation");common(sp)
    s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("checkpoint",help="Save a compact handoff at a verified task boundary")
    r.add_argument("--task-id",required=True);r.add_argument("--goal",required=True)
    r.add_argument("--completed",action="append");r.add_argument("--next",dest="next_steps",action="append")
    r.add_argument("--decision",action="append");r.add_argument("--blocker",action="append")
    r.add_argument("--evidence",action="append")
    r.add_argument("--status",choices=["IN_PROGRESS","VERIFIED","COMPLETED","BLOCKED"],default="IN_PROGRESS")
    r.add_argument("--kind",choices=["ongoing","plan_ready","plan_resume","completed"],default=None,
                   help="Checkpoint profile: ongoing (in-progress work), plan_ready (plan finished), plan_resume (planning paused), completed. Omitted: auto-detected (completed from status; plan_ready when the task JSON exists and no run has started; plan_resume when planning paused; else previous kind or ongoing)")
    r.add_argument("--host",choices=["claude","codex","opencode","agy"],help="CLI saving this checkpoint (default: detected from the environment)")
    r=s.add_parser("list",help="List saved checkpoints, newest first (open ones unless --all)");r.add_argument("--all",action="store_true")
    r=s.add_parser("status");r.add_argument("task_id")
    r=s.add_parser("resume",help="Print /clear instructions and a memory-based restart prompt")
    r.add_argument("task_id",nargs="?",help="Default: the latest open interactive checkpoint")
    r.add_argument("--host",choices=["claude","codex","opencode","agy"],help="CLI being cleared (default: detected, else claude)")
    r.add_argument("--from",dest="from_host",choices=["claude","codex","opencode","agy"],
                   help="Continue here, without clearing, the latest checkpoint that CLI saved")
    r=s.add_parser("handoff",help="Continue a checkpoint in another CLI: print (or --launch) its interactive start command")
    r.add_argument("task_id",nargs="?",help="Default: the latest open interactive checkpoint")
    r.add_argument("--to",required=True,choices=["claude","codex","opencode","agy"])
    r.add_argument("--from",dest="from_host",choices=["claude","codex","opencode","agy"],help="Take the latest checkpoint that CLI saved")
    r.add_argument("--launch",action="store_true",help="Start the target CLI in this terminal")
    r=s.add_parser("analyze",help="Run the analyzer host headless on a recorded session and attach the analysis (alter_analysis)")
    r.add_argument("session_id")
    sp.set_defaults(func=_cmd_session)
    sp=sub.add_parser("run",help="Start an interactive CLI that ego checkpoints, clears and resumes by itself at the context limit");common(sp)
    sp.add_argument("host",choices=["claude","codex","opencode","agy"])
    sp.add_argument("--clear-tokens",type=int,metavar="N",help="Context tokens at which to rotate (default: clear_suggest_tokens)")
    sp.add_argument("--yolo",action=argparse.BooleanOptionalAction,default=None,help="Start the CLI with its skip-permissions switch (default: run_yolo in .ego/config.toml)")
    sp.add_argument("args",nargs="*",help="Arguments for the CLI itself, after --")
    sp.set_defaults(func=_cmd_run)
    sp=sub.add_parser("code");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("doctor")
    r=s.add_parser("search");r.add_argument("query")
    r=s.add_parser("symbol");r.add_argument("name")
    r=s.add_parser("impact");r.add_argument("name")
    r=s.add_parser("affected");r.add_argument("files",nargs="+")
    r=s.add_parser("index");r.add_argument("--execute",action="store_true",help="Index using installed provider after manual review")
    sp.set_defaults(func=_cmd_code)
    sp=sub.add_parser("hook");common(sp);sp.add_argument("event",choices=["pre-tool","stop","session-start","session-end","user-prompt","statusline","context-probe"]);sp.add_argument("--host",choices=["claude","codex","opencode","agy"],default="claude",help="Host CLI whose hook payload arrives on stdin");sp.set_defaults(func=_cmd_hook)
    sp=sub.add_parser("tools",help="Propose, apply, or restore reversible MCP tool pruning");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("propose",help="List unused mcp__ tools eligible for pruning")
    r=s.add_parser("apply",help="Apply proposed mcp__ tool pruning to host settings");r.add_argument("--yes",action="store_true");r.add_argument("--host",default="claude")
    r=s.add_parser("restore",help="Restore a pruned tool and pin it");r.add_argument("tool")
    sp.set_defaults(func=_cmd_tools)
    sp=sub.add_parser("fold",help=argparse.SUPPRESS);common(sp);sp.add_argument("--session",required=True);sp.add_argument("--transcript",required=True);sp.set_defaults(func=_cmd_fold)
    return p

def _correct_update_typo(args_list:list[str],parser:argparse.ArgumentParser)->None:
    """Normalize close typos of 'update' (e.g. 'udpate') to 'update'."""
    if not args_list or args_list[0].startswith("-"):
        return
    cmd=args_list[0]
    choices:set[str]=set()
    for action in parser._actions:
        if isinstance(action,argparse._SubParsersAction):
            choices.update(action.choices.keys())
    if cmd in choices or "update" not in choices:
        return
    if difflib.get_close_matches(cmd,["update"],n=1,cutoff=0.75):
        print(f"ego: assuming 'update' for typo '{cmd}'",file=sys.stderr)
        args_list[0]="update"


def main(argv:list[str]|None=None)->int:
    parser=build_parser()
    # Accept --root both directly after the command and after a nested action.
    # Relocation remains explicit; it does not rewrite user prompt arguments.
    args_list=list(argv if argv is not None else sys.argv[1:])
    # After "--" the arguments belong to the wrapped CLI. Split them off before argparse: on Python 3.11
    # argparse rejects them when a positional with nargs="*" precedes "--" (`ego run codex --yolo -- --model x`).
    passthrough:list[str]=[]
    if "--" in args_list:
        cut=args_list.index("--")
        passthrough=args_list[cut+1:]
        args_list=args_list[:cut]
    root_eq = next((i for i, a in enumerate(args_list) if a.startswith("--root=")), None)
    if root_eq is not None:
        item = [args_list.pop(root_eq)]
        if args_list: args_list[1:1] = item
        else: args_list.extend(item)
    elif "--root" in args_list:
        idx=args_list.index("--root")
        if idx+1>=len(args_list):
            parser.error("--root requires a path")
        pair=args_list[idx:idx+2];del args_list[idx:idx+2]
        if args_list:args_list[1:1]=pair
        else:args_list.extend(pair)
    _correct_update_typo(args_list,parser)
    args=parser.parse_args(args_list)
    if passthrough:
        if not hasattr(args,"args"):parser.error(f"unrecognized arguments: -- {' '.join(passthrough)}")
        args.args=passthrough
    try:return args.func(args)
    except EgoError as exc:
        print(json.dumps({"status":"ERROR","reason_code":exc.code,"message":str(exc)},ensure_ascii=False),file=sys.stderr)
        return exc.exit_code
    except (OSError,KeyError,ValueError,TypeError,json.JSONDecodeError) as exc:
        print(json.dumps({"status":"ERROR","reason_code":"INFRA_ERROR","message":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 5

if __name__=="__main__":raise SystemExit(main())
