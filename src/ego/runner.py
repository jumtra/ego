"""CLI execution with strict argv, timeout, process group shutdown and parsed evidence."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any
from .hosts import build_command, capability
from .util import SEHError, elapsed, redact
from .policy import check_command

@dataclass
class RunResult:
    status: str
    output: str
    stderr: str
    exit_code: int
    elapsed_seconds: float
    host: str
    tokens: int | None = None
    cost_usd: float | None = None
    reason: str = ""
    model: str | None = None
    effort: str | None = None
    usage: dict | None = None

    def as_dict(self): return asdict(self)


def run_process(argv: list[str], cwd: Path, timeout: int, *, env: dict[str,str] | None = None, input_text: str | None = None) -> tuple[int,str,str,float]:
    start = time.monotonic()
    if not argv: raise SEHError("INVALID_ARGV", "Empty command")
    if timeout <= 0: raise SEHError("INVALID_TIMEOUT", str(timeout), 2)
    proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True, env=env)
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode or 0, stdout[-1024*1024:], stderr[-256*1024:], elapsed(start)
    except subprocess.TimeoutExpired:
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: stdout,stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            stdout,stderr = proc.communicate()
        return 124, stdout[-1024*1024:], stderr[-256*1024:], elapsed(start)


def parse_host_response(host: str, raw: str) -> tuple[str,int|None,float|None]:
    messages: list[str] = []
    tokens: int|None = None
    cost: float|None = None
    for line in raw.splitlines():
        try: obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict): continue
        if host == "claude" and obj.get("type") == "result":
            val = obj.get("result", "")
            if isinstance(val, str): messages.append(val)
            if isinstance(obj.get("total_cost_usd"), (int,float)): cost = float(obj["total_cost_usd"])
            usage = obj.get("usage")
            if isinstance(usage, dict): tokens = int(usage.get("input_tokens",0) + usage.get("output_tokens",0))
        if host == "codex" and obj.get("type") in ("item.completed", "turn.completed"):
            item = obj.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message": messages.append(str(item.get("text", "")))
            usage = obj.get("usage", {})
            if isinstance(usage, dict) and "input_tokens" in usage:
                tokens = int(usage.get("input_tokens",0) + usage.get("output_tokens",0))
        if host == "opencode" and obj.get("type") == "text":
            part = obj.get("part", {})
            if isinstance(part, dict): messages.append(str(part.get("text", "")))
            if isinstance(obj.get("cost"), (float,int)): cost = float(obj["cost"])
    if not messages:
        try:
            obj = json.loads(raw)
            if isinstance(obj,dict) and isinstance(obj.get("result"),str): messages = [obj["result"]]
        except ValueError: pass
    return ("\n".join(messages).strip() if messages else raw.strip()), tokens, cost


def parse_host_usage(host: str, raw: str) -> dict | None:
    """Token accounting including prompt-cache traffic, which dominates agent cost."""
    found: dict | None = None
    for line in raw.splitlines() or [raw]:
        try: obj = json.loads(line)
        except ValueError: continue
        if not isinstance(obj, dict) or not isinstance(obj.get("usage"), dict): continue
        if host == "claude" and obj.get("type") not in (None, "result"): continue
        u = obj["usage"]
        def num(*keys):
            for k in keys:
                if isinstance(u.get(k), (int, float)): return int(u[k])
            return 0
        found = {"input_tokens": num("input_tokens"), "output_tokens": num("output_tokens"),
                 "cache_read_tokens": num("cache_read_input_tokens", "cached_input_tokens"),
                 "cache_creation_tokens": num("cache_creation_input_tokens")}
        if "cached_input_tokens" in u and "cache_read_input_tokens" not in u:
            # OpenAI-style usage counts cached tokens inside input_tokens; keep the two
            # disjoint (as Claude reports them) so a token total does not count them twice.
            found["input_tokens"] = max(0, found["input_tokens"] - found["cache_read_tokens"])
        if isinstance(obj.get("num_turns"), int): found["turns"] = obj["num_turns"]
    return found


def run_agent(host: str, prompt: str, cwd: Path, timeout: int = 180, *, model: str|None = None,
              effort: str|None = None, read_only: bool = True, max_turns: int = 4,
              autonomy: str = "auto", isolated: bool = False, lean: bool = False,
              max_budget_usd: float|None = None) -> RunResult:
    start = time.monotonic()
    if host == "mock":
        # Offline deterministic test driver; it never edits a file and is not an LLM.
        matched = re.search(r"\[\[MOCK:(.*?)\]\]", prompt, re.S)
        output = matched.group(1) if matched else prompt
        return RunResult("PASS", output, "", 0, elapsed(start), "mock", 0, 0.0, model="mock", effort=effort)
    stdin_supported = host in ("claude", "codex")
    command = build_command(host, prompt, model=model, effort=effort, read_only=read_only, max_turns=max_turns, use_stdin=stdin_supported, autonomy=autonomy, isolated=isolated,
                            lean=lean, max_budget_usd=max_budget_usd)
    try:
        code,stdout,stderr,seconds = run_process(command, cwd, timeout, input_text=prompt if stdin_supported else None)
    except OSError as exc:
        return RunResult("UNAVAILABLE", "", redact(str(exc)), 127, elapsed(start), host, reason="CLI process unavailable", model=model or "native-default", effort=effort)
    output,tokens,cost = parse_host_response(host,stdout)
    error_text = stderr.lower() + " " + output.lower()
    status = "PASS" if code == 0 else "TIMEOUT" if code == 124 else "RATE_LIMIT" if ("rate limit" in error_text or "429" in error_text) else "FAIL"
    return RunResult(status, redact(output), redact(stderr),code,seconds,host,tokens,cost,"" if code == 0 else "Host CLI error", model=model or "native-default", effort=effort,
                     usage=parse_host_usage(host,stdout))


def run_checked_command(argv: list[str], cwd: Path, *, timeout: int = 90, allow: list[str] | None = None) -> RunResult:
    check_command(argv, allow or ["python", "python3", "pytest", "git", "ruff", "npm", "pnpm", "yarn", "bun", "node", "deno", "tsc", "cargo", "rustc", "go", "dotnet", "swift", "swiftc", "cmake", "ctest", "mvn", "gradle", "gradlew", "java", "javac", "ruby", "bundle", "php", "composer"])
    code, out, err, secs = run_process(argv, cwd, timeout)
    return RunResult("PASS" if code == 0 else "TIMEOUT" if code == 124 else "FAIL", redact(out),redact(err),code,secs,"local",0,0.0)
