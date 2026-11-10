"""Bounded local CLI capability probing; no network and no per-task expensive probes.

A changed CLI executable fingerprint invalidates the cached flag map. We only
use documented, already-tested flag translations, guarded by the actual CLI's
--help output. Arbitrary help text is never interpreted as executable code.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from .util import SEHError, read_json, write_json

BINARIES = {"claude": "claude", "codex": "codex", "opencode": "opencode", "agy": "agy"}
CACHE_TTL = 7 * 86400
FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def cache_path() -> Path:
    return Path(os.environ.get("SEH_CACHE_DIR", str(Path.home()/".cache"/"seh"))) / "host_capabilities.json"


def _fingerprint(executable: str) -> str:
    target = Path(executable).resolve()
    stat = target.stat()
    return f"{target}:{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def _bounded(argv: list[str], timeout: float) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
        return p.returncode == 0, (p.stdout + "\n" + p.stderr)[:128000]
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def inspect_host(host: str, *, refresh: bool = False, timeout: int = 5) -> dict:
    if host not in BINARIES:
        raise SEHError("UNKNOWN_HOST", host, 2)
    executable = shutil.which(BINARIES[host])
    if not executable:
        return {"host": host, "available": False, "supported": False, "reason": "CLI not installed", "flags": [], "version": None}
    try:
        key = _fingerprint(executable)
    except OSError:
        return {"host": host, "available": False, "supported": False, "reason": "CLI path disappeared", "flags": [], "version": None}
    path=cache_path()
    cache=read_json(path,{}) or {}
    old=cache.get(host,{})
    if not refresh and old.get("fingerprint")==key and time.time()-old.get("checked_at",0)<CACHE_TTL:
        return old
    okv, version = _bounded([executable,"--version"], timeout)
    command=[executable,"exec","--help"] if host=="codex" else [executable,"run","--help"] if host=="opencode" else [executable,"--help"]
    okh, help_text = _bounded(command,timeout)
    flags=sorted(set(FLAG_RE.findall(help_text))) if okh else []
    modes=sorted(mode for mode in ("auto","acceptEdits","bypassPermissions","plan")
                 if re.search(r"(?<![A-Za-z])"+re.escape(mode)+r"(?![A-Za-z])",help_text))
    result={"host":host,"available":True,"supported":okh,"version":version.strip().splitlines()[0][:180] if okv else None,
            "executable":str(Path(executable).resolve()),"fingerprint":key,"flags":flags,"permission_modes":modes,
            "checked_at":time.time(),"reason":"ready" if okh else "Could not inspect CLI help; refresh or update host CLI"}
    cache[host]=result
    write_json(path,cache)
    return result


def require_flags(info: dict, *names: str) -> None:
    missing=sorted(set(names)-set(info.get("flags",[])))
    if not info.get("supported") or missing:
        raise SEHError("CLI_INCOMPATIBLE", f"{info.get('host')}: required CLI switches absent: {missing}. Run 'ego hosts refresh' after updating the vendor CLI.",3)


def list_host_capabilities(*, refresh: bool=False) -> dict:
    return {host:inspect_host(host,refresh=refresh) for host in BINARIES}
