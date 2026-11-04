"""Deterministic eval-first runner, split-aware reports and comparisons."""
from __future__ import annotations
import json
from pathlib import Path
import re
import tempfile
import time
from .config import load_config
from .runner import run_agent, run_checked_command
from .util import SEHError, ident, now, read_json, write_json, simple_copy_tree, sha256, json_text

VALID_SPLITS={"training","regression","holdout","security"}

def load_suite(path:Path)->list[dict]:
    if path.is_file():
        obj=read_json(path)
        rows=obj["cases"] if isinstance(obj,dict) and isinstance(obj.get("cases"),list) else obj
    elif path.is_dir():
        rows=[]
        for file in sorted(path.glob("*.json")):
            data=read_json(file)
            rows.extend(data["cases"] if isinstance(data,dict) and isinstance(data.get("cases"),list) else [data])
    else:raise SEHError("EVAL_NOT_FOUND",str(path),3)
    if not isinstance(rows,list) or not rows:raise SEHError("EVAL_INVALID","Missing cases",2)
    ids=set()
    for case in rows:
        if not isinstance(case,dict) or not isinstance(case.get("id"),str) or not isinstance(case.get("prompt"),str) or not isinstance(case.get("checks"),dict):
            raise SEHError("EVAL_INVALID",str(case),2)
        if case["id"] in ids or case.get("split","training") not in VALID_SPLITS:raise SEHError("EVAL_INVALID",case["id"],2)
        ids.add(case["id"])
    return rows


def check_result(case:dict, output:str,exit_code:int=0,workspace:Path|None=None)->dict:
    checks=case["checks"]
    failures=[]
    if exit_code != checks.get("exit_code",0):failures.append("wrong_exit_code")
    for value in checks.get("contains",[]):
        if value not in output:failures.append(f"missing:{value}")
    for value in checks.get("not_contains",[]):
        if value in output:failures.append(f"forbidden:{value}")
    for pattern in checks.get("regex",[]):
        try:
            if not re.search(pattern,output):failures.append(f"regex:{pattern}")
        except re.error:raise SEHError("EVAL_INVALID",f"Bad regex {pattern}",2)
    if "json" in checks:
        try:obj=json.loads(output)
        except ValueError:failures.append("invalid_json")
        else:
            expected=checks["json"]
            for key,value in expected.items():
                current=obj
                for seg in key.split("."):
                    if isinstance(current,dict) and seg in current:current=current[seg]
                    else:current=object();break
                if current != value:failures.append(f"json:{key}")
    if workspace:
        for rel in checks.get("files_exist",[]):
            from .util import safe_path
            if not safe_path(workspace,rel).is_file():failures.append(f"file:{rel}")
    return {"status":"PASS" if not failures else "FAIL","failures":failures}


def run_eval(root:Path,suite_path:Path,*,host:str="mock",trials:int=1,model:str|None=None,
             split:str|None=None,prompt_prefix:str="",timeout:int=120,save:bool=True,overrides:dict[str,str]|None=None) -> dict:
    if not 1<=trials<=20:raise SEHError("EVAL_INVALID","trials must be 1-20",2)
    cases=load_suite(suite_path)
    if split:
        desired=set(split.split(","))
        if not desired<=VALID_SPLITS:raise SEHError("EVAL_INVALID","Unknown split",2)
        cases=[case for case in cases if case.get("split","training") in desired]
    if not cases:raise SEHError("EVAL_EMPTY","No matching cases",2)
    start=time.monotonic()
    run_id=ident("eval");rows=[]
    for case in cases:
        for number in range(1,trials+1):
            with tempfile.TemporaryDirectory(prefix="seh-eval-") as td:
                workspace=Path(td)/"repo"
                simple_copy_tree(root,workspace)
                if overrides:
                    from .util import safe_path, atomic_text
                    for rel, content in overrides.items(): atomic_text(safe_path(workspace,rel),content)
                prompt = (prompt_prefix+"\n" if prompt_prefix else "")+case["prompt"]
                try:
                    rr=run_agent(host,prompt,workspace,timeout,model=model,read_only=True)
                    if rr.status=="TIMEOUT": verdict={"status":"INFRA","failures":["timeout"]}
                    elif rr.status!="PASS": verdict={"status":"INFRA","failures":[rr.stderr or "runner failed"]}
                    else:verdict=check_result(case,rr.output,rr.exit_code,workspace)
                    rows.append({"case_id":case["id"],"split":case.get("split","training"),"trial":number,"verdict":verdict,"result":rr.as_dict()})
                except (OSError,SEHError) as exc:
                    rows.append({"case_id":case["id"],"split":case.get("split","training"),"trial":number,"verdict":{"status":"INFRA","failures":[str(exc)]},"result":None})
    summary={}
    for row in rows:
        key=row["case_id"]
        if key not in summary:summary[key]={"split":row["split"],"passes":0,"fails":0,"infra":0,"trials":0}
        summary[key][{"PASS":"passes","FAIL":"fails"}.get(row["verdict"]["status"],"infra")]+=1
        summary[key]["trials"]+=1
    report={"schema_version":1,"run_id":run_id,"host":host,"model":model,"suite_sha256":sha256(json_text(cases)),
            "created_at":now(),"rows":rows,"summary":summary,"elapsed_seconds":round(time.monotonic()-start,4),
            "all_passed":all(v["passes"]==v["trials"] for v in summary.values())}
    if save:write_json(root/".harness"/"artifacts"/"eval"/(run_id+".json"),report)
    return report


def compare(a:dict,b:dict)->dict:
    if a.get("suite_sha256") != b.get("suite_sha256") or set(a.get("summary",{}))!=set(b.get("summary",{})):
        return {"comparison":"INCOMPARABLE","promotable":False,"reason":"Fixture set differs"}
    regressions=[];improvements=[];infra=[]
    for case_id,ra in a["summary"].items():
        rb=b["summary"][case_id]
        if ra["infra"] or rb["infra"]:infra.append(case_id);continue
        if rb["passes"] < ra["passes"]:regressions.append(case_id)
        if rb["passes"] > ra["passes"]:improvements.append(case_id)
    if infra:status="INCONCLUSIVE"
    elif regressions:status="REGRESSION"
    elif improvements:status="IMPROVEMENT_OBSERVED"
    else:status="NO_GAIN"
    return {"comparison":status,"improvements":improvements,"regressions":regressions,"infra":infra,
            "promotable":status=="IMPROVEMENT_OBSERVED" and not regressions and not infra and all(x["passes"]==x["trials"] for x in b["summary"].values()),
            "baseline_id":a["run_id"],"candidate_id":b["run_id"]}
