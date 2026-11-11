# Setup

[Docs index](README.md) · [日本語](../ja/setup.md)

## Requirements

- Python 3.11+ on macOS or Linux. ego itself has no Python dependencies.
- The AI CLIs you want to use, installed and signed in separately: `claude`, `codex`, `opencode`, `agy`. ego never installs them.
- Git, for worktree isolation and for updating a development checkout.

## Install

```bash
# Global CLI from the repository (either one)
uv tool install git+https://github.com/jumtra/ego
pipx install git+https://github.com/jumtra/ego

# From a published release wheel (replace the version with the latest release)
pipx install https://github.com/jumtra/ego/releases/download/v0.0.9/ego-0.0.9-py3-none-any.whl
pip install https://github.com/jumtra/ego/releases/download/v0.0.9/ego-0.0.9-py3-none-any.whl   # inside a virtualenv

ego --version
```

The package installs two equivalent commands: `ego` and `seh` (older name).

**Developing ego itself:** clone the repository, then `make install` (symlinks `bin/ego` into `~/.local/bin`; override with `PREFIX=` / `BINDIR=`) or `pip install -e .`. Make sure `~/.local/bin` is on your `PATH`.

## Update

```bash
ego update --check              # report only
ego update --yes                # update ego
ego update --yes --sync-all     # update, then re-render every registered project
ego update --yes --sync --root /path/to/project   # update, then re-render one project
```

`ego update` detects how ego was installed:

| Install type | What `ego update` does |
|---|---|
| Git checkout (`make install`, `pip install -e .`) | `git fetch` and a fast-forward-only merge of the upstream branch. Local commits or conflicting edits stop the update; nothing is merged, rebased, stashed or discarded |
| Wheel (virtualenv, pipx, `uv tool`) | Installs the latest GitHub Release wheel after SHA256 verification |

Both need `--yes` or an interactive confirmation. Offline: `ego update --wheel <file> --sha256 <digest> --yes`.

## Apply to a project

```bash
cd /path/to/project
ego apply
```

`ego apply` refuses to run against your home directory or `/`. Its result is JSON on stdout; wizard text goes to stderr.

### The wizard

In an interactive terminal, with none of the selection flags below given, a wizard asks for:

| Question | Choices | Default |
|---|---|---|
| Profile | `minimal` / `balanced` / `full` | `balanced` |
| Hosts (multiple) | `claude` / `codex` / `opencode` / `agy` | all four |
| Code intelligence | `codegraph` / `graphify` / `none` | `none` |
| Memory | `file` / `engram` | `file` |
| Default runtime | `native` / `managed` | `native` |
| Detect test commands | yes / no (only when none are configured) | no |
| Apply these changes? | yes / no, after the list of changed files | **no** |

Answer with a number or a name; hosts accept `1,2` or `all`. Enter keeps the current value, `q` cancels. On re-apply the existing `.ego/config.toml` supplies the defaults. Prompts are currently in Japanese.

The wizard does not ask for the policy mode, default models or session thresholds. Set those with flags or in `.ego/config.toml`.

### Flags

| Flag | Meaning |
|---|---|
| `--hosts <list>` | Comma-separated CLIs to generate files for: `claude,codex,opencode,agy` |
| `--profile <name>` | `minimal`, `balanced` or `full`. Today only `full` changes the output: it adds a Claude Code `PreToolUse` policy hook |
| `--policy <mode>` | `strict` (default) or `relaxed`. See [Configuration](configuration.md#policy-mode) |
| `--models <json>` | Default model per host, e.g. `'{"claude":"sonnet","codex":"auto"}'` |
| `--graph <provider>` | `none`, `codegraph` or `graphify` |
| `--memory <provider>` | `file` or `engram` |
| `--runtime <mode>` | `native` or `managed` (recorded default) |
| `--clear-suggest-tokens <N>` | Context size at which the session is checkpointed and cleared; `0` = off. See [Sessions](sessions.md) |
| `--detect-tests` | Detect candidate test commands and store them in the config (nothing is executed) |
| `--dry-run` | Print the plan, write nothing |
| `--check` | Like `--dry-run`, but exit `5` when there are pending changes or conflicts (CI) |
| `--interactive` | Always run the wizard, even with flags or redirected stdin |
| `--non-interactive` | Never prompt |
| `--root <path>` | Project to apply to (default: current directory) |

Passing `--hosts`, `--profile`, `--graph`, `--memory`, `--runtime` or `--detect-tests` skips the wizard and applies directly. `--policy`, `--models` and `--clear-suggest-tokens` alone do not, so add `--non-interactive` in scripts.

```bash
ego apply --hosts claude,codex --policy relaxed --models '{"claude":"sonnet"}' --profile balanced
ego apply --non-interactive        # re-apply with the saved config
ego apply --interactive --dry-run  # try different choices without writing
```

### Result statuses

| `status` | Meaning |
|---|---|
| `UP_TO_DATE` | Nothing to change |
| `CHANGES_PENDING` | `--dry-run` / `--check` found changes |
| `APPLIED` | Files written; `snapshot_id` identifies the backup |
| `BLOCKED` | A file ego would write was edited by you; nothing is written. See `conflicts` |
| `CANCELLED` | You declined or cancelled the wizard |

## What gets generated

| Path | Content | When |
|---|---|---|
| `.ego/config.toml` | Project settings | always |
| `.ego/ownership.json` | Hashes of the files ego owns, installed version | always |
| `.ego/spec/skills/{work,harness-init}/` | Canonical skills (source for the host copies) | always |
| `.ego/spec/prompts/tool-guide.md`, `.ego/knowledge/domain.md` | Tool guidance, project knowledge stub | always |
| `CLAUDE.md` | Managed instruction block | host `claude` |
| `.claude/skills/`, `.claude/agents/`, `.claude/rules/` | `/work` and `/harness-init` skills, 4 agents, 2 rules | host `claude` |
| `.claude/settings.json` | Hooks / status line | host `claude`, and only with profile `full`, `clear_suggest_tokens > 0` or `usage_switch_percent > 0` |
| `AGENTS.md` | Managed instruction block | host `codex`, `opencode` or `agy` |
| `.agents/skills/` | Skills | host `codex`, `opencode` or `agy` |
| `.codex/agents/*.toml` | 4 agents | host `codex` |
| `.opencode/agents/*.md` | 4 agents | host `opencode` |
| `.agents/agents/*.md` | 4 agents | host `agy` |

The four agents are `implementer`, `reviewer` (read-only), `fixer` and `evolver`.

In `CLAUDE.md` / `AGENTS.md`, ego only manages the block between its `SEH:BEGIN` / `SEH:END` markers; the rest of the file is yours. Every other generated file is tracked by hash: ego replaces a file only while it still matches what ego wrote, and reports a conflict (`BLOCKED`) otherwise.

Runtime state lives under `.ego/` too: `state/` (runs, loops, events), `memory/` (memory items, checkpoints in `handoffs/`), `backups/` (apply snapshots), `locks/`. Keep `.ego/state/`, `.ego/backups/`, `.ego/locks/` and `.ego/memory/handoffs/` out of Git.

## Change settings later

1. Edit `.ego/config.toml` ([Configuration](configuration.md)), or re-run `ego apply` with flags.
2. Re-generate the host files:

```bash
ego sync            # preview only
ego sync --apply    # write
```

`ego sync` is a dry run unless `--apply` is given.

## Status, rollback, uninstall

```bash
ego status                # UP_TO_DATE / CHANGES_PENDING / BLOCKED, installed vs. running version
ego rollback              # restore the snapshot of the last apply
ego rollback <snapshot>   # restore a specific snapshot from .ego/backups/
ego uninstall             # asks for confirmation; -y / --yes skips it
ego uninstall --cli       # remove the ego/seh symlinks from ~/.local/bin
```

`ego rollback` refuses to overwrite files edited after the apply.

> [!WARNING]
> `ego uninstall` removes the managed block from `CLAUDE.md` / `AGENTS.md` and deletes the **entire** `.ego/`, `.claude/`, `.agents/`, `.codex/` and `.opencode/` directories, including files in them that ego did not create. Back up your own skills, agents or settings in those directories first.

## Several projects

`ego apply` and `ego sync --apply` record the project path in a user-level registry: `~/.config/ego/projects.json` (or `$EGO_HOME/projects.json`).

```bash
ego projects list                 # each project: installed_version, version_differs, drift status
ego projects scan ~/Develop       # register projects that already have ego applied (--depth N, default 4)
ego projects add <path>...        # register explicitly
ego projects remove <path>...     # forget (files untouched)
ego sync --all                    # preview a re-render of every registered project
ego sync --all --apply            # re-render them, each from its own .ego/config.toml
```

A project with user-modified owned files is reported `BLOCKED` and left untouched; the others are still applied and the exit code is `5`.

## Check the installation

```bash
ego doctor            # host CLIs found, code intelligence, memory, sandbox, skill lint, modules
ego hosts inspect     # CLI versions and the flags ego detected
ego models list       # model catalog per host
```
