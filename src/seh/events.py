"""Append-only event journal. No untrusted plugin code executes on event emission."""
from __future__ import annotations
from pathlib import Path
import json
from .util import ident, now, read_json, exclusive_lock


def emit(root:Path,name:str,subject:str,details:dict|None=None)->dict:
    if not name.replace("-","").replace("_","").replace(".","").isalnum():raise ValueError("Invalid event type")
    event={"id":ident("event"),"name":name,"subject":subject,"details":details or {},"created_at":now()}
    path=root/".harness"/"state"/"events.jsonl"
    with exclusive_lock(root/".harness"/"locks"/"events.lock"):
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open("a",encoding="utf8") as f:f.write(json.dumps(event,ensure_ascii=False)+"\n")
    return event


def tail(root:Path,n:int=20)->list[dict]:
    path=root/".harness"/"state"/"events.jsonl"
    if not path.exists():return []
    lines=path.read_text(encoding="utf8").splitlines()
    return [json.loads(line) for line in lines[-min(max(n,1),200):]]
