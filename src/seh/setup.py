"""Small stdlib-only onboarding UX, separate from the fast execution path.

Interactive TTY: inspect -> choose -> preview -> confirm -> apply.
Automation: seh setup --yes [flags].  Non-TTY without --yes: dry-run.
No external package manager or model call runs during setup.
"""
from __future__ import annotations
from pathlib import Path
import sys
from .config import Config, HOSTS, PROVIDERS, MEMORY, load_config
from .hosts import capability
from .install import installation_plan, apply_install, uninstallation_plan, apply_uninstall, OWNERSHIP, INSTRUCTIONS, _block_is_owned
from .util import SEHError, read_json, filehash, safe_path


def _configured_hosts(root: Path) -> tuple[list[str], list[str]]:
    installed = [h for h in HOSTS if h != 'agy' and capability(h).available]
    if installed:
        return installed, []
    # The local machine may not have an AI CLI yet; generating config is safe.
    return ['claude'], ['No supported AI CLI was found. Generated Claude Code config only; install/sign in separately.']


def _prompt(label: str, default: str, *, choices: tuple[str,...] | None = None,
            ask=input) -> str:
    while True:
        value = ask(f'{label} [{default}]: ').strip() or default
        if choices is None or value in choices:
            return value
        print('  Allowed: ' + ', '.join(choices))


def _confirm(label: str, *, ask=input) -> bool:
    return ask(label + ' [y/N]: ').strip().lower() in {'y','yes'}


def resolve_setup(root: Path, *, profile: str | None, hosts: str | None, graph: str | None,
                  memory: str | None, detect_tests: bool = False, interactive: bool = False,
                  ask=input, scan: bool = True) -> tuple[Config, dict, list[str]]:
    root = root.resolve()
    if not root.is_dir():
        raise SEHError('NOT_FOUND', f'Project directory does not exist: {root}', 3)
    cfg_path = root / '.harness' / 'config.toml'
    existing = cfg_path.exists()
    cfg = load_config(root) if existing else Config()
    warnings: list[str] = []
    if not existing and hosts is None:
        chosen, messages = _configured_hosts(root)
        cfg.hosts = chosen
        warnings.extend(messages)

    if interactive:
        default_hosts = ','.join(cfg.hosts)
        choice = _prompt('AI hosts (comma-separated or all/auto)', hosts or default_hosts, ask=ask)
        hosts = choice
        profile = _prompt('Setup profile', profile or cfg.profile, choices=('minimal','balanced','full'), ask=ask)
        graph = _prompt('Code intelligence', graph or cfg.code_intelligence, choices=PROVIDERS, ask=ask)
        memory = _prompt('Memory backend', memory or cfg.memory, choices=MEMORY, ask=ask)

    if hosts:
        if hosts == 'all':
            cfg.hosts = list(HOSTS)
        elif hosts == 'auto':
            cfg.hosts, messages = _configured_hosts(root)
            warnings.extend(messages)
        else:
            cfg.hosts = [h.strip() for h in hosts.split(',') if h.strip()]
    if profile: cfg.profile = profile
    if graph: cfg.code_intelligence = graph
    if memory: cfg.memory = memory
    cfg.validate()

    detection = {'root': str(root), 'stack': [], 'languages': [], 'projects': [],
                 'test_commands': [], 'test_targets': []}
    if scan:
        from .detect import detect
        detection = detect(root)
    if detect_tests and not cfg.tests:
        # Only proposals for root project, no test scripts are executed.
        cfg.tests = detection['test_commands']
        cfg.validate()
    for host in cfg.hosts:
        cap = capability(host)
        if not cap.available:
            warnings.append(f'{host} CLI is not installed; host instructions can still be generated.')
        if host == 'agy':
            warnings.append('AGY: config generation only. Automated CLI invocation is disabled.')
    if cfg.code_intelligence != 'none':
        warnings.append(f'{cfg.code_intelligence} requires its own CLI/install; setup will not install or run it.')
    if cfg.memory == 'engram':
        warnings.append('Engram MCP must be connected separately; setup does not start external services.')
    return cfg, detection, list(dict.fromkeys(warnings))


def _view_plan(title: str, root: Path, cfg: Config | None, plan: dict,
               detection: dict | None, warnings: list[str]) -> str:
    changes = [c for c in plan['changes'] if c['action'] not in ('unchanged','already_missing')]
    lines = [f'\n{title}', '=' * len(title), f'Project: {root}',
             f'Changes: {len(changes)} | Conflicts: {len(plan["conflicts"])}']
    if cfg is not None:
        lines.extend([f'Hosts: {", ".join(cfg.hosts)}',
                      f'Profile: {cfg.profile} (no always-on model calls)',
                      f'Code intelligence: {cfg.code_intelligence} | Memory: {cfg.memory}',
                      'Harness² optimization: OFF by default'])
    if detection:
        lines.append('Languages: ' + (', '.join(detection.get('languages', [])) or 'not detected'))
        lines.append(f'Projects detected: {len(detection.get("projects", []))}')
    if changes:
        lines.append('\nPlanned file changes:')
        for row in changes[:40]:
            lines.append(f'  {row["action"]:19} {row["path"]}')
        if len(changes) > 40:
            lines.append(f'  ... and {len(changes)-40} more')
    else:
        lines.append('No managed file changes needed.')
    for message in warnings:
        lines.append('NOTE: ' + message)
    if plan['conflicts']:
        lines.append('CONFLICT: Existing edited/unowned files will not be overwritten or removed.')
    return '\n'.join(lines)


def _compact(plan: dict) -> dict:
    return {'root': plan['root'], 'changes': plan['changes'], 'conflicts': plan['conflicts']}


def setup_command(root: Path, *, profile: str | None = None, hosts: str | None = None,
                  graph: str | None = None, memory: str | None = None,
                  detect_tests: bool = False, yes: bool = False, non_interactive: bool = False,
                  json_format: bool = False, scan: bool = True, ask=input) -> tuple[dict,int]:
    root = root.resolve()
    interactive = sys.stdin.isatty() and not non_interactive and not yes and not json_format
    cfg, detected, warnings = resolve_setup(root, profile=profile, hosts=hosts,
              graph=graph, memory=memory, detect_tests=detect_tests,
              interactive=interactive, ask=ask, scan=scan)
    plan = installation_plan(root, cfg)
    if not json_format:
        print(_view_plan('Self-Evolve Harness setup', root, cfg, plan, detected, warnings))
    if plan['conflicts']:
        return {'status':'CONFLICT', 'applied':False,'plan':_compact(plan),'warnings':warnings}, 4
    approved = yes or (interactive and _confirm('Apply these changes?', ask=ask))
    if not approved:
        if not json_format:
            print('\nPreview only. To apply without prompts: seh setup --root . --yes')
        return {'status':'PREVIEW','applied':False,'plan':_compact(plan), 'warnings':warnings}, 0
    outcome = apply_install(root, cfg)
    if not json_format:
        print(f'\n{outcome["status"]}: {len(outcome["changes"])} file changes')
        if outcome['snapshot_id']:
            print(f'Undo this change: seh migrate rollback {outcome["snapshot_id"]} --root {root}')
        print(f'Next: seh status --root {root}')
        print(f'      seh project checks --root {root}  (read-only)')
    return {'status':outcome['status'], 'applied':True, 'snapshot_id':outcome['snapshot_id'],
            'plan':_compact(plan), 'warnings':warnings},0


def uninstall_command(root: Path, *, yes: bool = False, json_format: bool = False,
                      non_interactive: bool = False, ask=input) -> tuple[dict,int]:
    root = root.resolve()
    plan = uninstallation_plan(root)
    interactive = sys.stdin.isatty() and not yes and not json_format and not non_interactive
    if not json_format:
        print(_view_plan('Self-Evolve Harness uninstall',root,None,plan,None,
              ['Memory, evidence, backups and user-created files are retained.']))
    if not plan['installed']:
        return {'status':'NOT_INSTALLED', 'applied':False, 'plan':_compact(plan)}, 0
    if plan['conflicts']:
        return {'status':'CONFLICT', 'applied':False, 'plan':_compact(plan)}, 4
    approved = yes or (interactive and _confirm('Remove ONLY SEH-managed files?', ask=ask))
    if not approved:
        if not json_format:
            print('\nPreview only. To remove without prompts: seh uninstall --root . --yes')
        return {'status':'PREVIEW', 'applied':False, 'plan':_compact(plan)},0
    outcome = apply_uninstall(root)
    if not json_format:
        print(f'\n{outcome["status"]}: {len(outcome["changes"])} managed files removed/updated.')
        print(f'Undo removal: seh migrate rollback {outcome["snapshot_id"]} --root {root}')
        print('User files, backups and memory were preserved.')
    return outcome,0


def status_command(root: Path) -> dict:
    """Quick read-only diagnosis: does not load optimization, index code or call an LLM."""
    root = root.resolve()
    owner = read_json(root / OWNERSHIP, None)
    if owner is None:
        return {'root':str(root),'installed':False,'status':'NOT_INSTALLED',
                'next_step':f'seh setup --root {root}'}
    data, files = (owner, owner.get('files', {}))
    if not isinstance(files, dict):
        raise SEHError('OWNERSHIP_INVALID', 'Malformed ownership file')
    drift = []
    for rel, digest in files.items():
        try:
            path=safe_path(root, rel)  # reject escaping symlinks
            actual=filehash(path)
            if rel in INSTRUCTIONS and _block_is_owned(path, digest, data.get('blocks',{}).get(rel)):
                continue  # user-written text outside our marker is not drift
        except (OSError, SEHError):
            actual=None
        if digest != actual:
            drift.append(rel)
    cfg = load_config(root)
    hosts = {h:{'available':capability(h).available,'mode':capability(h).mode}
             for h in cfg.hosts}
    return {'root':str(root),'installed':True,'status':'DRIFT' if drift else 'READY',
            'profile':cfg.profile,'hosts':hosts, 'code_intelligence':cfg.code_intelligence,
            'memory':cfg.memory,'managed_files':len(files),'drift':drift,
            'optimization':'OFF by default; separate opt-in config',
            'next_step':'seh setup --root . (preview changes)' if drift else 'seh project checks --root .'}


def display_status(obj: dict) -> str:
    if not obj['installed']:
        return f'SEH: not installed in {obj["root"]}\nNext: {obj["next_step"]}'
    hosts = ', '.join(f'{h}({"available" if v["available"] else "not installed"})'
                      for h,v in obj['hosts'].items())
    lines = [f'SEH: {obj["status"]} in {obj["root"]}',
             f'Hosts: {hosts}',f'Profile: {obj["profile"]} | Code: {obj["code_intelligence"]} | Memory: {obj["memory"]}',
             f'Managed files: {obj["managed_files"]} | Drift: {len(obj["drift"])}']
    if obj['drift']:
        lines.extend('  changed: ' + f for f in obj['drift'][:10])
    lines.append('Next: ' + obj['next_step'])
    return '\n'.join(lines)
