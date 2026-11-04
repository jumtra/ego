"""CLI-first entrypoint. Optimization modules are never imported on the normal path."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from . import __version__
from .util import SEHError, read_json, write_json, now

def _print(data):print(json.dumps(data,indent=2,ensure_ascii=False,default=str))
def _root(args):return Path(getattr(args,"root", ".")).resolve()

def _cmd_setup(args):
    from .setup import setup_command
    result, exit_code = setup_command(_root(args), profile=args.profile, hosts=args.hosts,
          graph=args.graph, memory=args.memory, detect_tests=args.detect_tests,
          yes=args.yes, non_interactive=args.non_interactive,
          json_format=args.json, scan=not args.skip_scan)
    if args.json: _print(result)
    return exit_code


def _cmd_uninstall(args):
    from .setup import uninstall_command
    result, exit_code = uninstall_command(_root(args), yes=args.yes,
        non_interactive=args.non_interactive, json_format=args.json)
    if args.json: _print(result)
    return exit_code


def _cmd_status(args):
    from .setup import status_command, display_status
    result = status_command(_root(args))
    if args.json: _print(result)
    else: print(display_status(result))
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

def _cmd_project(args):
    """All language discovery is read-only; running project scripts is opt-in."""
    from .toolchains import detect_projects, resolve_check
    from .detect import detect
    root = _root(args)
    if args.action == "detect":
        _print(detect(root)); return 0
    if args.action == "checks":
        projects = detect_projects(root)
        _print({"projects": [{"path": p["path"], "ecosystem": p["ecosystem"],
                              "languages": p["languages"], "checks": p["checks"]}
                             for p in projects], "executed": False})
        return 0
    check = resolve_check(root, args.kind, project=args.project, ecosystem=args.ecosystem)
    if not args.execute:
        _print({**check, "executed": False, "note": "No test/build/lint was executed; --execute --approve is required"})
        return 0
    if not args.approve:
        raise SEHError("APPROVAL_REQUIRED", "Running repository scripts requires --execute --approve", 4)
    if not check["available"]:
        raise SEHError("CHECK_TOOL_UNAVAILABLE", "Missing executable: " + check["argv"][0], 3)
    from .config import load_config
    from .runner import run_checked_command
    cfg = load_config(root)
    result = run_checked_command(check["argv"], Path(check["cwd"]),
                                 timeout=min(args.timeout, cfg.timeout_seconds), allow=cfg.allow_commands)
    _print({**check, "executed": True, "result": result.as_dict()})
    return 0 if result.status == "PASS" else 5


def _cmd_sync(args):
    from .config import load_config
    from .install import installation_plan,apply_install
    cfg=load_config(_root(args))
    result=apply_install(_root(args),cfg) if args.apply else installation_plan(_root(args),cfg)
    if not args.apply:result.pop("wanted",None)
    _print(result);return 0

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
        if not args.approve:raise SEHError("APPROVAL_REQUIRED","Explicit --approve required")
        _print(restore_legacy(root,args.snapshot));return 0
    if args.action=="remove-legacy":
        if not args.approve:raise SEHError("APPROVAL_REQUIRED","Explicit --approve required")
        _print(remove_legacy(root,read_json(Path(args.hashes))));return 0

def _cmd_work(args):
    from .work import execute,status,cancel
    root=_root(args)
    if args.action=="status":_print(status(root,args.run_id));return 0
    if args.action=="cancel":_print(cancel(root,args.run_id));return 0
    if args.action=="plan":
        from .planner import plan_work
        result=plan_work(root,args.goal,args.host,args.scope.split(",") if args.scope else None,execute=args.execute)
        if args.out:write_json(Path(args.out),result["task"])
        _print(result);return 0
    if args.action=="native":
        _print({"mode":"native","status":"USE_HOST_DIRECTLY","additional_llm_calls":0,
                "instructions":"Use /work in the host, or seh work run for bounded graph execution"});return 0
    task=read_json(Path(args.task))
    if not task:raise SEHError("TASK_MISSING",str(args.task),3)
    result=execute(root,task,host=args.host,apply=args.apply,approve=args.approve,trust_cli=args.trust_cli,
                   run_id=args.run_id if args.action=="resume" else None,resume=args.action=="resume")
    _print(result);return 0 if result["final_status"]=="PASSED" else 5

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
        write_json(root/".harness"/"state"/"candidates"/(rec["candidate_id"]+".json"),rec)
        _print(rec);return 0
    if args.action=="gate":
        res=gate(root,read_json(Path(args.candidate)),Path(args.suite),args.host,allow_mock=args.allow_mock_test)
        if args.out:write_json(Path(args.out),res)
        _print({k:v for k,v in res.items() if k!="report"});return 0 if res["status"]=="PASS" else 5
    if args.action=="promote":
        if not args.approve:raise SEHError("APPROVAL_REQUIRED","Explicit --approve required")
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
        if not args.approve:raise SEHError("APPROVAL_REQUIRED","--approve required")
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
        if not hasattr(p,"list"):raise SEHError("UNSUPPORTED","Memory provider does not support list")
        _print(p.list());return 0
    if args.action=="delete":
        if not hasattr(p,"delete"):raise SEHError("UNSUPPORTED","Memory provider does not support delete")
        _print({"deleted":p.delete(args.id)});return 0

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
    raw=sys.stdin.read(1024*1024)
    try:obj=json.loads(raw)
    except ValueError:
        _print({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Malformed event JSON"}});return 0
    result=check_hook_payload(args.event,obj)
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
        p=subprocess.run([sys.executable,"-m","seh","work","native","--root",str(root)],capture_output=True,text=True)
        if p.returncode:raise SEHError("BENCH_FAILED",p.stderr,5)
        samples.append((time.perf_counter()-start)*1000)
    results=sorted(samples)
    _print({"metric":"seh work native no-agent dispatch","count":len(results),"p50_ms":round(statistics.median(results),3),
            "p95_ms":round(results[min(len(results)-1,int(len(results)*.95))],3),
            "baseline_note":"This measures SEH CLI dispatch only, not native Claude/Codex/OpenCode task execution"})
    return 0

def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="seh",description="Standalone CLI-first self-evolve-harness")
    p.add_argument("--version",action="version",version=f"seh {__version__}")
    sub=p.add_subparsers(dest="command",required=True)
    def common(sp):sp.add_argument("--root",default=".")
    setup=sub.add_parser("setup",help="One-command, safe interactive or --yes setup");common(setup)
    setup.add_argument("--profile",choices=["minimal","balanced","full"])
    setup.add_argument("--hosts",help="auto, all, or claude,codex,opencode,agy")
    setup.add_argument("--graph",choices=["none","codegraph","graphify"])
    setup.add_argument("--memory",choices=["file","engram"])
    setup.add_argument("--detect-tests",action="store_true",help="Store suggested test commands without executing them")
    setup.add_argument("--skip-scan",action="store_true",help="Skip multi-language project scan")
    setup.add_argument("--yes",action="store_true",help="Explicitly approve and apply safe file changes")
    setup.add_argument("--non-interactive",action="store_true",help="Never prompt; preview unless --yes")
    setup.add_argument("--json",action="store_true",help="Machine-readable output, no prompts")
    setup.set_defaults(func=_cmd_setup)
    sp=sub.add_parser("status",help="Human-friendly, read-only installation status");common(sp)
    sp.add_argument("--json",action="store_true");sp.set_defaults(func=_cmd_status)
    sp=sub.add_parser("uninstall",help="Preview/removes only SEH-managed files, preserving user data");common(sp)
    sp.add_argument("--yes",action="store_true",help="Explicit removal approval")
    sp.add_argument("--non-interactive",action="store_true")
    sp.add_argument("--json",action="store_true")
    sp.set_defaults(func=_cmd_uninstall)
    init=sub.add_parser("init",help="Preview/create minimal portable harness (dry-run default)");common(init)
    init.add_argument("--profile",choices=["minimal","balanced","full"])
    init.add_argument("--hosts",help="Comma-separated claude,codex,opencode,agy")
    init.add_argument("--graph",choices=["codegraph","graphify","none"])
    init.add_argument("--memory",choices=["file","engram"])
    init.add_argument("--detect-tests",action="store_true",help="Add verified test command candidates to config")
    init.add_argument("--apply",action="store_true");init.set_defaults(func=_cmd_init)
    sp=sub.add_parser("project",help="Discover languages and review/run project-specific checks");common(sp)
    actions=sp.add_subparsers(dest="action",required=True)
    actions.add_parser("detect",help="Detect projects, ecosystems and frameworks without executing commands")
    actions.add_parser("checks",help="List project test/lint/typecheck/build/format argv proposals")
    r=actions.add_parser("check",help="Preview (default) or explicitly execute one check")
    r.add_argument("--kind",choices=["test","lint","typecheck","build","format"],required=True)
    r.add_argument("--project",help="Repo-relative project path, e.g. apps/web")
    r.add_argument("--ecosystem",help="Disambiguate projects with several toolchains")
    r.add_argument("--timeout",type=int,default=120)
    r.add_argument("--execute",action="store_true");r.add_argument("--approve",action="store_true")
    sp.set_defaults(func=_cmd_project)
    sp=sub.add_parser("sync",help="Re-render host configurations");common(sp);sp.add_argument("--apply",action="store_true");sp.set_defaults(func=_cmd_sync)
    sp=sub.add_parser("doctor");common(sp);sp.set_defaults(func=_cmd_doctor)
    sp=sub.add_parser("modules");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("list");r=s.add_parser("resolve");r.add_argument("modules");r=s.add_parser("scaffold");r.add_argument("name");r.add_argument("--apply",action="store_true");sp.set_defaults(func=_cmd_modules)
    sp=sub.add_parser("migrate");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("plan");r=s.add_parser("rollback");r.add_argument("snapshot");r=s.add_parser("restore-legacy");r.add_argument("snapshot");r.add_argument("--approve",action="store_true");r=s.add_parser("remove-legacy");r.add_argument("--hashes",required=True);r.add_argument("--approve",action="store_true");sp.set_defaults(func=_cmd_migrate)
    sp=sub.add_parser("work");common(sp);s=sp.add_subparsers(dest="action",required=True)
    s.add_parser("native");r=s.add_parser("status");r.add_argument("run_id")
    r=s.add_parser("cancel");r.add_argument("run_id")
    r=s.add_parser("plan");r.add_argument("--goal",required=True);r.add_argument("--host",default="mock");r.add_argument("--scope");r.add_argument("--out");r.add_argument("--execute",action="store_true")
    for name in ("run","resume"):
        r=s.add_parser(name);r.add_argument("--task",required=True);r.add_argument("--host",choices=["mock","claude","codex","opencode","agy"],default="mock")
        r.add_argument("--apply",action="store_true");r.add_argument("--approve",action="store_true");r.add_argument("--trust-cli",action="store_true")
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
    r=s.add_parser("gate");r.add_argument("--candidate",required=True);r.add_argument("--suite",required=True);r.add_argument("--host",default="mock");r.add_argument("--out");r.add_argument("--allow-mock-test",action="store_true")
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
    sp=sub.add_parser("hook");common(sp);sp.add_argument("event",choices=["pre-tool"]);sp.set_defaults(func=_cmd_hook)
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
    if "--root" in args_list:
        idx=args_list.index("--root")
        if idx+1>=len(args_list):
            parser.error("--root requires a path")
        pair=args_list[idx:idx+2];del args_list[idx:idx+2]
        if args_list:args_list[1:1]=pair
        else:args_list.extend(pair)
    args=parser.parse_args(args_list)
    try:return args.func(args)
    except SEHError as exc:
        print(json.dumps({"status":"ERROR","reason_code":exc.code,"message":str(exc)},ensure_ascii=False),file=sys.stderr)
        return exc.exit_code
    except (OSError,KeyError,ValueError,TypeError,json.JSONDecodeError) as exc:
        print(json.dumps({"status":"ERROR","reason_code":"INFRA_ERROR","message":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 5

if __name__=="__main__":raise SystemExit(main())
