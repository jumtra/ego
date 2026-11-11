"""CLI-first entrypoint. Optimization modules are never imported on the normal path."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
from . import __version__
from .util import EgoError, read_json, write_json, now

def _print(data):print(json.dumps(data,indent=2,ensure_ascii=False,default=str))
def _root(args):return Path(getattr(args,"root", ".")).resolve()

def _project_target(args) -> Path:
    target = Path(getattr(args, "root", None) or Path.cwd()).resolve()
    if target in (Path.home(), Path("/")):
        raise EgoError("INVALID_PROJECT_ROOT", f"Cannot apply harness directly to {target}", 2)
    return target

def _cmd_apply(args):
    """CWD-first installation; guided TTY choices or deterministic non-interactive use."""
    from .config import load_config
    from .install import installation_plan, apply_install
    root = _project_target(args)
    cfg = load_config(root)
    if args.hosts is not None: cfg.hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if args.profile is not None: cfg.profile = args.profile
    if args.graph is not None: cfg.code_intelligence = args.graph
    if args.memory is not None: cfg.memory = args.memory
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
    # No extra import or stdin inspection on scripted apply/check/dry-run.
    from .apply_wizard import should_prompt, select_options, confirm_apply, WizardCancelled
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
        from .detect import detect
        cfg.tests = detect(root)["test_commands"]
    plan = installation_plan(root, cfg)
    changes = [c for c in plan["changes"] if c["action"] != "unchanged"]
    status = "BLOCKED" if plan["conflicts"] else "UP_TO_DATE" if not changes else "CHANGES_PENDING"
    summary = {"status": status, "root": str(root), "harness_version": __version__,
               "profile": cfg.profile, "policy_mode": cfg.policy_mode, "hosts": cfg.hosts, "models": cfg.models,
               "code_intelligence": cfg.code_intelligence, "memory": cfg.memory,
               "runtime_mode": cfg.runtime_mode, "tests_detected": bool(detect_tests),
               "changes": changes, "conflicts": plan["conflicts"],
               "changed_count": len(changes)}
    if args.dry_run or args.check or plan["conflicts"]:
        _print(summary)
        return 5 if args.check and (changes or plan["conflicts"]) else 0
    if not changes:
        _print(summary)
        return 0
    if interactive:
        try:
            if not confirm_apply(cfg, changes, root):
                _print({**summary, "status": "CANCELLED"})
                return 0
        except WizardCancelled:
            _print({**summary, "status": "CANCELLED"})
            return 0
    result = apply_install(root, cfg)
    _print({**summary, "status": result["status"], "snapshot_id": result["snapshot_id"]})
    return 0

def _cmd_status(args):
    from .config import load_config
    from .install import installation_plan, OWNERSHIP
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
    from .install import rollback, OWNERSHIP
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
    from .install import uninstall_project, uninstall_cli
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
    from .config import Config, load_config
    from .install import installation_plan,apply_install
    cfg=load_config(_root(args))
    if args.profile:cfg.profile=args.profile
    if args.hosts:cfg.hosts=args.hosts.split(",")
    if args.graph:cfg.code_intelligence=args.graph
    if args.memory:cfg.memory=args.memory
    cfg.validate()
    from .detect import detect
    detection=detect(_root(args))
    if args.detect_tests and not cfg.tests:
        cfg.tests=detection["test_commands"]
    result=apply_install(_root(args),cfg) if args.apply else installation_plan(_root(args),cfg)
    if not args.apply:result.pop("wanted",None)
    result["detection"]=detection
    _print(result);return 0

def _cmd_sync(args):
    if args.all:
        from .projects import sync_all
        result=sync_all(apply=args.apply)
        _print(result)
        return 5 if any(k in result["counts"] for k in ("ERROR","BLOCKED")) else 0
    from .config import load_config
    from .install import installation_plan,apply_install
    cfg=load_config(_root(args))
    result=apply_install(_root(args),cfg) if args.apply else installation_plan(_root(args),cfg)
    if not args.apply:result.pop("wanted",None)
    _print(result);return 0

def _cmd_projects(args):
    from .projects import list_projects,register,unregister,scan,project_info,registry_path
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
    from .config import load_config
    from .hosts import capability
    from .codeintel import health
    from .registry import resolve
    from .author import lint
    from .policy import sandbox_diagnostics
    cfg=load_config(_root(args))
    results={"version":__version__,"hosts":{x:capability(x).__dict__ for x in cfg.hosts},
             "code_intelligence":health(cfg.code_intelligence),"memory":cfg.memory,
             "sandbox":sandbox_diagnostics(),"skills":lint(_root(args)),
             "modules":resolve(_root(args),["core","eval","author","evolve","memory-"+cfg.memory]+([cfg.code_intelligence] if cfg.code_intelligence!="none" else []))}
    _print(results);return 0

def _cmd_modules(args):
    from .registry import discover,resolve,scaffold
    if args.action=="list":_print(discover(_root(args)))
    elif args.action=="resolve":_print(resolve(_root(args),args.modules.split(",")))
    else:_print(scaffold(_root(args),args.name,apply=args.apply))
    return 0

def _cmd_migrate(args):
    from .install import legacy_plan,remove_legacy,rollback,restore_legacy
    root=_root(args)
    if args.action=="plan":_print({"legacy":legacy_plan(root)});return 0
    if args.action=="rollback":_print(rollback(root,args.snapshot));return 0
    if args.action=="restore-legacy":
        if not args.approve:raise EgoError("APPROVAL_REQUIRED","Explicit --approve required")
        _print(restore_legacy(root,args.snapshot));return 0
    if args.action=="remove-legacy":
        if not args.approve:raise EgoError("APPROVAL_REQUIRED","Explicit --approve required")
        _print(remove_legacy(root,read_json(Path(args.hashes))));return 0

def _cmd_work(args):
    from .work import execute,status,cancel,compact_result
    root=_root(args)
    # Default output is a compact summary: the full record holds a per-file
    # snapshot and raw worker output, which would land in the caller's context.
    shape=(lambda r:r) if getattr(args,"verbose",False) else compact_result
    if args.action=="status":_print(shape(status(root,args.run_id)));return 0
    if args.action=="cancel":
        from .loop import ledger_path,cancel_loop
        _print(cancel_loop(root,args.run_id) if ledger_path(root,args.run_id).exists() else cancel(root,args.run_id));return 0
    if args.action=="loop-status":
        from .loop import loop_status
        _print(loop_status(root,args.loop_id));return 0
    if args.action=="loop-apply":
        from .loop import apply_loop
        result=apply_loop(root,args.loop_id,approve=args.approve)
        _print(result);return 0 if result["status"]=="COMPLETED" else 5
    if args.action=="loop":
        from .loop import run_loop,SUCCESS
        checks=[]
        for raw in args.done_check:
            try:argv=json.loads(raw) if raw.lstrip().startswith("[") else __import__("shlex").split(raw)
            except ValueError as exc:raise EgoError("INVALID_DONE_CHECK",str(exc),2) from exc
            if not isinstance(argv,list) or not argv or not all(isinstance(x,str) for x in argv):
                raise EgoError("INVALID_DONE_CHECK","Each --done-check is a command or a JSON argv array",2)
            checks.append(argv)
        result=run_loop(root,args.goal,done_checks=checks,host=args.host,
                        scope=args.scope.split(",") if args.scope else None,max_iterations=args.max_iterations,
                        max_budget_usd=args.max_budget_usd,max_wall_seconds=args.max_wall_seconds,
                        trust_cli=args.trust_cli,execute_planner=args.execute,apply=args.apply,approve=args.approve,
                        autonomy=args.autonomy,isolated=args.attest_external_sandbox,plan_first=args.plan_first)
        _print(result);return 0 if result["status"] in SUCCESS else 5
    if args.action=="plan":
        from .planner import plan_work
        result=plan_work(root,args.goal,args.host,args.scope.split(",") if args.scope else None,execute=args.execute)
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
    if any(getattr(args,attr,None) for attr in ("hosts","strategy","model","models")):
        task=dict(task)
        defaults=dict(task.get("defaults",{}))
        if args.hosts:defaults["hosts"]=[h.strip() for h in args.hosts.split(",")]
        if args.strategy:defaults["strategy"]=args.strategy
        if args.model:defaults["model"]=args.model
        if args.models:
            try:defaults["models"]=json.loads(args.models)
            except json.JSONDecodeError as exc:raise EgoError("INVALID_MODEL",str(exc),2) from exc
        task["defaults"]=defaults
    result=execute(root,task,host=args.host,apply=args.apply,approve=args.approve,trust_cli=args.trust_cli,
                   run_id=args.run_id if args.action=="resume" else None,resume=args.action=="resume",
                   autonomy=args.autonomy,isolated=args.attest_external_sandbox,
                   worktree=args.worktree,cleanup_worktree=args.cleanup_worktree,planner_usage=planner_usage)
    _print(shape(result));return 0 if result["final_status"] in ("PASSED","COMPARISON_READY") else 5

def _cmd_context(args):
    from .context import audit
    result=audit(_root(args));_print(result)
    return 0 if result["status"]=="PASS" else 5

def _cmd_eval(args):
    from .evals import run_eval,compare
    if args.action=="compare":
        result=compare(read_json(Path(args.baseline)),read_json(Path(args.candidate)));_print(result)
        return 0 if result["promotable"] else 5
    res=run_eval(_root(args),Path(args.suite),host=args.host,trials=args.trials,model=args.model,split=args.split,timeout=args.timeout)
    _print({"run_id":res["run_id"],"all_passed":res["all_passed"],"summary":res["summary"],"elapsed_seconds":res["elapsed_seconds"]})
    return 0 if res["all_passed"] else 5

def _cmd_author(args):
    from .author import lint,plan,gate
    from .candidates import new_candidate,promote,rollback_promotion
    root=_root(args)
    if args.action=="lint":res=lint(root);_print(res);return 0 if res["status"]=="PASS" else 5
    if args.action=="plan":_print(plan(root,args.skill,args.field,args.goal));return 0
    if args.action=="candidate":
        replacement=Path(args.replacement_file).read_text(encoding="utf8")
        rec=new_candidate(root,args.skill,args.field,replacement,args.goal)
        write_json(root/".ego"/"state"/"candidates"/(rec["candidate_id"]+".json"),rec)
        _print(rec);return 0
    if args.action=="gate":
        res=gate(root,read_json(Path(args.candidate)),Path(args.suite),args.host,allow_mock=args.allow_mock_test,model=args.model)
        if args.out:write_json(Path(args.out),res)
        _print({k:v for k,v in res.items() if k!="report"});return 0 if res["status"]=="PASS" else 5
    if args.action=="promote":
        if not args.approve:raise EgoError("APPROVAL_REQUIRED","Explicit --approve required")
        _print(promote(root,read_json(Path(args.candidate)),approval_ref=args.approval_ref,gate=read_json(Path(args.gate)),allow_mock=args.allow_mock_test))
        return 0
    if args.action=="rollback":_print(rollback_promotion(root,args.promotion_id));return 0

def _cmd_evolve(args):
    from .evolve import observe,propose,lesson
    root=_root(args)
    if args.action=="observe":_print(observe(root,args.message,args.outcome,args.task_class,args.evidence or []));return 0
    if args.action=="propose":
        replacement=Path(args.replacement_file).read_text(encoding="utf8") if args.replacement_file else None
        _print(propose(root,args.observation_id,args.skill,args.field,args.host,replacement=replacement));return 0
    if args.action=="lesson":_print(lesson(root,args.evidence,args.summary,args.host,args.task_class));return 0

def _cmd_optimize(args):
    from . import optimize as opt   # LAZY import -- never happens in Fast mode
    from .config import load_opt,save_opt
    root=_root(args)
    if args.action=="status":_print(opt.status(root));return 0
    if args.action=="doctor":_print(opt.doctor(root,args.host));return 0
    if args.action=="configure":
        cfg=load_opt(root)
        if args.mode:cfg.mode=args.mode
        if args.profile:cfg.profile=args.profile
        if args.cost_policy:cfg.cost_policy=args.cost_policy
        if args.max_candidates is not None:cfg.max_candidates=args.max_candidates
        if args.max_probe_runs is not None:cfg.max_probe_runs=args.max_probe_runs
        if args.max_rounds is not None:cfg.max_rounds=args.max_rounds
        if args.max_wall_seconds is not None:cfg.max_wall_seconds=args.max_wall_seconds
        cfg.validate()
        if args.apply:save_opt(root,cfg)
        _print({"mode":cfg.mode,"profile":cfg.profile,"cost_policy":cfg.cost_policy,"applied":args.apply});return 0
    if args.action=="disable":
        cfg=load_opt(root);cfg.mode="off"
        if args.apply:save_opt(root,cfg)
        _print({"mode":"off","applied":args.apply});return 0
    if args.action=="plan":_print(opt.plan(root,Path(args.fixture),args.host,args.skill,args.field,model=args.model));return 0
    if args.action=="propose":
        repl=Path(args.replacement_file).read_text(encoding="utf8") if args.replacement_file else None
        _print(opt.propose(root,args.experiment,replacement=repl,goal=args.goal,execute=args.execute,spending_ref=args.spend_approval,trust_local=args.trusted_local));return 0
    if args.action=="run":_print(opt.run(root,args.experiment,execute=args.execute,spending_ref=args.spend_approval,trust_local=args.trusted_local));return 0
    if args.action=="report":_print(opt.report(root,args.experiment));return 0
    if args.action=="promote":
        if not args.approve:raise EgoError("APPROVAL_REQUIRED","--approve required")
        _print(opt.promote_experiment(root,args.experiment,approval_ref=args.approval_ref,author_suite=Path(args.suite),host=args.host));return 0
    if args.action=="full":
        _print(opt.full(root,Path(args.fixture),args.host,args.skill,args.field,args.rounds,execute=args.execute,
                        spending_ref=args.spend_approval,trust_local=args.trusted_local));return 0

def _cmd_memory(args):
    from .config import load_config
    from .memory import memory_provider
    root=_root(args);p=memory_provider(root,load_config(root).memory)
    if args.action=="add":_print(p.add(args.text,tags=args.tags or [],evidence=args.evidence or []));return 0
    if args.action=="search":_print(p.search(args.query));return 0
    if args.action=="list":
        if not hasattr(p,"list"):raise EgoError("UNSUPPORTED","Memory provider does not support list")
        _print(p.list());return 0
    if args.action=="delete":
        if not hasattr(p,"delete"):raise EgoError("UNSUPPORTED","Memory provider does not support delete")
        _print({"deleted":p.delete(args.id)});return 0

def _cmd_session(args):
    from .session import (save_checkpoint, load_checkpoint, rotation_instructions, arm_auto_resume,
                          detect_host, list_checkpoints, resolve_task, handoff, pickup)
    root = _root(args)
    if args.action == "checkpoint":
        result = save_checkpoint(
            root, args.task_id, goal=args.goal, completed=args.completed,
            next_steps=args.next_steps, decisions=args.decision,
            blockers=args.blocker, evidence=args.evidence,
            status=args.status, source="interactive", host=args.host or detect_host(),
        )
        # Acknowledge only: the caller just supplied these notes, echoing them back costs context twice.
        result = {"task_id": result["task_id"], "status": result["status"], "revision": result["revision"],
                  "path": result["path"], "auto_resume_after_clear": arm_auto_resume(root, args.task_id, args.status)}
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
            os.execvp(result["launch_argv"][0], result["launch_argv"])
    elif args.from_host:
        result = pickup(root, args.task_id, args.from_host)
    else:
        result = rotation_instructions(root, resolve_task(root, args.task_id), args.host or detect_host() or "claude")
    _print(result)
    return 0


def _cmd_run(args):
    from .config import load_config
    from .ptyrun import run_host
    root=_root(args)
    cfg=load_config(root)
    threshold=args.clear_tokens if args.clear_tokens is not None else cfg.clear_suggest_tokens
    return run_host(args.host,args.args,root,threshold,yolo=cfg.run_yolo if args.yolo is None else args.yolo)


def _cmd_code(args):
    from .codeintel import health,search,symbol,index,impact,affected_tests
    from .config import load_config
    provider=load_config(_root(args)).code_intelligence
    res={"doctor":lambda:health(provider),"search":lambda:search(_root(args),args.query,provider),
         "symbol":lambda:symbol(_root(args),args.name,provider),"index":lambda:index(_root(args),provider,execute=args.execute),
         "impact":lambda:impact(_root(args),args.name,provider),"affected":lambda:affected_tests(_root(args),args.files,provider)}[args.action]()
    _print(res);return 0

def _cmd_tool(args):
    from .tools import available_tools,invoke_registered
    if args.action=="list":_print(available_tools(_root(args)));return 0
    _print(invoke_registered(args.name,json.loads(args.arguments),_root(args),approve_external=args.approve_external));return 0

def _cmd_hook(args):
    from .policy import check_hook_payload
    from .config import load_config
    root=_root(args)
    raw=sys.stdin.read(1024*1024)
    if args.event=="context-probe":
        # Status line of a host started by `ego run`: report its context size to the wrapper.
        try:
            from .ptyrun import STATE_ENV,record_context
            print(record_context(json.loads(raw),Path(os.environ[STATE_ENV])))
        except (ValueError,OSError,EgoError,KeyError,TypeError):print("")
        return 0
    if args.event=="statusline":
        # Prints the status line itself (plain text); any problem means an empty line.
        try:
            from .session import record_usage
            print(record_usage(json.loads(raw),root))
        except (ValueError,OSError,EgoError,KeyError,TypeError):print("")
        return 0
    if args.event in ("stop","session-start"):
        # Advisory hooks never fail the host turn: any problem means "say nothing".
        host=args.host
        try:
            from .session import adapt_output,clear_advice,clear_resume,codex_limits,normalize_payload,project_root,usage_advice
            obj=normalize_payload(host,json.loads(raw))
            root=project_root(root,obj.get("cwd"))
            info={}
            if args.event=="stop":
                cfg=load_config(root)
                # A nearly spent usage limit outranks a large context: there may be no later turn to save in.
                # Under `ego run` the wrapper saves, clears and resumes: the hooks must not ask for the same again.
                wrapped=bool(os.environ.get("EGO_RUN"))
                # Codex reports its usage limits in the session record; Claude's come from the status line.
                # OpenCode and AGY report none, so there usage_switch_percent has nothing to act on.
                limits=None
                if host=="codex":limits=codex_limits(Path(obj["transcript_path"]).expanduser()) if isinstance(obj.get("transcript_path"),str) else {}
                elif host in ("opencode","agy"):limits={}
                out=usage_advice(root,obj,cfg.usage_switch_percent,cfg.hosts,host=host,limits=limits) or ({} if wrapped else clear_advice(root,obj,cfg.clear_suggest_tokens,host=host,info=info))
            else:out={} if os.environ.get("EGO_RUN") else clear_resume(root,obj)
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


def _cmd_prompt(args):
    from .prompting import extract_reply,register_prompt,get_prompt
    root=_root(args)
    if args.action=="register":
        _print(register_prompt(root,args.name,Path(args.file).read_text(encoding="utf8"),args.role));return 0
    if args.action=="show":_print(get_prompt(root,args.name,args.version));return 0
    if args.action=="extract":_print(extract_reply(Path(args.file).read_text(encoding="utf8"),args.contract));return 0

def _cmd_events(args):
    from .events import tail
    _print(tail(_root(args),args.n));return 0

def _cmd_pr(args):
    from .github import create_pr,reviews
    if args.action=="create":_print(create_pr(_root(args),args.title,args.body,args.base,approve=args.approve));return 0
    if args.action=="reviews":_print(reviews(_root(args),args.number));return 0

def _cmd_bench(args):
    import statistics,subprocess,time,sys
    root=_root(args)
    samples=[]
    for _ in range(args.trials):
        start=time.perf_counter()
        p=subprocess.run([sys.executable,"-m","ego","work","native","--root",str(root)],capture_output=True,text=True)
        if p.returncode:raise EgoError("BENCH_FAILED",p.stderr,5)
        samples.append((time.perf_counter()-start)*1000)
    results=sorted(samples)
    _print({"metric":"ego work native no-agent dispatch","count":len(results),"p50_ms":round(statistics.median(results),3),
            "p95_ms":round(results[min(len(results)-1,int(len(results)*.95))],3),
            "baseline_note":"This measures ego CLI dispatch only, not native Claude/Codex/OpenCode task execution"})
    return 0

def _cmd_hosts(args):
    from .hostcompat import list_host_capabilities, inspect_host
    if args.host:
        _print(inspect_host(args.host, refresh=args.action=="refresh"))
    else:
        _print(list_host_capabilities(refresh=args.action=="refresh"))
    return 0


def _cmd_models(args):
    from .models import list_models,refresh_catalog
    root=_root(args)
    if args.action=="refresh":
        _print(refresh_catalog(root,hosts=[args.host] if args.host else None))
    else:
        _print(list_models(root,host=args.host,refresh_if_stale=not args.no_refresh))
    return 0


def _cmd_update(args):
    from .selfupdate import self_update, update_from_local_wheel, self_argv
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
    if (args.sync or args.sync_all) and not args.check:
        import subprocess
        # Projects are synced whether or not a newer version was just installed: they may lag the CLI.
        # A new process runs the code now on disk; no stale templates.
        target=["--all"] if args.sync_all else ["--root",str(project)]
        p=subprocess.run([*self_argv(),"sync",*target,"--apply"],capture_output=True,text=True,timeout=300)
        try:synced=json.loads(p.stdout)
        except ValueError:synced=None
        if synced is None or (p.returncode and not args.sync_all):
            raise EgoError("PROJECT_SYNC_FAILED",p.stderr[-1600:] or p.stdout[-1600:],3)
        if args.sync_all:result["projects_sync"]=synced
        else:
            synced.pop("wanted",None);result["project_sync"]=synced
        _print(result)
        return 5 if p.returncode else 0
    _print(result)
    return 0


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
    sp.add_argument("--memory",choices=["file","engram"])
    sp.add_argument("--detect-tests",action="store_true",help="Detect candidate test commands without running tests")
    sp.add_argument("--runtime",choices=["native","managed"],help="Default runtime; per-task runtime remains configurable")
    sp.add_argument("--policy",choices=["strict","relaxed"],help="Policy mode (strict: safe default; relaxed: loose mode allowing git push and edits)")
    sp.add_argument("--models",help="Default models JSON mapping, e.g. '{\"claude\":\"sonnet\"}'")
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
    init.add_argument("--memory",choices=["file","engram"])
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
    sp=sub.add_parser("modules");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("list");r=s.add_parser("resolve");r.add_argument("modules");r=s.add_parser("scaffold");r.add_argument("name");r.add_argument("--apply",action="store_true");sp.set_defaults(func=_cmd_modules)
    sp=sub.add_parser("migrate");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("plan");r=s.add_parser("rollback");r.add_argument("snapshot");r=s.add_parser("restore-legacy");r.add_argument("snapshot");r.add_argument("--approve",action="store_true");r=s.add_parser("remove-legacy");r.add_argument("--hashes",required=True);r.add_argument("--approve",action="store_true");sp.set_defaults(func=_cmd_migrate)
    sp=sub.add_parser("work");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("native");r=s.add_parser("status");r.add_argument("run_id");r.add_argument("--verbose",action="store_true",help="Print the full run record")
    r=s.add_parser("cancel");r.add_argument("run_id",help="Run ID or loop ID")
    r=s.add_parser("loop",help="Plan, execute, evaluate and replan until the done-checks pass or a limit is hit")
    r.add_argument("--goal",required=True)
    r.add_argument("--done-check",action="append",required=True,help="Command that exits 0 when the goal holds; repeatable. A command line or a JSON argv array")
    r.add_argument("--host",choices=["mock","claude","codex","opencode"],default="mock");r.add_argument("--scope")
    r.add_argument("--max-iterations",type=int,default=3);r.add_argument("--max-budget-usd",type=float);r.add_argument("--max-wall-seconds",type=int)
    r.add_argument("--execute",action="store_true",help="Allow the planning model call");r.add_argument("--trust-cli",action="store_true")
    r.add_argument("--plan-first",action="store_true",help="Call the planner before the first iteration instead of only after a failed direct attempt")
    r.add_argument("--apply",action="store_true");r.add_argument("--approve",action="store_true")
    r.add_argument("--autonomy",choices=["auto","safe","bypass"],default="auto");r.add_argument("--attest-external-sandbox",action="store_true")
    r=s.add_parser("loop-status");r.add_argument("loop_id")
    r=s.add_parser("loop-apply",help="Apply the staged result of a VERIFIED loop");r.add_argument("loop_id");r.add_argument("--approve",action="store_true")
    r=s.add_parser("plan");r.add_argument("--goal",required=True);r.add_argument("--host",default="mock");r.add_argument("--scope");r.add_argument("--out");r.add_argument("--execute",action="store_true")
    for name in ("run","resume"):
        r=s.add_parser(name);r.add_argument("--task",required=True);r.add_argument("--host",choices=["mock","claude","codex","opencode","agy"],default="mock")
        r.add_argument("--apply",action="store_true");r.add_argument("--approve",action="store_true");r.add_argument("--trust-cli",action="store_true")
        r.add_argument("--hosts", help="Comma-separated task default hosts; per-node settings take precedence")
        r.add_argument("--strategy",choices=["single","fallback","compare"])
        r.add_argument("--model", help="Single-host explicit model ID; omit for native host default")
        r.add_argument("--models",help='JSON object of host to model overrides, e.g. {"claude":"sonnet"}')
        r.add_argument("--autonomy",choices=["auto","safe","bypass"],default="auto")
        r.add_argument("--attest-external-sandbox",action="store_true",help="Assert externally hardened OS sandbox; needed for bypass mode")
        r.add_argument("--worktree",action="store_true",default=None,help="Execute task in an isolated Git worktree")
        r.add_argument("--keep-worktree",dest="cleanup_worktree",action="store_false",default=None,help="Keep the Git worktree after task completion")
        r.add_argument("--verbose",action="store_true",help="Print the full run record instead of the compact summary")
        if name=="resume":r.add_argument("run_id")
    sp.set_defaults(func=_cmd_work)
    sp=sub.add_parser("eval");common(sp);s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("run");r.add_argument("--suite",required=True);r.add_argument("--host",default="mock");r.add_argument("--trials",type=int,default=1);r.add_argument("--model");r.add_argument("--split");r.add_argument("--timeout",type=int,default=120)
    r=s.add_parser("compare");r.add_argument("baseline");r.add_argument("candidate")
    sp.set_defaults(func=_cmd_eval)
    sp=sub.add_parser("author");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("lint")
    r=s.add_parser("plan");r.add_argument("--skill",default="work");r.add_argument("--field",default="skill.description");r.add_argument("--goal",required=True)
    r=s.add_parser("candidate");r.add_argument("--skill",default="work");r.add_argument("--field",default="skill.description");r.add_argument("--replacement-file",required=True);r.add_argument("--goal",required=True)
    r=s.add_parser("gate");r.add_argument("--candidate",required=True);r.add_argument("--suite",required=True);r.add_argument("--host",default="mock");r.add_argument("--model");r.add_argument("--out");r.add_argument("--allow-mock-test",action="store_true")
    r=s.add_parser("promote");r.add_argument("--candidate",required=True);r.add_argument("--gate",required=True);r.add_argument("--approval-ref",required=True);r.add_argument("--approve",action="store_true");r.add_argument("--allow-mock-test",action="store_true")
    r=s.add_parser("rollback");r.add_argument("promotion_id")
    sp.set_defaults(func=_cmd_author)
    sp=sub.add_parser("evolve");common(sp);s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("observe");r.add_argument("message");r.add_argument("--outcome",default="FAIL");r.add_argument("--task-class",default="general");r.add_argument("--evidence",action="append")
    r=s.add_parser("propose");r.add_argument("observation_id");r.add_argument("--skill",default="work");r.add_argument("--field",default="skill.description");r.add_argument("--host",default="mock");r.add_argument("--replacement-file")
    r=s.add_parser("lesson");r.add_argument("summary");r.add_argument("--host",default="claude");r.add_argument("--task-class",default="general");r.add_argument("--evidence",action="append",required=True)
    sp.set_defaults(func=_cmd_evolve)
    sp=sub.add_parser("optimize");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("status");r=s.add_parser("doctor");r.add_argument("--host",default="mock")
    r=s.add_parser("configure");r.add_argument("--mode",choices=["off","analyze","shadow","assisted"]);r.add_argument("--profile",choices=["lite","full"]);r.add_argument("--cost-policy",choices=["manual_consent","require_metered"]);r.add_argument("--max-candidates",type=int);r.add_argument("--max-probe-runs",type=int);r.add_argument("--max-rounds",type=int);r.add_argument("--max-wall-seconds",type=int);r.add_argument("--apply",action="store_true")
    r=s.add_parser("disable");r.add_argument("--apply",action="store_true")
    r=s.add_parser("plan");r.add_argument("--fixture",required=True);r.add_argument("--host",default="mock");r.add_argument("--model");r.add_argument("--skill",default="work");r.add_argument("--field",default="skill.description")
    r=s.add_parser("propose");r.add_argument("experiment");r.add_argument("--replacement-file");r.add_argument("--goal",default="Improve failed fixture");r.add_argument("--execute",action="store_true");r.add_argument("--spend-approval");r.add_argument("--trusted-local",action="store_true")
    r=s.add_parser("run");r.add_argument("experiment");r.add_argument("--execute",action="store_true");r.add_argument("--spend-approval");r.add_argument("--trusted-local",action="store_true")
    r=s.add_parser("report");r.add_argument("experiment")
    r=s.add_parser("promote");r.add_argument("experiment");r.add_argument("--approval-ref",required=True);r.add_argument("--suite",required=True);r.add_argument("--host",default="mock");r.add_argument("--approve",action="store_true")
    r=s.add_parser("full");r.add_argument("--fixture",required=True);r.add_argument("--host",default="mock");r.add_argument("--skill",default="work");r.add_argument("--field",default="skill.description");r.add_argument("--rounds",type=int,default=1);r.add_argument("--execute",action="store_true");r.add_argument("--spend-approval");r.add_argument("--trusted-local",action="store_true")
    sp.set_defaults(func=_cmd_optimize)
    sp=sub.add_parser("memory");common(sp);s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("add");r.add_argument("text");r.add_argument("--tags",action="append");r.add_argument("--evidence",action="append")
    r=s.add_parser("search");r.add_argument("query");s.add_parser("list");r=s.add_parser("delete");r.add_argument("id")
    sp.set_defaults(func=_cmd_memory)
    sp=sub.add_parser("session",help="Small durable task checkpoints and manual interactive session rotation");common(sp)
    s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("checkpoint",help="Save a compact handoff at a verified task boundary")
    r.add_argument("--task-id",required=True);r.add_argument("--goal",required=True)
    r.add_argument("--completed",action="append");r.add_argument("--next",dest="next_steps",action="append")
    r.add_argument("--decision",action="append");r.add_argument("--blocker",action="append")
    r.add_argument("--evidence",action="append")
    r.add_argument("--status",choices=["IN_PROGRESS","VERIFIED","COMPLETED","BLOCKED"],default="IN_PROGRESS")
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
    sp.set_defaults(func=_cmd_session)
    sp=sub.add_parser("run",help="Start an interactive CLI that ego checkpoints, clears and resumes by itself at the context limit");common(sp)
    sp.add_argument("host",choices=["claude","codex","opencode","agy"])
    sp.add_argument("--clear-tokens",type=int,metavar="N",help="Context tokens at which to rotate (default: clear_suggest_tokens)")
    sp.add_argument("--yolo",action=argparse.BooleanOptionalAction,default=None,help="Start the CLI with its skip-permissions switch (default: run_yolo in .ego/config.toml)")
    sp.add_argument("args",nargs="*",help="Arguments for the CLI itself, after --")
    sp.set_defaults(func=_cmd_run)
    sp=sub.add_parser("context",help="Measure always-on and on-demand context against budgets");common(sp)
    s=sp.add_subparsers(dest="action",required=True);s.add_parser("audit");sp.set_defaults(func=_cmd_context)
    sp=sub.add_parser("code");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("doctor")
    r=s.add_parser("search");r.add_argument("query")
    r=s.add_parser("symbol");r.add_argument("name")
    r=s.add_parser("impact");r.add_argument("name")
    r=s.add_parser("affected");r.add_argument("files",nargs="+")
    r=s.add_parser("index");r.add_argument("--execute",action="store_true",help="Index using installed provider after manual review")
    sp.set_defaults(func=_cmd_code)
    sp=sub.add_parser("tool");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("list");r=s.add_parser("call");r.add_argument("name");r.add_argument("arguments");r.add_argument("--approve-external",action="store_true")
    sp.set_defaults(func=_cmd_tool)
    sp=sub.add_parser("hook");common(sp);sp.add_argument("event",choices=["pre-tool","stop","session-start","statusline","context-probe"]);sp.add_argument("--host",choices=["claude","codex","opencode","agy"],default="claude",help="Host CLI whose hook payload arrives on stdin");sp.set_defaults(func=_cmd_hook)
    sp=sub.add_parser("prompt");common(sp);s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("register");r.add_argument("name");r.add_argument("file");r.add_argument("--role",default="worker")
    r=s.add_parser("show");r.add_argument("name");r.add_argument("--version")
    r=s.add_parser("extract");r.add_argument("file");r.add_argument("--contract",choices=["json","reply"],default="json")
    sp.set_defaults(func=_cmd_prompt)
    sp=sub.add_parser("events");common(sp);s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("tail");r.add_argument("-n",type=int,default=20);sp.set_defaults(func=_cmd_events)
    sp=sub.add_parser("pr");common(sp);s=sp.add_subparsers(dest="action",required=True)
    r=s.add_parser("create");r.add_argument("--title",required=True);r.add_argument("--body",required=True);r.add_argument("--base",default="main");r.add_argument("--approve",action="store_true")
    r=s.add_parser("reviews");r.add_argument("number",type=int)
    sp.set_defaults(func=_cmd_pr)
    sp=sub.add_parser("bench");common(sp);sp.add_argument("--trials",type=int,default=10);sp.set_defaults(func=_cmd_bench)
    return p

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
    if "--root" in args_list:
        idx=args_list.index("--root")
        if idx+1>=len(args_list):
            parser.error("--root requires a path")
        pair=args_list[idx:idx+2];del args_list[idx:idx+2]
        if args_list:args_list[1:1]=pair
        else:args_list.extend(pair)
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
