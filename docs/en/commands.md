# CLI command index

Run `ego <command> --help` for the authoritative current flags. All commands support `--root` immediately after the command or a nested action, e.g. `ego work run --root /tmp/app ...`.

| Command group | Main actions | Failure conditions |
|---|---|---|
| `apply` | Interactive configuration (TTY), `--interactive`, `--non-interactive`, `--runtime`, `--dry-run`, `--check`, `--hosts`, `--profile` | cancelled, owned-file conflict, invalid root |
| `status` | installation version, planned drift | malformed config or conflict |
| `rollback` | latest apply snapshot or explicit ID | edited-after-apply conflict |
| `uninstall` | Remove ego from project (`--root`), or remove global CLI (`--cli`) | cancelled by prompt without `--yes` |
| `init` | detect/setup plan, `--apply` | ownership conflict, invalid config |
| `sync` | re-render plan, `--apply` | conflicting generated artifacts |
| `doctor` | check host tools, providers, config | missing optional components reported |
| `modules` | list, resolve, scaffold | conflicts/missing dependency |
| `migrate` | plan, remove-legacy, restore-legacy, rollback | hash mismatch, missing approval |
| `work` | native, plan, run, resume, status, cancel, loop, loop-status, loop-apply | invalid DAG, check failure, trust boundary, loop limit reached |
| `context` | audit | always-on context or a skill body over budget |
| `eval` | run, compare | failed cases, incompatible suites |
| `author` | lint, plan, candidate, gate, promote, rollback | invalid skill, missing evidence, approval |
| `evolve` | observe, propose, lesson | missing evidence/observation |
| `optimize` | status, doctor, configure, plan, propose, run, report, promote, full, disable | disabled, isolation, budget, negative comparison |
| `session` | checkpoint, list, status, resume, handoff | missing checkpoint, invalid task ID, target CLI not installed, `--launch` without a terminal |
| `memory` | add, search, list, delete | unsupported MCP method |
| `code` | doctor, search, symbol, index | missing provider, unsupported feature |
| `tool` | list, call | invalid inputs or command permission |
| `prompt` | register, show, extract | invalid contract |
| `hook` | pre-tool | blocked command/policy |
| `events` | tail | log not present |
| `pr` | create, reviews | `gh` absent, missing authorization |
| `bench` | repeated native dispatch | process execution failure |

Status and output fields are JSON. Nonzero statuses distinguish validation, missing dependency, safety/policy refusal and failed validation. Paths in examples are relative to where the command is run unless an explicit absolute path is used. In `optimize`, all active experiments require a separately configured mode; normal development never starts optimization implicitly.

## Task routing and host / model compatibility

```bash
ego hosts inspect
ego hosts refresh --host claude
ego models list --host opencode
ego models refresh
ego work run --task task.json --hosts claude,codex --strategy fallback --models '{"claude":"sonnet","codex":"auto"}' --trust-cli
ego work run --task task.json --autonomy bypass --attest-external-sandbox --trust-cli  # external OS isolation REQUIRED
ego update --check                 # git checkout: commits behind upstream; wheel install: latest release
ego update --yes                   # git checkout: fetch + fast-forward only; otherwise verified release wheel
ego update --yes --sync-all        # then re-render every registered project with its own saved config
ego update --yes --sync --root /your/project
ego projects list                  # each applied project: installed_version, version_differs, drift status
ego projects scan ~/Develop        # register projects applied before the registry existed
ego sync --all [--apply]           # preview / re-render all registered projects (no CLI update)
ego apply --clear-suggest-tokens 120000
```

`ego update` detects how ego is installed. From a git checkout (`make install`, `pip install -e .`) it runs `git fetch` and a fast-forward-only merge of the checkout's upstream branch: local commits or conflicting edits stop the update, and nothing is merged, rebased, stashed or discarded. Otherwise it installs the latest GitHub Release wheel after SHA256 verification (virtual environment, pipx or `uv tool` only; environments without pip are updated through `uv`). Both need `--yes` or an interactive confirmation.

`ego apply` and `ego sync --apply` record the project in a user-level registry (`~/.config/ego/projects.json`, or `$EGO_HOME/projects.json`; paths only). `ego projects list` and `ego status` show the version that rendered each project (`installed_version`) next to the running CLI (`harness_version`). `ego sync --all --apply` and `ego update --sync-all` re-render every registered project from its own `.ego/config.toml`, so each keeps its settings; a project with user-modified owned files is reported `BLOCKED` and left untouched (exit code 5), the others are still applied.

`ego` is the primary console command installed by `make install` or `pip install -e .`. For Task JSON examples, see [examples/multihost/task.json](../examples/multihost/task.json).

## Goal loop and context budget

```bash
# Plan -> execute -> evaluate -> replan until the done-checks pass or a limit is hit.
ego work loop --goal "Reject negative timeouts in the API" --done-check "pytest -q tests/api" \
  --host claude --scope 'src/**,tests/**' --max-iterations 3 --max-budget-usd 5 --execute --trust-cli
ego work loop-status <loop_id>
ego work loop-apply <loop_id> --approve      # apply a VERIFIED loop after review
ego work cancel <loop_id>                    # stops before the next iteration
ego context audit                            # exit 5 when a context budget is exceeded
```

`work run`, `work resume` and `work status` print a compact summary (status, changed files, clipped failures, usage including prompt-cache tokens). Pass `--verbose` for the full record, which is always kept at `record_path`.

The first iteration skips the planner (one worker repaired against the done-checks); the planner runs only after a failed iteration, or up front with `--plan-first`.

Loop end states: `VERIFIED` (goal holds in the staged copy, nothing applied), `COMPLETED` (applied), `STALLED` (two iterations with identical staged content and failure), `MAX_ITERATIONS`, `BUDGET_EXHAUSTED`, `TIMEOUT`, `CANCELLED`. Costs are the host CLI's client-side estimates; `cost_known: false` means a host reported none.

`.ego/config.toml`: `max_turns` (default 30) bounds each managed worker; `worker_context = "lean"` (default) starts Claude workers without the user's MCP servers, `"inherit"` restores them.

`usage_switch_percent` (default 0 = off, 50-99) covers a nearly spent Claude subscription limit: it generates a status line command (`ego hook statusline`) that records the `rate_limits` Claude Code reports there (5-hour and 7-day windows; nothing is estimated) and shows them, and the Stop hook then asks the session, once per session and window, for one short extra turn that saves a checkpoint and shows the `ego session handoff` launch command for each other installed CLI. Nothing is launched or cleared. A project `statusLine` overrides a user-level one.

Checkpoints are host-neutral, so a session saved in one CLI continues in another: `ego session checkpoint` records the saving CLI (detected from the environment the CLI gives its commands, or `--host <cli>`), `ego session list` shows open checkpoints from any CLI, `ego session resume --from <cli>` continues that CLI's latest checkpoint inside the CLI that is already open (ask it to "continue from codex"), and `ego session handoff [task_id] --to <claude|codex|opencode|agy>` prints the command that opens the target CLI on the resume prompt (`--launch` starts it from a real terminal). `resume` and `handoff` default to the latest open interactive checkpoint. No conversation transcript is carried over.

`clear_suggest_tokens` (default 0 = off; set it in `.ego/config.toml` or with `ego apply --clear-suggest-tokens N`) generates Claude Code hooks that enforce a context limit: after each turn `ego hook stop` measures the conversation size from the host transcript. On reaching the limit, and again each time the context has grown 25% since, a session without a current checkpoint gets one short extra turn (Stop `decision: "block"`) in which it must save `ego session checkpoint` and tell the user to `/clear`; a session that already saved one is only told to `/clear`. Every later turn above the limit repeats the `/clear` reminder to the user. After `/clear`, `ego hook session-start` injects the resume prompt of a checkpoint saved in the last 30 minutes or armed by `ego session resume`. The hooks are mechanical (no model decides whether they fire), never clear anything themselves, and never block the same turn twice.

`ego run <claude|codex|opencode|agy> [--clear-tokens N] [-- <cli args>]` does the whole rotation by itself, for every CLI. It starts the interactive CLI inside a pseudo-terminal and passes everything through unchanged; at `clear_suggest_tokens` (or `--clear-tokens`), while the CLI is idle, the screen is still and the input box holds no draft, it types a request to save `ego session checkpoint`, waits for a checkpoint saved after that request, types the CLI's clear command (`/clear`, or `/new` for OpenCode) and then the resume prompt. Context size is read from each CLI's own session record (Claude Code transcript, Codex rollout `token_count`, OpenCode database, AGY status line input via a session-only `--settings` status line); nothing is estimated, and an unreadable record means no rotation. Without a new checkpoint within 5 minutes nothing is cleared. Inside `ego run` the Claude Code Stop / SessionStart clear hooks stand down.

