# Command reference

[Docs index](README.md) · [日本語](../ja/commands.md)

`ego <command> --help` shows the flags of the installed version.

## Conventions

- **Project root:** every command acts on the current directory unless `--root <path>` is given. `--root` may appear after the command or after a nested action (`ego work run --root /tmp/app ...`).
- **Output:** JSON on stdout. Errors are JSON on stderr: `{"status": "ERROR", "reason_code": "...", "message": "..."}`.
- **Dry run by default:** commands that write project files (`init`, `sync`) only preview unless `--apply` is given. `ego apply` asks for confirmation instead.
- **Hosts:** `claude`, `codex`, `opencode`, `agy`, plus the offline `mock` host for runs and evaluations.

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 2 | Invalid input, task or configuration |
| 3 | Missing prerequisite: CLI not installed, record not found, isolation not attested |
| 4 | Refused by policy or missing approval |
| 5 | Failed checks, pending drift (`--check`), blocked projects, or an infrastructure error |

## Installation and projects

Details: [Setup](setup.md).

| Command | Purpose |
|---|---|
| `ego apply [flags]` | Configure a project and generate host files. Flags: `--hosts`, `--profile`, `--policy`, `--models`, `--graph`, `--knowledge` (`--memory` is an alias), `--runtime`, `--clear-suggest-tokens`, `--detect-tests`, `--install-alter-skills`, `--roles`, `--plan-schema`, `--session-schema`, `--dry-run`, `--check`, `--interactive`, `--non-interactive` |
| `ego status` | Drift status, installed vs. running version, last snapshot |
| `ego sync [--apply] [--all]` | Re-render from `.ego/config.toml`; `--all` covers every registered project |
| `ego rollback [snapshot]` | Restore the last (or a named) apply snapshot |
| `ego uninstall [-y] [--cli]` | Remove ego from the project, or the CLI symlinks |
| `ego init [--apply]` | Non-interactive preview/creation with project detection. Flags: `--profile`, `--hosts`, `--graph`, `--knowledge`, `--detect-tests`. Used by the `/harness-init` skill |
| `ego projects list` / `scan <dir> [--depth N]` / `add <path>...` / `remove <path>...` | The registry of applied projects |
| `ego update [--check] [--yes] [--sync] [--sync-all]` | Update ego itself; in a terminal, asks whether to apply pending project configs (this project's diff / all projects / none). Offline: `--wheel <file> --sha256 <digest>` |
| `ego doctor` | Host CLIs, code intelligence, memory, sandbox, skill lint |
| `ego hosts inspect` / `refresh [--host <cli>]` | Installed CLI versions and detected flags (cached) |
| `ego models list [--host <cli>] [--no-refresh]` / `refresh [--host <cli>]` | Model catalogs per host |

## Work

Details: [Workflows](workflows.md).

| Command | Purpose |
|---|---|
| `ego work plan --goal <text> [--host <h>] [--scope <globs>] [--out <file>] [--execute]` | Produce a Task JSON. A real host needs `--execute`; `mock` plans one node offline |
| `ego work run --task <file> [flags]` | Execute a Task JSON in isolation |
| `ego work resume <run_id> --task <file> [flags]` | Continue a run, or apply a verified one with `--apply --approve` |
| `ego work status <run_id> [--verbose]` | Show a run |
| `ego work cancel <run_id\|loop_id>` | Request cancellation |
| `ego work loop --goal <text> --done-check <cmd> [flags]` | Goal loop |
| `ego work loop-status <loop_id>` / `loop-apply <loop_id> --approve` | Inspect or apply a loop |
| `ego work native` | Reports that native mode means using `/work` in the CLI; makes no model call |

`run` / `resume` flags: `--host`, `--trust-cli`, `--apply`, `--approve`, `--hosts`, `--strategy`, `--model`, `--models`, `--autonomy`, `--attest-external-sandbox`, `--worktree`, `--keep-worktree`, `--verbose`.

`loop` flags: `--host`, `--scope`, `--max-iterations`, `--max-budget-usd`, `--max-wall-seconds`, `--execute`, `--plan-first`, `--trust-cli`, `--apply`, `--approve`, `--autonomy`, `--attest-external-sandbox`.

## Sessions

Details: [Sessions](sessions.md).

| Command | Purpose |
|---|---|
| `ego run <claude\|codex\|opencode\|agy> [--clear-tokens N] [--yolo\|--no-yolo] [-- <cli args>]` | Start a CLI that ego checkpoints, clears and resumes at the context threshold |
| `ego session checkpoint --task-id <id> --goal <text> [notes] [--status <s>] [--kind <k>] [--host <cli>]` | Save a checkpoint. Notes: `--completed`, `--next`, `--decision`, `--blocker`, `--evidence` |
| `ego session list [--all]` / `status <task_id>` | List or show checkpoints |
| `ego session resume [task_id] [--host <cli>] [--from <cli>]` | Clear instructions and resume prompt; `--from` continues another CLI's checkpoint without clearing |
| `ego session handoff [task_id] --to <cli> [--from <cli>] [--launch]` | Start command for continuing in another CLI |

## Memory and code

| Command | Purpose |
|---|---|
| `ego knowledge add <text> [--topic <slug>] [--summary <text>] [--keyword <w>]...` | `knowledge = "file"`: record verified knowledge. Reads the index first and merges into the matching topic file, creating one only when none matches |
| `ego knowledge index` / `search <words>` / `get <slug>...` | `knowledge = "file"`: read the index, find topics, print topic files. All `ego knowledge` commands are an error with `knowledge = "alter"` |
| `ego memory add <text> [--tags <t>]... [--evidence <ref>]...` | Store a process fact (always local, `.ego/memory/items.json`) |
| `ego memory search <query>` / `list` / `delete <id>` | Recall or manage process facts |
| `ego memory install-skills [--scope global\|project] [--clients <list>] [--no-agents] [--dry-run]` | Install external `alter-skills` from `github:jumtra/alter-skills` via `npx` |
| `ego code doctor` | Provider availability and capabilities |
| `ego code search <query>` / `symbol <name>` | Search; falls back to built-in text search |
| `ego code impact <name>` / `affected <files>...` | Impact and affected tests (needs a provider) |
| `ego code index [--execute]` | Show, or with `--execute` run, the provider's indexing command |

## Extension and internals

| Command | Purpose |
|---|---|
| `ego hook <pre-tool\|stop\|session-start\|statusline\|context-probe>` | Entry points for generated host hooks; they read the hook payload on stdin. `--host claude|codex|opencode|agy` (default `claude`) selects the payload and output format. Not meant to be run by hand |
| `ego --version` | Version |
