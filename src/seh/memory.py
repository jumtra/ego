"""File memory and optional MCP stdio adapter for Engram-like servers."""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import time
from typing import Any
from .util import SEHError, ident, now, read_json, write_json, exclusive_lock

class FileMemory:
    def __init__(self, root: Path):
        self.path = root/".ego"/"memory"/"items.json"
        self.lock = root/".ego"/"locks"/"memory.lock"
    def list(self) -> list[dict]:
        items = read_json(self.path, [])
        return items if isinstance(items,list) else []
    def add(self, text: str, *, tags: list[str]|None=None, evidence: list[str]|None=None, ttl_days: int|None=None) -> dict:
        if not text.strip() or len(text) > 20000: raise SEHError("INVALID_MEMORY", "Empty or too large", 2)
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
    def __init__(self, command: list[str], timeout:int=10):
        if not command or not Path(command[0]).name: raise SEHError("MCP_CONFIG", "Missing command", 2)
        self.command,self.timeout=command,timeout
    def call_tool(self, name: str, args:dict) -> Any:
        # Protocol version is intentionally configurable by server negotiation at init.
        p=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           bufsize=0)
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
                        if "error" in obj: raise SEHError("MCP_ERROR",str(obj["error"]))
                        return obj.get("result")
                ready,_,_=select.select([p.stdout],[],[],min(0.5,max(0,deadline-time.monotonic())))
                if ready:
                    chunk=os.read(p.stdout.fileno(),65536)
                    if not chunk: break
                    read_buffer+=chunk
            raise SEHError("MCP_TIMEOUT",name,5)
        try:
            send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"seh","version":"1.4"}}})
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
        raw=os.environ.get("SEH_ENGRAM_MCP_COMMAND", "")
        if not raw: raise SEHError("ENGRAM_UNAVAILABLE","Set SEH_ENGRAM_MCP_COMMAND and tool names",3)
        self.mcp=MCPStdio(shlex.split(raw))
    def add(self,text:str,**kw)->dict:
        tool=os.environ.get("SEH_ENGRAM_WRITE_TOOL")
        if not tool: raise SEHError("ENGRAM_CONFIG", "SEH_ENGRAM_WRITE_TOOL is required",3)
        return {"provider":"engram","result":self.mcp.call_tool(tool,{"content":text,"tags":kw.get("tags",[])})}
    def search(self,query:str,limit:int=10)->list[dict]:
        tool=os.environ.get("SEH_ENGRAM_SEARCH_TOOL")
        if not tool: raise SEHError("ENGRAM_CONFIG", "SEH_ENGRAM_SEARCH_TOOL is required",3)
        return [{"provider":"engram","result":self.mcp.call_tool(tool,{"query":query,"limit":limit})}]

def memory_provider(root:Path, name:str):
    if name == "file": return FileMemory(root)
    if name == "engram": return EngramMemory()
    raise SEHError("UNKNOWN_MEMORY",name,2)
