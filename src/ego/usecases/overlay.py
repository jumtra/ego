"""Session-scoped instruction overlay: `ego run` puts the managed block on top of CLAUDE.md / AGENTS.md
for the length of the session and restores the file when the session ends.

Nothing is persisted by `ego apply`, so an existing instruction file is never changed in git. Every run
records what it changed under `.ego/state/overlay/` (gitignored). A run that died without restoring is
cleaned up by the next `ego run` in the same project, because its recorded pid is no longer alive.
"""
from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..domain.config import Config
from ..domain.templates import instruction_block, MARK_BEGIN, MARK_END
from ..shared.util import atomic_text, exclusive_lock, now, read_json, safe_path, write_json

STATE_DIR = ".ego/state/overlay"
LOCK = ".ego/locks/overlay.lock"
_BLOCK_RE = re.compile(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n*", re.S)


def instruction_target(host: str) -> tuple[str, str]:
    """The instruction file a host reads, and the rules directory its block points at."""
    return ("CLAUDE.md", ".claude/rules") if host == "claude" else ("AGENTS.md", ".agents/rules")


@dataclass
class Overlay:
    root: Path
    state_file: Path
    state: dict


def begin_overlay(root: Path, host: str, cfg: Config) -> Overlay:
    """Prepend the managed block to the host's instruction file (creating it when absent)."""
    root = root.resolve()
    rel, rules_dir = instruction_target(host)
    prefix = instruction_block(cfg, rules_dir) + "\n"
    with exclusive_lock(root / LOCK):
        _recover_stale(root)
        path = safe_path(root, rel)
        text = path.read_text(encoding="utf-8") if path.exists() else None
        # A block already present belongs to another live run (or to a legacy persisted install):
        # this run then leaves the file alone and only takes ownership if the other run ends first.
        owner = text is None or MARK_BEGIN not in text
        if owner:
            atomic_text(path, prefix + (text or ""))
        state = {"file": rel, "prefix": prefix, "created": text is None, "owner": owner,
                 "pid": os.getpid(), "started": now()}
        state_file = root / STATE_DIR / f"{uuid.uuid4().hex}.json"
        write_json(state_file, state)
    return Overlay(root, state_file, state)


def end_overlay(overlay: Overlay | None) -> None:
    if overlay is None:
        return
    with exclusive_lock(overlay.root / LOCK):
        current = read_json(overlay.state_file)  # another run may have handed the block over meanwhile
        state = current if isinstance(current, dict) else overlay.state
        _release(overlay.root, state, overlay.state_file)


@contextmanager
def overlaid(root: Path, host: str, cfg: Config) -> Iterator[None]:
    """Hold the managed block on `root`'s instruction file while one host session runs (workers use a copy)."""
    overlay = begin_overlay(root, host, cfg)
    try:
        yield
    finally:
        end_overlay(overlay)


def _scan(root: Path) -> tuple[list[tuple[Path, dict]], list[tuple[Path, dict]]]:
    live, stale = [], []
    directory = root / STATE_DIR
    if not directory.is_dir():
        return live, stale
    for path in sorted(directory.glob("*.json")):
        data = read_json(path)
        if not isinstance(data, dict) or "file" not in data:
            path.unlink(missing_ok=True)
            continue
        (live if _alive(data.get("pid")) else stale).append((path, data))
    return live, stale


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _recover_stale(root: Path) -> None:
    _, stale = _scan(root)
    for path, data in stale:
        _release(root, data, path)


def _release(root: Path, state: dict, state_file: Path) -> None:
    """Forget this run; the block stays while another live run still needs the same file."""
    state_file.unlink(missing_ok=True)
    if not state.get("owner"):
        return
    live, _ = _scan(root)
    for path, other in live:
        if other["file"] == state["file"]:
            other["owner"] = True
            other["created"] = bool(other.get("created")) or bool(state.get("created"))
            write_json(path, other)
            return
    _restore(root, state)


def _restore(root: Path, state: dict) -> None:
    path = safe_path(root, state["file"])
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    prefix = state.get("prefix") or ""
    if prefix and text.startswith(prefix):
        rest = text[len(prefix):]  # exact: the original content follows the prefix untouched
    else:
        rest = _BLOCK_RE.sub("", text, count=1)  # the block was edited during the session: cut it out
    if rest == text:
        return
    if state.get("created") and not rest.strip():
        path.unlink()
        return
    atomic_text(path, rest)
