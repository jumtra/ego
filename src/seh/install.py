"""Ownership-aware, reversible installation and removal of SEH-generated files.

All planning is read-only. Neither setup nor uninstall touches user-owned content,
and both use the same lock / snapshot / rollback mechanism.
"""
from __future__ import annotations
from pathlib import Path
import shutil
from .config import Config, dump_config
from .templates import generated_files, root_instructions, MARK_BEGIN, MARK_END
from .util import SEHError, safe_path, filehash, sha256, read_json, write_json, atomic_text, ident, exclusive_lock

OWNERSHIP = '.harness/ownership.json'
INSTRUCTIONS = ('CLAUDE.md', 'AGENTS.md')
USER_SEED_FILES = {'.harness/knowledge/domain.md'}


def _block(previous: str) -> str | None:
    """Return only the owned marker span, never adjacent user content."""
    if MARK_BEGIN not in previous and MARK_END not in previous:
        return None
    if previous.count(MARK_BEGIN) != 1 or previous.count(MARK_END) != 1:
        raise SEHError('MANAGED_BLOCK_CONFLICT', 'Ambiguous generated block markers')
    a, b = previous.index(MARK_BEGIN), previous.index(MARK_END)
    if b < a:
        raise SEHError('MANAGED_BLOCK_CONFLICT', 'Managed block markers are reversed')
    return previous[a:b + len(MARK_END)]


def _apply_block(previous: str, block: str) -> str:
    span = _block(previous)
    if span is None:
        return previous.rstrip() + ('\n\n' if previous.strip() else '') + block
    start = previous.index(span)
    return previous[:start] + block.rstrip('\n') + previous[start + len(span):]


def _strip_block(previous: str) -> str:
    span = _block(previous)
    if span is None:
        return previous
    at = previous.index(span)
    # Only the managed span (and at most its adjacent separator) is removed.
    before, after = previous[:at], previous[at + len(span):]
    if before.endswith('\n\n'):
        before = before[:-1]
    if after.startswith('\n'):
        after = after[1:]
    remaining = before + after
    return remaining if remaining.strip() else ''


def _block_is_owned(path: Path, previous_hash: str | None, block_hash: str | None) -> bool:
    if not path.exists():
        return False
    if filehash(path) == previous_hash:
        return True
    if not block_hash:
        return False
    existing = _block(path.read_text(encoding='utf8'))
    return existing is not None and sha256(existing) == block_hash


def _owned_files(root: Path) -> tuple[dict, dict[str,str]]:
    raw = read_json(root / OWNERSHIP, {'files': {}})
    if not isinstance(raw, dict) or not isinstance(raw.get('files'), dict):
        raise SEHError('OWNERSHIP_INVALID', 'Invalid .harness/ownership.json')
    return raw, raw['files']


def installation_plan(root: Path, cfg: Config) -> dict:
    cfg.validate()
    root = root.resolve()
    ownership, prev = _owned_files(root)
    old_blocks = ownership.get('blocks', {})
    wanted: dict[str, str] = generated_files(cfg)
    # Domain knowledge is a user-managed seed, not a generated artifact to erase.
    # After first creation it must survive re-setup and uninstall even if edited.
    for rel in USER_SEED_FILES:
        if safe_path(root, rel).exists() and rel not in prev:
            wanted.pop(rel, None)
    # A user-updated canonical Skill remains the source of truth if its hash is
    # still owned. User-modified canonical files are never silently overwritten.
    for skill in ('harness-init', 'work'):
        rel = f'.harness/spec/skills/{skill}/SKILL.md'
        canonical = safe_path(root, rel)
        if canonical.exists() and rel in prev and filehash(canonical) == prev[rel]:
            content = canonical.read_text(encoding='utf8')
            wanted[rel] = content
            if 'claude' in cfg.hosts:
                wanted[f'.claude/skills/{skill}/SKILL.md'] = content
            if any(h in cfg.hosts for h in ('codex', 'opencode', 'agy')):
                wanted[f'.agents/skills/{skill}/SKILL.md'] = content
    guide = '.harness/spec/prompts/tool-guide.md'
    gpath = safe_path(root, guide)
    if gpath.exists() and guide in prev and filehash(gpath) == prev[guide]:
        wanted[guide] = gpath.read_text(encoding='utf8')

    config_path = safe_path(root, '.harness/config.toml')
    if config_path.exists():
        from .config import load_config
        # Do not silently accept corrupt existing configuration.
        current = load_config(root)
        wanted['.harness/config.toml'] = config_path.read_text(encoding='utf8') if current == cfg else dump_config(cfg)
    else:
        wanted['.harness/config.toml'] = dump_config(cfg)

    instruction_inputs = root_instructions(cfg)
    for rel, block in instruction_inputs.items():
        path = safe_path(root, rel)
        wanted[rel] = _apply_block(path.read_text(encoding='utf8') if path.exists() else '', block)

    changes = []
    for rel, content in sorted(wanted.items()):
        path = safe_path(root, rel)
        old, desired = filehash(path), sha256(content)
        if old == desired:
            action = 'unchanged'
        elif old is None:
            action = 'create'
        elif rel in INSTRUCTIONS:
            span = _block(path.read_text(encoding='utf8'))
            if span is None:
                action = 'append_block' if rel not in prev else 'conflict'
            elif rel in prev and _block_is_owned(path, prev[rel], old_blocks.get(rel)):
                action = 'update_block'
            else:
                action = 'conflict'
        elif rel in prev and prev[rel] == old:
            action = 'replace_owned'
        else:
            action = 'conflict'
        changes.append({'path': rel, 'action': action, 'previous_sha256': old, 'desired_sha256': desired})

    # Removed hosts must not leave obsolete instruction blocks behind.
    for rel, expected in prev.items():
        if rel in wanted:
            continue
        path = safe_path(root, rel)
        actual = filehash(path)
        if rel in INSTRUCTIONS:
            if actual is None:
                continue
            if not _block_is_owned(path, expected, old_blocks.get(rel)):
                action, desired = 'conflict_orphan', None
            else:
                remaining = _strip_block(path.read_text(encoding='utf8'))
                action, desired = 'remove_block', (sha256(remaining) if remaining else None)
                wanted[rel] = remaining
        else:
            action, desired = ('remove_owned', None) if actual == expected else ('conflict_orphan', None)
        changes.append({'path': rel, 'action': action, 'previous_sha256': actual, 'desired_sha256': desired})

    return {'root': str(root), 'changes': changes,
            'conflicts': [c for c in changes if c['action'].startswith('conflict')], 'wanted': wanted}


def _snapshot_file(root: Path, backup: Path, item: dict, meta: dict) -> None:
    rel = item['path']
    path = safe_path(root, rel)
    exists = path.exists()
    meta['paths'][rel] = exists
    if exists:
        bpath = backup / rel
        bpath.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bpath)


def _write_plan_item(root: Path, plan: dict, item: dict) -> None:
    path = safe_path(root, item['path'])
    if item['action'] in ('remove_owned',) or (item['action'] == 'remove_block' and not plan['wanted'][item['path']]):
        if path.exists():
            path.unlink()
    else:
        atomic_text(path, plan['wanted'][item['path']])


def apply_install(root: Path, cfg: Config) -> dict:
    root = root.resolve()
    with exclusive_lock(root / '.harness' / 'locks' / 'install.lock'):
        plan = installation_plan(root, cfg)
        if plan['conflicts']:
            raise SEHError('OWNERSHIP_CONFLICT', str(plan['conflicts']))
        changes = [c for c in plan['changes'] if c['action'] != 'unchanged']
        if not changes:
            return {'status': 'UNCHANGED', 'snapshot_id': None, 'changes': []}
        ownership_path = root / OWNERSHIP
        old_data, _ = _owned_files(root)
        snapshot_id = ident('snapshot')
        backup = root / '.harness' / 'backups' / snapshot_id
        backup.mkdir(parents=True, exist_ok=False)
        meta: dict = {'id': snapshot_id, 'paths': {}, 'previous_ownership': old_data,
                      'previous_ownership_exists': ownership_path.exists(), 'applied_files': {}}
        try:
            for item in changes:
                _snapshot_file(root, backup, item, meta)
                _write_plan_item(root, plan, item)
                meta['applied_files'][item['path']] = filehash(safe_path(root, item['path']))
            initial_actions = {c['path']: c['action'] for c in plan['changes']}
            instruction_keys = set(root_instructions(cfg))
            files = {rel: filehash(safe_path(root, rel)) for rel in plan['wanted']
                     if safe_path(root, rel).exists()
                     and (rel not in INSTRUCTIONS or rel in instruction_keys)
                     and rel not in USER_SEED_FILES
                     and (rel in old_data.get('files', {}) or initial_actions.get(rel) != 'unchanged')}
            blocks = {rel: sha256(_block(safe_path(root, rel).read_text(encoding='utf8')))
                      for rel in instruction_keys if rel in files}
            write_json(ownership_path, {'schema_version': 1, 'files': files, 'blocks': blocks})
            meta['applied_ownership_sha256'] = filehash(ownership_path)
            write_json(backup / 'meta.json', meta)
        except Exception:
            for rel, existed in meta['paths'].items():
                path = safe_path(root, rel)
                if existed:
                    atomic_text(path, (backup / rel).read_text(encoding='utf8'))
                elif path.exists():
                    path.unlink()
            write_json(ownership_path, old_data)
            raise
    return {'status': 'APPLIED', 'snapshot_id': snapshot_id, 'changes': changes}


def uninstallation_plan(root: Path) -> dict:
    root = root.resolve()
    ownership_path = root / OWNERSHIP
    if not ownership_path.exists():
        return {'root': str(root), 'installed': False, 'changes': [], 'conflicts': [], 'wanted': {}}
    ownership, prev = _owned_files(root)
    blocks = ownership.get('blocks', {})
    wanted = {}
    changes = []
    for rel, expected in sorted(prev.items()):
        path = safe_path(root, rel)
        actual = filehash(path)
        if actual is None:
            action = 'already_missing'
        elif rel in INSTRUCTIONS:
            if _block_is_owned(path, expected, blocks.get(rel)):
                stripped = _strip_block(path.read_text(encoding='utf8'))
                action = 'remove_block'
                wanted[rel] = stripped
            else:
                action = 'conflict'
        elif actual == expected:
            action = 'remove_owned'
        else:
            action = 'conflict'
        changes.append({'path': rel, 'action': action, 'previous_sha256': actual,
                        'desired_sha256': sha256(wanted[rel]) if rel in wanted and wanted[rel] else None})
    return {'root': str(root), 'installed': True, 'changes': changes,
            'conflicts': [c for c in changes if c['action'] == 'conflict'], 'wanted': wanted}


def apply_uninstall(root: Path) -> dict:
    """Remove only owned artifacts. Leave memory, user data and backups intact."""
    root = root.resolve()
    with exclusive_lock(root / '.harness' / 'locks' / 'install.lock'):
        plan = uninstallation_plan(root)
        if not plan['installed']:
            return {'status': 'NOT_INSTALLED', 'snapshot_id': None, 'changes': []}
        if plan['conflicts']:
            raise SEHError('OWNERSHIP_CONFLICT', str(plan['conflicts']))
        old_data, _ = _owned_files(root)
        snap_id = ident('snapshot')
        backup = root / '.harness' / 'backups' / snap_id
        backup.mkdir(parents=True, exist_ok=False)
        meta = {'id': snap_id, 'paths': {}, 'previous_ownership': old_data, 'applied_files': {}}
        try:
            for item in plan['changes']:
                if item['action'] == 'already_missing':
                    continue
                _snapshot_file(root, backup, item, meta)
                if item['action'] == 'remove_owned' or (item['action'] == 'remove_block' and not plan['wanted'].get(item['path'])):
                    safe_path(root, item['path']).unlink()
                else:
                    atomic_text(safe_path(root, item['path']), plan['wanted'][item['path']])
                meta['applied_files'][item['path']] = filehash(safe_path(root, item['path']))
            meta['previous_ownership_exists'] = (root / OWNERSHIP).exists()
            (root / OWNERSHIP).unlink()
            meta['applied_ownership_sha256'] = None
            write_json(backup / 'meta.json', meta)
        except Exception:
            for rel, existed in meta['paths'].items():
                path = safe_path(root, rel)
                if existed: atomic_text(path, (backup / rel).read_text(encoding='utf8'))
                elif path.exists(): path.unlink()
            write_json(root / OWNERSHIP, old_data)
            raise
    return {'status': 'UNINSTALLED', 'snapshot_id': snap_id,
            'changes': [c for c in plan['changes'] if c['action'] not in ('already_missing',)],
            'data_retained': ['.harness/backups', '.harness/state', '.harness/artifacts', '.harness/knowledge', '.harness/memory']}


def rollback(root: Path, snapshot_id: str) -> dict:
    if not snapshot_id.startswith('snapshot-') or not snapshot_id.removeprefix('snapshot-').isalnum():
        raise SEHError('INVALID_ID', snapshot_id, 2)
    root = root.resolve()
    backup = safe_path(root, f'.harness/backups/{snapshot_id}')
    meta = read_json(backup / 'meta.json')
    if not meta:
        raise SEHError('NOT_FOUND', snapshot_id, 3)
    with exclusive_lock(root / '.harness' / 'locks' / 'install.lock'):
        # CAS protects post-snapshot user edits from being overwritten on rollback.
        expected = meta.get('applied_files', {})
        if 'applied_ownership_sha256' in meta and filehash(root / OWNERSHIP) != meta['applied_ownership_sha256']:
            raise SEHError('ROLLBACK_CONFLICT', 'Ownership metadata was modified after snapshot')
        for rel, digest in expected.items():
            if filehash(safe_path(root, rel)) != digest:
                raise SEHError('ROLLBACK_CONFLICT', f'File was modified after snapshot: {rel}')
        for rel, existed in meta['paths'].items():
            path = safe_path(root, rel)
            if existed:
                atomic_text(path, (backup / rel).read_text(encoding='utf8'))
            elif path.exists():
                path.unlink()
        if meta.get('previous_ownership_exists', True):
            write_json(root / OWNERSHIP, meta['previous_ownership'])
        elif (root / OWNERSHIP).exists():
            (root / OWNERSHIP).unlink()
    return {'status': 'ROLLED_BACK', 'snapshot_id': snapshot_id}

LEGACY_SKILLS = ('brainstorm', 'create-pr', 'address-review', 'self-review', 'evolve-rules', 'save-session', 'create-skill', 'harness-sync')
LEGACY_AGENTS = ('test-writer', 'refactorer', 'design-reviewer', 'ci-triage', 'researcher')
LEGACY_RULES = ('debugging', 'workflow', 'memory', 'domain', 'coding')

def legacy_plan(root: Path) -> list[dict]:
    results = []
    for rel in [*(f'.claude/skills/{n}/SKILL.md' for n in LEGACY_SKILLS), *(f'.claude/agents/{n}.md' for n in LEGACY_AGENTS), *(f'.claude/rules/{n}.md' for n in LEGACY_RULES)]:
        path = safe_path(root, rel)
        if path.exists(): results.append({'path': rel, 'status': 'USER_OWNED_REVIEW_REQUIRED', 'sha256': filehash(path)})
    return results

def remove_legacy(root: Path, approved_hashes: dict[str, str]) -> dict:
    """Never remove unknown originals without exact file hash approval."""
    deleted = []
    allowed = {row['path']: row['sha256'] for row in legacy_plan(root)}
    with exclusive_lock(root / '.harness' / 'locks' / 'install.lock'):
        snap = root / '.harness' / 'backups' / ident('legacy')
        for rel, wanted_hash in approved_hashes.items():
            if rel not in allowed or wanted_hash != allowed[rel]: raise SEHError('APPROVAL_MISMATCH', rel)
            p = safe_path(root, rel)
            dest = snap / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            p.unlink()
            deleted.append(rel)
    return {'deleted': deleted, 'backup': str(snap) if deleted else None}

def restore_legacy(root: Path, snapshot_id: str) -> dict:
    if not snapshot_id.startswith('legacy-') or not snapshot_id.removeprefix('legacy-').isalnum():
        raise SEHError('INVALID_ID', snapshot_id, 2)
    backup = safe_path(root, f'.harness/backups/{snapshot_id}', allow_missing=False)
    files = [p for p in backup.rglob('*') if p.is_file() and not p.is_symlink()]
    paths = [p.relative_to(backup).as_posix() for p in files]
    with exclusive_lock(root / '.harness' / 'locks' / 'install.lock'):
        for rel in paths:
            if safe_path(root, rel).exists():
                raise SEHError('RESTORE_CONFLICT', rel)
        for p, rel in zip(files, paths):
            target = safe_path(root, rel)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(p,target)
    return {'status':'RESTORED','snapshot_id':snapshot_id,'paths':paths}
