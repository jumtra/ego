"""Deterministic tools with input contracts. No arbitrary shell tool registry."""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from .util import SEHError, safe_path, sha256

PLAN_PRICES = {"Basic": Decimal(30), "Plus": Decimal(45), "Premium": Decimal(60), "Unlimited": Decimal(80)}

TOOL_SCHEMAS = {
 "calculate_proration": {"required":["current_plan","new_plan","cycle_day","cycle_length"],"properties":{"current_plan":"str","new_plan":"str","cycle_day":"int","cycle_length":"int"}},
 "sha256_text": {"required":["text"],"properties":{"text":"str"}},
 "read_file": {"required":["path"],"properties":{"path":"str"}},
}

def invoke(name: str, arguments: dict, root: Path) -> dict:
    if name not in TOOL_SCHEMAS: raise SEHError("UNKNOWN_TOOL", name, 2)
    if not isinstance(arguments,dict): raise SEHError("INVALID_TOOL_INPUT", name, 2)
    schema=TOOL_SCHEMAS[name]
    if set(arguments) != set(schema["required"]): raise SEHError("INVALID_TOOL_INPUT", "Missing or unknown arguments", 2)
    for k,t in schema["properties"].items():
        if type(arguments[k]).__name__ != t: raise SEHError("INVALID_TOOL_INPUT", k, 2)
    if name == "sha256_text": return {"sha256":sha256(arguments["text"])}
    if name == "read_file":
        p=safe_path(root,arguments["path"],allow_missing=False)
        if p.stat().st_size > 1024*1024: raise SEHError("FILE_TOO_LARGE",str(p))
        return {"content":p.read_text(encoding="utf-8")}
    current = arguments["current_plan"]; new = arguments["new_plan"]
    day = arguments["cycle_day"]; length = arguments["cycle_length"]
    if current not in PLAN_PRICES or new not in PLAN_PRICES or not (1 <= day <= length <= 366):
        raise SEHError("INVALID_TOOL_INPUT", "Unknown plan or cycle day", 2)
    remaining = length-day
    catch_up = (PLAN_PRICES[new]-PLAN_PRICES[current])*Decimal(remaining)/Decimal(length)
    catch_up = catch_up.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
    nxt=(PLAN_PRICES[new]+catch_up).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
    return {"next_bill_amount":str(nxt),"catch_up":str(catch_up),"remaining":remaining,
            "breakdown":f"{new} plan {PLAN_PRICES[new]} + prorated difference {catch_up} ({remaining}/{length} remaining days)"}


def available_tools(root:Path)->dict:
    """Builtins plus manifest-declared tools from locally reviewed module paths."""
    from .registry import discover
    result={name:{"kind":"builtin",**data} for name,data in TOOL_SCHEMAS.items()}
    module_dir=root/".ego"/"modules"
    if not module_dir.exists():return result
    import tomllib
    from .util import read_json, safe_path
    for manifest in module_dir.glob("*/module.toml"):
        with manifest.open("rb") as f:meta=tomllib.load(f)
        for spec_name in meta.get("tool_specs",[]):
            if not isinstance(spec_name,str) or "/" in spec_name or "\\" in spec_name or spec_name.startswith("."):
                raise SEHError("INVALID_TOOL_SPEC",str(spec_name),2)
            spec_path=safe_path(root,(manifest.parent.relative_to(root)/spec_name).as_posix(),allow_missing=False)
            spec=read_json(spec_path)
            if not isinstance(spec,dict) or not isinstance(spec.get("name"),str) or not isinstance(spec.get("argv"),list):
                raise SEHError("INVALID_TOOL_SPEC",str(spec_path),2)
            if spec["name"] in result:raise SEHError("DUPLICATE_TOOL",spec["name"],2)
            result[spec["name"]]={"kind":"external","module":meta["id"],"schema":spec,"manifest":str(manifest)}
    return result


def invoke_registered(name:str,arguments:dict,root:Path,*,approve_external:bool=False)->dict:
    tools=available_tools(root)
    if name not in tools:raise SEHError("UNKNOWN_TOOL",name,2)
    if tools[name]["kind"]=="builtin":return invoke(name,arguments,root)
    if not approve_external:raise SEHError("EXTERNAL_TOOL_APPROVAL_REQUIRED",name)
    from .runner import run_checked_command
    from .config import load_config
    spec=tools[name]["schema"]
    inputs=spec.get("inputs",{})
    if set(arguments)!=set(inputs):raise SEHError("INVALID_TOOL_INPUT","Missing/unknown custom arguments",2)
    for key, typ in inputs.items():
        if typ not in ("str","int","bool") or type(arguments[key]).__name__!=typ:
            raise SEHError("INVALID_TOOL_INPUT",f"{key}: expected {typ}",2)
    argv=[]
    for part in spec["argv"]:
        if not isinstance(part,str):raise SEHError("INVALID_TOOL_SPEC","Invalid argv item",2)
        if part.startswith("{") and part.endswith("}"):
            key=part[1:-1]
            if key not in inputs:raise SEHError("INVALID_TOOL_SPEC",f"Unknown placeholder {key}",2)
            argv.append(str(arguments[key]))
        else:
            if "{" in part or "}" in part:raise SEHError("INVALID_TOOL_SPEC","Partial interpolation forbidden")
            argv.append(part)
    result=run_checked_command(argv,root,timeout=min(int(spec.get("timeout_seconds",30)),120),allow=load_config(root).allow_commands)
    if result.status!="PASS":raise SEHError("TOOL_FAILED",result.stderr or result.output,5)
    import json
    try:return json.loads(result.output)
    except ValueError:raise SEHError("INVALID_TOOL_OUTPUT","Custom tools must output JSON",5)
