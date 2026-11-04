"""Tool capability matrices and host command creation; AGY is config-only by default."""
from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
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
    if host not in BINARIES: raise SEHError("UNKNOWN_HOST", host, 2)
    if host == "mock": return HostCapability(host, None, True, True, "offline", "Deterministic test adapter")
    path = shutil.which(BINARIES[host] or "")
    if host == "agy": return HostCapability(host, path, bool(path), False, "configuration-only", "CLI wrapping may be restricted by provider conditions; explicitly unsupported in this distribution")
    return HostCapability(host, path, bool(path), bool(path), "cli", "ready" if path else "CLI not installed (see host vendor)")

def build_command(host: str, prompt: str, *, model: str | None = None, read_only: bool = True, max_turns: int = 4) -> list[str]:
    cap = capability(host)
    if not cap.supported: raise SEHError("HOST_UNAVAILABLE", cap.reason, 3)
    if host == "mock": return []
    if not cap.binary: raise SEHError("HOST_UNAVAILABLE", host, 3)
    if host == "claude":
        argv = [cap.binary, "-p", "--output-format", "json", "--max-turns", str(max_turns)]
        if read_only: argv += ["--permission-mode", "plan"]
        if model: argv += ["--model", model]
        return argv + [prompt]
    if host == "codex":
        argv = [cap.binary, "exec", "--json", "--sandbox", "read-only" if read_only else "workspace-write"]
        if model: argv += ["--model", model]
        return argv + [prompt]
    if host == "opencode":
        argv = [cap.binary, "run", "--format", "json"]
        if model: argv += ["--model", model]
        return argv + [prompt]
    raise SEHError("HOST_UNAVAILABLE", host, 3)
