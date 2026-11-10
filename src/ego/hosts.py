"""Host CLI adapters. All flags are checked against installed CLI help.

Permission bypass is permitted only when an externally isolated workspace has
been explicitly attested and the invocation explicitly opts into that risk.
"""
from __future__ import annotations
from dataclasses import dataclass
import shutil
from .util import SEHError

@dataclass(frozen=True)
class HostCapability:
    host: str
    binary: str | None
    available: bool
    supported: bool
    mode: str
    reason: str

BINARIES = {"claude": "claude", "codex": "codex", "opencode": "opencode", "agy": "agy", "mock": None}


def capability(host: str) -> HostCapability:
    if host not in BINARIES: raise SEHError("UNKNOWN_HOST",host,2)
    if host=="mock":return HostCapability(host,None,True,True,"offline","Deterministic test adapter")
    path=shutil.which(BINARIES[host] or "")
    return HostCapability(host,path,bool(path),bool(path),"cli","ready" if path else "CLI not installed")


def build_command(host: str, prompt: str, *, model: str | None = None, effort: str | None = None,
                  read_only: bool = True, max_turns: int = 4, use_stdin: bool = False,
                  autonomy: str = "auto", isolated: bool = False, lean: bool = False,
                  max_budget_usd: float | None = None) -> list[str]:
    cap=capability(host)
    if not cap.supported:raise SEHError("HOST_UNAVAILABLE",cap.reason,3)
    if host=="mock":return []
    if autonomy not in ("auto","safe","bypass"):
        raise SEHError("INVALID_AUTONOMY",autonomy,2)
    if autonomy=="bypass" and not isolated:
        raise SEHError("ISOLATION_REQUIRED","Permission bypass requires externally isolated execution and explicit acceptance",3)
    from .hostcompat import inspect_host,require_flags
    info=inspect_host(host)
    if host=="claude":
        require_flags(info,"--output-format")
        argv=[cap.binary,"-p","--output-format","json"]
        if "--max-turns" in info["flags"]:argv += ["--max-turns",str(max_turns)]
        # Lean workers do not inherit the user's MCP servers: their tool
        # definitions and server instructions are context a scoped node never uses.
        if lean and "--strict-mcp-config" in info["flags"]:argv.append("--strict-mcp-config")
        if max_budget_usd is not None and max_budget_usd > 0 and "--max-budget-usd" in info["flags"]:
            argv += ["--max-budget-usd",f"{max_budget_usd:.2f}"]
        if model:
            require_flags(info,"--model")
            argv += ["--model",model]
        if effort and effort != "auto":
            require_flags(info,"--effort")
            argv += ["--effort",effort]
        if autonomy=="bypass":
            require_flags(info,"--dangerously-skip-permissions")
            argv.append("--dangerously-skip-permissions")
        elif read_only or autonomy=="safe":
            require_flags(info,"--permission-mode")
            argv += ["--permission-mode","plan"]
        elif "--permission-mode" in info["flags"]:
            # Prefer safe automatic approval if the installed version documents it.
            modes=info.get("permission_modes",[])
            if "auto" in modes:
                argv += ["--permission-mode","auto"]
            elif "acceptEdits" in modes:
                argv += ["--permission-mode","acceptEdits"]
        return argv + ([] if use_stdin else [prompt])
    if host=="codex":
        require_flags(info,"--json")
        argv=[cap.binary,"exec","--json"]
        if model:
            require_flags(info,"--model")
            argv += ["--model",model]
        if autonomy=="bypass":
            # Alias is optional; use the full documented form if --yolo disappears.
            flag="--yolo" if "--yolo" in info["flags"] else "--dangerously-bypass-approvals-and-sandbox"
            require_flags(info,flag)
            argv.append(flag)
        else:
            require_flags(info,"--sandbox")
            argv += ["--sandbox","read-only" if read_only else "workspace-write"]
            if not read_only and autonomy=="auto":
                if "--ask-for-approval" in info["flags"]:
                    argv += ["--ask-for-approval","never"]
                elif "--approve-for-me" in info["flags"]:
                    argv.append("--approve-for-me")
        return argv + (["-"] if use_stdin else [prompt])
    if host=="opencode":
        require_flags(info,"--format")
        argv=[cap.binary,"run","--format","json"]
        if model:
            require_flags(info,"--model")
            argv += ["--model",model]
        # OpenCode --auto auto-approves only non-explicitly-denied permissions;
        # it is NOT equivalent to removing an OS sandbox.
        if not read_only and autonomy!="safe" and "--auto" in info["flags"]:argv.append("--auto")
        return argv+[prompt]
    raise SEHError("HOST_UNAVAILABLE",host,3)
