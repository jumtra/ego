"""Process memory (file store), the alter MCP stdio adapter and the alter session/checkpoint recording."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import subprocess
import time
from typing import Any
from ..shared.util import EgoError, alter_project_root, ident, now, read_json, write_json, exclusive_lock, redact, sha256

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

ALTER_HOST_HINT="Add the alter MCP server to the host (alter_task_context / alter_ai_usage) or set EGO_ALTER_MCP_COMMAND"
ALTER_SKILLS_VERSION="v0.2.1"
ALTER_SKILLS_SOURCE=f"github:jumtra/alter-skills#{ALTER_SKILLS_VERSION}"
ALTER_SKILLS_CLIENTS=("claude","codex","opencode","agy")
ALTER_SKILLS_MARKER="alter-context-loader"

def _client_skill_dirs(root: Path, home: Path, client: str) -> tuple[Path, ...]:
    if client == "claude":
        return (home / ".claude" / "skills", root / ".claude" / "skills")
    if client == "codex":
        return (home / ".codex" / "skills", root / ".agents" / "skills")
    if client == "agy":
        return (home / ".gemini" / "config" / "skills", root / ".agents" / "skills")
    if client == "opencode":
        return (home / ".config" / "opencode" / "skills", root / ".opencode" / "skills")
    return ()


def alter_skills_installed(
    root: Path,
    home: Path | None = None,
    *,
    clients: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Return True if alter-skills appears installed in global or project skill dirs."""
    home = home or Path.home()
    if clients is not None:
        selected = [c.strip().lower() for c in clients if isinstance(c, str) and c.strip() in ALTER_SKILLS_CLIENTS]
        if selected:
            return all(
                any((d / ALTER_SKILLS_MARKER).exists() for d in _client_skill_dirs(root, home, client))
                for client in selected
            )
    dirs = (
        home/".claude"/"skills", home/".codex"/"skills",
        home/".gemini"/"config"/"skills", home/".config"/"opencode"/"skills",
        root/".claude"/"skills", root/".agents"/"skills", root/".opencode"/"skills",
    )
    return any((d/ALTER_SKILLS_MARKER).exists() for d in dirs)

def _cached_alter_skills_script() -> Path | None:
    """Locate alter-skills bin/install.mjs in npm's _npx cache (workaround for symlink entrypoint check)."""
    npx_cache = Path.home() / ".npm" / "_npx"
    if not npx_cache.is_dir():
        return None
    candidates = [p for p in npx_cache.glob("*/node_modules/alter-skills/bin/install.mjs") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


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
    if proc.returncode == 0 and not dry_run and not (proc.stdout or "").strip():
        node = shutil.which("node")
        script = _cached_alter_skills_script()
        if node and script is not None:
            proc = subprocess.run([node, str(script), *argv[3:]], cwd=str(root), capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise EgoError("ALTER_SKILLS_INSTALL_FAILED", detail[:500], 5)
    return {"status": "DRY_RUN" if dry_run else "INSTALLED", "source": ALTER_SKILLS_SOURCE,
            "scope": scope, "clients": selected, "agents": agents, "command": argv,
            "output": (proc.stdout or "").strip()}


def ensure_alter_skills_quietly(root: Path, *, clients: list[str]|tuple[str,...]|None=None) -> dict[str, Any]:
    """Install alter-skills globally when memory='alter' and skills are not yet installed (fail-open)."""
    if os.environ.get("EGO_ALTER_AUTO_INSTALL_SKILLS", "1").strip().lower() in ("0", "false", "off", "no"):
        return {"status": "skipped", "reason": "disabled"}
    if alter_skills_installed(root, clients=clients):
        return {"status": "already_installed"}
    try:
        return install_alter_skills(root, clients=clients, scope="global")
    except (EgoError, OSError, subprocess.SubprocessError) as exc:
        return {"status": "skipped" if getattr(exc, "code", "") == "NPX_MISSING" else "error", "reason": str(exc)}

def _alter_registry_path() -> Path:
    return Path(os.environ.get("EGO_HOME") or Path.home() / ".config" / "ego") / "alter_projects.json"


_ACTIVE_HOST: ContextVar[str | None] = ContextVar("ego_active_host", default=None)


@contextmanager
def use_host(host: str | None):
    """Temporarily bind the active host CLI name for MCP server resolution."""
    norm = host.strip().lower() if isinstance(host, str) and host.strip() else None
    token = _ACTIVE_HOST.set(norm)
    try:
        yield
    finally:
        _ACTIVE_HOST.reset(token)


def _resolve_host() -> str | None:
    active = _ACTIVE_HOST.get()
    if active:
        return active
    from ..shared.util import detect_host
    return detect_host()


def _alter_mcp_spec(root: Path) -> tuple[list[str], dict[str, str] | None] | None:
    raw = os.environ.get("EGO_ALTER_MCP_COMMAND", "").strip()
    if raw:
        return shlex.split(raw), None
    from .hosts import select_mcp_servers
    cfg = select_mcp_servers(root, ["alter"], host=_resolve_host()).get("alter")
    if isinstance(cfg, dict) and isinstance(cfg.get("command"), str) and cfg["command"].strip():
        cmd = [cfg["command"], *[str(a) for a in (cfg.get("args") or [])]]
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else None
        return cmd, env
    return None


def ensure_alter_project(root: Path, *, force: bool = False, timeout: int = 10) -> dict[str, Any]:
    """Ensure the repository at root is registered in Alter via alter_bootstrap(cwd=...)."""
    if os.environ.get("EGO_ALTER_AUTO_REGISTER", "1").lower() in ("0", "false", "off", "no"):
        return {"status": "skipped", "reason": "disabled"}
    key = str(alter_project_root(root).resolve())
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


_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})


def _extract_text_blocks(content: Any) -> str:
    """Extract only explicit text blocks, excluding thinking, reasoning, and tool blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block.strip():
                parts.append(block)
        elif isinstance(block, dict):
            btype = str(block.get("type") or "").strip().lower()
            if btype in _TEXT_BLOCK_TYPES or (not btype and isinstance(block.get("text"), str)):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
    return "\n".join(parts)


def _make_turn(
    role: str,
    raw_text: str,
    *,
    turn_id: Any = None,
    sequence: Any = None,
    at: Any = None,
) -> dict[str, Any] | None:
    cleaned = redact(raw_text).strip()
    if not cleaned:
        return None
    turn: dict[str, Any] = {"role": role, "text": cleaned}
    if isinstance(turn_id, str) and turn_id.strip():
        turn["id"] = turn_id.strip()
    if isinstance(sequence, int) and not isinstance(sequence, bool):
        turn["sequence"] = sequence
    if isinstance(at, str) and at.strip():
        turn["at"] = at.strip()
    elif isinstance(at, (int, float)) and not isinstance(at, bool):
        turn["at"] = str(at)
    return turn


def _extract_turn_from_obj(obj: dict[str, Any], host: str) -> dict[str, Any] | None:
    """Normalize a single Claude/Codex/AGY/OpenCode record into a user/assistant turn."""
    # AGY transcript format: USER_INPUT (non-SYSTEM) / PLANNER_RESPONSE (MODEL)
    otype = obj.get("type")
    source = obj.get("source")
    if otype in ("USER_INPUT", "PLANNER_RESPONSE"):
        if source == "SYSTEM":
            return None
        role = "user" if otype == "USER_INPUT" else "assistant"
        if otype == "PLANNER_RESPONSE" and source not in (None, "MODEL"):
            return None
        content = obj.get("content")
        text = content if isinstance(content, str) else _extract_text_blocks(content)
        return _make_turn(
            role,
            text,
            turn_id=obj.get("id"),
            sequence=obj.get("step_index"),
            at=obj.get("created_at"),
        )

    # Codex rollout event_msg: user_message / agent_message
    if otype == "event_msg" and isinstance(obj.get("payload"), dict):
        payload = obj["payload"]
        ptype = payload.get("type")
        if ptype in ("user_message", "agent_message"):
            role = "user" if ptype == "user_message" else "assistant"
            raw = payload.get("message") if isinstance(payload.get("message"), str) else payload.get("text", "")
            return _make_turn(
                role,
                raw if isinstance(raw, str) else "",
                turn_id=payload.get("id") or obj.get("id"),
                sequence=obj.get("sequence"),
                at=obj.get("timestamp") or obj.get("at"),
            )
        return None

    # Codex rollout response_item: payload.type == "message"
    if otype == "response_item" and isinstance(obj.get("payload"), dict):
        payload = obj["payload"]
        if payload.get("type") != "message":
            return None
        role = str(payload.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            return None
        text = _extract_text_blocks(payload.get("content"))
        return _make_turn(
            role,
            text,
            turn_id=payload.get("id") or obj.get("id"),
            sequence=obj.get("sequence"),
            at=obj.get("timestamp") or obj.get("at"),
        )

    # Claude Code JSONL: type in ("user", "assistant"), message: {role, content}
    if otype in ("user", "assistant") and isinstance(obj.get("message"), dict):
        if obj.get("isMeta"):
            return None
        msg = obj["message"]
        role = str(msg.get("role") or otype).strip().lower()
        if role not in ("user", "assistant"):
            return None
        text = _extract_text_blocks(msg.get("content"))
        return _make_turn(
            role,
            text,
            turn_id=msg.get("id") or obj.get("uuid"),
            sequence=obj.get("sequence"),
            at=obj.get("timestamp") or obj.get("at"),
        )

    # OpenCode SDK message shape: {"info": {"id", "role", "time": {"created"}}, "parts": [...]}
    if isinstance(obj.get("info"), dict) and "parts" in obj:
        info = obj["info"]
        role = str(info.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            return None
        text = _extract_text_blocks(obj.get("parts"))
        time_info = info.get("time") if isinstance(info.get("time"), dict) else {}
        return _make_turn(
            role,
            text,
            turn_id=info.get("id"),
            sequence=info.get("sequence") or obj.get("sequence"),
            at=time_info.get("created") or info.get("at"),
        )

    # Generic / OpenCode flat message: {"role": "user"|"assistant", "text"|"content"|"parts": ...}
    role = str(obj.get("role") or "").strip().lower()
    if role in ("user", "assistant") and not obj.get("isMeta"):
        if "text" in obj and isinstance(obj.get("text"), str):
            text = obj["text"]
        elif "parts" in obj:
            text = _extract_text_blocks(obj.get("parts"))
        else:
            text = _extract_text_blocks(obj.get("content"))
        return _make_turn(
            role,
            text,
            turn_id=obj.get("id"),
            sequence=obj.get("sequence"),
            at=obj.get("at") or obj.get("timestamp") or obj.get("created_at"),
        )

    return None


def extract_session_messages(
    transcript: Path | str | None = None,
    *,
    host: str = "claude",
    messages: list[dict[str, Any]] | None = None,
    max_turns: int = 80,
) -> list[dict[str, Any]]:
    """Extract chronological user/assistant text turns from Claude/Codex/AGY JSONL or OpenCode messages."""
    if max_turns <= 0:
        return []
    norm_host = (host or "claude").strip().lower()
    turns: list[dict[str, Any]] = []

    if messages is not None:
        for item in messages:
            if isinstance(item, dict):
                turn = _extract_turn_from_obj(item, norm_host)
                if turn is not None:
                    turns.append(turn)

    if transcript is not None:
        path = Path(transcript).expanduser()
        if path.is_file():
            try:
                raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                raw_lines = []
            for line in raw_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    turn = _extract_turn_from_obj(obj, norm_host)
                    if turn is not None:
                        turns.append(turn)

    if len(turns) > max_turns:
        turns = turns[-max_turns:]
    return turns


def _alter_session_state_path(root: Path) -> Path:
    return root.resolve() / ".ego" / "state" / "alter_sessions.json"


def _alter_auto_review_enabled() -> bool:
    return os.environ.get("EGO_ALTER_AUTO_REVIEW", "1").strip().lower() not in ("0", "false", "off", "no")


def _safe_sid(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(sid or "").strip()) or "unknown"


def alter_run_session_id(run_id: str) -> str:
    """Logical Alter session_id shared across automatic /clear rotations of a single `ego run`."""
    return f"ego-run-{run_id}"


def resolve_alter_session(host_session_id: str, transcript: Path | str | None = None) -> tuple[str, str | None]:
    """Return `(session_id, segment_id)` for Alter recording, grouping `ego run` rotations under `EGO_RUN_ID`."""
    run_id = os.environ.get("EGO_RUN_ID", "").strip() if os.environ.get("EGO_RUN") else ""
    sid = str(host_session_id or "").strip()
    if run_id:
        seg = sid or (f"transcript-{sha256(str(transcript).strip())[:12]}" if transcript and str(transcript).strip() else "default")
        return alter_run_session_id(run_id), seg
    return sid, None


def alter_turns_path(root: Path, sid: str) -> Path:
    return root.resolve() / ".ego" / "state" / "alter_turns" / f"{_safe_sid(sid)}.json"


def alter_segments_path(root: Path, sid: str) -> Path:
    return root.resolve() / ".ego" / "state" / "alter_segments" / f"{_safe_sid(sid)}.json"


def _upsert_segment(existing: list[dict[str, Any]], segment_id: str, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace `segment_id` in place if already recorded, otherwise append a new segment."""
    out: list[dict[str, Any]] = []
    replaced = False
    for seg in existing:
        if not isinstance(seg, dict) or not isinstance(seg.get("turns"), list):
            continue
        if seg.get("id") == segment_id:
            out.append({"id": segment_id, "turns": turns})
            replaced = True
        else:
            out.append({"id": str(seg.get("id") or ""), "turns": seg["turns"]})
    if not replaced:
        out.append({"id": segment_id, "turns": turns})
    return out


def _merge_segment_turns(segments: list[dict[str, Any]], *, max_turns: int = 200) -> list[dict[str, Any]]:
    """Concatenate segment turns in order and renumber sequence/auto-generated ids to avoid collisions across /clear."""
    merged: list[dict[str, Any]] = []
    for seg in segments:
        turns = seg.get("turns")
        if isinstance(turns, list):
            merged.extend(turns)
    if len(merged) > max_turns:
        merged = merged[-max_turns:]
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for seq, turn in enumerate(merged):
        item = dict(turn)
        item["sequence"] = seq
        raw_id = item.get("id")
        if not (isinstance(raw_id, str) and raw_id.strip()) or re.fullmatch(r"turn-\d+", raw_id.strip()):
            tid = f"turn-{seq}"
        else:
            tid = raw_id.strip()
        if tid in seen_ids:
            base = tid
            tid = f"{base}-{seq}"
            suffix = 1
            while tid in seen_ids:
                tid = f"{base}-{seq}-{suffix}"
                suffix += 1
        seen_ids.add(tid)
        item["id"] = tid
        out.append(item)
    return out


def _ensure_turn_ids(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return copies of the turns with the id and sequence alter assigns when storing them.

    alter defaults a missing sequence to the index and a missing id to `turn-{sequence}`; the
    analyzer cites these ids, so they must match what alter stored for the conversation."""
    out: list[dict[str, Any]] = []
    for index, turn in enumerate(turns):
        item = dict(turn)
        if not (isinstance(item.get("sequence"), int) and not isinstance(item.get("sequence"), bool)):
            item["sequence"] = index
        if not (isinstance(item.get("id"), str) and item["id"].strip()):
            item["id"] = f"turn-{item['sequence']}"
        out.append(item)
    return out


def _conversation_id(result: Any) -> str | None:
    """Tolerantly extract the conversation id from an alter_record_conversation result."""
    if not isinstance(result, dict):
        return None
    containers: list[Any] = [result, result.get("structuredContent"), result.get("conversation")]
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                try:
                    parsed = json.loads(item["text"])
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    containers.extend([parsed, parsed.get("conversation")])
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("conversation_id", "conversationId", "id"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def submit_alter_session_review(
    root: Path,
    *,
    session_id: str,
    host: str,
    transcript: Path | str | None = None,
    messages: list[dict[str, Any]] | None = None,
    title: str | None = None,
    timeout: int = 10,
    keep_turns: bool = False,
    segment_id: str | None = None,
) -> dict[str, Any]:
    """Extract, redact, deduplicate, and submit session turns to Alter fail-open.

    With segment_id, turns from multiple cleared host segments are accumulated under one
    logical session_id so automatic /clear rotations record as a single Alter conversation.
    With keep_turns=True, a successful submission also persists the turns and the Alter
    conversation id to `alter_turns_path` so a later analyzer can attach to that conversation."""
    if not _alter_auto_review_enabled():
        return {"status": "skipped", "reason": "disabled"}
    sid = str(session_id or "").strip()
    if not sid:
        return {"status": "skipped", "reason": "missing session_id"}
    try:
        extracted = extract_session_messages(transcript, host=host, messages=messages)
        if not extracted:
            return {"status": "skipped", "reason": "empty", "session_id": sid}

        seg_key = str(segment_id or "").strip()
        segments_to_save: list[dict[str, Any]] | None = None
        if seg_key:
            seg_data = read_json(alter_segments_path(root, sid), {})
            existing = seg_data.get("segments") if isinstance(seg_data, dict) and isinstance(seg_data.get("segments"), list) else []
            segments_to_save = _upsert_segment(existing, seg_key, extracted)
            extracted = _merge_segment_turns(segments_to_save)

        digest = sha256(json.dumps(extracted, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        state_path = _alter_session_state_path(root)
        state = read_json(state_path, {})
        sessions = state.get("sessions") if isinstance(state, dict) and isinstance(state.get("sessions"), dict) else {}
        prev = sessions.get(sid)
        if isinstance(prev, dict) and prev.get("sha256") == digest and prev.get("count") == len(extracted):
            return {"status": "skipped", "reason": "unchanged", "session_id": sid, "count": len(extracted)}

        with use_host(host):
            spec = _alter_mcp_spec(root)
        if spec is None:
            return {"status": "skipped", "reason": "alter MCP not configured", "session_id": sid}
        cmd, env = spec

        args: dict[str, Any] = {
            "cwd": str(alter_project_root(root).resolve()),
            "session_id": sid,
            "messages": extracted,
            "capture": "important",
        }
        if title and str(title).strip():
            args["title"] = redact(str(title).strip())

        result = MCPStdio(cmd, timeout=timeout, env=env).call_tool("alter_record_conversation", args)

        if segments_to_save is not None:
            write_json(alter_segments_path(root, sid), {
                "schema_version": 1,
                "session_id": sid,
                "segments": segments_to_save,
                "updated_at": now(),
            })

        updated_sessions = dict(sessions)
        updated_sessions[sid] = {
            "count": len(extracted),
            "sha256": digest,
            "host": host,
            "updated_at": now(),
        }
        write_json(state_path, {"schema_version": 1, "sessions": updated_sessions})
        conversation_id = _conversation_id(result)
        if keep_turns:
            write_json(alter_turns_path(root, sid), {
                "session_id": sid,
                "host": host,
                "conversation_id": conversation_id,
                "turns": _ensure_turn_ids(extracted),
                "recorded_at": now(),
            })
        from ..domain.retention import prune_state
        prune_state(root)
        response: dict[str, Any] = {"status": "submitted", "session_id": sid, "count": len(extracted), "result": result}
        if keep_turns:
            response["conversation_id"] = conversation_id
        return response
    except Exception as exc:
        return {"status": "error", "reason": str(exc), "session_id": sid}


def _checkpoint_session_id(root: Path, checkpoint: dict[str, Any], task_id: str) -> str:
    """The Alter session a checkpoint's digest belongs to, so one ego run's digests land in one session."""
    from ..usecases.session import RUN_SESSION_ENV
    if str(checkpoint.get("session_id") or "").strip():
        return str(checkpoint["session_id"]).strip()
    run = str(checkpoint.get("run") or "").strip()
    if run:
        return alter_run_session_id(run)
    bound = read_json(Path(os.environ[RUN_SESSION_ENV]), {}) if os.environ.get(RUN_SESSION_ENV) else {}
    host_sid = bound.get("session_id") if isinstance(bound, dict) else None
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID"):
        host_sid = host_sid or os.environ.get(name)
    if host_sid:
        return str(host_sid).strip()
    state = read_json(_alter_session_state_path(root), {})
    sessions = state.get("sessions") if isinstance(state, dict) and isinstance(state.get("sessions"), dict) else {}
    latest = max((k for k, v in sessions.items() if isinstance(v, dict)),
                 key=lambda k: str(sessions[k].get("updated_at", "")), default=None)
    return latest or (f"checkpoint-{task_id}" if task_id else "")


def submit_alter_checkpoint_review(
    root: Path,
    checkpoint: dict[str, Any],
    *,
    timeout: int = 10,
) -> dict[str, Any]:
    """Submit compact checkpoint facts to Alter via alter_session_digest(action='patch') fail-open."""
    if not _alter_auto_review_enabled():
        return {"status": "skipped", "reason": "disabled"}
    if not isinstance(checkpoint, dict):
        return {"status": "skipped", "reason": "invalid checkpoint"}
    try:
        task_id = str(checkpoint.get("task_id") or "").strip()
        sid = _checkpoint_session_id(root, checkpoint, task_id)
        if not sid:
            return {"status": "skipped", "reason": "missing session_id"}

        goal = str(checkpoint.get("goal") or "").strip()
        status = str(checkpoint.get("status") or "IN_PROGRESS").strip()
        title = redact(goal or task_id or sid)[:120]

        add: list[dict[str, str]] = []
        for item in (checkpoint.get("decisions") or [])[:8]:
            text = redact(str(item)).strip()
            if text:
                add.append({"kind": "decision", "text": text})
        for item in (checkpoint.get("blockers") or [])[:8]:
            text = redact(str(item)).strip()
            if text:
                add.append({"kind": "blocker", "text": text})
        for item in (checkpoint.get("completed") or [])[:8]:
            text = redact(str(item)).strip()
            if text:
                add.append({"kind": "fact", "text": f"Completed: {text}"})
        for item in (checkpoint.get("evidence") or [])[:8]:
            text = redact(str(item)).strip()
            if text:
                add.append({"kind": "evidence", "text": text})
        for item in (checkpoint.get("next_steps") or [])[:8]:
            text = redact(str(item)).strip()
            if text:
                add.append({"kind": "next", "text": text})
        if not add and goal:
            add.append({"kind": "goal", "text": redact(f"[{status}] {goal}")})
        if not add:
            return {"status": "skipped", "reason": "empty", "session_id": sid}

        cp_host = checkpoint.get("host") if isinstance(checkpoint.get("host"), str) else None
        with use_host(cp_host or _ACTIVE_HOST.get()):
            spec = _alter_mcp_spec(root)
        if spec is None:
            return {"status": "skipped", "reason": "alter MCP not configured", "session_id": sid}
        cmd, env = spec

        args = {
            "action": "patch",
            "cwd": str(alter_project_root(root).resolve()),
            "session_id": sid,
            "title": title,
            "add": add,
        }
        result = MCPStdio(cmd, timeout=timeout, env=env).call_tool("alter_session_digest", args)
        return {"status": "submitted", "session_id": sid, "count": len(add), "result": result}
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}


def memory_health(root: Path) -> dict:
    """Process memory (`ego memory`): always the local file store."""
    fm = FileMemory(root)
    return {"provider": "file", "status": "ok", "path": str(fm.path), "items": len(fm.list())}


def knowledge_health(root: Path, name: str) -> dict:
    """Read-only status of where project knowledge lives. Never starts an MCP server."""
    if name == "file":
        from .knowledge import KnowledgeStore
        store = KnowledgeStore(root)
        return {"provider": "file", "status": "ok", "index": str(store.index_path), "topics": len(store.entries())}
    if name == "alter":
        configured = bool(os.environ.get("EGO_ALTER_MCP_COMMAND"))
        resolved = configured or _alter_mcp_spec(root) is not None  # resolved from host config only; no server is started
        return {"provider": "alter", "status": "ok" if resolved else "host-managed",
                "mode": "mcp-cli" if resolved else "host-mcp", "command_configured": configured, "mcp_resolved": resolved,
                "skills_installed": alter_skills_installed(root),
                "hint": None if configured else ALTER_HOST_HINT}
    raise EgoError("UNKNOWN_KNOWLEDGE", name, 2)
