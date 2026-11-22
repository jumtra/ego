"""CLI execution with strict argv, timeout, process group shutdown and parsed evidence."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
from typing import Callable
from .hosts import build_command
from ..shared.util import ALTER_ROOT_ENV, EgoError, elapsed, redact
from ..domain.policy import check_command

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
    session_id: str | None = None
    stop_reason: str | None = None
    segments: int = 1
    rotations: int = 0
    context_tokens: int | None = None

    def as_dict(self): return asdict(self)


def _terminate_pg(proc: subprocess.Popen, *, first_signal: int = signal.SIGTERM, first_wait: float = 2.0) -> None:
    """Terminate the child process group gracefully, escalating to SIGTERM then SIGKILL."""
    try:
        os.killpg(proc.pid, first_signal)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=first_wait)
        return
    except subprocess.TimeoutExpired:
        pass
    if first_signal != signal.SIGTERM:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def run_process(argv: list[str], cwd: Path, timeout: int, *, env: dict[str,str] | None = None, input_text: str | None = None) -> tuple[int,str,str,float]:
    start = time.monotonic()
    if not argv: raise EgoError("INVALID_ARGV", "Empty command")
    if timeout <= 0: raise EgoError("INVALID_TIMEOUT", str(timeout), 2)
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


def _int_num(mapping: object, *keys: str) -> int:
    if not isinstance(mapping, dict):
        return 0
    for k in keys:
        val = mapping.get(k)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return int(val)
    return 0


def _sum_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if inp is None and out is None:
        return None
    return int((inp if isinstance(inp, (int, float)) else 0) + (out if isinstance(out, (int, float)) else 0))


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
            tok = _sum_tokens(obj.get("usage"))
            if tok is not None: tokens = tok
        if host == "codex" and obj.get("type") in ("item.completed", "turn.completed"):
            item = obj.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message": messages.append(str(item.get("text", "")))
            usage = obj.get("usage", {})
            if isinstance(usage, dict) and "input_tokens" in usage:
                tok = _sum_tokens(usage)
                if tok is not None: tokens = tok
        if host == "opencode" and obj.get("type") == "text":
            part = obj.get("part", {})
            if isinstance(part, dict): messages.append(str(part.get("text", "")))
            if isinstance(obj.get("cost"), (float,int)): cost = float(obj["cost"])
        if host == "agy" and (isinstance(obj.get("response"), str) or (obj.get("type") == "result" and isinstance(obj.get("result"), str))):
            # Single JSON object or stream-json `result` event.
            text = obj.get("response") if isinstance(obj.get("response"), str) else obj.get("result")
            messages.append(text)
            tok = _sum_tokens(obj.get("usage"))
            if tok is not None: tokens = tok
    if not messages:
        try:
            obj = json.loads(raw)
            if isinstance(obj,dict):
                text = obj.get("result") if isinstance(obj.get("result"),str) else obj.get("response")
                if isinstance(text,str): messages = [text]
                if tokens is None: tokens = _sum_tokens(obj.get("usage"))
                if cost is None and isinstance(obj.get("total_cost_usd"), (int,float)): cost = float(obj["total_cost_usd"])
        except ValueError: pass
    return ("\n".join(messages).strip() if messages else raw.strip()), tokens, cost


def _extract_usage(obj: object, host: str) -> dict | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("usage"), dict): return None
    if host == "claude" and obj.get("type") not in (None, "result"): return None
    u = obj["usage"]
    def num(*keys): return _int_num(u, *keys)
    found = {"input_tokens": num("input_tokens"), "output_tokens": num("output_tokens"),
             "cache_read_tokens": num("cache_read_input_tokens", "cached_input_tokens", "cache_read_tokens"),
             "cache_creation_tokens": num("cache_creation_input_tokens")}
    if "cached_input_tokens" in u and "cache_read_input_tokens" not in u:
        # OpenAI-style usage counts cached tokens inside input_tokens; keep the two
        # disjoint (as Claude reports them) so a token total does not count them twice.
        found["input_tokens"] = max(0, found["input_tokens"] - found["cache_read_tokens"])
    cache_creation = u.get("cache_creation")
    if isinstance(cache_creation, dict) and ("ephemeral_5m_input_tokens" in cache_creation or "ephemeral_1h_input_tokens" in cache_creation):
        # Claude splits cache writes into 5-minute and 1-hour tiers, which are priced differently.
        # Without tier keys, cache_write_tiers() prices cache_creation_tokens as 5m.
        found["cache_write_5m_tokens"] = _int_num(cache_creation, "ephemeral_5m_input_tokens")
        found["cache_write_1h_tokens"] = _int_num(cache_creation, "ephemeral_1h_input_tokens")
    model_usage = obj.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        # Claude lists every model it called; the one that produced the most output is the agent's model.
        found["model"] = max(model_usage, key=lambda name: _int_num(model_usage[name], "outputTokens"))
    if isinstance(obj.get("num_turns"), int): found["turns"] = obj["num_turns"]
    return found


def _extract_opencode_tokens(obj: object) -> dict | None:
    if not isinstance(obj, dict) or obj.get("type") != "step_finish":
        return None
    part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
    t = part.get("tokens") if isinstance(part.get("tokens"), dict) else obj.get("tokens")
    if not isinstance(t, dict):
        return None
    cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
    return {
        "input_tokens": _int_num(t, "input"),
        "output_tokens": _int_num(t, "output"),
        "cache_read_tokens": _int_num(cache, "read"),
        "cache_creation_tokens": _int_num(cache, "write"),
    }


def parse_host_usage(host: str, raw: str) -> dict | None:
    """Token accounting including prompt-cache traffic, which dominates agent cost."""
    found: dict | None = None
    opencode_acc: dict[str, int] | None = None
    def _consume(obj: object) -> None:
        nonlocal found, opencode_acc
        if host == "opencode":
            oc = _extract_opencode_tokens(obj)
            if oc is not None:
                if opencode_acc is None:
                    opencode_acc = {k: 0 for k in oc}
                for k, v in oc.items():
                    opencode_acc[k] += v
        extracted = _extract_usage(obj, host)
        if extracted is not None:
            found = extracted
    for line in raw.splitlines() or [raw]:
        try: obj = json.loads(line)
        except ValueError: continue
        _consume(obj)
    if found is None and opencode_acc is None:
        try: _consume(json.loads(raw))
        except ValueError: pass
    return opencode_acc if opencode_acc is not None else found


def _extract_session_id(host: str, obj: object) -> str | None:
    if not isinstance(obj, dict):
        return None
    if host == "claude":
        val = obj.get("session_id") if obj.get("type") == "result" else None
        return val if isinstance(val, str) and val else None
    if host == "codex":
        for key in ("thread_id", "session_id"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        return None
    if host == "opencode":
        containers = [obj, obj["part"]] if isinstance(obj.get("part"), dict) else [obj]
        for container in containers:
            for key in ("sessionID", "session_id"):
                val = container.get(key)
                if isinstance(val, str) and val:
                    return val
        return None
    if host == "agy":
        containers = [obj] + [obj[k] for k in ("data", "step", "metadata", "session") if isinstance(obj.get(k), dict)]
        for container in containers:
            for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
                val = container.get(key)
                if isinstance(val, str) and val:
                    return val
        return None
    return None


def _extract_event_context_tokens(host: str, obj: object) -> int | None:
    if not isinstance(obj, dict):
        return None
    if host == "codex":
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            return _int_num(obj["usage"], "input_tokens") or None
        return None
    if host == "opencode":
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        t = part.get("tokens") if isinstance(part.get("tokens"), dict) else obj.get("tokens")
        if isinstance(t, dict):
            cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
            total = _int_num(t, "input") + _int_num(cache, "read") + _int_num(cache, "write")
            return total or None
        return None
    if host == "agy":
        candidates = [
            obj.get("usage"),
            obj["step"].get("usage") if isinstance(obj.get("step"), dict) else None,
            obj["metadata"].get("usage") if isinstance(obj.get("metadata"), dict) else None,
            obj if "input_tokens" in obj else None,
        ]
        for u in candidates:
            if isinstance(u, dict):
                total = (
                    _int_num(u, "input_tokens")
                    + _int_num(u, "cache_read_tokens", "cache_read_input_tokens")
                    + _int_num(u, "cache_creation_tokens", "cache_creation_input_tokens")
                )
                if total > 0:
                    return total
        return None
    return None


def _is_terminal_event(host: str, obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    if host == "agy":
        return obj.get("type") == "result"
    if host == "codex":
        return obj.get("type") == "turn.completed"
    if host == "opencode":
        if obj.get("type") != "step_finish":
            return False
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        reason = part.get("reason") or obj.get("reason")
        return reason not in (None, "", "tool-calls")
    return False


def parse_claude_session(raw: str) -> tuple[str | None, str | None]:
    """(session_id, subtype) of the final `result` line of `claude -p --output-format json`."""
    lines = raw.splitlines() or [raw]
    for line in reversed(lines):
        try: obj = json.loads(line)
        except ValueError: continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            sid, sub = obj.get("session_id"), obj.get("subtype")
            return (sid if isinstance(sid, str) else None), (sub if isinstance(sub, str) else None)
    return None, None


def parse_host_session(host: str, raw: str) -> tuple[str | None, str | None]:
    """Extract the host session/thread/conversation ID from raw CLI stdout."""
    if host == "claude":
        return parse_claude_session(raw)
    sid: str | None = None
    for line in raw.splitlines() or [raw]:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        found = _extract_session_id(host, obj)
        if found:
            sid = found
    if sid is None:
        try:
            sid = _extract_session_id(host, json.loads(raw))
        except ValueError:
            pass
    return sid, None


def _agy_failed(raw: str) -> bool:
    """AGY print/stream mode can exit 0 while its JSON/result reports a non-SUCCESS turn."""
    for line in reversed(raw.splitlines() or [raw]):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("status"), str):
            return obj["status"] != "SUCCESS"
    try: obj = json.loads(raw)
    except ValueError: return False
    return isinstance(obj, dict) and isinstance(obj.get("status"), str) and obj["status"] != "SUCCESS"


def _append_bounded(buf: list[str], chunk: str, max_bytes: int) -> None:
    buf.append(chunk)
    if len(buf) > 256 or sum(len(c) for c in buf) > max_bytes * 2:
        buf[:] = ["".join(buf)[-max_bytes:]]


def run_process_monitored(
    argv: list[str], cwd: Path, timeout: int, *, host: str,
    context_limit: int, context_probe: Callable[[str | None], int | None] | None = None,
    env: dict[str, str] | None = None, input_text: str | None = None,
    poll_interval: float = 0.5,
) -> tuple[int, str, str, float, bool, str | None, int | None]:
    """Run a host CLI process while monitoring live context usage; interrupt on context_limit."""
    start = time.monotonic()
    if not argv:
        raise EgoError("INVALID_ARGV", "Empty command")
    if timeout <= 0:
        raise EgoError("INVALID_TIMEOUT", str(timeout), 2)
    proc = subprocess.Popen(
        argv, cwd=str(cwd),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", start_new_session=True, env=env,
    )
    q: queue.Queue[str | None] = queue.Queue()
    stderr_buf: list[str] = []

    def _feed_stdin() -> None:
        if proc.stdin is None:
            return
        try:
            if input_text is not None:
                proc.stdin.write(input_text)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _read_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                chunk = proc.stderr.read(8192)
                if not chunk:
                    break
                _append_bounded(stderr_buf, chunk, 256 * 1024)
        except OSError:
            pass

    def _read_stdout() -> None:
        if proc.stdout is None:
            q.put(None)
            return
        try:
            for line in iter(proc.stdout.readline, ""):
                q.put(line)
        except OSError:
            pass
        finally:
            q.put(None)

    t_in = threading.Thread(target=_feed_stdin, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_in.start()
    t_err.start()
    t_out.start()

    deadline = start + timeout
    stdout_buf: list[str] = []
    rotated = False
    timed_out = False
    session_id: str | None = None
    measured: int | None = None
    stop_tokens: int | None = None

    def _probe() -> None:
        nonlocal measured
        if context_probe is None:
            return
        try:
            probed = context_probe(session_id)
        except (OSError, ValueError, TypeError, KeyError):
            return
        if probed is not None:
            measured = probed

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_pg(proc, first_signal=signal.SIGTERM, first_wait=2.0)
            break
        wait_for = min(poll_interval, max(0.05, remaining))
        try:
            line = q.get(timeout=wait_for)
        except queue.Empty:
            _probe()
            if proc.poll() is None and context_limit > 0 and measured is not None and measured >= context_limit:
                rotated = True
                stop_tokens = measured
                _terminate_pg(proc, first_signal=signal.SIGINT, first_wait=3.0)
                break
            continue
        if line is None:
            break
        _append_bounded(stdout_buf, line, 1024 * 1024)
        obj = None
        try:
            obj = json.loads(line)
        except ValueError:
            pass
        if obj is not None:
            sid = _extract_session_id(host, obj)
            if sid:
                session_id = sid
            ev_tok = _extract_event_context_tokens(host, obj)
            if ev_tok is not None:
                measured = ev_tok
            if _is_terminal_event(host, obj):
                try:
                    proc.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    pass
                if proc.poll() is not None:
                    continue
            else:
                _probe()
        else:
            _probe()
        if proc.poll() is None and context_limit > 0 and measured is not None and measured >= context_limit:
            rotated = True
            stop_tokens = measured
            _terminate_pg(proc, first_signal=signal.SIGINT, first_wait=3.0)
            break

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    t_in.join(timeout=0.5)
    while not q.empty():
        try:
            rem = q.get_nowait()
        except queue.Empty:
            break
        if rem is None:
            continue
        _append_bounded(stdout_buf, rem, 1024 * 1024)
        try:
            obj = json.loads(rem)
        except ValueError:
            obj = None
        sid = _extract_session_id(host, obj)
        if sid:
            session_id = sid
    if not timed_out and proc.poll() is None:
        proc.wait()
    code = 124 if timed_out else (proc.returncode or 0)
    return (
        code,
        "".join(stdout_buf)[-1024 * 1024:],
        "".join(stderr_buf)[-256 * 1024:],
        elapsed(start),
        rotated,
        session_id,
        stop_tokens,
    )


_RATE_LIMIT_RE = re.compile(
    r"rate[\s_-]?limit|\b429\b|too many requests|usage limit|"
    r"quota\s+(?:exceeded|limit|exhausted|reached)|out\s+of\s+quota|insufficient_quota|"
    r"hit (?:your|the) limit|"
    r"(?:usage|rate|token|spend|billing|daily|weekly|hourly)\s+limit\s+reached|"
    r"overloaded_error|(?:server|api|model|service|system)\s+(?:is\s+)?overloaded|"
    r"(?:at|over|exceeded)\s+capacity|capacity\s+limit",
    re.I,
)


def run_agent(host: str, prompt: str, cwd: Path, timeout: int = 180, *, model: str|None = None,
              effort: str|None = None, read_only: bool = True, max_turns: int = 4,
              autonomy: str = "auto", isolated: bool = False, lean: bool = False,
              mcp_servers: list[str]|tuple[str,...]|None = None, resume_session: str|None = None,
              env: dict[str,str]|None = None, allowed_tools: list[str]|tuple[str,...]|None = None,
              context_limit: int = 0, context_probe: Callable[[str | None], int | None] | None = None) -> RunResult:
    start = time.monotonic()
    if host == "mock":
        # Offline deterministic test driver; it never edits a file and is not an LLM.
        matched = re.search(r"\[\[MOCK:(.*?)\]\]", prompt, re.S)
        output = matched.group(1) if matched else prompt
        return RunResult("PASS", output, "", 0, elapsed(start), "mock", 0, 0.0, model="mock", effort=effort)
    stdin_supported = host in ("claude", "codex")
    project_root = Path(env[ALTER_ROOT_ENV]) if env and env.get(ALTER_ROOT_ENV) else None
    monitor = context_limit > 0 and host in ("codex", "opencode", "agy")
    command = build_command(host, prompt, model=model, effort=effort, read_only=read_only, max_turns=max_turns, use_stdin=stdin_supported, autonomy=autonomy, isolated=isolated,
                            lean=lean, mcp_servers=mcp_servers, cwd=cwd, resume_session=resume_session,
                            allowed_tools=allowed_tools, project_root=project_root,
                            stream=monitor and host == "agy")
    rotated = False
    mon_sid: str | None = None
    stop_tokens: int | None = None
    try:
        if monitor:
            code, stdout, stderr, seconds, rotated, mon_sid, stop_tokens = run_process_monitored(
                command, cwd, timeout, host=host, context_limit=context_limit, context_probe=context_probe,
                env=env, input_text=prompt if stdin_supported else None,
            )
        else:
            code,stdout,stderr,seconds = run_process(command, cwd, timeout, input_text=prompt if stdin_supported else None, env=env)
    except OSError as exc:
        return RunResult("UNAVAILABLE", "", redact(str(exc)), 127, elapsed(start), host, reason="CLI process unavailable", model=model or "native-default", effort=effort)
    output,tokens,cost = parse_host_response(host,stdout)
    parsed_sid, stop_reason = parse_host_session(host, stdout)
    session_id = mon_sid or parsed_sid
    if rotated:
        return RunResult("PASS", redact(output), redact(stderr), code, seconds, host, tokens, cost, "CONTEXT_LIMIT",
                         model=model or "native-default", effort=effort, usage=parse_host_usage(host, stdout),
                         session_id=session_id, stop_reason="context_limit", context_tokens=stop_tokens)
    error_text = stderr.lower() + " " + output.lower()
    if host == "agy" and code == 0 and _agy_failed(stdout): code = 1
    status = "PASS" if code == 0 else "TIMEOUT" if code == 124 else "RATE_LIMIT" if _RATE_LIMIT_RE.search(error_text) else "FAIL"
    return RunResult(status, redact(output), redact(stderr),code,seconds,host,tokens,cost,"" if code == 0 else "Host CLI error", model=model or "native-default", effort=effort,
                     usage=parse_host_usage(host,stdout), session_id=session_id, stop_reason=stop_reason)


def run_checked_command(argv: list[str], cwd: Path, *, timeout: int = 90, allow: list[str] | None = None) -> RunResult:
    check_command(argv, allow or ["python", "python3", "pytest", "git", "ruff", "npm", "pnpm", "yarn", "bun", "node", "deno", "tsc", "cargo", "rustc", "go", "dotnet", "swift", "swiftc", "cmake", "ctest", "mvn", "gradle", "gradlew", "java", "javac", "ruby", "bundle", "php", "composer"])
    code, out, err, secs = run_process(argv, cwd, timeout)
    return RunResult("PASS" if code == 0 else "TIMEOUT" if code == 124 else "FAIL", redact(out),redact(err),code,secs,"local",0,0.0)
