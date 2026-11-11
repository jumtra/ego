"""Registry of projects ego was applied to: which version each one runs, and bulk re-sync.

The registry is one small user-level JSON file (paths only). Each project's own
`.ego/config.toml` stays the source of its settings, so a re-sync inherits them.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from . import __version__
from .util import SEHError, exclusive_lock, now, read_json, write_json

SCAN_SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "Library", ".cache", ".Trash"}
MAX_SCAN_DEPTH = 6


def registry_path() -> Path:
    return Path(os.environ.get("EGO_HOME") or Path.home() / ".config" / "ego") / "projects.json"


def _load() -> dict[str, dict[str, Any]]:
    raw = read_json(registry_path(), {})
    projects = raw.get("projects") if isinstance(raw, dict) else None
    return {k: v for k, v in projects.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(projects, dict) else {}


def _mutate(change) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path.with_suffix(".lock")):
        projects = _load()
        change(projects)
        write_json(path, {"schema_version": 1, "projects": projects})


def register(root: Path) -> None:
    key = str(root.resolve())
    _mutate(lambda projects: projects.__setitem__(key, {"registered_at": projects.get(key, {}).get("registered_at") or now()}))


def register_quietly(root: Path) -> None:
    """Called from apply: an unwritable home directory must never fail a project install."""
    try:
        register(root)
    except (OSError, SEHError, ValueError):
        pass


def unregister(root: Path) -> bool:
    key = str(root.resolve())
    found = key in _load()
    if found:
        _mutate(lambda projects: projects.pop(key, None))
    return found


def project_info(root: Path) -> dict[str, Any]:
    """Version and drift of one applied project, read from its own files."""
    from .config import load_config
    from .install import OWNERSHIP, installation_plan
    info: dict[str, Any] = {"root": str(root), "harness_version": __version__}
    try:
        ownership = read_json(root / OWNERSHIP)
    except SEHError:
        ownership = None
    if not isinstance(ownership, dict):
        return {**info, "status": "MISSING" if not root.is_dir() else "NOT_APPLIED", "installed_version": None}
    installed = ownership.get("installed_version") or ownership.get("version")
    info["installed_version"] = installed
    info["version_differs"] = installed != __version__
    try:
        cfg = load_config(root)
        plan = installation_plan(root, cfg)
    except (SEHError, OSError, ValueError) as exc:
        return {**info, "status": "ERROR", "message": str(exc)}
    changes = [c for c in plan["changes"] if c["action"] != "unchanged"]
    info.update(status="BLOCKED" if plan["conflicts"] else "CHANGES_PENDING" if changes else "UP_TO_DATE",
                changed_count=len(changes), profile=cfg.profile, hosts=cfg.hosts,
                clear_suggest_tokens=cfg.clear_suggest_tokens)
    if plan["conflicts"]:
        info["conflicts"] = [c["path"] for c in plan["conflicts"]]
    return info


def list_projects() -> list[dict[str, Any]]:
    return [project_info(Path(key)) for key in sorted(_load())]


def scan(base: Path, depth: int = 4) -> list[str]:
    """Register every applied project (one with .ego/ownership.json) under base."""
    base = base.resolve()
    if not base.is_dir():
        raise SEHError("NOT_FOUND", str(base), 3)
    depth = max(0, min(depth, MAX_SCAN_DEPTH))
    found: list[str] = []

    def walk(directory: Path, left: int) -> None:
        if (directory / ".ego" / "ownership.json").is_file():
            found.append(str(directory))
            return  # a project's own subdirectories are not separate installs
        if left == 0:
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()
                              and p.name not in SCAN_SKIP and not p.name.startswith("."))
        except OSError:
            return
        for child in children:
            walk(child, left - 1)

    walk(base, depth)
    if found:
        _mutate(lambda projects: [projects.setdefault(k, {"registered_at": now()}) for k in found])
    return found


def sync_project(root: Path, *, apply: bool) -> dict[str, Any]:
    """Re-render one project with its own saved settings; a failure is reported, never raised."""
    from .config import load_config
    from .install import apply_install
    before = project_info(root)
    if before["status"] in ("MISSING", "NOT_APPLIED", "ERROR", "BLOCKED") or not apply or before["status"] == "UP_TO_DATE":
        return before
    try:
        result = apply_install(root, load_config(root))
    except (SEHError, OSError, ValueError) as exc:
        return {**before, "status": "ERROR", "message": str(exc)}
    return {**project_info(root), "status": "APPLIED", "previous_version": before["installed_version"],
            "snapshot_id": result["snapshot_id"], "changed": [c["path"] for c in result["changes"]]}


def sync_all(*, apply: bool) -> dict[str, Any]:
    results = [sync_project(Path(key), apply=apply) for key in sorted(_load())]
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"harness_version": __version__, "applied": apply, "counts": counts, "projects": results}
