"""File memory and optional MCP stdio adapter for Engram-like servers."""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import subprocess
import time
from typing import Any
from .util import EgoError, ident, now, read_json, write_json, exclusive_lock

class FileMemory:
    def __init__(self, root: Path):
        self.path = root/".ego"/"memory"/"items.json"
        self.lock = root/".ego"/"locks"/"memory.lock"
    def list(self) -> list[dict]:
        items = read_json(self.path, [])
        return items if isinstance(items,list) else []
    def add(self, text: str, *, tags: list[str]|None=None, evidence: list[str]|None=None, ttl_days: int|None=None) -> dict:
        if not text.strip() or len(text) > 20000: raise EgoError("INVALID_MEMORY", "Empty or too large", 2)
        item={"id":ident("mem"),"text":text,"tags":tags or [],"evidence":evidence or [],"created_at":now(),"ttl_days":ttl_days}
        with exclusive_lock(self.lock):
            items=self.list();items.append(item);write_json(self.path,items)
        return item
    def search(self, query: str, limit: int=10) -> list[dict]:
        terms={word.lower() for word in query.split() if word}
        today=time.time()
        scored=[]
        from datetime import datetime
        for item in self.list():
            if item.get("ttl_days") is not None:
                try:
                    created=datetime.fromisoformat(item["created_at"]).timestamp()
                    if today > created + item["ttl_days"]*86400: continue
                except (ValueError,KeyError): continue
            corpus=(item["text"]+" "+" ".join(item.get("tags",[]))).lower()
            score=sum(1 for word in terms if word in corpus)
            if score: scored.append((score,item))
        return [obj for _,obj in sorted(scored,key=lambda t:t[0],reverse=True)[:limit]]
    def delete(self,id:str) -> bool:
        with exclusive_lock(self.lock):
            items=self.list();new=[x for x in items if x["id"]!=id]
            if len(new)==len(items): return False
            write_json(self.path,new)
            return True

class MCPStdio:
    """Minimal newline-delimited MCP client. Requires a trusted locally configured command."""
    def __init__(self, command: list[str], timeout:int=10, env: dict[str, str] | None = None):
        if not command or not Path(command[0]).name: raise EgoError("MCP_CONFIG", "Missing command", 2)
        self.command,self.timeout,self.env=command,timeout,env
    def call_tool(self, name: str, args:dict) -> Any:
        # Protocol version is intentionally configurable by server negotiation at init.
        merged_env = {**os.environ, **{str(k): str(v) for k, v in self.env.items()}} if self.env else None
        p=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           bufsize=0,env=merged_env)
        assert p.stdin and p.stdout
        read_buffer=b""
        def send(obj:dict):
            p.stdin.write((json.dumps(obj,separators=(",", ":"))+"\n").encode("utf8"));p.stdin.flush()
        def receive(req_id:int):
            nonlocal read_buffer
            import select
            deadline=time.monotonic()+self.timeout
            while time.monotonic()<deadline:
                if b"\n" in read_buffer:
                    line,read_buffer=read_buffer.split(b"\n",1)
                    try: obj=json.loads(line)
                    except ValueError: continue
                    if obj.get("id")==req_id:
                        if "error" in obj: raise EgoError("MCP_ERROR",str(obj["error"]))
                        return obj.get("result")
                ready,_,_=select.select([p.stdout],[],[],min(0.5,max(0,deadline-time.monotonic())))
                if ready:
                    chunk=os.read(p.stdout.fileno(),65536)
                    if not chunk: break
                    read_buffer+=chunk
            raise EgoError("MCP_TIMEOUT",name,5)
        try:
            send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ego","version":"1.4"}}})
            receive(1)
            send({"jsonrpc":"2.0","method":"notifications/initialized"})
            send({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}})
            return receive(2)
        finally:
            p.terminate()
            try:p.wait(timeout=2)
            except subprocess.TimeoutExpired:p.kill();p.wait()

class EngramMemory:
    """Optional configured Engram MCP. No silent fallback to a separate memory truth."""
    def __init__(self):
        raw=os.environ.get("EGO_ENGRAM_MCP_COMMAND", "")
        if not raw: raise EgoError("ENGRAM_UNAVAILABLE","Set EGO_ENGRAM_MCP_COMMAND and tool names",3)
        self.mcp=MCPStdio(shlex.split(raw))
    def add(self,text:str,**kw)->dict:
        tool=os.environ.get("EGO_ENGRAM_WRITE_TOOL")
        if not tool: raise EgoError("ENGRAM_CONFIG", "EGO_ENGRAM_WRITE_TOOL is required",3)
        return {"provider":"engram","result":self.mcp.call_tool(tool,{"content":text,"tags":kw.get("tags",[])})}
    def search(self,query:str,limit:int=10)->list[dict]:
        tool=os.environ.get("EGO_ENGRAM_SEARCH_TOOL")
        if not tool: raise EgoError("ENGRAM_CONFIG", "EGO_ENGRAM_SEARCH_TOOL is required",3)
        return [{"provider":"engram","result":self.mcp.call_tool(tool,{"query":query,"limit":limit})}]

ALTER_HOST_HINT="Use host alter MCP tools (alter_task_context / alter_search / alter_get_note) or set EGO_ALTER_MCP_COMMAND"
ALTER_SKILLS_SOURCE="github:jumtra/alter-skills"
ALTER_SKILLS_CLIENTS=("claude","codex","opencode","agy")
ALTER_SKILLS_MARKER="alter-context-loader"

def alter_skills_installed(root: Path, home: Path | None = None) -> bool:
    """Return True if alter-skills appears installed in global or project skill dirs."""
    home = home or Path.home()
    dirs = (
        home/".claude"/"skills", home/".codex"/"skills",
        home/".gemini"/"config"/"skills", home/".config"/"opencode"/"skills",
        root/".claude"/"skills", root/".agents"/"skills", root/".opencode"/"skills",
    )
    return any((d/ALTER_SKILLS_MARKER).exists() for d in dirs)

def install_alter_skills(root: Path, *, clients: list[str]|tuple[str,...]|None=None, scope: str="global",
                         agents: bool=True, dry_run: bool=False) -> dict[str, Any]:
    """Install external alter-skills from github:jumtra/alter-skills via npx."""
    if scope not in ("global", "project"):
        raise EgoError("INVALID_ARGUMENT", f"Unsupported scope: {scope} (use global or project)", 2)
    selected = [c.strip().lower() for c in (clients or ALTER_SKILLS_CLIENTS) if str(c).strip()]
    if not selected or any(c not in ALTER_SKILLS_CLIENTS for c in selected):
        raise EgoError("INVALID_ARGUMENT", f"clients must be a subset of {', '.join(ALTER_SKILLS_CLIENTS)}", 2)
    npx = shutil.which("npx")
    if not npx and not dry_run:
        raise EgoError("NPX_MISSING", "npx (Node.js >= 20) is required to install alter-skills", 3)
    argv = [npx or "npx", "--yes", ALTER_SKILLS_SOURCE, "--client", ",".join(selected), "--scope", scope]
    if scope == "project":
        argv += ["--project", str(root)]
    if not agents:
        argv.append("--no-agents")
    if dry_run:
        argv.append("--dry-run")
    if dry_run and not npx:
        return {"status": "DRY_RUN", "source": ALTER_SKILLS_SOURCE, "scope": scope,
                "clients": selected, "agents": agents, "command": argv, "output": ""}
    proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise EgoError("ALTER_SKILLS_INSTALL_FAILED", detail[:500], 5)
    return {"status": "DRY_RUN" if dry_run else "INSTALLED", "source": ALTER_SKILLS_SOURCE,
            "scope": scope, "clients": selected, "agents": agents, "command": argv,
            "output": (proc.stdout or "").strip()}

def _alter_registry_path() -> Path:
    return Path(os.environ.get("EGO_HOME") or Path.home() / ".config" / "ego") / "alter_projects.json"


def _alter_mcp_spec(root: Path) -> tuple[list[str], dict[str, str] | None] | None:
    raw = os.environ.get("EGO_ALTER_MCP_COMMAND", "").strip()
    if raw:
        return shlex.split(raw), None
    from .hosts import select_mcp_servers
    cfg = select_mcp_servers(root, ["alter"]).get("alter")
    if isinstance(cfg, dict) and isinstance(cfg.get("command"), str) and cfg["command"].strip():
        cmd = [cfg["command"], *[str(a) for a in (cfg.get("args") or [])]]
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else None
        return cmd, env
    return None


def ensure_alter_project(root: Path, *, force: bool = False, timeout: int = 10) -> dict[str, Any]:
    """Ensure the repository at root is registered in Alter via alter_bootstrap(cwd=...)."""
    if os.environ.get("EGO_ALTER_AUTO_REGISTER", "1").lower() in ("0", "false", "off", "no"):
        return {"status": "skipped", "reason": "disabled"}
    key = str(root.resolve())
    reg_path = _alter_registry_path()
    reg = read_json(reg_path)
    projects = reg.get("projects") if isinstance(reg, dict) and isinstance(reg.get("projects"), dict) else {}
    if not force and key in projects:
        return {"status": "already_registered", "project": key}
    spec = _alter_mcp_spec(root)
    if spec is None:
        return {"status": "skipped", "reason": "alter MCP not configured"}
    cmd, env = spec
    try:
        MCPStdio(cmd, timeout=timeout, env=env).call_tool("alter_bootstrap", {"cwd": key})
    except (EgoError, OSError, ValueError) as exc:
        return {"status": "error", "reason": str(exc)}
    updated = dict(projects)
    updated[key] = {"registered_at": now()}
    write_json(reg_path, {"schema_version": 1, "projects": updated})
    return {"status": "registered", "project": key}


def ensure_alter_project_quietly(root: Path, *, force: bool = False, timeout: int = 10) -> dict[str, Any]:
    try:
        return ensure_alter_project(root, force=force, timeout=timeout)
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}


class AlterMemory:
    """alter-mcp provider. Host-configured alter MCP is the default; EGO_ALTER_MCP_COMMAND enables direct CLI calls."""
    def __init__(self, root: Path):
        self.root = root
        raw=os.environ.get("EGO_ALTER_MCP_COMMAND", "")
        self.mcp=MCPStdio(shlex.split(raw)) if raw else None
    def _host_guidance(self) -> dict:
        return {"provider":"alter","mode":"host-mcp","hint":ALTER_HOST_HINT}
    def add(self,text:str,**kw)->dict:
        if self.mcp is None: return self._host_guidance()
        tool=os.environ.get("EGO_ALTER_WRITE_TOOL","alter_create_note")
        return {"provider":"alter","result":self.mcp.call_tool(tool,{"content":text,"tags":kw.get("tags",[])})}
    def search(self,query:str,limit:int=10)->list[dict]:
        if self.mcp is None: return [self._host_guidance()]
        tool=os.environ.get("EGO_ALTER_SEARCH_TOOL","alter_search")
        return [{"provider":"alter","result":self.mcp.call_tool(tool,{"query":query,"limit":limit})}]

def memory_provider(root:Path, name:str):
    if name == "file": return FileMemory(root)
    if name == "engram": return EngramMemory()
    if name == "alter": return AlterMemory(root)
    raise EgoError("UNKNOWN_MEMORY",name,2)

def memory_health(root:Path, name:str) -> dict:
    """Read-only provider status for ego doctor. Never starts an MCP server."""
    if name == "file":
        fm=FileMemory(root)
        return {"provider":"file","status":"ok","path":str(fm.path),"items":len(fm.list())}
    if name == "engram":
        configured=bool(os.environ.get("EGO_ENGRAM_MCP_COMMAND"))
        tools_ok=bool(os.environ.get("EGO_ENGRAM_SEARCH_TOOL")) and bool(os.environ.get("EGO_ENGRAM_WRITE_TOOL"))
        return {"provider":"engram","status":"ok" if configured and tools_ok else "unconfigured",
                "command_configured":configured,"tools_configured":tools_ok}
    if name == "alter":
        configured=bool(os.environ.get("EGO_ALTER_MCP_COMMAND"))
        return {"provider":"alter","status":"ok" if configured else "host-managed",
                "mode":"mcp-cli" if configured else "host-mcp","command_configured":configured,
                "skills_installed":alter_skills_installed(root),
                "hint":None if configured else ALTER_HOST_HINT}
    raise EgoError("UNKNOWN_MEMORY",name,2)
