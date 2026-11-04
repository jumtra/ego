"""Non-executing project profiler, retaining the v1.4 detection response contract."""
from __future__ import annotations
from pathlib import Path
from .toolchains import detect_projects


def detect(root: Path) -> dict:
    root=root.resolve()
    projects=detect_projects(root)
    ecosystems=sorted({p['ecosystem'] for p in projects})
    languages=sorted({s for p in projects for s in p['languages']})
    # Old clients expect top-level check commands without cwd information.
    tests=[p['checks']['test'] for p in projects if p['path']=='.' and 'test' in p['checks']]
    targets=[{'project':p['path'],'ecosystem':p['ecosystem'],'argv':p['checks']['test']}
             for p in projects if 'test' in p['checks']]
    ci=[]
    for provider,path in (('github',root/'.github'/'workflows'),('gitlab',root/'.gitlab-ci.yml'),('circleci',root/'.circleci')):
        if path.exists():ci.append(provider)
    return {'root':str(root),'stack':ecosystems,'languages':languages,'projects':projects,
            'test_commands':tests,'test_targets':targets,'ci':ci,
            'git':(root/'.git').exists(),
            'existing_host_configs':[name for name in ('CLAUDE.md','AGENTS.md','.claude','.agents','.opencode','.codex') if (root/name).exists()]}
