# Multi-language coding and project checks

The harness **runtime** is Python 3.11+, but the repository being developed can be in any language. No project source is converted to Python by default. Host agents should follow existing project conventions, lockfiles and framework tools. `seh project` discovers toolchain candidates without installing packages or invoking scripts.

## Built-in ecosystems

| Ecosystem | Manifest / signal | Default check candidates (only when applicable) |
|---|---|---|
| Python | `pyproject.toml`, `setup.py`, `requirements.txt` | `python3 -m pytest -q` when pytest evidence exists, Ruff when configured |
| Node (JS / TS) | `package.json`, `tsconfig.json` | Only declared package scripts: test, lint, typecheck, build, format:check; npm / pnpm / yarn / bun selected from `packageManager` or lockfile |
| Deno | `deno.json` / `deno.jsonc` | `deno test`, `deno lint` |
| Go | `go.mod` / `go.work` | `go test ./...`, `go vet ./...`, `go build ./...` |
| Rust | `Cargo.toml` | `cargo test`, `cargo check`, `cargo build`, `cargo clippy`, `cargo fmt --check` |
| .NET (C#, F#, VB) | `*.csproj` / `*.fsproj` / `*.vbproj` / `*.sln` | `dotnet test`, `dotnet build`, `dotnet format --verify-no-changes` |
| Java / Kotlin | `pom.xml`, `build.gradle`, `build.gradle.kts` | Maven or installed Gradle test/build; does not automatically run `gradlew` |
| Swift | `Package.swift` | `swift test`, `swift build` |
| Ruby | `Gemfile`, `*.gemspec` | RSpec when tests configured, otherwise Rake test when Rakefile exists |
| PHP | `composer.json` | Declared Composer test script or present PHPUnit binary |
| Dart / Flutter | `pubspec.yaml` | `dart`/`flutter analyze`, test when `test/` exists |
| C / C++ | `CMakeLists.txt`, `meson.build` | Only build/test operations for already configured build directories |
| Elixir | `mix.exs` | `mix test`, `mix compile`, `mix format --check-formatted` |
| Zig | `build.zig` | `zig build test`, `zig build` |
| Godot, Unity, Unreal | Engine manifests and project markers | Detected; checks must be configured per engine/version/project |
| Other / source-only | Known source extensions or user configuration | No invented check; add explicit commands in `.harness/toolchains.toml` |

These are *command candidates*, not claims that the command's executable is installed or that dependencies are present. The harness does not install dependencies automatically. Running project scripts is inherently capable of executing arbitrary project code; use trusted repositories only. The `seh work` copy-based runner omits generated/vendor directories such as `node_modules`, which means its tests may need an explicitly prepared isolated dependency environment. `seh project check --execute --approve` instead runs **in the selected local project directory** after explicit approval.

## Discover and run checks

```bash
# Read-only and fast. No model calls.
seh project detect --root /path/to/repo
seh project checks --root /path/to/repo

# Preview an exact test: no execution or writes by the harness.
seh project check --root /path/to/repo --kind test --project services/api --ecosystem go

# Run a project-defined test explicitly (project scripts may write files).
seh project check --root /path/to/repo --kind test --project services/api --ecosystem go --execute --approve
```

If multiple projects provide the requested check, you must select `--project` and optionally `--ecosystem`. Missing executables show `available: false` in previews. Attempts to run commands absent from `.harness/config.toml` `allow_commands` are refused. Built-in blocked operations include dependency installation, publication/deploy tasks, unsafe inline interpreters and shell wrappers. No auto-`npm install`, `go get`, `cargo publish` or network setup is performed.

## Declarative custom toolchains

To add a new language or override a check without writing Python adapter code, create `.harness/toolchains.toml`:

```toml
schema_version = 1

[[projects]]
path = "apps/editor"
ecosystem = "zig-custom"
languages = ["zig"]

[projects.checks]
test = ["zig", "test", "src/main.zig"]
build = ["zig", "build"]
```

`schema_version` must be 1; `path` must be inside the repository (no traversal or escaping symlinks); each command is an **argv array** (never a shell string). Supported command kinds are `test`, `lint`, `typecheck`, `build`, `format`. A custom record with the same `(path, ecosystem)` key overrides only that auto-detected record. Other projects remain discovered. The toolchain executable must be in the approved `allow_commands` list; add a custom executable deliberately after reviewing it.

## Refer to discovered toolchains inside a work DAG

Use a toolchain reference in place of hard-coded Python check commands:

```json
{
  "nodes": [{
    "id": "verify-api",
    "kind": "gate",
    "depends_on": [],
    "checks": [{"toolchain": "test", "project": "services/api", "ecosystem": "go"}]
  }]
}
```

The DAG resolves the reference inside its **working copy**, uses the subproject directory as the command's working directory, and applies the usual allowlist, timeout and evidence rules. It never silently selects a different project to get a passing result. The repository itself is not modified unless `seh work run --apply --approve` is used, but external build scripts may access resources outside that copy; no OS sandbox is claimed.

## Performance and compatibility

- Normal native execution does **not** invoke project discovery or Harness².
- `init` and `seh project` perform bounded, read-only discovery (depth 3 / 1,000 directories by default), skipping caches and dependency trees.
- No toolchain is imported or executed during discovery; only standard library TOML/JSON parsing is used.
- Code search fallback recognizes Python, JS/TS, C#, Java/Kotlin, Go, Rust, Swift, PHP, Ruby, Dart, C/C++, GDScript, Elixir, Zig, Lua and shell file extensions. It is **text search**, not AST-level semantic support for every language.
- End-to-end model coding quality depends on the installed Claude Code, Codex or OpenCode CLI; AGY remains configuration-only due to provider restrictions.
