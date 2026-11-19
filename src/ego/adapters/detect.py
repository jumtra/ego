"""Deterministic, non-mutating project profiler used by setup and troubleshooting."""
from __future__ import annotations
import json
import re
import tomllib
from pathlib import Path

AGENT_DIRS = (
    (".claude/agents", "*.md"),
    (".agents/agents", "*.md"),
    (".opencode/agents", "*.md"),
    (".codex/agents", "*.toml"),
)


def _agent_description(path: Path) -> str:
    """Description from YAML-style frontmatter (.md) or a top-level key (.toml); empty when absent."""
    try:
        text = path.read_text(encoding="utf8")
    except (OSError, UnicodeDecodeError):
        return ""
    if path.suffix == ".toml":
        try:
            value = tomllib.loads(text).get("description", "")
        except (tomllib.TOMLDecodeError, ValueError):
            return ""
        return value.strip() if isinstance(value, str) else ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^description\s*:\s*(.*)$", line)
        if match:
            return match.group(1).strip().strip("\"'")
    return ""


def discover_agents(root: Path) -> list[dict[str, str]]:
    """Project custom agents from host agent directories, sorted and deduplicated by file stem (first in list wins)."""
    root = root.resolve()
    found: dict[str, dict[str, str]] = {}
    candidates = []
    for rel_dir, pattern in AGENT_DIRS:
        base = root / rel_dir
        if base.is_dir():
            candidates.extend(sorted(p for p in base.glob(pattern) if p.is_file()))
    for path in candidates:
        name = path.stem
        if name in found:
            continue
        found[name] = {"name": name, "path": str(path.relative_to(root)), "description": _agent_description(path)}
    return [found[name] for name in sorted(found)]


def detect(root:Path)->dict:
    root=root.resolve()
    patterns={
      "python":("pyproject.toml","requirements.txt","setup.py","Pipfile"),
      "node":("package.json","pnpm-lock.yaml","yarn.lock","bun.lockb","bun.lock"),
      "typescript":("tsconfig.json",),
      "rust":("Cargo.toml",),
      "go":("go.mod",),
      "dotnet":("*.csproj","*.sln","*.slnx"),
      "godot":("project.godot",),
      "unity":("Assets/","ProjectSettings/"),
      "unreal":("*.uproject",),
      "java":("pom.xml","build.gradle","build.gradle.kts"),
      "swift":("Package.swift",),
      "ruby":("Gemfile","Rakefile"),
      "php":("composer.json",),
      "cmake":("CMakeLists.txt",)
    }
    languages=[]
    for lang,globs in patterns.items():
        if any(any(root.glob(glob)) for glob in globs):languages.append(lang)
    tests=[]
    if "python" in languages:
        if (root/"tests").exists() or (root/"pytest.ini").exists():tests.append(["python3","-m","pytest","-q"])
    if "node" in languages:
        try: pkg=json.loads((root/"package.json").read_text(encoding="utf8"))
        except (OSError,ValueError):pkg={}
        script=pkg.get("scripts",{}).get("test", "") if isinstance(pkg,dict) else ""
        if script and script not in ("echo \"Error: no test specified\" && exit 1",):tests.append(["npm","test","--","--runInBand"] if "jest" in script else ["npm","test"])
    if "rust" in languages:tests.append(["cargo","test"])
    if "go" in languages:tests.append(["go","test","./..."])
    if "dotnet" in languages:tests.append(["dotnet","test"])
    if "swift" in languages:tests.append(["swift","test"])
    if "java" in languages:
        if (root/"pom.xml").exists():tests.append(["mvn","test"])
        elif (root/"gradlew").exists():tests.append(["./gradlew","test"])
        else:tests.append(["gradle","test"])
    if "ruby" in languages and (root/"Rakefile").exists():tests.append(["bundle","exec","rake","test"])
    if "php" in languages and (root/"vendor"/"bin"/"phpunit").exists():tests.append(["php","vendor/bin/phpunit"])
    if "cmake" in languages and (root/"build").exists():tests.append(["ctest","--test-dir","build","--output-on-failure"])
    ci=[]
    for provider,path in (("github",root/".github"/"workflows"),("gitlab",root/".gitlab-ci.yml"),("circleci",root/".circleci")):
        if path.exists():ci.append(provider)
    return {"root":str(root),"stack":languages,"test_commands":tests,"ci":ci,
            "custom_agents":[a['name'] for a in discover_agents(root)],
            "git":(root/".git").exists(),"existing_host_configs":[name for name in ("CLAUDE.md","AGENTS.md",".claude", ".agents", ".opencode", ".codex") if (root/name).exists()]}
