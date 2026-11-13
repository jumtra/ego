"""Non-destructive installation, diff planning, backup, ownership and rollback."""
from __future__ import annotations
from pathlib import Path
import json
import shutil
import time
from .config import Config, save_config, dump_config
from .templates import generated_files, root_instructions, MARK_BEGIN, MARK_END, LEGACY_WORK_HASHES
from .util import EgoError, safe_path, filehash, sha256, read_json, write_json, atomic_text, ident, exclusive_lock

OWNERSHIP = ".ego/ownership.json"

def _apply_block(previous: str, block: str) -> str:
    if MARK_BEGIN in previous and MARK_END in previous:
        if previous.count(MARK_BEGIN) != 1 or previous.count(MARK_END) != 1:
            raise EgoError("MANAGED_BLOCK_CONFLICT", "Ambiguous generated block")
        a = previous.index(MARK_BEGIN)
        b = previous.index(MARK_END) + len(MARK_END)
        return previous[:a] + block.rstrip("\n") + previous[b:]
    if MARK_BEGIN not in previous and MARK_END not in previous:
        return previous.rstrip() + ("\n\n" if previous.strip() else "") + block
    raise EgoError("MANAGED_BLOCK_CONFLICT", "Ambiguous generated block")


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
                and not (skill == "work" and prev[rel] in LEGACY_WORK_HASHES)):
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
        from .config import load_config
        try:
            current = load_config(root)
        except EgoError:
            current = None
        wanted[".ego/config.toml"] = cfg_file.read_text(encoding="utf-8") if current == cfg else dump_config(cfg)
    else:
        wanted[".ego/config.toml"] = dump_config(cfg)
    wanted.update({k: _apply_block(safe_path(root, k).read_text(encoding="utf-8") if safe_path(root,k).exists() else "", v) for k,v in root_instructions(cfg).items()})
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
        elif rel in ("CLAUDE.md", "AGENTS.md") and MARK_BEGIN in path.read_text(encoding="utf-8"):
            action = "update_block"
        elif rel in ("CLAUDE.md", "AGENTS.md"):
            action = "append_block"
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
                if item["action"] == "remove_owned":
                    path.unlink()
                else:
                    atomic_text(path, plan["wanted"][item["path"]])
            files = {rel: filehash(safe_path(root, rel)) for rel in plan["wanted"]}
            from . import __version__
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
    from .projects import register_quietly
    register_quietly(root)
    return {"status": "APPLIED", "snapshot_id": snapshot_id, "changes": [x for x in plan["changes"] if x["action"] != "unchanged"]}


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

LEGACY_SKILLS = ("brainstorm", "create-pr", "address-review", "self-review", "evolve-rules", "save-session", "create-skill", "harness-sync")
LEGACY_AGENTS = ("test-writer", "refactorer", "design-reviewer", "ci-triage", "researcher")
LEGACY_RULES = ("debugging", "workflow", "memory", "domain", "coding")

def legacy_plan(root: Path) -> list[dict]:
    results = []
    for rel in [*(f".claude/skills/{n}/SKILL.md" for n in LEGACY_SKILLS), *(f".claude/agents/{n}.md" for n in LEGACY_AGENTS), *(f".claude/rules/{n}.md" for n in LEGACY_RULES)]:
        path = safe_path(root, rel)
        if path.exists(): results.append({"path": rel, "status": "USER_OWNED_REVIEW_REQUIRED", "sha256": filehash(path)})
    return results

def remove_legacy(root: Path, approved_hashes: dict[str, str]) -> dict:
    """Never remove unknown originals without exact file hash approval."""
    deleted = []
    allowed = {row["path"]:row["sha256"] for row in legacy_plan(root)}
    with exclusive_lock(root / ".ego" / "locks" / "install.lock"):
        snap = root / ".ego" / "backups" / ident("legacy")
        for rel, wanted_hash in approved_hashes.items():
            if rel not in allowed or wanted_hash != allowed[rel]: raise EgoError("APPROVAL_MISMATCH", rel)
            p = safe_path(root, rel)
            dest = snap / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            p.unlink()
            deleted.append(rel)
    return {"deleted": deleted, "backup": str(snap) if deleted else None}


def restore_legacy(root: Path, snapshot_id: str) -> dict:
    """Restore exactly one previously snapshotted set of legacy files, without clobbering."""
    if not snapshot_id.startswith("legacy-") or not snapshot_id.replace("legacy-", "").isalnum():
        raise EgoError("INVALID_ID", snapshot_id, 2)
    backup = safe_path(root, f".ego/backups/{snapshot_id}", allow_missing=False)
    files = [p for p in backup.rglob("*") if p.is_file() and not p.is_symlink()]
    paths = [p.relative_to(backup).as_posix() for p in files]
    with exclusive_lock(root / ".ego" / "locks" / "install.lock"):
        for rel in paths:
            if safe_path(root, rel).exists():
                raise EgoError("RESTORE_CONFLICT", rel)
        for p, rel in zip(files, paths):
            target = safe_path(root, rel)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(p,target)
    return {"status":"RESTORED", "snapshot_id":snapshot_id,"paths":paths}


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
        from .projects import unregister
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
