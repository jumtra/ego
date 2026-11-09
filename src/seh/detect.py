"""Deterministic, non-mutating project profiler used by setup and troubleshooting."""
from __future__ import annotations
import json
from pathlib import Path
from .util import safe_path


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
        elif (root/"gradlew").exists():tests.append(["gradlew","test"])
        else:tests.append(["gradle","test"])
    if "ruby" in languages and (root/"Rakefile").exists():tests.append(["bundle","exec","rake","test"])
    if "php" in languages and (root/"vendor"/"bin"/"phpunit").exists():tests.append(["php","vendor/bin/phpunit"])
    if "cmake" in languages and (root/"build").exists():tests.append(["ctest","--test-dir","build","--output-on-failure"])
    ci=[]
    for provider,path in (("github",root/".github"/"workflows"),("gitlab",root/".gitlab-ci.yml"),("circleci",root/".circleci")):
        if path.exists():ci.append(provider)
    return {"root":str(root),"stack":languages,"test_commands":tests,"ci":ci,
            "git":(root/".git").exists(),"existing_host_configs":[name for name in ("CLAUDE.md","AGENTS.md",".claude", ".agents", ".opencode", ".codex") if (root/name).exists()]}
