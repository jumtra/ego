# Configuration

[Docs index](README.md) · [日本語](../ja/configuration.md)

Project settings live in **`.ego/config.toml`**, created by `ego apply`. It is a flat TOML file (no sections). After editing it, run `ego sync --apply` to re-generate the host files. Unknown keys and out-of-range values are rejected with `INVALID_CONFIG`.

## Example

```toml
schema_version = 1
profile = "balanced"
policy_mode = "relaxed"
hosts = ["claude", "codex", "opencode", "agy"]
code_intelligence = "none"
knowledge = "alter"
runtime_mode = "native"
max_parallel = 2
max_attempts = 3
timeout_seconds = 3600
max_turns = 30
worker_context = "lean"
worker_segment_turns = 10
clear_suggest_tokens = 200000
usage_switch_percent = 99
run_yolo = true
models = {claude = {model = "sonnet", effort = "auto"}, codex = {model = "auto", effort = "medium"}, opencode = "auto", agy = {model = "auto", effort = "auto"}}
isolation = "auto"
cleanup_worktree = true
git_exclude = true
tests = []
allow_commands = ["python", "python3", "pytest", "git", "ruff", "npm", "pnpm", "yarn", "bun", "node", "deno", "tsc", "cargo", "rustc", "go", "dotnet", "swift", "swiftc", "cmake", "ctest", "mvn", "gradle", "gradlew", "java", "javac", "ruby", "bundle", "php", "composer"]
```

## What you should decide

Most keys can stay at their defaults. These are the ones worth a decision:

| Decision | Key | Guidance |
|---|---|---|
| Which CLIs you use | `hosts` | Only listed CLIs get skills, agents and instructions |
| How much the AI may do unattended | `policy_mode` | `strict` for shared or important repositories; `relaxed` for personal projects where the AI may push and edit config on request |
| Which model each CLI uses in managed runs | `models` | Defaults to `claude` (`sonnet`), `codex` (`auto`, `medium`), `opencode` (`auto`), `agy` (`auto`) |
| When to rotate long sessions | `clear_suggest_tokens` | default `200000`; `0` leaves it off |
| Commands that checks may run | `allow_commands` | Add your project's test runner if it is not listed |

## All keys

### Generation

| Key | Default | Values | Effect |
|---|---|---|---|
| `schema_version` | `1` | `1` | Config format version |
| `profile` | `"balanced"` | `minimal`, `balanced`, `full` | `full` adds a Claude Code `PreToolUse` hook (`ego hook pre-tool`) that screens Bash commands. `minimal` and `balanced` currently generate the same files |
| `hosts` | all four | `claude`, `codex`, `opencode`, `agy` | CLIs to generate files for. This is not a routing choice: the host that runs a task is chosen per task |
| `policy_mode` | `"relaxed"` | `strict`, `relaxed` | See [Policy mode](#policy-mode) |
| `code_intelligence` | `"none"` | `none`, `codegraph`, `graphify` | Provider behind `ego code ...`. External tools are never installed by ego; without one, a built-in text search is used |
| `knowledge` | `"alter"` | `file`, `alter` | Where project knowledge lives (previously `memory`). See [Knowledge and memory](#knowledge-and-memory) |
| `runtime_mode` | `"native"` | `native`, `managed` | Recorded default working style. It does not change command behavior: `/work` in the CLI is native, `ego work run` is always managed |
| `tests` | `[]` | list of argv arrays | Test commands recorded by `--detect-tests`, e.g. `[["pytest", "-q"]]` |

### Managed runs (`ego work run`)

| Key | Default | Range | Effect |
|---|---|---|---|
| `models` | `{claude = {model = "sonnet", effort = "auto"}, codex = {model = "auto", effort = "medium"}, opencode = "auto", agy = {model = "auto", effort = "auto"}}` | host → model | Default model per host. See [Models](#models) |
| `max_parallel` | `2` | 1–8 | Independent nodes run at the same time |
| `max_attempts` | `3` | 1–5 | Upper bound on attempts (first try + repairs) per node |
| `timeout_seconds` | `3600` | 5–7200 | Upper bound per node. There is no whole-run time limit |
| `max_rounds` | `2` | 0–5 | Reviewer rejections allowed per node before `on_exhausted` applies |
| `max_replans` | `1` | 0–5 | Plan revisions allowed per task |
| `max_run_tokens` | `0` | `0` or integer ≥ 1000 | Per-invocation (`run`/`resume`) billable token cap (`0` = unlimited, default; when set ≥ 1000, resets on resume; `cache_read` excluded) |
| `max_turns` | `30` | 1–200 | Agent turns per worker invocation |
| `worker_context` | `"lean"` | `lean`, `inherit` | `lean` starts Claude workers without your MCP servers; `inherit` keeps them |
| `worker_segment_turns` | `10` | 1–200 | Claude workers run this many turns per segment and measure context against `clear_suggest_tokens` between segments; Codex, OpenCode, and AGY monitor live stream/session context against `clear_suggest_tokens`. At the limit, the worker checkpoints (or falls back mechanically on non-Claude hosts) and continues in a fresh session |
| `isolation` | `"auto"` | `auto`, `copy`, `worktree` | Where a run works: a staging copy, or a Git worktree. `auto` uses a worktree only when two writer nodes (coder/tester/code/worker nodes with no dependency path between them) can run at the same time, and a staging copy otherwise. `worktree` always uses one, `copy` never does. `--worktree` overrides per run |
| `cleanup_worktree` | `true` | bool | Remove the worktree after the run. `--keep-worktree` overrides per run |
| `git_exclude` | `true` | bool | `apply`/`sync`/`update` list `.ego/` and the generated files in `.git/info/exclude` (local only, never committed). Files git already tracks stay tracked: untrack them with `git rm -r --cached`. Set `false` to leave git's ignore rules alone and commit `.ego/config.toml` and the spec yourself |
| `allow_commands` | common toolchains | list of command names | Executables that `checks` and `--done-check` may start. Shells and `python -c` are always refused |

### Sessions

| Key | Default | Range | Effect |
|---|---|---|---|
| `clear_suggest_tokens` | `200000` | `0` or 20000–2000000 | Context size at which a session is checkpointed and cleared. Used by the generated hooks of every CLI (see [Sessions](sessions.md)) and by `ego run` |
| `usage_switch_percent` | `99` | `0` or 50–99 | Claude Code, Codex and AGY (the CLIs that report their usage limit; OpenCode reports none): at this percentage of the 5-hour or 7-day usage limit, save a checkpoint and propose continuing in another CLI (`0` disables) |
| `run_yolo` | `true` | bool | `ego run` starts the CLI with its skip-permissions switch. `--yolo` / `--no-yolo` override per run |

Details: [Sessions](sessions.md).

## Fine-grained parameters (`.ego/defaults.toml`)

Most limits and defaults are read from the packaged `src/ego/defaults.toml`. To change them for one project, add `[tuning]` or any settings section (`[usage]`, `[lifecycle]`, `[loopguard]`, `[toolprune]`, `[mcp]`, `[analysis]`) to `.ego/defaults.toml`. No CLI flag overrides these. A `.ego/config.toml` without the new keys still works; the package defaults fill them in.

### `[tuning]`

| Key | Default | Used for |
|---|---|---|
| `max_feedback_chars` | `4000` | Characters of check output and reviewer findings fed back into a retry |
| `max_summary_chars` | `800` | Characters of summaries in events, checkpoints and the replan history |
| `max_record_chars` | `20000` | Characters of output kept per attempt in `record.json` |
| `max_context_hits` | `8` | Code-search hits given to the planner |
| `max_save_turns` | `3` | Turns allowed for the checkpoint-save call |
| `max_handoff_chars` | `1000` | Characters of handoff text passed between nodes |
| `max_role_candidates` | `8` | Cap on role routing candidates |
| `[tuning.plan_schema]` | see the packaged `defaults.toml` | Task and plan schema limits (12 keys, e.g. `max_goal_chars` 6000) |
| `[tuning.session_schema]` | `max_goal_chars` 800, `max_item_chars` 350, `max_notes` 8, `max_prompt_chars` 1600 | Checkpoint and resume-prompt limits |

### `[usage]`

| Key | Default | Effect |
|---|---|---|
| `track_cache_tiers` | `true` | Track 5m and 1h cache creation token tiers separately |
| `track_overhead_separately` | `true` | Split usage ledger entries between `agent` and `overhead` |
| `session_cost_warning_usd` | `5.0` | Session cost warning threshold in USD |

### `[lifecycle]`

| Key | Default | Effect |
|---|---|---|
| `enable_envfacts` | `true` | Enable EnvFacts extraction and lifecycle updates during post-session fold |
| `min_confirmations` | `2` | Distinct sessions with successful observations required to promote `candidate` to `active` |
| `contradiction_demote_threshold` | `2` | Contradictions (failures) required to demote `active` to `demoted` |
| `max_injected_facts` | `12` | Maximum number of `active` facts injected at the prompt tail |
| `max_injected_chars` | `2400` | Maximum total characters of `active` facts injected at the prompt tail |
| `alter_feedback` | `true` | Send `alter_knowledge_feedback` when a fact is `demoted` or `forgotten` (when `knowledge = "alter"`) |

### `[loopguard]`

LoopGuard ships with `dry_run = true` by default: it observes segments and emits `loopguard` progress events without injecting nudges or halting on `STAGNATION`. To enable active intervention, override it in `.ego/defaults.toml`:

```toml
[loopguard]
dry_run = false
```

| Key | Default | Effect |
|---|---|---|
| `enabled` | `true` | Enable LoopGuard stagnation detection |
| `dry_run` | `true` | When `true`, observe and emit events only without nudging or halting |
| `envelope_min_samples` | `5` | Minimum recorded samples before using the per-repo turn envelope |
| `envelope_percentile` | `95` | Percentile used to compute the learned turn envelope |
| `min_turns` | `20` | Minimum assistant turns before stagnation checks trigger (also the fallback when samples are insufficient) |
| `edit_churn` | `4` | Repeated edits to the same file threshold |
| `distinct_edit_files` | `3` | Distinct edited files scatter threshold |
| `error_outputs` | `6` | Error command output count threshold |
| `stagnation_ratio` | `0.2` | Failure signature repetition ratio (`1 - distinct / total`) threshold |
| `envelope_margin` | `1.25` | Multiplier on the learned turn envelope |
| `backstop_mult` | `2.5` | Backstop multiplier |
| `max_nudges_per_node` | `1` | Maximum redirection nudges per node before escalating |
| `escalate_to_replan` | `true` | Allow escalation to replan when `STAGNATION` occurs |

### `[toolprune]`

| Key | Default | Effect |
|---|---|---|
| `unused_days` | `14` | Days without use before an `mcp__` tool is considered unused |
| `min_sessions` | `5` | Minimum recent sessions required before proposing tools to prune |

### `[mcp]`

| Key | Default | Effect |
|---|---|---|
| `alter_worker_profile` | `"ego-worker"` | Profile name passed to the alter MCP server for worker invocations |

### `[analysis]`

| Key | Default | Effect |
|---|---|---|
| `min_user_chars` | `400` | Minimum user-character count before post-session analysis runs |

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

### Per-role routing (`roles`)

```toml
roles = {reviewer = {host = "agy", model = "gemini-2.5-pro", effort = "high"}, coder = {host = "claude", model = "sonnet"}}
```

- Keys are `coder`, `tester`, `reviewer`; each table takes optional `host`, `model`, `effort` (same vocabulary as `hosts` / `models`).
- Omitted fields fall back to `models` / `hosts`. An effort-only entry keeps the host and model and just sets effort.
- Precedence: node > Task `defaults.roles` > `roles` > Task `defaults` > `models`. Set with `ego apply --roles '{"reviewer":{"host":"agy"}}'`.

Per-host and multiple specs: a host name may be a key inside a role. `host` picks the active one (otherwise the run's default host is used).

```toml
roles = {coder = {host = "claude", claude = [{model = "haiku", effort = ["low", "middle"]}, {model = "sonnet"}], codex = {model = "gpt-5-codex", effort = "medium"}}}
```

- A spec is a `{model, effort}` table or a list of them. `effort` may be a list (`["low", "middle"]`), each entry becoming a candidate: the example above is haiku/low, haiku/medium, sonnet/auto. `middle` means `medium`; omitted `effort` / `auto` means the host default.
- With two or more candidates the node runs as `fallback`: the next candidate is tried on `UNAVAILABLE` / `TIMEOUT` / `RATE_LIMIT` (at most 8 candidates). A node that sets its own `model`/`models` ignores per-host specs.

## Knowledge and memory

Two different things are stored, and each has its own place.

| What | Where | Notes |
|---|---|---|
| **Project knowledge** (domain terms, glossary, decisions) | `knowledge = "file"`: `.ego/knowledge/`<br>`knowledge = "alter"`: the alter MCP server | Chosen by the `knowledge` key. Read and written through the hooks, `ego knowledge ...` (file) or the alter tools (alter) |
| **Process memory** (facts about the work itself) | `.ego/memory/items.json` | Always local, whatever `knowledge` says. `ego memory add`, `search`, `list`, `delete` |

Checkpoints are separate again and always stored in `.ego/memory/handoffs/`.

### `knowledge = "file"`

```
.ego/knowledge/
  index.md          one line per topic: slug, summary, keywords. Read this first
  topics/<slug>.md  one reading unit: the facts of one topic
```

- **Reading:** `CLAUDE.md` / `AGENTS.md` tell the agent to read `index.md` first and open only the topic files the task needs (`ego knowledge search <words>`, `ego knowledge get <slug>`). Managed nodes get the same instruction when an index exists.
- **Writing:** `ego knowledge add '<fact>' [--topic <slug>] [--keyword <word>]...` reads the index first. A fact that matches an existing topic (shared keyword, or enough shared words) is merged into that file; identical lines are skipped and a longer line replaces its shorter version. Only a fact with no matching topic creates a new file and index line.
- **Automatic recording:** `ego session checkpoint` writes the checkpoint's `decisions` into the store the same way (no model is called; the goal's words become keywords). Set `EGO_KNOWLEDGE_AUTO_RECORD=0` to turn it off.
- A single `.ego/knowledge/domain.md` from earlier versions is still listed in the index as the read-only topic `domain`.

### `knowledge = "alter"`

- **Rules:** `CLAUDE.md` / `AGENTS.md` and `.claude/rules/` plus the project's AI usage rules from alter (`alter_ai_usage`, injected at session start).
- **Reading:** `alter_task_context` for every prompt (UserPromptSubmit, AGY `PreInvocation`, OpenCode `chat.message`). Managed runs keep the alter MCP server available (`lean = False`).
- **Writing:** automatic. The Stop hook and `ego session checkpoint` send the redacted conversation and checkpoint facts to alter (`alter_record_conversation`, `alter_session_digest`) for reviewable confirmation. Set `EGO_ALTER_AUTO_REVIEW=0` to turn it off. `ego knowledge ...` is an error in this mode.
- **alter unreachable:** the SessionStart and UserPromptSubmit hooks report `ego ERROR: alter に接続できません (...)` to the user and the agent instead of staying silent. They never block the turn. `ego doctor` shows `knowledge_health`.

`memory` was the earlier name of the `knowledge` key; an old `.ego/config.toml` with `memory = "..."` is read as `knowledge` and rewritten on the next `ego apply`. `--memory` still works as an alias of `--knowledge`.

### Session analysis (`alter_analysis`)

Opt-in. Only with `knowledge = "alter"`. Off by default because it spends model budget.

When `alter_analysis = true`, a session that has been recorded to alter is analyzed after it ends. ego launches the analyzer host headless (`ego session analyze <sid>`, detached), which runs the `alter-session-analyzer` skill and attaches the analysis to the same recorded conversation id. Analysis runs after the session, so it never blocks session end, and any failure is ignored (fail open). Run it by hand with `ego session analyze <sid>`.

| Key | Default | Range | Effect |
|---|---|---|---|
| `alter_analysis` | `false` | bool | Enables the post-session analysis above |
| `alter_analyzer` | `{opencode = {model = "opencode/glm-5.3-flash-free"}}` | table of host → `{model = "..."}` | Analyzer model per host. Hosts not listed use the session's host, so the analyzer host is the session host unless overridden here |
| `alter_analysis_timeout_seconds` | `600` | integer > 0 | Wall-clock limit for one analysis, in seconds |

## Environment variables

| Variable | Purpose |
|---|---|
| `EGO_HOME` | Directory of the project registry (`projects.json`). Default `~/.config/ego` |
| `EGO_CACHE_DIR` | Cache for detected host CLI capabilities. Default `~/.cache/ego` |
| `EGO_ALTER_MCP_COMMAND` | Optional command line to start the Alter MCP server for the direct calls the hooks and `ego session checkpoint` make (default: the alter server the host already configures) |
| `EGO_ALTER_AUTO_REVIEW` | `0` stops the hooks and `ego session checkpoint` from sending conversations and checkpoint facts to alter (default on) |
| `EGO_KNOWLEDGE_AUTO_RECORD` | `0` stops `ego session checkpoint` from recording decisions into `.ego/knowledge` (default on) |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_DATA_HOME` | Respected when `ego run` reads each CLI's session records |

`EGO_RUN` is set by `ego run` for the CLI it starts; do not set it yourself.
