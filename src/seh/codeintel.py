"""Code intelligence provider selection with explicit capabilities and real CLI adapters.

Advanced operations are NEVER described as graph results when only a text fallback ran.
"""
from __future__ import annotations
from pathlib import Path
import json
import re
import shutil
import subprocess
from .util import SEHError
from .runner import run_process

PROVIDER_BINS={"codegraph":"codegraph","graphify":"graphify","none":None}


def health(provider:str)->dict:
    if provider not in PROVIDER_BINS:raise SEHError("UNKNOWN_CODE_PROVIDER",provider,2)
    cmd=PROVIDER_BINS[provider]
    path=shutil.which(cmd) if cmd else None
    supported={"none":["text_search","symbol_heuristic"],
               "codegraph":["index","query","symbol","impact","affected_tests","callers","callees"],
               "graphify":["extract","query","path","explain","knowledge_graph"]}
    return {"provider":provider,"installed":bool(path) if cmd else True,
            "external_capabilities":supported[provider] if path else [],
            "fallback_capabilities":["text_search","symbol_heuristic"],
            "limitations": "Absent graph tooling falls back only to source text search; unsupported graph operations return UNSUPPORTED"}


def _cli(provider:str,root:Path,argv:list[str],timeout:int=120)->dict:
    path=shutil.which(provider)
    if not path:raise SEHError("CODE_PROVIDER_UNAVAILABLE",f"{provider} is not installed",3)
    code,out,err,seconds=run_process([path]+argv,root,timeout)
    if code!=0:
        return {"provider":provider,"status":"CLI_FAILED","exit_code":code,"error":err[-1200:],"stdout":out[-2000:]}
    try: result=json.loads(out)
    except (TypeError,ValueError):result=out.strip()
    return {"provider":provider,"status":"PASS","result":result,"elapsed_seconds":seconds}


def text_fallback(root:Path,query:str,limit:int=30)->dict:
    if not query or len(query)>400:raise SEHError("BAD_QUERY",query,2)
    results=[];count=0
    root=root.resolve()
    skip={".git",".harness","node_modules",".venv","__pycache__","dist","build",".codegraph","graphify-out"}
    needles=query.lower().split()
    for file in root.rglob("*"):
        if len(results)>=limit or count>1200:break
        if any(x in skip for x in file.relative_to(root).parts) or not file.is_file() or file.is_symlink():continue
        if file.suffix not in (".py",".js",".jsx",".mjs",".ts",".tsx",".mts",".cs",".fs",".go",".rs",".java",".kt",".kts",".swift",".rb",".php",".dart",".c",".h",".cpp",".hpp",".cc",".gd",".ex",".exs",".zig",".lua",".sh",".md",".json",".toml",".yaml",".yml") or file.stat().st_size>300000:continue
        count+=1
        try:lines=file.read_text(encoding="utf-8").splitlines()
        except UnicodeError:continue
        for i,line in enumerate(lines,1):
            if all(term in line.lower() for term in needles):
                results.append({"path":file.relative_to(root).as_posix(),"line":i,"snippet":line.strip()[:240]})
                if len(results)>=limit:break
    return {"strategy":"local_text_fallback","supported":True,"matches":results,"truncated":len(results)>=limit}


def search(root:Path,query:str,provider:str="none",limit:int=30)->dict:
    if provider not in PROVIDER_BINS:raise SEHError("UNKNOWN_CODE_PROVIDER",provider,2)
    if provider=="codegraph" and shutil.which("codegraph"):
        return _cli("codegraph",root,["query",query,"--limit",str(limit),"--json"])
    if provider=="graphify" and shutil.which("graphify"):
        return _cli("graphify",root,["query",query])
    return {"provider":provider,**text_fallback(root,query,limit)}


def symbol(root:Path,name:str,provider:str="none")->dict:
    if not re.fullmatch(r"[A-Za-z_][\w.]{0,120}",name):raise SEHError("BAD_SYMBOL",name,2)
    return {"provider":provider,"strategy":"search","result":search(root,name,provider),"advanced_impact":"CALL_SEPARATE_API"}


def index(root:Path,provider:str,execute:bool=False)->dict:
    if provider=="none":return {"provider":"none","status":"NOT_APPLICABLE"}
    if provider not in PROVIDER_BINS:raise SEHError("UNKNOWN_CODE_PROVIDER",provider,2)
    if not shutil.which(provider):return {"provider":provider,"status":"UNAVAILABLE"}
    argv=["init","--local-only"] if provider=="codegraph" else ["extract","--code-only","--backend","none"]
    if not execute:return {"provider":provider,"status":"DRY_RUN","argv":[provider]+argv,"writes_external_index":True}
    return _cli(provider,root,argv,timeout=600)


def impact(root:Path,symbol_name:str,provider:str)->dict:
    if provider=="codegraph" and shutil.which("codegraph"):
        return _cli(provider,root,["impact",symbol_name,"--json"])
    if provider=="graphify" and shutil.which("graphify"):
        return _cli(provider,root,["explain",symbol_name])
    return {"provider":provider,"status":"UNSUPPORTED","reason":"External graph capability unavailable"}


def affected_tests(root:Path,files:list[str],provider:str)->dict:
    if provider=="codegraph" and shutil.which("codegraph"):
        from .util import safe_path
        for rel in files:safe_path(root,rel)
        return _cli(provider,root,["affected"]+files)
    return {"provider":provider,"status":"UNSUPPORTED","reason":"Affected tests require CodeGraph index"}
