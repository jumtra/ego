"""Optional GitHub CLI integration for PR create and review triage. Never implicit."""
from __future__ import annotations
import shutil
import json
from pathlib import Path
from .runner import run_process
from .util import EgoError


def gh_available()->bool:return bool(shutil.which("gh"))

def create_pr(root:Path,title:str,body:str,base:str,*,approve:bool=False)->dict:
    if not approve:raise EgoError("APPROVAL_REQUIRED","PR creation requires --approve")
    if not gh_available():raise EgoError("GH_NOT_INSTALLED","Install/authenticate GitHub CLI",3)
    code,out,err,_=run_process(["gh","pr","create","--title",title,"--body",body,"--base",base],root,120)
    if code:raise EgoError("GH_FAILED",err,5)
    return {"status":"CREATED","url":out.strip()}

def reviews(root:Path,number:int)->dict:
    if not gh_available():raise EgoError("GH_NOT_INSTALLED","Install/authenticate GitHub CLI",3)
    code,out,err,_=run_process(["gh","pr","view",str(number),"--json","reviews,comments,files"],root,90)
    if code:raise EgoError("GH_FAILED",err,5)
    return json.loads(out)
