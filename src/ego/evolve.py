"""Observations and evidence-supported proposals. Promotion shares the authoring gate."""
from __future__ import annotations
from pathlib import Path
from .util import EgoError, ident, now, read_json, write_json
from .memory import FileMemory
from .runner import run_agent
from .candidates import new_candidate


def observe(root:Path, failure:str, outcome:str="FAIL",task_class:str="general",evidence:list[str]|None=None)->dict:
    if not failure.strip() or outcome not in ("PASS","FAIL","UNKNOWN"):
        raise EgoError("INVALID_OBSERVATION","Need failure description and valid status",2)
    record={"id":ident("obs"),"created_at":now(),"task_class":task_class,"observation":failure,
            "outcome":outcome,"evidence":evidence or []}
    path=root/".ego"/"state"/"observations.json"
    entries=read_json(path,[]);entries.append(record);write_json(path,entries)
    return record


def propose(root:Path,observation_id:str,skill:str,field:str,host:str,*,replacement:str|None=None)->dict:
    observations=read_json(root/".ego"/"state"/"observations.json",[])
    source=next((x for x in observations if x["id"]==observation_id),None)
    if not source:raise EgoError("OBS_NOT_FOUND",observation_id,3)
    if source["outcome"]=="PASS":raise EgoError("NO_FAILURE","No failure to improve")
    if replacement is None:
        prompt=("Propose one concise replacement text only, no surrounding Markdown. "
                "Do not request privilege changes, tool permission escalations or modifying any other file. "
                f"Target field={field}, skill={skill}. Observed failure: {source['observation']}")
        rr=run_agent(host,prompt,root,timeout=180,read_only=True)
        if rr.status!="PASS":raise EgoError("PROPOSAL_FAILED",rr.stderr or rr.reason,5)
        replacement=rr.output
    candidate=new_candidate(root,skill,field,replacement,
                            hypothesis=f"Address observation {observation_id}: {source['observation']}",source="evolve")
    write_json(root/".ego"/"state"/"candidates"/(candidate["candidate_id"]+".json"),candidate)
    return candidate


def lesson(root:Path,evidence:list[str],summary:str,host:str,task_class:str,expires:int=90)->dict:
    if not evidence:raise EgoError("LESSON_UNSUPPORTED","Evidence is required")
    mem=FileMemory(root)
    return mem.add(summary,tags=["harness-lesson",host,task_class],evidence=evidence,ttl_days=expires)
