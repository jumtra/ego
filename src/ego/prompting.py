"""Prompt contracts and versioning, following Eval-first/tool-before-guess principles."""
from __future__ import annotations
import json
from pathlib import Path
import re
from .util import EgoError, sha256, safe_path, read_json, write_json, ident, now

REPLY_OPEN="<reply>"; REPLY_CLOSE="</reply>"

def extract_reply(text:str,contract:str="json")->dict:
    """Parsing is NOT the same as token-level model stop sequences in a provider API."""
    if contract=="reply":
        if text.count(REPLY_OPEN)!=1 or text.count(REPLY_CLOSE)!=1 or text.index(REPLY_CLOSE)<text.index(REPLY_OPEN):
            raise EgoError("OUTPUT_CONTRACT_FAILED", "Expected exactly one <reply> body")
        answer=text.split(REPLY_OPEN,1)[1].split(REPLY_CLOSE,1)[0].strip()
        if not answer:raise EgoError("OUTPUT_CONTRACT_FAILED","Empty reply")
        return {"reply":answer,"contract":"reply","stop_sequence_enforced":False}
    if contract=="json":
        try: obj=json.loads(text)
        except ValueError as exc:raise EgoError("OUTPUT_CONTRACT_FAILED", "Invalid JSON") from exc
        if not isinstance(obj,dict):raise EgoError("OUTPUT_CONTRACT_FAILED", "Expected JSON object")
        return {"reply":obj,"contract":"json","stop_sequence_enforced":False}
    raise EgoError("UNKNOWN_OUTPUT_CONTRACT",contract,2)


def register_prompt(root:Path,name:str,body:str,role:str="worker")->dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,60}",name):raise EgoError("INVALID_PROMPT_NAME",name,2)
    if not body.strip():raise EgoError("EMPTY_PROMPT",name,2)
    history=root/".ego"/"prompts"/(name+".json")
    state=read_json(history,{"name":name,"versions":[]})
    digest=sha256(body)
    if any(v["sha256"]==digest for v in state["versions"]):
        raise EgoError("PROMPT_DUPLICATE","No change to versioned prompt")
    record={"id":ident("pv"),"created_at":now(),"sha256":digest,"role":role,"body":body}
    state["versions"].append(record);write_json(history,state)
    return {k:v for k,v in record.items() if k!="body"}

def get_prompt(root:Path,name:str,version:str|None=None)->dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,60}",name):raise EgoError("INVALID_PROMPT_NAME",name,2)
    state=read_json(root/".ego"/"prompts"/(name+".json"))
    if not state:raise EgoError("PROMPT_NOT_FOUND",name,3)
    match=next((v for v in state["versions"] if v["id"]==version),None) if version else state["versions"][-1]
    if not match:raise EgoError("PROMPT_VERSION_NOT_FOUND",version or "",3)
    return match
