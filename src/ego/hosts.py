"""Host CLI adapters. All flags are checked against installed CLI help.

Permission bypass is permitted only when an externally isolated workspace has
been explicitly attested and the invocation explicitly opts into that risk.
AGY is the exception: its print mode has no other unattended approval path, so
write runs pass --dangerously-skip-permissions (read-only runs use plan mode).
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any
from .util import EgoError

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
    if host not in BINARIES: raise EgoError("UNKNOWN_HOST",host,2)
    if host=="mock":return HostCapability(host,None,True,True,"offline","Deterministic test adapter")
    path=shutil.which(BINARIES[host] or "")
    return HostCapability(host,path,bool(path),bool(path),"cli","ready" if path else "CLI not installed")


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_toml_dict(path: Path) -> dict[str, Any]:
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _normalize_opencode_mcp(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or entry.get("enabled") is False:
        return None
    cmd = entry.get("command")
    if isinstance(cmd, list) and cmd and isinstance(cmd[0], str) and cmd[0].strip():
        command, args = cmd[0], [str(a) for a in cmd[1:]]
    elif isinstance(cmd, str) and cmd.strip():
        command = cmd
        args = [str(a) for a in (entry.get("args") or [])] if isinstance(entry.get("args"), list) else []
    else:
        return None
    out: dict[str, Any] = {"command": command, "args": args}
    env = entry.get("environment") if isinstance(entry.get("environment"), dict) else entry.get("env")
    if isinstance(env, dict):
        out["env"] = env
    return out


def select_mcp_servers(cwd: Path | None, names: list[str] | tuple[str, ...], *, host: str | None = None) -> dict[str, Any]:
    wanted = [n for n in names if isinstance(n, str) and n]
    if not wanted:
        return {}
    servers: dict[str, Any] = {}
    claude_cfg = _read_json_dict(Path.home() / ".claude.json")
    if isinstance(claude_cfg.get("mcpServers"), dict):
        servers.update(claude_cfg["mcpServers"])
    if cwd is not None:
        projects = claude_cfg.get("projects")
        if isinstance(projects, dict):
            proj_cfg = projects.get(str(cwd.resolve()))
            if isinstance(proj_cfg, dict) and isinstance(proj_cfg.get("mcpServers"), dict):
                servers.update(proj_cfg["mcpServers"])
    if host == "agy":
        for cfg_path in (
            Path.home() / ".gemini" / "antigravity-cli" / "mcp_config.json",
            Path.home() / ".gemini" / "config" / "mcp_config.json",
        ):
            agy_cfg = _read_json_dict(cfg_path)
            if isinstance(agy_cfg.get("mcpServers"), dict):
                servers.update(agy_cfg["mcpServers"])
    elif host == "codex":
        codex_paths = [Path.home() / ".codex" / "config.toml"]
        if cwd is not None:
            codex_paths.append(cwd / ".codex" / "config.toml")
        for cfg_path in codex_paths:
            codex_cfg = _read_toml_dict(cfg_path)
            mcp_servers = codex_cfg.get("mcp_servers")
            if isinstance(mcp_servers, dict):
                for name, entry in mcp_servers.items():
                    if isinstance(entry, dict) and entry.get("enabled") is not False:
                        servers[name] = entry
    elif host == "opencode":
        oc_paths = [
            Path.home() / ".config" / "opencode" / "config.json",
            Path.home() / ".config" / "opencode" / "opencode.json",
        ]
        if cwd is not None:
            oc_paths.append(cwd / "opencode.json")
        for cfg_path in oc_paths:
            oc_cfg = _read_json_dict(cfg_path)
            mcp_map = oc_cfg.get("mcp")
            if isinstance(mcp_map, dict):
                for name, entry in mcp_map.items():
                    norm = _normalize_opencode_mcp(entry)
                    if norm is not None:
                        servers[name] = norm
    if cwd is not None:
        mcp_json = _read_json_dict(cwd / ".mcp.json")
        if isinstance(mcp_json.get("mcpServers"), dict):
            servers.update(mcp_json["mcpServers"])
    return {name: servers[name] for name in wanted if name in servers and isinstance(servers[name], dict)}


def build_command(host: str, prompt: str, *, model: str | None = None, effort: str | None = None,
                  read_only: bool = True, max_turns: int = 4, use_stdin: bool = False,
                  autonomy: str = "auto", isolated: bool = False, lean: bool = False,
                  mcp_servers: list[str] | tuple[str, ...] | None = None, cwd: Path | None = None,
                  max_budget_usd: float | None = None, resume_session: str | None = None) -> list[str]:
    cap=capability(host)
    if not cap.supported:raise EgoError("HOST_UNAVAILABLE",cap.reason,3)
    if host=="mock":return []
    if autonomy not in ("auto","safe","bypass"):
        raise EgoError("INVALID_AUTONOMY",autonomy,2)
    if autonomy=="bypass" and not isolated:
        raise EgoError("ISOLATION_REQUIRED","Permission bypass requires externally isolated execution and explicit acceptance",3)
    from .hostcompat import inspect_host,require_flags
    info=inspect_host(host)
    if host=="claude":
        require_flags(info,"--output-format")
        argv=[cap.binary,"-p","--output-format","json"]
        if "--max-turns" in info["flags"]:argv += ["--max-turns",str(max_turns)]
        # Lean workers do not inherit the user's MCP servers, except servers explicitly requested (e.g. alter).
        if lean and "--strict-mcp-config" in info["flags"]:
            argv.append("--strict-mcp-config")
            if mcp_servers and "--mcp-config" in info["flags"]:
                selected = select_mcp_servers(cwd, mcp_servers, host="claude")
                if selected:
                    argv += ["--mcp-config", json.dumps({"mcpServers": selected}, ensure_ascii=False)]
        if max_budget_usd is not None and max_budget_usd > 0 and "--max-budget-usd" in info["flags"]:
            argv += ["--max-budget-usd",f"{max_budget_usd:.2f}"]
        if model:
            require_flags(info,"--model")
            argv += ["--model",model]
        if effort and effort != "auto":
            require_flags(info,"--effort")
            argv += ["--effort",effort]
        if resume_session:
            require_flags(info,"--resume")
            argv += ["--resume",resume_session]
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
    if host=="agy":
        require_flags(info,"--print","--output-format")
        argv=[cap.binary,"--output-format","json"]
        if model:
            require_flags(info,"--model")
            argv += ["--model",model]
        if effort and effort != "auto" and "--effort" in info["flags"]:argv += ["--effort",effort]
        if (read_only or autonomy=="safe") and autonomy!="bypass":
            require_flags(info,"--mode")
            argv += ["--mode","plan"]
        else:
            # Print mode cannot answer a permission prompt, so an AGY write run is
            # always auto-approved. This is the operator's stated choice for this host;
            # the run still happens in the harness's staging copy or worktree.
            require_flags(info,"--dangerously-skip-permissions")
            argv.append("--dangerously-skip-permissions")
        # --print takes the prompt as its value; the attached form keeps a prompt
        # that starts with '-' from being read as another switch.
        return argv+["--print="+prompt]
    raise EgoError("HOST_UNAVAILABLE",host,3)
