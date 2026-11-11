# Command reference

[Docs index](README.md) · [日本語](../ja/commands.md)

`ego <command> --help` shows the flags of the installed version. `seh` is an alias of `ego`.

## Conventions

- **Project root:** every command acts on the current directory unless `--root <path>` is given. `--root` may appear after the command or after a nested action (`ego work run --root /tmp/app ...`).
- **Output:** JSON on stdout. Errors are JSON on stderr: `{"status": "ERROR", "reason_code": "...", "message": "..."}`.
- **Dry run by default:** commands that write project files (`init`, `sync`, `modules scaffold`, `optimize configure`) only preview unless `--apply` is given. `ego apply` asks for confirmation instead.
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
| `ego apply [flags]` | Configure a project and generate host files. Flags: `--hosts`, `--profile`, `--policy`, `--models`, `--graph`, `--memory`, `--runtime`, `--clear-suggest-tokens`, `--detect-tests`, `--dry-run`, `--check`, `--interactive`, `--non-interactive` |
| `ego status` | Drift status, installed vs. running version, last snapshot |
| `ego sync [--apply] [--all]` | Re-render from `.ego/config.toml`; `--all` covers every registered project |
| `ego rollback [snapshot]` | Restore the last (or a named) apply snapshot |
| `ego uninstall [-y] [--cli]` | Remove ego from the project, or the CLI symlinks |
| `ego init [--apply]` | Non-interactive preview/creation with project detection. Flags: `--profile`, `--hosts`, `--graph`, `--memory`, `--detect-tests`. Used by the `/harness-init` skill |
| `ego projects list` / `scan <dir> [--depth N]` / `add <path>...` / `remove <path>...` | The registry of applied projects |
| `ego update [--check] [--yes] [--sync] [--sync-all]` | Update ego itself. Offline: `--wheel <file> --sha256 <digest>` |
| `ego doctor` | Host CLIs, code intelligence, memory, sandbox, skill lint, modules |
| `ego hosts inspect` / `refresh [--host <cli>]` | Installed CLI versions and detected flags (cached) |
| `ego models list [--host <cli>] [--no-refresh]` / `refresh [--host <cli>]` | Model catalogs per host |
| `ego migrate plan` / `remove-legacy --hashes <file> --approve` / `restore-legacy <snapshot> --approve` / `rollback <snapshot>` | Find and remove files of older harness versions |

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
| `ego session checkpoint --task-id <id> --goal <text> [notes] [--status <s>] [--host <cli>]` | Save a checkpoint. Notes: `--completed`, `--next`, `--decision`, `--blocker`, `--evidence` |
| `ego session list [--all]` / `status <task_id>` | List or show checkpoints |
| `ego session resume [task_id] [--host <cli>] [--from <cli>]` | Clear instructions and resume prompt; `--from` continues another CLI's checkpoint without clearing |
| `ego session handoff [task_id] --to <cli> [--from <cli>] [--launch]` | Start command for continuing in another CLI |

## Memory, code and context

| Command | Purpose |
|---|---|
| `ego memory add <text> [--tags <t>]... [--evidence <ref>]...` | Store a fact |
| `ego memory search <query>` / `list` / `delete <id>` | Recall or manage facts (`list` and `delete`: file provider only) |
| `ego code doctor` | Provider availability and capabilities |
| `ego code search <query>` / `symbol <name>` | Search; falls back to built-in text search |
| `ego code impact <name>` / `affected <files>...` | Impact and affected tests (needs a provider) |
| `ego code index [--execute]` | Show, or with `--execute` run, the provider's indexing command |
| `ego context audit` | Estimated always-on and on-demand context against budgets; exit 5 when exceeded |
| `ego events tail [-n N]` | Recent events (default 20) |

## Skill and prompt improvement

These change the generated skills and prompts, always behind evaluation and explicit approval. Normal development never starts them.

| Command | Purpose |
|---|---|
| `ego eval run --suite <file> [--host <h>] [--trials N] [--model <id>] [--split <name>] [--timeout S]` | Run an evaluation suite; exit 5 unless all cases pass |
| `ego eval compare <baseline> <candidate>` | Compare two results; exit 5 unless promotable |
| `ego author lint` | Validate skill files |
| `ego author plan --goal <text> [--skill work] [--field skill.description]` | Plan a skill edit |
| `ego author candidate --replacement-file <file> --goal <text>` | Record a candidate change |
| `ego author gate --candidate <file> --suite <file> [--host <h>] [--model <id>] [--out <file>]` | Evaluate a candidate against a suite |
| `ego author promote --candidate <file> --gate <file> --approval-ref <ref> --approve` | Adopt a gated candidate |
| `ego author rollback <promotion_id>` | Undo a promotion |
| `ego evolve observe <message> [--outcome FAIL] [--task-class <c>] [--evidence <ref>]...` | Record an observation |
| `ego evolve propose <observation_id> [--replacement-file <file>]` | Turn an observation into a candidate |
| `ego evolve lesson <summary> --evidence <ref>...` | Record a lesson with evidence |
| `ego optimize status` / `doctor` | State of the opt-in optimizer |
| `ego optimize configure [--mode ...] [--profile ...] [--cost-policy ...] [limits] [--apply]` / `disable [--apply]` | Change `.ego/optimization.toml` ([settings](configuration.md#optimization-settings)) |
| `ego optimize plan --fixture <file>` / `propose <exp>` / `run <exp>` / `report <exp>` / `promote <exp> --approval-ref <ref> --suite <file> --approve` / `full --fixture <file>` | Run an experiment. Model calls need `--execute` and, when unmetered, `--spend-approval`; without a verified sandbox they also need `--trusted-local` |

`--allow-mock-test` on `author gate` / `promote` permits the offline `mock` host for testing the pipeline; a mock result is not evidence about a real model.

## Extension and internals

| Command | Purpose |
|---|---|
| `ego modules list` / `resolve <a,b,...>` / `scaffold <name> [--apply]` | Built-in and project modules (`.ego/modules/<name>/module.toml`), dependency resolution |
| `ego tool list` / `call <name> '<json args>' [--approve-external]` | Deterministic tools, and tools declared by modules |
| `ego prompt register <name> <file> [--role worker]` / `show <name> [--version V]` / `extract <file> [--contract json\|reply]` | Versioned prompts and output-contract parsing |
| `ego pr create --title <t> --body <b> [--base main] --approve` / `reviews <number>` | Pull requests through the `gh` CLI |
| `ego hook <pre-tool\|stop\|session-start\|statusline\|context-probe>` | Entry points for generated host hooks; they read the hook payload on stdin. `--host claude|codex|opencode|agy` (default `claude`) selects the payload and output format. Not meant to be run by hand |
| `ego bench [--trials N]` | Measures ego's own CLI dispatch time |
| `ego --version` | Version |
