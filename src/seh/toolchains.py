"""Language-independent project detection and approved, reviewable check commands.

No command is executed during discovery. Project overrides are data-only TOML,
never imported as Python modules or evaluated as shell snippets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tomllib
from typing import Any
from .util import SEHError

# Skip generated, downloaded and vendor content. Walking is bounded for monorepos.
SKIP_DIRS = frozenset({
    '.git', '.harness', '.venv', 'venv', '__pycache__', '.pytest_cache',
    'node_modules', '.next', '.nuxt', 'vendor', 'target', 'dist', 'build',
    '.gradle', '.idea', '.dart_tool', '.terraform', 'Pods', 'Library',
    'Temp', 'obj', '.build', '.swiftpm', '.yarn', 'coverage', '.turbo',
})
SOURCE_EXTENSIONS = {
    '.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript', '.mts': 'typescript',
    '.rs': 'rust', '.go': 'go', '.cs': 'csharp', '.fs': 'fsharp',
    '.vb': 'visualbasic', '.java': 'java', '.kt': 'kotlin', '.kts': 'kotlin',
    '.swift': 'swift', '.rb': 'ruby', '.php': 'php', '.dart': 'dart',
    '.c': 'c', '.h': 'c', '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp',
    '.gd': 'gdscript', '.ex': 'elixir', '.exs': 'elixir',
    '.zig': 'zig', '.lua': 'lua', '.sh': 'shell',
}
CHECK_KINDS = ('test', 'lint', 'typecheck', 'build', 'format')


def _files(path: Path) -> set[str]:
    try: return {p.name for p in path.iterdir() if p.is_file() and not p.is_symlink()}
    except (PermissionError, OSError): return set()


def _source_languages(path: Path, filenames: set[str]) -> list[str]:
    found = {SOURCE_EXTENSIONS[Path(n).suffix] for n in filenames if Path(n).suffix in SOURCE_EXTENSIONS}
    for sub in ('src', 'app', 'lib', 'scripts', 'Assets', 'Sources'):
        directory = path / sub
        if directory.is_dir() and not directory.is_symlink():
            try:
                for entry in list(directory.iterdir())[:100]:
                    if entry.is_file() and entry.suffix in SOURCE_EXTENSIONS:
                        found.add(SOURCE_EXTENSIONS[entry.suffix])
            except OSError: pass
    return sorted(found)


def _read_package(path: Path) -> dict:
    try:
        pkg = json.loads((path / 'package.json').read_text(encoding='utf8'))
        return pkg if isinstance(pkg, dict) else {}
    except (OSError, ValueError): return {}


def _node_manager(files: set[str], pkg: dict) -> str:
    pm = pkg.get('packageManager', '')
    if isinstance(pm, str):
        for prefix in ('pnpm@', 'yarn@', 'bun@', 'npm@'):
            if pm.startswith(prefix): return prefix[:-1]
    if 'pnpm-lock.yaml' in files: return 'pnpm'
    if 'yarn.lock' in files: return 'yarn'
    if 'bun.lock' in files or 'bun.lockb' in files: return 'bun'
    return 'npm'


def _script_command(manager: str, name: str) -> list[str]:
    return [manager, 'run', name]


def _infer(path: Path) -> list[dict]:
    files = _files(path)
    detected: list[dict] = []
    def add(ecosystem: str, languages: list[str], checks: dict[str,list[str]], markers: list[str], notes: str = ''):
        detected.append({'ecosystem': ecosystem, 'languages': sorted(set(languages)),
                         'markers': markers, 'checks': checks, 'notes': notes})

    if 'pyproject.toml' in files or files & {'requirements.txt', 'setup.py', 'setup.cfg', 'Pipfile', 'poetry.lock', 'uv.lock', 'pytest.ini', 'tox.ini'}:
        checks = {}
        if (path/'tests').is_dir() or files & {'pytest.ini', 'tox.ini'}:
            checks['test'] = ['python3', '-m', 'pytest', '-q']
        if files & {'ruff.toml', '.ruff.toml'}:
            checks['lint'] = ['ruff', 'check', '.']
        elif 'pyproject.toml' in files:
            try:
                data = tomllib.loads((path/'pyproject.toml').read_text(encoding='utf8'))
                if isinstance(data.get('tool'),dict) and 'pytest' in data['tool']:
                    checks.setdefault('test',['python3','-m','pytest','-q'])
                if isinstance(data.get('tool'),dict) and 'ruff' in data['tool']:
                    checks['lint'] = ['ruff','check','.']
            except (OSError, ValueError): pass
        add('python', ['python'], checks, sorted(files & {'pyproject.toml','requirements.txt','setup.py','Pipfile','pytest.ini','uv.lock'}))

    if 'package.json' in files or files & {'deno.json','deno.jsonc'}:
        if 'package.json' in files:
            pkg = _read_package(path)
            mgr = _node_manager(files,pkg)
            scripts = pkg.get('scripts',{}) if isinstance(pkg.get('scripts'),dict) else {}
            scripts = {k:v for k,v in scripts.items() if isinstance(v,str) and v.strip()
                       and 'no test specified' not in v.lower()}
            checks = {}
            for kind, names in {'test':['test'], 'lint':['lint'], 'typecheck':['typecheck','check-types','type-check'],
                                'build':['build'], 'format':['format:check','fmt:check','prettier:check']}.items():
                for name in names:
                    if name in scripts:
                        checks[kind] = _script_command(mgr,name);break
            languages = ['typescript'] if 'tsconfig.json' in files or any(s in files for s in ('tsconfig.base.json','tsconfig.build.json')) or 'typescript' in _source_languages(path, files) else ['javascript']
            dependencies=pkg.get('dependencies') if isinstance(pkg.get('dependencies'),dict) else {}
            dev_dependencies=pkg.get('devDependencies') if isinstance(pkg.get('devDependencies'),dict) else {}
            deps = {**dependencies, **dev_dependencies}
            if isinstance(deps,dict) and 'typescript' in deps: languages = ['typescript','javascript']
            add('node',languages,checks,['package.json'], f'Package manager: {mgr}; scripts execute repository code')
        else:
            checks = {'test':['deno','test'], 'lint':['deno','lint'], 'typecheck':['deno','check','main.ts']}
            # No arbitrary assumption that main.ts exists.
            checks.pop('typecheck')
            add('deno',['typescript','javascript'],checks,sorted(files & {'deno.json','deno.jsonc'}))

    if 'Cargo.toml' in files:
        add('rust',['rust'],{'test':['cargo','test'], 'lint':['cargo','clippy','--all-targets'],
                            'typecheck':['cargo','check'], 'build':['cargo','build'],
                            'format':['cargo','fmt','--all','--','--check']}, ['Cargo.toml'])
    if 'go.mod' in files or 'go.work' in files:
        add('go',['go'],{'test':['go','test','./...'],'lint':['go','vet','./...'],
                        'build':['go','build','./...']}, sorted(files & {'go.mod','go.work'}))
    if any(n.endswith(('.csproj','.fsproj','.vbproj','.sln','.slnx')) for n in files):
        languages = []
        for suffix,lang in (('.csproj','csharp'),('.fsproj','fsharp'),('.vbproj','visualbasic')):
            if any(n.endswith(suffix) for n in files):languages.append(lang)
        add('dotnet',languages or ['csharp'],{'test':['dotnet','test'], 'build':['dotnet','build'],
                                          'format':['dotnet','format','--verify-no-changes']},
            sorted(n for n in files if n.endswith(('.csproj','.fsproj','.vbproj','.sln','.slnx')))[:12])
    if 'pom.xml' in files:
        add('maven',['java'],{'test':['mvn','-q','test'], 'build':['mvn','-q','-DskipTests','package']},['pom.xml'])
    if 'build.gradle' in files or 'build.gradle.kts' in files:
        languages = [x for x in ('java','kotlin') if x in _source_languages(path,files)] or ['java', 'kotlin']
        add('gradle', languages, {'test':['gradle','test'], 'build':['gradle','build']},
            sorted(files & {'build.gradle','build.gradle.kts'}),
            'Use installed gradle; project gradlew wrapper is not executed automatically')
    if 'Package.swift' in files:
        add('swift',['swift'],{'test':['swift','test'],'build':['swift','build']},['Package.swift'])
    if 'Gemfile' in files or any(n.endswith('.gemspec') for n in files):
        checks = {}
        if (path/'spec').is_dir() or '.rspec' in files: checks['test'] = ['bundle','exec','rspec']
        elif 'Rakefile' in files: checks['test'] = ['bundle','exec','rake','test']
        add('ruby',['ruby'],checks,sorted(files & {'Gemfile','.rspec','Rakefile'}))
    if 'composer.json' in files:
        checks={}
        try:
            composer=json.loads((path/'composer.json').read_text(encoding='utf8'))
            if isinstance(composer,dict) and isinstance(composer.get('scripts'),dict) and 'test' in composer['scripts']:
                checks['test']=['composer','run-script','test']
        except (OSError,ValueError): pass
        if (path/'vendor'/'bin'/'phpunit').is_file():checks.setdefault('test',['php','vendor/bin/phpunit'])
        add('php',['php'],checks,['composer.json'])
    if 'pubspec.yaml' in files:
        is_flutter=(path/'android').exists() or (path/'ios').exists() or (path/'.metadata').exists()
        cmd='flutter' if is_flutter else 'dart'
        checks={'lint':[cmd,'analyze']}
        if (path/'test').is_dir():checks['test']=[cmd,'test']
        add('flutter' if is_flutter else 'dart',['dart'],checks,['pubspec.yaml'])
    if 'CMakeLists.txt' in files or 'meson.build' in files:
        if 'CMakeLists.txt' in files:
            checks={}
            if (path/'build'/'CMakeCache.txt').exists(): checks['build']=['cmake','--build','build']
            if (path/'build'/'CTestTestfile.cmake').exists():checks['test']=['ctest','--test-dir','build','--output-on-failure']
            add('cmake',['c','cpp'],checks,['CMakeLists.txt'], 'Configure build directory before running checks')
        else:
            checks={}
            if (path/'build'/'build.ninja').exists():checks['build']=['meson','compile','-C','build']
            add('meson',['c','cpp'],checks,['meson.build'])
    if 'mix.exs' in files:
        add('elixir',['elixir'],{'test':['mix','test'],'build':['mix','compile','--warnings-as-errors'],
                                'format':['mix','format','--check-formatted']},['mix.exs'])
    if 'build.zig' in files:
        add('zig',['zig'],{'test':['zig','build','test'], 'build':['zig','build']},['build.zig'])
    if 'project.godot' in files:
        add('godot',['gdscript'],{},['project.godot'], 'Godot test runner requires project-specific configuration')
    if (path/'ProjectSettings'/'ProjectVersion.txt').exists() and (path/'Assets').is_dir():
        add('unity',['csharp'],{},['ProjectSettings/ProjectVersion.txt'], 'Unity batchmode commands are project/installation specific')
    if any(n.endswith('.uproject') for n in files):
        add('unreal',['cpp'],{},sorted(n for n in files if n.endswith('.uproject')),'Configure engine build/test commands explicitly')
    if not detected:
        # Without build manifests, preserve visibility of languages, but invent no test command.
        langs=_source_languages(path,files)
        if langs:
            add('source',langs,{},[], 'No build manifest detected; add .harness/toolchains.toml for checks')
    return detected


def _discover_paths(root: Path, *, max_depth: int, max_directories: int) -> list[Path]:
    paths=[]
    root=root.resolve()
    for base, dirs, _ in os.walk(root, followlinks=False):
        p=Path(base)
        depth=len(p.relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (p/d).is_symlink()) if depth < max_depth else []
        paths.append(p)
        if len(paths) >= max_directories:break
    return paths


def _load_custom(root: Path) -> list[dict]:
    config=root/'.harness'/'toolchains.toml'
    if not config.exists():return []
    try: data=tomllib.loads(config.read_text(encoding='utf8'))
    except (OSError,ValueError) as exc:raise SEHError('INVALID_TOOLCHAINS',str(exc),2) from exc
    if not isinstance(data,dict) or set(data)-{'schema_version','projects'} or data.get('schema_version')!=1 or not isinstance(data.get('projects'),list):
        raise SEHError('INVALID_TOOLCHAINS','Expected schema_version=1 and [[projects]]',2)
    result=[]
    for item in data['projects']:
        if not isinstance(item,dict) or set(item)-{'path','ecosystem','languages','checks'}:
            raise SEHError('INVALID_TOOLCHAINS','Unexpected project fields',2)
        rel=item.get('path','.')
        if not isinstance(rel,str) or Path(rel).is_absolute() or '..' in Path(rel).parts or '\\' in rel:
            raise SEHError('INVALID_TOOLCHAINS','Project path must be inside repository',2)
        resolved=(root/rel).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_dir():
            raise SEHError('INVALID_TOOLCHAINS','Unknown or escaping project path',2)
        name=item.get('ecosystem','custom')
        if not isinstance(name,str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,50}',name):
            raise SEHError('INVALID_TOOLCHAINS','Invalid ecosystem id',2)
        checks=item.get('checks',{})
        if not isinstance(checks,dict) or set(checks)-set(CHECK_KINDS):
            raise SEHError('INVALID_TOOLCHAINS','Unexpected check type',2)
        for kind,argv in checks.items():
            if not isinstance(argv,list) or not 1<=len(argv)<=40 or any(not isinstance(a,str) or not a or '\x00' in a or '\n' in a for a in argv):
                raise SEHError('INVALID_TOOLCHAINS',f'{kind} must be an argv array',2)
        languages=item.get('languages',[])
        if not isinstance(languages,list) or any(not isinstance(s,str) or not re.fullmatch(r'[\w+-]{1,50}',s) for s in languages):
            raise SEHError('INVALID_TOOLCHAINS','Invalid languages list',2)
        result.append({'path':resolved.relative_to(root.resolve()).as_posix() or '.',
                       'ecosystem':name,'languages':languages,'checks':checks,'markers':['.harness/toolchains.toml'],
                       'notes':'Explicit project configuration; commands require approval to execute','source':'custom'})
    return result


def detect_projects(root: Path, *, max_depth: int=3, max_directories: int=1000) -> list[dict]:
    root=root.resolve()
    if not root.is_dir():raise SEHError('NOT_FOUND',str(root),3)
    found:dict[tuple[str,str],dict]={}
    for path in _discover_paths(root,max_depth=max_depth,max_directories=max_directories):
        rel=path.relative_to(root).as_posix() or '.'
        for item in _infer(path):
            found[(rel,item['ecosystem'])]={'path':rel,'source':'auto',**item}
    for item in _load_custom(root):found[(item['path'],item['ecosystem'])]=item
    return sorted(found.values(),key=lambda p:(p['path']!='.',p['path'],p['ecosystem']))


def resolve_check(root:Path, kind:str, *, project:str|None=None, ecosystem:str|None=None)->dict:
    if kind not in CHECK_KINDS:raise SEHError('UNKNOWN_CHECK',kind,2)
    projects=detect_projects(root)
    eligible=[p for p in projects if kind in p['checks'] and (project is None or p['path']==project)
              and (ecosystem is None or p['ecosystem']==ecosystem)]
    if not eligible:raise SEHError('CHECK_NOT_FOUND',f'No {kind} for project={project}, ecosystem={ecosystem}',3)
    if len(eligible)!=1:
        raise SEHError('AMBIGUOUS_CHECK','Choose --project and --ecosystem: '+', '.join(f"{p['path']}:{p['ecosystem']}" for p in eligible),2)
    chosen=eligible[0]
    root=root.resolve()
    cwd=(root/chosen['path']).resolve()
    if not cwd.is_relative_to(root):raise SEHError('PATH_ESCAPE',chosen['path'])
    argv=chosen['checks'][kind]
    return {'project':chosen['path'],'ecosystem':chosen['ecosystem'],'languages':chosen['languages'],
            'kind':kind,'argv':argv,'cwd':str(cwd),'available':shutil.which(Path(argv[0]).name) is not None,
            'requires_approval':True,'source':chosen['source']}
