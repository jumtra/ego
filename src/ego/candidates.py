"""Immutable, narrowly scoped candidate records and human-approved CAS promotion."""
from __future__ import annotations
from pathlib import Path
import json
import re
from .templates import MARK_BEGIN
from .util import SEHError, safe_path, filehash, sha256, ident, now, read_json, write_json, atomic_text, exclusive_lock
from .install import apply_install
from .config import load_config

TARGETS={"skill.description","skill.body","tool.guide"}


def canonical_path(root:Path, skill:str, field:str)->str:
    if field not in TARGETS:raise SEHError("SURFACE_DENIED",field,2)
    if field=="tool.guide":return ".ego/spec/prompts/tool-guide.md"
    if skill not in ("work","harness-init"):raise SEHError("SKILL_NOT_OWNED",skill)
    return f".ego/spec/skills/{skill}/SKILL.md"


def replace_field(text:str,field:str,replacement:str)->str:
    if not replacement.strip() or len(replacement.encode("utf8"))>8192:
        raise SEHError("CANDIDATE_INVALID", "Empty or oversized replacement")
    if field=="tool.guide":return replacement.rstrip()+"\n"
    if field=="skill.description":
        pattern=r'(?m)^(description:\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\n]+)$'
        if len(re.findall(pattern,text))!=1:raise SEHError("CANDIDATE_INVALID","Description must exist exactly once")
        if "\n" in replacement or "\r" in replacement:raise SEHError("CANDIDATE_INVALID","Multiline description not supported")
        return re.sub(pattern,lambda m:m.group(1)+json.dumps(replacement,ensure_ascii=False),text,count=1)
    if field=="skill.body":
        a="<!-- SEH:PROMPT:BEGIN -->";b="<!-- SEH:PROMPT:END -->"
        if text.count(a)!=1 or text.count(b)!=1 or text.index(a)>text.index(b):
            raise SEHError("CANDIDATE_INVALID","Missing or duplicate body markers")
        if a in replacement or b in replacement:raise SEHError("CANDIDATE_INVALID","Cannot inject markers")
        i=text.index(a)+len(a); j=text.index(b)
        return text[:i]+"\n"+replacement.strip()+"\n"+text[j:]
    raise SEHError("SURFACE_DENIED",field,2)


def new_candidate(root:Path,skill:str,field:str,replacement:str,hypothesis:str,*,experiment_id:str|None=None,source:str="user") -> dict:
    rel=canonical_path(root,skill,field)
    path=safe_path(root,rel,allow_missing=False)
    ownership=read_json(root/".ego"/"ownership.json",{"files":{}})
    if filehash(path)!=ownership.get("files",{}).get(rel):raise SEHError("OWNERSHIP_CONFLICT",rel)
    modified=replace_field(path.read_text(encoding="utf8"),field,replacement)
    rec={"schema_version":1,"candidate_id":ident("cand"),"created_at":now(),"experiment_id":experiment_id,
         "target":{"path":rel,"skill":skill,"field":field},"base_sha256":filehash(path),
         "replacement":replacement,"result_sha256":sha256(modified),"hypothesis":hypothesis,"source":source}
    return rec


def validate_candidate(root:Path,candidate:dict) -> str:
    if candidate.get("schema_version")!=1:raise SEHError("CANDIDATE_INVALID","Unknown version")
    target=candidate["target"];rel=canonical_path(root,target["skill"],target["field"])
    if target["path"]!=rel:raise SEHError("CANDIDATE_INVALID","Target path mismatch")
    p=safe_path(root,rel,allow_missing=False)
    if filehash(p)!=candidate["base_sha256"]:raise SEHError("STALE_BASE",rel)
    ownership=read_json(root/".ego"/"ownership.json",{"files":{}})
    if ownership.get("files",{}).get(rel)!=candidate["base_sha256"]:raise SEHError("OWNERSHIP_CONFLICT",rel)
    modified=replace_field(p.read_text(encoding="utf8"),target["field"],candidate["replacement"])
    if sha256(modified)!=candidate["result_sha256"]:raise SEHError("CANDIDATE_TAMPERED",rel)
    return modified


def promote(root:Path,candidate:dict,*,approval_ref:str,gate:dict|None=None,allow_mock:bool=False)->dict:
    """Required explicit local human approval and passing independent authoring evidence."""
    if not approval_ref.startswith("human:") or len(approval_ref)<8:
        raise SEHError("APPROVAL_REQUIRED","Approval reference must be human:<ticket-or-decision>")
    if not gate or gate.get("status")!="PASS" or not gate.get("evidence"):
        raise SEHError("AUTHORING_GATE_FAILED","Need passing evaluation with independent evidence")
    if gate.get("host")=="mock" and not allow_mock:
        raise SEHError("MOCK_NOT_PROMOTABLE","Real-host evaluation required for behavioral changes")
    if gate.get("candidate_id") != candidate.get("candidate_id") or gate.get("candidate_result_sha256") != candidate.get("result_sha256"):
        raise SEHError("EVIDENCE_MISMATCH", "Evidence belongs to a different candidate")
    from .util import json_text
    saved=read_json(root/".ego"/"artifacts"/"eval"/(gate["evidence"]+".json"))
    if not saved or saved.get("run_id")!=gate["evidence"] or saved.get("host")!=gate.get("host") or not saved.get("all_passed"):
        raise SEHError("EVIDENCE_MISMATCH", "Matching verified eval artifact is required")
    if sha256(json_text(saved))!=gate.get("report_sha256"):
        raise SEHError("EVIDENCE_MISMATCH", "Evidence checksum mismatch")
    if {v.get("split") for v in saved.get("summary",{}).values()}!={"training","regression","holdout","security"}:
        raise SEHError("AUTHORING_GATE_FAILED", "Insufficient evaluation split coverage")
    with exclusive_lock(root/".ego"/"locks"/"promotion.lock"):
        modified=validate_candidate(root,candidate)
        rel=candidate["target"]["path"]
        target=safe_path(root,rel)
        ownership_path=root/".ego"/"ownership.json"
        ownership=read_json(ownership_path)
        ledger_path=root/".ego"/"state"/"promotions.json"
        ledger=read_json(ledger_path,[])
        for item in ledger:
            if item["candidate_id"]==candidate["candidate_id"] and item["status"]=="PROMOTED":
                raise SEHError("ALREADY_PROMOTED",candidate["candidate_id"])
        original=target.read_bytes()
        before=ownership
        try:
            atomic_text(target,modified)
            fresh=json.loads(json.dumps(ownership))
            fresh["files"][rel]=sha256(modified)
            write_json(ownership_path,fresh)
            sync=apply_install(root,load_config(root))
        except Exception:
            from .util import atomic_bytes
            atomic_bytes(target,original)
            write_json(ownership_path,before)
            raise
        entry={"promotion_id":ident("promo"),"candidate_id":candidate["candidate_id"],"target":rel,"original":original.decode("utf8"),
               "new_hash":sha256(modified),"approval_ref":approval_ref,"created_at":now(),"snapshot_id":sync["snapshot_id"],"status":"PROMOTED"}
        ledger.append(entry);write_json(ledger_path,ledger)
        return {k:v for k,v in entry.items() if k!="original"}


def rollback_promotion(root:Path,promo_id:str)->dict:
    with exclusive_lock(root/".ego"/"locks"/"promotion.lock"):
        path=root/".ego"/"state"/"promotions.json"
        ledger=read_json(path,[])
        entry=next((x for x in ledger if x["promotion_id"]==promo_id),None)
        if not entry:raise SEHError("PROMOTION_NOT_FOUND",promo_id,3)
        if entry["status"]!="PROMOTED":raise SEHError("ALREADY_ROLLED_BACK",promo_id)
        target=safe_path(root,entry["target"])
        if filehash(target)!=entry["new_hash"]:raise SEHError("STALE_BASE","Target edited since promotion")
        from .util import atomic_bytes
        atomic_text(target,entry["original"])
        ownership_path=root/".ego"/"ownership.json"
        owned=read_json(ownership_path);owned["files"][entry["target"]]=sha256(entry["original"]);write_json(ownership_path,owned)
        sync=apply_install(root,load_config(root))
        entry["status"]="ROLLED_BACK";write_json(path,ledger)
        return {"status":"ROLLED_BACK","promotion_id":promo_id,"sync_snapshot":sync["snapshot_id"]}
