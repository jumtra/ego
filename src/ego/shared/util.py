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

class EgoError(Exception):
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
    fd, name = tempfile.mkstemp(prefix=".ego-", dir=path.parent)
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
    except (UnicodeError, ValueError) as exc: raise EgoError("INVALID_JSON", f"{path}: {exc}", 2) from exc


def safe_path(root: Path, relative: str, *, allow_missing: bool = True) -> Path:
    """Reject absolute paths, traversal, symlink chains and escaping roots."""
    root = root.resolve()
    p = Path(relative)
    if not relative or p.is_absolute() or ".." in p.parts or "\\" in relative or "\x00" in relative:
        raise EgoError("PATH_DENIED", f"Unsafe relative path: {relative}")
    if any(part == ".git" for part in p.parts):
        raise EgoError("PATH_DENIED", "Writing into .git is forbidden")
    cur = root
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink(): raise EgoError("SYMLINK_DENIED", f"Symlink path: {cur}")
    resolved = cur.resolve(strict=False)
    if not resolved.is_relative_to(root): raise EgoError("PATH_ESCAPE", relative)
    if not allow_missing and not resolved.exists(): raise EgoError("NOT_FOUND", relative, 3)
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


def alter_project_root(root: Path) -> Path:
    """The checkout alter knows the project by: a linked worktree maps to its main checkout, so alter never
    creates a separate project for a worktree."""
    try:
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
                           cwd=root, capture_output=True, text=True, timeout=5)
        git_dir, common_dir = r.stdout.split("\n")[:2] if r.returncode == 0 else ("", "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return root
    if git_dir and common_dir and Path(git_dir).resolve() != Path(common_dir).resolve():
        return Path(common_dir).resolve().parent
    return root


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


def git_branch_delete(root: Path, branch: str) -> bool:
    """Safe delete (-d): refuses a branch holding unmerged commits."""
    res = subprocess.run(["git", "branch", "-d", branch], cwd=root, capture_output=True, text=True, timeout=30)
    return res.returncode == 0


def git_dirty(root: Path) -> bool:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=5)
    return r.returncode == 0 and bool(r.stdout.strip())


@contextlib.contextmanager
def exclusive_lock(path: Path, *, blocking: bool = True, busy_code: str = 'RUN_ACTIVE'):
    """Simple process-exclusivity lock; locks are not silently stolen.

    blocking=False fails fast with EgoError(busy_code) when another process holds the lock.
    """
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if blocking:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                fd = -1
                raise EgoError(busy_code, f'Lock held by another process: {path.name}', 3)
        yield
    finally:
        if fd >= 0:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def is_locked(path: Path) -> bool:
    """True when another open file description currently holds an exclusive flock on path."""
    import fcntl
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def snapshot(root: Path, excludes: tuple[str, ...] = (".git", ".ego/state", ".ego/locks", ".ego/artifacts", "__pycache__", ".pytest_cache", ".venv", "node_modules")) -> dict[str, str]:
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


# `git worktree add` checks out only tracked files, so gitignored project state (ego memory, host
# configs generated for .claude/.codex/..., CLAUDE.md, node_modules, .env, ...) would be missing.
# Copy every ignored entry so a worktree run sees the same environment as the project root, except
# run state (it contains the worktrees themselves) and regenerable caches/build outputs.
IGNORED_COPY_EXCLUDES = (".ego/state/", ".ego/locks/", ".ego/backups/")
IGNORED_COPY_SKIP_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "build", "dist"}


def copy_ignored_files(root: Path, dst: Path) -> None:
    res = subprocess.run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
                         cwd=root, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return
    for rel in filter(None, res.stdout.split("\0")):
        if rel.startswith(IGNORED_COPY_EXCLUDES) or any(rel == x.rstrip("/") for x in IGNORED_COPY_EXCLUDES):
            continue
        parts = rel.rstrip("/").split("/")
        if any(p in IGNORED_COPY_SKIP_PARTS or p.endswith(".egg-info") for p in parts):
            continue
        src = root / rel.rstrip("/")
        if src.is_symlink():
            continue
        target = dst / rel.rstrip("/")
        def _ignore(d: str, names: list[str]) -> list[str]:
            base = Path(d).relative_to(root).as_posix()
            skipped = []
            for n in names:
                rel_n = (n if base == "." else f"{base}/{n}") + "/"
                if n in IGNORED_COPY_SKIP_PARTS or n.endswith(".egg-info") or rel_n in IGNORED_COPY_EXCLUDES:
                    skipped.append(n)
            return skipped

        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True, symlinks=False, ignore=_ignore)
        elif src.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def elapsed(start: float) -> float: return round(time.monotonic() - start, 4)


def clip_output(text: str, limit: int = 4000, head_ratio: float = 0.25) -> str:
    """Bound verifier output, keeping the tail where failures are reported."""
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n...[{len(text) - limit} chars omitted]...\n"
    keep = max(limit - len(marker), 0)
    head = int(keep * head_ratio)
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")


HOST_ENV = {"claude": ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID"), "codex": ("CODEX_THREAD_ID", "CODEX_CI"),
            "opencode": ("OPENCODE",), "agy": ("ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID")}


def detect_host() -> str | None:
    """The CLI this process runs under, when exactly one says so. None rather than a guess."""
    found = [host for host, names in HOST_ENV.items() if any(os.environ.get(n) for n in names)]
    return found[0] if len(found) == 1 else None


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise EgoError("INVALID_TASK_ID", "Use an alphanumeric task ID (max 80 chars) with '-' or '_'", 2)
    return value




def handoff_relpath(task_id: str) -> str:
    return ".ego/memory/handoffs/" + _id(task_id) + ".json"

