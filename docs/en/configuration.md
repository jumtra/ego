# Configuration

[Docs index](README.md) · [日本語](../ja/configuration.md)

Project settings live in **`.ego/config.toml`**, created by `ego apply`. It is a flat TOML file (no sections). After editing it, run `ego sync --apply` to re-generate the host files. Unknown keys and out-of-range values are rejected with `INVALID_CONFIG`.

## Example

```toml
schema_version = 1
profile = "balanced"
policy_mode = "strict"
hosts = ["claude", "codex", "opencode", "agy"]
code_intelligence = "none"
memory = "file"
runtime_mode = "native"
max_parallel = 2
max_attempts = 3
timeout_seconds = 3600
max_turns = 30
worker_context = "lean"
clear_suggest_tokens = 0
usage_switch_percent = 0
run_yolo = false
models = {claude = {model = "sonnet", effort = "medium"}, codex = "auto"}
isolation = "copy"
cleanup_worktree = true
tests = []
allow_commands = ["python", "python3", "pytest", "git", "ruff", "npm", "pnpm", "yarn", "bun", "node", "deno", "tsc", "cargo", "rustc", "go", "dotnet", "swift", "swiftc", "cmake", "ctest", "mvn", "gradle", "gradlew", "java", "javac", "ruby", "bundle", "php", "composer"]
```

## What you should decide

Most keys can stay at their defaults. These are the ones worth a decision:

| Decision | Key | Guidance |
|---|---|---|
| Which CLIs you use | `hosts` | Only listed CLIs get skills, agents and instructions |
| How much the AI may do unattended | `policy_mode` | `strict` for shared or important repositories; `relaxed` for personal projects where the AI may push and edit config on request |
| Which model each CLI uses in managed runs | `models` | Leave empty to use each CLI's own default |
| When to rotate long sessions | `clear_suggest_tokens` | e.g. `120000`; `0` leaves it off |
| Commands that checks may run | `allow_commands` | Add your project's test runner if it is not listed |

## All keys

### Generation

| Key | Default | Values | Effect |
|---|---|---|---|
| `schema_version` | `1` | `1` | Config format version |
| `profile` | `"balanced"` | `minimal`, `balanced`, `full` | `full` adds a Claude Code `PreToolUse` hook (`ego hook pre-tool`) that screens Bash commands. `minimal` and `balanced` currently generate the same files |
| `hosts` | all four | `claude`, `codex`, `opencode`, `agy` | CLIs to generate files for. This is not a routing choice: the host that runs a task is chosen per task |
| `policy_mode` | `"strict"` | `strict`, `relaxed` | See [Policy mode](#policy-mode) |
| `code_intelligence` | `"none"` | `none`, `codegraph`, `graphify` | Provider behind `ego code ...`. External tools are never installed by ego; without one, a built-in text search is used |
| `memory` | `"file"` | `file`, `engram` | Provider behind `ego memory ...`. See [Memory](#memory) |
| `runtime_mode` | `"native"` | `native`, `managed` | Recorded default working style. It does not change command behavior: `/work` in the CLI is native, `ego work run` is always managed |
| `tests` | `[]` | list of argv arrays | Test commands recorded by `--detect-tests`, e.g. `[["pytest", "-q"]]` |

### Managed runs (`ego work run`, `ego work loop`)

| Key | Default | Range | Effect |
|---|---|---|---|
| `models` | `{}` | host → model | Default model per host. See [Models](#models) |
| `max_parallel` | `2` | 1–8 | Independent nodes run at the same time |
| `max_attempts` | `3` | 1–5 | Upper bound on attempts (first try + repairs) per node |
| `timeout_seconds` | `3600` | 5–7200 | Upper bound per node; a whole run is limited to this × number of nodes |
| `max_turns` | `30` | 1–200 | Agent turns per worker invocation |
| `worker_context` | `"lean"` | `lean`, `inherit` | `lean` starts Claude workers without your MCP servers; `inherit` keeps them |
| `isolation` | `"copy"` | `copy`, `worktree` | Where a run works: a staging copy, or a Git worktree. `--worktree` overrides per run |
| `cleanup_worktree` | `true` | bool | Remove the worktree after the run. `--keep-worktree` overrides per run |
| `allow_commands` | common toolchains | list of command names | Executables that `checks` and `--done-check` may start. Shells and `python -c` are always refused |

### Sessions

| Key | Default | Range | Effect |
|---|---|---|---|
| `clear_suggest_tokens` | `0` (off) | `0` or 20000–2000000 | Context size at which a session is checkpointed and cleared. Used by the generated hooks of every CLI (see [Sessions](sessions.md)) and by `ego run` |
| `usage_switch_percent` | `0` (off) | `0` or 50–99 | Claude Code and Codex (the CLIs that report their usage limit): at this percentage of the 5-hour or 7-day usage limit, save a checkpoint and propose continuing in another CLI |
| `run_yolo` | `false` | bool | `ego run` starts the CLI with its skip-permissions switch. `--yolo` / `--no-yolo` override per run |

Details of all three: [Sessions](sessions.md).

## Policy mode

| | `strict` | `relaxed` |
|---|---|---|
| Instruction given to the AI | Remote-mutating Git and sensitive config edits need manual user action | User-requested `git push`, PR creation and config edits may be done autonomously |
| `git push` / `reset` / `clean` / `rebase` in check commands | Refused | Allowed |
| `PreToolUse` hook (profile `full`) | Blocks destructive shell commands and edits of `.ego/ownership.json`, `.ego/locks/`, `.git/` | Blocks destructive shell commands only |

Destructive patterns (`rm -rf /`, `curl | sh`, `mkfs`, ...) are blocked in both modes. The policy is advisory: it is not an OS sandbox. See [Security](security.md).

## Models

```toml
models = {claude = "sonnet", codex = "auto", opencode = "anthropic/claude-sonnet-4-5", agy = {model = "auto", effort = "high"}}
```

- A value is a model ID string, or a table `{model = "...", effort = "..."}`.
- `"auto"` means "the CLI's own current default"; ego then passes no model flag.
- `effort`: `auto`, `low`, `medium`, `high`, `xhigh`, `max`.
- Use the IDs your CLI accepts; `ego models list --host <cli>` shows the catalog ego could read.

These are defaults for managed runs and the planner. A Task JSON or `--model` / `--models` flag overrides them; see [Workflows](workflows.md#choosing-host-and-model). They do not change the model of your interactive CLI session.

## Memory

| Provider | Storage | Notes |
|---|---|---|
| `file` | `.ego/memory/items.json` in the project | Local only. Supports `add`, `search`, `list`, `delete` |
| `engram` | An external MCP server you run | Supports `add` and `search`. Requires the environment variables below; there is no fallback to file memory |

Checkpoints are separate from memory items and always stored in `.ego/memory/handoffs/`.

## Optimization settings

`ego optimize` (opt-in prompt/skill experiments) reads `.ego/optimization.toml`, written by `ego optimize configure ... --apply`. It is off by default and is never loaded by normal commands.

| Key | Default | Range |
|---|---|---|
| `mode` | `"off"` | `off`, `analyze`, `shadow`, `assisted` |
| `profile` | `"lite"` | `lite`, `full` |
| `cost_policy` | `"manual_consent"` | `manual_consent`, `require_metered` |
| `max_candidates` | `1` | 1–8 |
| `max_rounds` | `1` | 1–5 |
| `max_probe_runs` | `2` | 0–40 |
| `max_wall_seconds` | `900` | 30–7200 |
| `max_candidate_bytes` | `8192` | 100–32768 |
| `require_human_approval` | `true` | must stay `true` |

## Environment variables

| Variable | Purpose |
|---|---|
| `EGO_HOME` | Directory of the project registry (`projects.json`). Default `~/.config/ego` |
| `SEH_CACHE_DIR` | Cache for detected host CLI capabilities. Default `~/.cache/seh` |
| `SEH_ENGRAM_MCP_COMMAND` | Command line that starts the Engram MCP server (required for `memory = "engram"`) |
| `SEH_ENGRAM_WRITE_TOOL` | MCP tool name used by `ego memory add` |
| `SEH_ENGRAM_SEARCH_TOOL` | MCP tool name used by `ego memory search` |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_DATA_HOME` | Respected when `ego run` reads each CLI's session records |

`EGO_RUN` is set by `ego run` for the CLI it starts; do not set it yourself.
