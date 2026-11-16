"""Registry of projects ego was applied to: which version each one runs, and bulk re-sync.

The registry is one small user-level JSON file (paths only). Each project's own
`.ego/config.toml` stays the source of its settings, so a re-sync inherits them.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from .. import __version__
from ..domain.config import load_config
from ..shared.util import EgoError, now, read_json
# Registry basics live in .install (no cycle); re-exported here for callers.
from .install import (OWNERSHIP, apply_install, installation_plan, registry_path, register,
                      register_quietly, unregister, _load, _mutate)

__all__ = ["OWNERSHIP", "apply_install", "installation_plan", "registry_path", "register",
           "register_quietly", "unregister", "project_info", "list_projects", "scan", "sync_project", "sync_all"]

SCAN_SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "Library", ".cache", ".Trash"}
MAX_SCAN_DEPTH = 6


def project_info(root: Path) -> dict[str, Any]:
    """Version and drift of one applied project, read from its own files."""
    info: dict[str, Any] = {"root": str(root), "harness_version": __version__}
    try:
        ownership = read_json(root / OWNERSHIP)
    except EgoError:
        ownership = None
    if not isinstance(ownership, dict):
        return {**info, "status": "MISSING" if not root.is_dir() else "NOT_APPLIED", "installed_version": None}
    installed = ownership.get("installed_version") or ownership.get("version")
    info["installed_version"] = installed
    info["version_differs"] = installed != __version__
    try:
        cfg = load_config(root)
        plan = installation_plan(root, cfg)
    except (EgoError, OSError, ValueError) as exc:
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
        raise EgoError("NOT_FOUND", str(base), 3)
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
    before = project_info(root)
    if before["status"] in ("MISSING", "NOT_APPLIED", "ERROR", "BLOCKED") or not apply or before["status"] == "UP_TO_DATE":
        return before
    try:
        result = apply_install(root, load_config(root))
    except (EgoError, OSError, ValueError) as exc:
        return {**before, "status": "ERROR", "message": str(exc)}
    return {**project_info(root), "status": "APPLIED", "previous_version": before["installed_version"],
            "snapshot_id": result["snapshot_id"], "changed": [c["path"] for c in result["changes"]]}


def sync_all(*, apply: bool) -> dict[str, Any]:
    results = [sync_project(Path(key), apply=apply) for key in sorted(_load())]
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"harness_version": __version__, "applied": apply, "counts": counts, "projects": results}
