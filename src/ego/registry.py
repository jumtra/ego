"""Module catalog and dependency resolver with no code execution while loading manifests."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
import tomllib
from .util import EgoError

BUILTINS = {
 "core": {"provides":["config","policy","ownership","runtime"],"requires":[],"conflicts":[],"default":True},
 "eval": {"provides":["evaluation"],"requires":["core"],"conflicts":[],"default":True},
 "author": {"provides":["authoring"],"requires":["eval"],"conflicts":[],"default":True},
 "evolve": {"provides":["evolution"],"requires":["author"],"conflicts":[],"default":True},
 "optimize": {"provides":["harness2"],"requires":["evolve"],"conflicts":[],"default":False},
 "memory-file": {"provides":["memory"],"requires":[],"conflicts":["memory-engram"],"default":True},
 "memory-engram": {"provides":["memory"],"requires":[],"conflicts":["memory-file"],"default":False},
 "codegraph": {"provides":["code_intelligence"],"requires":[],"conflicts":["graphify"],"default":False},
 "graphify": {"provides":["code_intelligence"],"requires":[],"conflicts":["codegraph"],"default":False},
 "optional-x-reference": {"provides":[],"requires":[],"conflicts":[],"default":False,"status":"SPEC_UNVERIFIED"},
}

def discover(root:Path)->dict:
    result={k:dict(v) for k,v in BUILTINS.items()}
    custom=root/".ego"/"modules"
    if custom.exists():
        for file in custom.glob("*/module.toml"):
            with file.open("rb") as f:
                try: entry=tomllib.load(f)
                except ValueError as exc: raise EgoError("INVALID_MODULE", str(exc), 2) from exc
            name=entry.get("id")
            if not isinstance(name,str) or name in result: raise EgoError("DUPLICATE_MODULE",str(name),2)
            if not isinstance(entry.get("provides"),list) or not isinstance(entry.get("requires",[]),list):raise EgoError("INVALID_MODULE",name,2)
            result[name]={"provides":entry["provides"],"requires":entry.get("requires",[]),"conflicts":entry.get("conflicts",[]),"default":False,"status":"DECLARATIVE_ONLY"}
    return result

def resolve(root:Path, selected:list[str])->dict:
    entries=discover(root)
    visiting=set();visited=set();order=[]
    def visit(name:str):
        if name not in entries: raise EgoError("MISSING_MODULE",name,3)
        if name in visiting:raise EgoError("DEPENDENCY_CYCLE",name,2)
        if name in visited:return
        visiting.add(name)
        for dep in entries[name].get("requires",[]):visit(dep)
        visiting.remove(name);visited.add(name);order.append(name)
    for name in selected:visit(name)
    for name in order:
        clashes=set(entries[name].get("conflicts",[]))&set(order)
        if clashes:raise EgoError("MODULE_CONFLICT",f"{name} conflicts with {sorted(clashes)}",2)
    return {"modules":order,"capabilities":sorted({c for mod in order for c in entries[mod].get("provides",[])}),"warnings":[f"{k}: {entries[k]['status']}" for k in order if entries[k].get("status")]}


def scaffold(root:Path,name:str,*,apply:bool=False)->dict:
    """Create a declarative module skeleton. No executable code runs at discovery time."""
    import re
    from .util import atomic_text, safe_path
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,48}",name):raise EgoError("INVALID_MODULE_ID",name,2)
    if name in BUILTINS:raise EgoError("MODULE_CONFLICT",name,2)
    rel=f".ego/modules/{name}/module.toml"
    dest=safe_path(root,rel)
    if dest.exists():raise EgoError("MODULE_EXISTS",name,2)
    manifest=(f'id = "{name}"\napi_version = "1"\nversion = "0.1.0"\n'
              'provides = []\nrequires = ["core"]\nconflicts = []\n'
              '# Declare tool_specs = ["tool.json"] only after review.\n')
    if apply:atomic_text(dest,manifest)
    return {"id":name,"path":rel,"applied":apply,"manifest":manifest}
