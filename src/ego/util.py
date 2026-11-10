"""Small trusted primitives shared by all subsystems."""
from __future__ import annotations
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any

class SEHError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 4):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ident(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex[:12]


def sha256(data: bytes | str) -> str:
    if isinstance(data, str): data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def filehash(path: Path) -> str | None:
    return sha256(path.read_bytes()) if path.is_file() else None


def json_text(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".seh-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def atomic_text(path: Path, content: str) -> None:
    atomic_bytes(path, content.encode("utf-8"))


def write_json(path: Path, obj: Any) -> None:
    atomic_text(path, json_text(obj))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc: raise SEHError("INVALID_JSON", f"{path}: {exc}", 2) from exc


def safe_path(root: Path, relative: str, *, allow_missing: bool = True) -> Path:
    """Reject absolute paths, traversal, symlink chains and escaping roots."""
    root = root.resolve()
    p = Path(relative)
    if not relative or p.is_absolute() or ".." in p.parts or "\\" in relative or "\x00" in relative:
        raise SEHError("PATH_DENIED", f"Unsafe relative path: {relative}")
    if any(part == ".git" for part in p.parts):
        raise SEHError("PATH_DENIED", "Writing into .git is forbidden")
    cur = root
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink(): raise SEHError("SYMLINK_DENIED", f"Symlink path: {cur}")
    resolved = cur.resolve(strict=False)
    if not resolved.is_relative_to(root): raise SEHError("PATH_ESCAPE", relative)
    if not allow_missing and not resolved.exists(): raise SEHError("NOT_FOUND", relative, 3)
    return resolved


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def redact(text: str) -> str:
    if not text: return text
    text = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|secret|token|password)\s*[=:]\s*)[^\s,;'\"]+", r"\1[REDACTED]", text)
    text = re.sub(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "[PRIVATE KEY REDACTED]", text, flags=re.S)
    return text


def git_head(root: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else None


def git_is_repo(root: Path) -> bool:
    result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, capture_output=True, text=True, timeout=5)
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_worktree_add(root: Path, path: Path, commit_or_branch: str = "HEAD", new_branch: str | None = None) -> bool:
    argv = ["git", "worktree", "add"]
    if new_branch:
        argv += ["-b", new_branch]
    argv += [str(path), commit_or_branch]
    res = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
    return res.returncode == 0


def git_worktree_remove(root: Path, path: Path, *, force: bool = True) -> bool:
    argv = ["git", "worktree", "remove"]
    if force:
        argv.append("--force")
    argv.append(str(path))
    res = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
    return res.returncode == 0


def git_dirty(root: Path) -> bool:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=5)
    return r.returncode == 0 and bool(r.stdout.strip())


@contextlib.contextmanager
def exclusive_lock(path: Path):
    """Simple process-exclusivity lock; locks are not silently stolen."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def snapshot(root: Path, excludes: tuple[str, ...] = (".git", ".ego/state", ".ego/artifacts", "__pycache__", ".pytest_cache", ".venv", "node_modules")) -> dict[str, str]:
    def excluded(rel: str) -> bool:
        return any(rel == x or rel.startswith(x + "/") or ("/" not in x and x in rel.split("/")) for x in excludes)

    result: dict[str, str] = {}
    root = root.resolve()
    for base, dirs, files in os.walk(root, followlinks=False):
        basep = Path(base)
        dirs[:] = [d for d in dirs if not excluded((basep / d).relative_to(root).as_posix()) and not (basep / d).is_symlink()]
        for name in files:
            p = basep / name
            rel = p.relative_to(root).as_posix()
            if excluded(rel): continue
            if p.is_file() and not p.is_symlink(): result[rel] = filehash(p) or ""
    return result


def simple_copy_tree(src: Path, dst: Path) -> None:
    def ignore(path: str, names: list[str]) -> list[str]:
        return [x for x in names if x in {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"} or (x == "state" and Path(path).name == ".ego") or (x == "artifacts" and Path(path).name == ".ego")]
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True, symlinks=False)


def elapsed(start: float) -> float: return round(time.monotonic() - start, 4)
