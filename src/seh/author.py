"""Evidence-gated authoring workflow. Model self-assessment is never enough."""
from __future__ import annotations
from pathlib import Path
import re
from .util import SEHError, ident, now, read_json, write_json, sha256, safe_path
from .candidates import new_candidate, validate_candidate
from .evals import load_suite, run_eval, compare

SKILL_NAME=re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

def lint_skill(path:Path)->list[str]:
    text=path.read_text(encoding="utf8")
    issues=[]
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:return ["missing YAML frontmatter"]
    front=text[4:].split("\n---\n",1)[0]
    keys={}
    for line in front.splitlines():
        if not line.strip():continue
        if ":" not in line:issues.append("malformed frontmatter line");continue
        key,val=line.split(":",1)
        if key in keys:issues.append(f"duplicate key {key}")
        keys[key]=val.strip().strip('"')
    if not SKILL_NAME.fullmatch(keys.get("name","")):issues.append("invalid name")
    if not 10 <= len(keys.get("description",""))<=1024:issues.append("missing or imprecise description")
    if keys.get("name")!=path.parent.name:issues.append("folder/name mismatch")
    if "Bash(*)" in front or "--dangerously-skip-permissions" in text:issues.append("unsafe blanket permission")
    if len(text)>60000:issues.append("oversized skill (move references on demand)")
    return issues


def lint(root:Path)->dict:
    seen={};issues=[];files=[]
    for parent in (root/".harness"/"spec"/"skills", root/".claude"/"skills", root/".agents"/"skills"):
        if not parent.is_dir():continue
        for path in parent.glob("*/SKILL.md"):
            rel=path.relative_to(root).as_posix()
            errors=lint_skill(path)
            files.append(rel)
            issues.extend({"path":rel,"problem":x} for x in errors)
            name=path.parent.name
            if name in seen and path.read_bytes()!=seen[name][1]:issues.append({"path":rel,"problem":f"conflicts with {seen[name][0]}"})
            seen[name]=(rel,path.read_bytes())
    return {"status":"PASS" if not issues else "FAIL","files":files,"issues":issues}


def plan(root:Path,skill:str,field:str,goal:str)->dict:
    from .candidates import canonical_path
    target=canonical_path(root,skill,field)
    path=safe_path(root,target,allow_missing=False)
    report={"id":ident("auth"),"created_at":now(),"target":target,"field":field,"goal":goal,
            "base_sha256":sha256(path.read_bytes()),"state":"EXPLORED","checks_required":["positive","negative","regression","holdout","security"],
            "context_decision":"Use existing skill or tool before adding new skill","evidence":{"source":target,"existing_skills":["work","harness-init"]}}
    write_json(root/".harness"/"authoring"/(report["id"]+".json"),report)
    return report


def gate(root:Path,candidate:dict,suite:Path,host:str="mock",trials:int=1, *, allow_mock:bool=False)->dict:
    """Run fresh-session suites with host-specific generated overlays; cannot fake trigger coverage."""
    modified=validate_candidate(root,candidate)
    skill=candidate["target"]["skill"]
    if skill=="work" or skill=="harness-init":
        overlays={f".claude/skills/{skill}/SKILL.md":modified,f".agents/skills/{skill}/SKILL.md":modified}
    else:overlays={candidate["target"]["path"]:modified}
    cases=load_suite(suite)
    splits={x.get("split","training") for x in cases}
    # Real behavioral gate mandates positive, negative, regression, holdout and security cases.
    required={"training","regression","holdout","security"}
    if not required<=splits:
        return {"status":"BLOCKED","reason":"Required training/regression/holdout/security splits missing", "missing":sorted(required-splits)}
    report=run_eval(root,suite,host=host,trials=trials,overrides=overlays)
    from .util import sha256, json_text
    proof={"host":host,"evidence":report["run_id"],
           "candidate_id":candidate["candidate_id"],"candidate_result_sha256":candidate["result_sha256"],
           "fixture_sha256":report["suite_sha256"],"report_sha256":sha256(json_text(report)),
           "trigger_instrumentation":"NOT_AVAILABLE", "report":report}
    if host=="mock" and not allow_mock:
        proof.update({"status":"BLOCKED","reason":"Mock results cannot establish real prompt quality"})
    else:
        proof["status"]="PASS" if report["all_passed"] else "FAIL"
    return proof
