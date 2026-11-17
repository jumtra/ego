"""Non-destructive installation, diff planning, backup, ownership and rollback."""
from __future__ import annotations
from pathlib import Path
import json
import shutil
import time
from ..domain.config import Config, save_config, dump_config
from ..domain.templates import generated_files, MARK_BEGIN, MARK_END, LEGACY_SKILL_HASHES
from .. import __version__
from ..shared.util import EgoError, safe_path, filehash, sha256, read_json, write_json, atomic_text, ident, exclusive_lock, now
import os
from typing import Any

OWNERSHIP = ".ego/ownership.json"

def _strip_block(text: str) -> str:
    """Remove a managed block that an earlier version appended to CLAUDE.md / AGENTS.md."""
    if MARK_BEGIN not in text and MARK_END not in text:
        return text
    if text.count(MARK_BEGIN) != 1 or text.count(MARK_END) != 1:
        raise EgoError("MANAGED_BLOCK_CONFLICT", "Ambiguous generated block")
    a = text.index(MARK_BEGIN)
    b = text.index(MARK_END) + len(MARK_END)
    kept = (text[:a] + text[b:]).strip()
    return kept + "\n" if kept else ""


def installation_plan(root: Path, cfg: Config) -> dict:
    cfg.validate()
    root = root.resolve()
    ownership_path = root / OWNERSHIP
    if not ownership_path.exists() and (root / ".harness" / "ownership.json").exists():
        ownership = read_json(root / ".harness" / "ownership.json", {"files": {}})
    else:
        ownership = read_json(ownership_path, {"files": {}})
    raw_prev = ownership.get("files", {})
    prev = {
        k.replace(".harness/", ".ego/"): (v.get("sha256") if isinstance(v, dict) else v)
        for k, v in raw_prev.items()
    }
    wanted = generated_files(cfg)
    # The canonical skill files are authoritative after first installation.
    for skill in ("harness-init", "work", "work-plan"):
        rel = f".ego/spec/skills/{skill}/SKILL.md"
        canon = safe_path(root, rel)
        if (canon.exists() and rel in prev and filehash(canon) == prev[rel]
                and prev[rel] not in LEGACY_SKILL_HASHES.get(skill, ())):
            content = canon.read_text(encoding="utf-8")
            wanted[rel] = content
            if "claude" in cfg.hosts: wanted[f".claude/skills/{skill}/SKILL.md"] = content
            if any(h in cfg.hosts for h in ("codex", "opencode", "agy")):
                wanted[f".agents/skills/{skill}/SKILL.md"] = content
    guide = ".ego/spec/prompts/tool-guide.md"
    gpath = safe_path(root, guide)
    if gpath.exists() and guide in prev and filehash(gpath) == prev[guide]:
        wanted[guide] = gpath.read_text(encoding="utf-8")
    cfg_file = safe_path(root, ".ego/config.toml")
    if cfg_file.exists():
        from ..domain.config import load_config
        try:
            current = load_config(root)
        except EgoError:
            current = None
        wanted[".ego/config.toml"] = cfg_file.read_text(encoding="utf-8") if current == cfg else dump_config(cfg)
    else:
        wanted[".ego/config.toml"] = dump_config(cfg)
    # The managed block is never persisted into CLAUDE.md / AGENTS.md: `ego run` overlays it for the session
    # only (usecases/overlay.py). A block left by an earlier version is removed, the rest is kept.
    for rel in ("CLAUDE.md", "AGENTS.md"):
        path = safe_path(root, rel)
        if path.exists() and MARK_BEGIN in path.read_text(encoding="utf-8"):
            wanted[rel] = _strip_block(path.read_text(encoding="utf-8"))
    changes = []
    for rel, content in sorted(wanted.items()):
        path = safe_path(root, rel)
        old = filehash(path)
        desired = sha256(content)
        if old == desired:
            action = "unchanged"
        elif old is None:
            action = "create"
        elif rel == ".ego/config.toml":
            action = "update_config"
        elif rel in ("CLAUDE.md", "AGENTS.md"):
            action = "strip_block"
        elif rel in prev and prev[rel] == old:
            action = "replace_owned"
        else:
            action = "conflict"
        changes.append({"path": rel, "action": action, "previous_sha256": old, "desired_sha256": desired})
    for rel, old_digest in prev.items():
        if rel not in wanted and rel not in ("CLAUDE.md", "AGENTS.md"):
            path = safe_path(root, rel)
            current_hash = filehash(path)
            if current_hash is None:
                continue
            if rel == ".ego/knowledge/domain.md" and current_hash != old_digest:
                continue  # an edited single-file knowledge note of earlier versions is the project's own: keep it, no conflict
            changes.append({"path": rel, "action": "remove_owned" if current_hash == old_digest else "conflict_orphan", "previous_sha256": current_hash, "desired_sha256": None})
    return {"root": str(root), "changes": changes, "conflicts": [x for x in changes if x["action"].startswith("conflict")], "wanted": wanted}


def apply_install(root: Path, cfg: Config) -> dict:
    root = root.resolve()
    with exclusive_lock(root / ".ego" / "locks" / "install.lock"):
        plan = installation_plan(root, cfg)
        if plan["conflicts"]: raise EgoError("OWNERSHIP_CONFLICT", str(plan["conflicts"]))
        ownership_path = root / OWNERSHIP
        old_data = read_json(ownership_path, {"files": {}})
        snapshot_id = ident("snapshot")
        backup = root / ".ego" / "backups" / snapshot_id
        backup.mkdir(parents=True, exist_ok=True)
        meta = {"id": snapshot_id, "paths": {}}
        try:
            for item in plan["changes"]:
                if item["action"] == "unchanged": continue
                path = safe_path(root, item["path"])
                if path.exists():
                    meta["paths"][item["path"]] = True
                    bpath = backup / item["path"]
                    bpath.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, bpath)
                else: meta["paths"][item["path"]] = False
                content = plan["wanted"].get(item["path"])
                if item["action"] == "remove_owned" or (item["action"] == "strip_block" and not content):
                    path.unlink()  # an instruction file that only held the legacy block goes away
                else:
                    atomic_text(path, plan["wanted"][item["path"]])
            files = {}
            for rel in plan["wanted"]:
                digest = filehash(safe_path(root, rel))
                if digest is not None:
                    files[rel] = digest
            write_json(ownership_path, {"schema_version": 1, "installed_version": __version__, "last_snapshot_id": snapshot_id, "files": files})
            meta["previous_ownership"] = old_data
            write_json(backup / "meta.json", meta)
            legacy_harness = root / ".harness"
            if legacy_harness.exists() and legacy_harness.is_dir():
                shutil.rmtree(legacy_harness, ignore_errors=True)
        except Exception:
            for rel, existed in meta["paths"].items():
                path = safe_path(root, rel)
                if existed:
                    atomic_text(path, (backup/rel).read_text(encoding="utf-8"))
                elif path.exists(): path.unlink()
            write_json(ownership_path, old_data)
            raise
    register_quietly(root)
    out = {"status": "APPLIED", "snapshot_id": snapshot_id, "changes": [x for x in plan["changes"] if x["action"] != "unchanged"]}
    if cfg.knowledge == "alter":
        from ..adapters.memory import ensure_alter_project_quietly, ensure_alter_skills_quietly
        out["alter_project"] = ensure_alter_project_quietly(root, force=True)
        out["alter_skills"] = ensure_alter_skills_quietly(root, clients=cfg.hosts)
    return out


def rollback(root: Path, snapshot_id: str) -> dict:
    if not snapshot_id.startswith("snapshot-") or not snapshot_id.replace("snapshot-", "").isalnum(): raise EgoError("INVALID_ID", snapshot_id, 2)
    root = root.resolve()
    backup = safe_path(root, f".ego/backups/{snapshot_id}")
    meta = read_json(backup / "meta.json")
    if not meta: raise EgoError("NOT_FOUND", snapshot_id, 3)
    with exclusive_lock(root / ".ego" / "locks" / "install.lock"):
        for rel, existed in meta["paths"].items():
            path = safe_path(root, rel)
            if existed:
                atomic_text(path, (backup / rel).read_text(encoding="utf-8"))
            elif path.exists(): path.unlink()
        write_json(root / OWNERSHIP, meta["previous_ownership"])
    return {"status": "ROLLED_BACK", "snapshot_id": snapshot_id}

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
    except (OSError, EgoError, ValueError):
        pass

def unregister(root: Path) -> bool:
    key = str(root.resolve())
    found = key in _load()
    if found:
        _mutate(lambda projects: projects.pop(key, None))
    return found


def uninstall_project(root: Path) -> dict:
    root = root.resolve()
    removed = []
    ownership_path = root / OWNERSHIP
    if ownership_path.exists():
        data = read_json(ownership_path, {"files": {}})
        for rel in sorted(data.get("files", {})):
            if rel.startswith(".ego/"):
                continue
            p = safe_path(root, rel)
            if p.exists() and p.is_file():
                p.unlink()
                removed.append(rel)
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = safe_path(root, name)
        if p.exists():
            text = p.read_text(encoding="utf-8")
            if MARK_BEGIN in text and MARK_END in text:
                a = text.index(MARK_BEGIN)
                b = text.index(MARK_END) + len(MARK_END)
                cleaned = (text[:a] + text[b:]).strip()
                if cleaned:
                    atomic_text(p, cleaned + "\n")
                    removed.append(f"{name} (cleaned)")
                else:
                    p.unlink()
                    removed.append(name)
    for d in (".claude", ".agents", ".codex", ".opencode"):
        dp = root / d
        if dp.exists() and dp.is_dir():
            shutil.rmtree(dp, ignore_errors=True)
            removed.append(d)
    ego_dir = root / ".ego"
    if ego_dir.exists() and ego_dir.is_dir():
        shutil.rmtree(ego_dir, ignore_errors=True)
        removed.append(".ego")
    legacy_harness = root / ".harness"
    if legacy_harness.exists() and legacy_harness.is_dir():
        shutil.rmtree(legacy_harness, ignore_errors=True)
        removed.append(".harness")
    try:
        unregister(root)
    except (OSError, EgoError, ValueError):
        pass
    return {"status": "UNINSTALLED", "root": str(root), "removed": removed}


def uninstall_cli(bindir: Path | None = None) -> dict:
    if bindir is None:
        bindir = Path.home() / ".local" / "bin"
    deleted = []
    for name in ("ego", "harness"):
        p = bindir / name
        if p.is_symlink() or p.exists():
            p.unlink()
            deleted.append(str(p))
    return {"status": "UNINSTALLED_CLI", "deleted": deleted}
