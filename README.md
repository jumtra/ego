<div align="center">

# ego

**One request. Changes you can verify.**

Bring planning, task routing, tests, and memory to Claude Code, Codex, OpenCode, and Antigravity.

![Version](https://img.shields.io/badge/version-0.0.9-2563eb)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-475569)

[English](docs/en/README.md) · [日本語 (Japanese)](docs/ja/README.md)

</div>

## Overview

`ego` is a CLI-first harness that enables multiple AI coding agents (Claude Code, Codex, OpenCode, Antigravity) to plan, delegate tasks, test changes in isolated worktrees, and retain knowledge across sessions.

<picture>
  <source media="(max-width: 600px)" srcset="docs/en/assets/workflow-en-mobile.svg">
  <img src="docs/en/assets/workflow-en.svg" alt="Request → Plan → Split UI and API work using a task specification → Tests → Approve and apply" width="1120">
</picture>

| | Goal | Capabilities |
|---|---|---|
| ⚡ | Fix code in your usual AI CLI | Use `/work` to inspect, implement, and run tests. No lengthy documents needed for small fixes |
| 🧩 | Delegate across multiple AI tools | Assign host and model per task. Respect dependencies and run parallel units |
| ✅ | Verify before applying | Run tests and bounded repairs in working copies before approving changes |
| 💾 | Resume in next session | Store and recall insights via Memory. Resume from checkpoints |
| 🎛️ | Tailor to your workflow | Select hosts, default models, security policy, profile, and memory providers |

---

## Usage

### 1 · Installation

Requires Python **3.11+** on macOS / Linux:

```bash
# Recommended: global CLI, no clone needed (either one)
uv tool install git+https://github.com/jumtra/ego
pipx install git+https://github.com/jumtra/ego

# pip-style install from a published release wheel (no git needed; replace the version with the latest release)
pipx install https://github.com/jumtra/ego/releases/download/v0.0.9/ego-0.0.9-py3-none-any.whl
uv tool install https://github.com/jumtra/ego/releases/download/v0.0.9/ego-0.0.9-py3-none-any.whl
pip install https://github.com/jumtra/ego/releases/download/v0.0.9/ego-0.0.9-py3-none-any.whl  # inside a virtualenv

ego --version
ego update --yes   # later: installs the latest verified release wheel
```

Developing ego itself? Clone the repository and run `make install` (symlink) or `pip install -e .`. `ego update --yes` then fast-forwards that checkout instead, so the CLI follows whatever branch is checked out there.

Add `~/.local/bin` to your `PATH` to access `ego` from anywhere.

---

### 2 · Apply to a Project (`ego apply`)

Run inside the target project directory:

```bash
cd /path/to/project
ego apply
```

In an interactive terminal, a wizard will guide your choices. Review the diff and confirm with `y`.

#### Command Arguments & Options

You can also pass arguments directly without interactive prompts:

| Option | Description | Example |
|---|---|---|
| `--hosts <list>` | Comma-separated AI hosts to generate configs for | `--hosts claude,antigravity` |
| `--policy <mode>` | Security policy: `strict` (default) or `relaxed` (allows autonomous push/edits) | `--policy relaxed` |
| `--profile <name>` | Configuration profile: `minimal`, `balanced` (recommended), `full` | `--profile balanced` |
| `--models <json>` | Default model mapping per host (JSON format) | `--models '{"claude":"sonnet"}'` |
| `--graph <provider>` | Code intelligence: `none`, `codegraph`, `graphify` | `--graph none` |
| `--memory <provider>` | Memory backend: `file` (.agent-memory), `engram` | `--memory file` |
| `--runtime <mode>` | Runtime mode: `native` (direct) or `managed` (worktree copy) | `--runtime native` |
| `--dry-run` | Preview generated changes without writing to disk | `--dry-run` |
| `--check` | Exit with nonzero code if diff exists (for CI validation) | `--check` |
| `--root <path>` | Target project path (defaults to current working directory) | `--root /path/to/project` |

Example:
```bash
# Non-interactive CI or scripted setup
ego apply --hosts claude,antigravity --policy relaxed --profile balanced
```

---

### 3 · Configuring `.ego/config.toml`

When you run `ego apply`, configuration is stored in **`.ego/config.toml`** at the project root.

#### How to Modify Configuration

1. **Direct Edit**: Open `.ego/config.toml` in your editor and modify settings.
2. **Sync**: After editing, run `ego sync` to re-generate host-specific files:
   ```bash
   ego sync
   ```

#### Key Configuration Options

```toml
schema_version = 1
profile = "balanced"            # "minimal" / "balanced" (recommended) / "full"
clear_suggest_tokens = 0       # Claude Code / `ego run`: context tokens at which to checkpoint and clear (0 = off)
run_yolo = false               # `ego run`: start the CLI with its skip-permissions switch (`--yolo` / `--no-yolo` per run)

[hosts]
enabled = [                     # Target AI agents to generate skills & rules for
    "claude",                   # Claude Code (.claude/, CLAUDE.md)
    "antigravity",              # Antigravity (.agents/, AGENTS.md)
    "codex",                    # OpenAI Codex (.codex/)
    "opencode",                 # OpenCode (.opencode/)
]

[hosts.models]                  # Default models per host (optional)
claude = "sonnet"
codex = "auto"

[runtime]
mode = "native"                 # "native" (direct execution) / "managed" (worktree testing)
policy_mode = "strict"          # "strict" (safety hooks on) / "relaxed" (autonomous git operations)

[runtime.security]
command_allowlist = ["git", "python", "pytest"] # Permitted command names

[memory]
provider = "file"               # "file" (.agent-memory) / "engram" (external MCP)

[code_intelligence]
provider = "none"               # "none" / "codegraph" / "graphify"
```

---

### 4 · Everyday Workflow: Ask `/work`

Launch your favorite AI CLI inside the project and describe your goal:

```text
/work Add timeout input validation and run related tests
```

**Use `/work` as your everyday entry point.** The AI automatically operates internal `ego` subcommands:
- Investigate → plan & split tasks → implement → test & verify → report diff
- Before editing, it confirms why the change is wanted and how completion is judged. It asks one question at a time only when an unresolved point would change what gets built and a wrong guess would be costly; otherwise it proceeds and lists its assumptions in the report
- Large or multi-host runs carry each stage's `intent`, `done_when`, `non_goals`, `assumptions` and `provides` into the worker prompts, so a stage knows why it exists and what later stages rely on

The final report includes a token usage line printed by `ego` (for example `plan 1.2k tokens · work 45.6k tokens (task A 9.0k) · total 46.8k tokens`). Tokens are input + output + cache read + cache creation, and the figure is reported as printed.

---

### 5 · Maintenance & Useful Commands

| Command | Description |
|---|---|
| `ego doctor` | Diagnose health of installed host CLIs and providers |
| `ego run <claude\|codex\|opencode\|agy>` | Run the CLI in a terminal ego owns; at `clear_suggest_tokens` it checkpoints, clears and resumes by itself. `--yolo` skips permission prompts |
| `ego status` | Inspect drift between `.ego/config.toml` and generated files |
| `ego projects list` | Show every applied project with the ego version that rendered it and its drift (`ego projects scan <dir>` registers existing ones) |
| `ego update --yes --sync-all` | Update ego (git checkout: fast-forward; otherwise release wheel), then re-render all registered projects with their own settings |
| `ego rollback` | Undo the last `ego apply` and restore previous snapshot |
| `ego uninstall` | Cleanly remove all ego files and managed blocks from project |
| `ego uninstall --cli` | Remove global symlinks from `~/.local/bin` (`make uninstall` also works) |

---

## Detailed Documentation

- **English**:
  - [Command Reference](docs/en/commands.md)
  - [Security Model](docs/en/security.md)
  - [Capability Matrix](docs/en/implementation_matrix.md)
- **日本語 (Japanese)**:
  - [日本語ドキュメントトップ](docs/ja/README.md)
  - [対話セットアップ詳細](docs/ja/apply_choices.md)
  - [コンテキスト引き継ぎガイド](docs/ja/context_handoff.md)
