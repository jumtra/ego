"""Execution policy. Hooks are advisory; OS-level containment is required for untrusted agents."""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
from .util import EgoError, safe_path

BLOCK_PATTERNS = [
    r"(?<!\w)(?:sudo\s+)?rm\s+-[a-zA-Z]*[rR][a-zA-Z]*[fF][a-zA-Z]*\s+(?:/|~|\$HOME)(?:\s|$)",
    r"(?:curl|wget)\s+[^|\n]+\|\s*(?:sh|bash|zsh)",
    r"\b(?:mkfs\.|dd\s+if=|chmod\s+-R\s+777\s+/)",
]
PROTECTED = (
    ".ego/ownership.json", ".ego/locks", ".ego/backups", ".git", ".env", "secrets", "credentials",
)

def check_command(argv: list[str], allow: list[str], policy_mode: str = "strict") -> None:
    if not argv or not all(isinstance(x, str) for x in argv): raise EgoError("INVALID_ARGV", "Commands must be argv arrays")
    exe = Path(argv[0]).name
    if exe not in allow: raise EgoError("COMMAND_DENIED", f"Not an approved command: {exe}")
    if exe in {"sh", "bash", "zsh"}: raise EgoError("COMMAND_DENIED", "Shell execution is not allowed")
    joined = " ".join(argv)
    if any(re.search(p, joined, flags=re.I) for p in BLOCK_PATTERNS): raise EgoError("DANGEROUS_COMMAND", joined)
    if policy_mode != "relaxed" and exe == "git" and any(x in argv for x in ("push", "reset", "clean", "rebase")):
        raise EgoError("COMMAND_DENIED", "Mutating remote or destructive Git commands need a separate manual process")
    if exe in {"python", "python3"} and "-c" in argv:
        raise EgoError("COMMAND_DENIED", "Inline Python code is forbidden in check commands")


def check_write(root: Path, rel: str, allow: list[str] | None = None) -> Path:
    import fnmatch
    p = safe_path(root, rel)
    if rel.startswith(PROTECTED): raise EgoError("PROTECTED_PATH", rel)
    if allow is not None and not any(fnmatch.fnmatch(rel, pattern) for pattern in allow):
        raise EgoError("OUT_OF_SCOPE", rel)
    return p


def sandbox_diagnostics() -> dict:
    """Conservative: filesystem separation alone never counts as OS sandbox."""
    docker = shutil.which("docker")
    bwrap = shutil.which("bwrap")
    podman = shutil.which("podman")
    detected = "docker" if docker else "podman" if podman else "bwrap" if bwrap else None
    return {"backend": detected, "verified": False, "reason": "External sandbox needs runtime-specific isolation, credential and network tests before use" if detected else "No supported isolation backend installed"}


def check_hook_payload(event: str, payload: dict, policy_mode: str = "strict") -> dict:
    if event != "pre-tool": return {"decision": "allow", "reason": "no policy change"}
    if not isinstance(payload, dict): return {"decision": "deny", "reason": "invalid JSON payload"}
    if policy_mode == "relaxed":
        cmd = str(payload.get("tool_input", {}).get("command", ""))
        if any(re.search(p, cmd, re.I) for p in BLOCK_PATTERNS):
            return {"decision": "deny", "reason": "destructive shell command"}
        return {"decision": "allow", "reason": "relaxed policy: operation permitted"}
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input", {})
    if not isinstance(ti, dict): return {"decision": "deny", "reason": "invalid tool_input"}
    target = ti.get("file_path", "")
    if tool in ("Write", "Edit", "MultiEdit") and any(s in str(target) for s in (".ego/ownership.json", ".ego/locks/", ".git/")):
        return {"decision": "deny", "reason": "protected path"}
    command = str(ti.get("command", ""))
    if tool == "Bash" and any(re.search(p, command, re.I) for p in BLOCK_PATTERNS):
        return {"decision": "deny", "reason": "destructive shell command"}
    return {"decision": "allow", "reason": "no high-risk operation detected"}
