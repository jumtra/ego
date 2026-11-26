# Workflows

[Docs index](README.md) · [日本語](../ja/workflows.md)

There are three ways to get work done, from lightest to heaviest. Start with `/work`; the AI moves to the heavier ones itself when the task calls for it.

| Workflow | Use it for | Who runs it |
|---|---|---|
| [`/work`](#work) | Everyday changes | You, in your AI CLI |
| [Managed run](#managed-runs) | Large, cross-component, risky or multi-host changes | `ego work run` (usually started by `/work`) |

## `/work`

```text
/work Add timeout input validation and run the related tests
```

`/work` is a skill generated into every selected CLI. It sizes the request and picks the smallest workflow:

| Request | What happens | Not created automatically |
|---|---|---|
| Small, low-risk | Read the relevant files → edit → focused tests → check the diff → report | Requirements, design or plan documents |
| Medium | The same, with a short inline checklist if useful | Full specification documents |
| Large, risky or multi-host | A managed run starting with `ego work plan` | Documents nobody asked for |

Behaviors to expect:

- Before editing, the AI states why the change is wanted and what will show it is done. It asks a question only when an open point would change what gets built and a wrong guess would be costly; otherwise it proceeds and lists its assumptions in the report.
- It reports **完了** (done) only after validation passed and the change is applied, with the tests actually run. Otherwise it reports "verified, not applied" or "not done".
- It reads project knowledge from where `knowledge` says (the `.ego/knowledge` index, or alter), uses `ego memory search` / `ego memory add` for process facts, and `ego session checkpoint` at boundaries ([Sessions](sessions.md)).

`/work-plan` and `/harness-init` are the other generated skills: `/work-plan` interviews you and gets a node-graph plan approved before editing, and `/harness-init` inspects or reconfigures the ego installation.

## Managed runs

A managed run executes a **Task JSON**: a graph of nodes. Agent nodes are done by an AI CLI in an isolated copy of the project; code and gate nodes run commands. Nothing reaches your working tree until the run has passed and you approve.

```bash
# 1. Plan (a model call needs --execute; --host mock produces a one-node offline plan)
ego work plan --goal "Reject negative timeouts in the API" --host claude --scope 'src/**,tests/**' --execute --out task.json

# 2. Run in isolation. Real CLIs require --trust-cli
ego work run --task task.json --host claude --trust-cli

# 3. Review, then apply the verified result
ego work resume <run_id> --task task.json --host claude --apply --approve --trust-cli
```

Or run and apply in one step with `ego work run ... --apply --approve`.

### What a run does

1. Copies the project to a staging directory under `.ego/state/runs/<run_id>/`. With `--worktree`, `isolation = "worktree"`, or the default `isolation = "auto"` when two writer nodes can run at the same time, the staging directory is a Git worktree, and each writer node works in its own worktree on a branch from the run's HEAD. Read-only nodes (reviewer, splitter, gate) never get a worktree. Under `auto`, serial writers only use the staging copy.
2. Starts every node whose dependencies have passed, up to `max_parallel` at once. A free slot is refilled as soon as any node finishes, so a node does not wait for the rest of its wave.
3. For each agent node: runs the CLI with the node prompt, checks every changed file against `write_allowlist`, runs the node's `checks`. On failure the node is repaired with the check output as feedback, up to `max_attempts`. A failed node stops only its dependents; independent nodes still finish.
4. Merges each passed node into the staging tree as it completes. With worktree isolation, the node's commit is 3-way merged; without it, the node's files are written directly, and a file that another node changed after this node started is a conflict. A conflict ends the run with `MERGE_CONFLICT`. Two nodes that can run in parallel and both list the same exact path in `write_allowlist` are rejected before the run starts (`PARALLEL_WRITE_OVERLAP`); make one depend on the other. Files git ignores (for example a `dist/` written by a check) are not counted as a node's edits unless its `write_allowlist` names them. `code` and `gate` nodes run once: rerunning the same command on the same input cannot change the result.
5. Saves the staging files, `record.json` and a checkpoint under the run ID as each node finishes, even while other nodes are still running. A node that passed stays saved even if a sibling fails or the process dies.
6. With `--apply --approve`, copies the changed files to the project, after confirming none of them changed since the run started (`STALE_BASE` otherwise).

**Staging retention.** Ignored files are copied as copy-on-write clones where the filesystem supports it, so they take almost no extra space. Staging directories not updated for 3 days, and the oldest ones beyond 3 GB in total, are removed automatically (a running run and the newest staging are never removed for size). `record.json` and progress stay. The staging of a run used with `--from` is removed as soon as the new run is created. A run whose staging was removed cannot be resumed, applied or used with `--from` (`STAGING_MISSING` / `SEED_MISSING`).

### Flags of `ego work run` / `ego work resume`

| Flag | Meaning |
|---|---|
| `--task <file>` | Task JSON (required) |
| `--host <name>` | Default host for nodes without one: `mock` (default, offline), `claude`, `codex`, `opencode`, `agy` |
| `--trust-cli` | Required to start a real AI CLI. It can run arbitrary tools inside its own process |
| `--apply --approve` | Apply the verified result to the project. `--apply` without `--approve` is refused |
| `--hosts <list>` / `--strategy <s>` | Task-wide default hosts and strategy (`single`, `fallback`, `compare`) |
| `--model <id>` / `--models <json>` | Task-wide default model, or host → model map |
| `--autonomy <mode>` | `auto` (default), `safe` (plan/read-only modes), `bypass` (skip permissions; requires `--attest-external-sandbox`) |
| `--worktree` / `--keep-worktree` | Use a Git worktree; keep it afterwards |
| `--max-run-tokens <N>` | Override `max_run_tokens` for this invocation (`0` = unlimited, or `>= 1000`) |
| `--run-id <id>` | (`run` only) Fixed run ID, so `ego work watch <id>` can follow the run |
| `--from <run_id>` | (`run` only) Continue a failed run with a corrected task: start from its verified changes and skip its verified agent nodes whose definition and upstream are unchanged |
| `--verbose` | Print the full run record instead of the summary |

### Results

Output is a compact JSON summary: `run_id`, `final_status`, `applied`, per-node status, changed files, clipped failures, usage and `checkpoint_path`. The full record is kept at `record_path`.

| `final_status` | Meaning | Exit code |
|---|---|---|
| `PASSED` | All nodes verified. `applied` tells whether the project was changed | 0 |
| `COMPARISON_READY` | A compare node produced candidates for you to review; nothing is applied | 0 |
| `FAILED` | A node still failed after its attempts | 5 |
| `BLOCKED_DEPENDENCY`, `MERGE_CONFLICT` | A dependency failed, or parallel nodes edited the same file incompatibly | 5 |
| `BUDGET_EXHAUSTED`, `CANCELLED` | The run token cap (`max_run_tokens`) was reached, or `ego work cancel <run_id>` was used | 5 |

**Continuing a failed run.** Correct only the failed nodes in the task file and run `ego work run --task <file> --from <run_id>`: only the corrected nodes and their dependents run. `ego work resume <run_id>` with the unchanged task reruns failed nodes with their last failure as feedback (for a reviewer REJECT, the reviewed coder gets the findings). An unfinished run keeps its worktree so it can be continued.

`ego work status <run_id>` shows a run again. The usage line (for example `plan 1.2k tokens · work 45.6k tokens (task A 9.0k) · total 46.8k tokens`) counts input + output + cache read + cache creation tokens. Costs are the host CLI's own estimates; `cost_known: false` means a host reported none.

### Checkpoints, interruption and locks

Each node is persisted the moment it finishes, not when the other nodes of its batch are done. Its staged files, `record.json` and the checkpoint under the run ID (`ego session list` shows it) are written then, so a run whose process dies keeps every node that had already passed.

- **Interrupted runs.** A run still marked `RUNNING` whose lock is free has lost its process. `ego work status <run_id>` reports `final_status: INTERRUPTED` with the `ego work resume` command as `next_action`, and `ego work watch` ends with a `run_stopped` event. Resume it with `ego work resume <run_id> --task <file> --host <host> --trust-cli`: nodes that PASSED are not run again, and nodes that were running run again.
- **Run lock.** A process running a managed run holds an exclusive, non-blocking lock on `.ego/locks/<run_id>.lock`. A second start or resume of the same run fails with `RUN_ACTIVE` and leaves the run untouched. `ego session resume --from`, `ego session handoff` and the context-clear instructions refuse a managed checkpoint whose lock is held, and without a task ID they skip it.

### Progress

While a run is going, ego appends one JSON line per event to `.ego/state/runs/<run_id>/progress.jsonl` (and echoes it to stderr). `ego work watch <run_id> [--after <seq>]` streams those lines until the run ends; `ego work status <run_id>` shows the last ten as `progress`.

| Event | Fields |
|---|---|
| `run_started` | `nodes`, `resume` |
| `nodes_carried` | `nodes`, `from_run` (agent nodes verified by the previous run, not executed again) |
| `node_started` | `node`, `kind`, `role` |
| `attempt_finished` | `node`, `attempt`, `host`, `model`, `status`, `checks` (argv + status), clipped `feedback`, `changed`, `tokens`, `segments`, `rotations` |
| `worker_segment` | `node`, `attempt`, `segment`, `tokens`, `action` (`continue` or `rotate`) |
| `node_finished` | `node`, `status`, `attempts`, `changed`, and for a reviewer `verdict` and `findings` |
| `review_round` | `reviewer`, `reviews`, `round`, `cause` (`reject` or `gate`). A stall emits `loop_stalled` |
| `run_finished` / `run_error` | `final_status`, `applied`, `changed_files`, `message` / `code`, `message` |

The summary adds `nodes` (status, attempts and check results per node) and, for a PASSED unapplied run, `next_action`: the exact command that applies it.

**`/work-plan` runs through this.** After you approve a plan, the AI writes it as a Task JSON, starts `ego work run --run-id ...`, relays these events to you as they arrive (Monitor in Claude Code, `ego work status` elsewhere), and asks before applying.

**Context limit for workers.** Writable workers across all hosts (`claude`, `codex`, `opencode`, `agy`) rotate automatically at `clear_suggest_tokens`. A Claude worker runs in segments of `worker_segment_turns` turns and measures context between segments; Codex, OpenCode, and AGY monitor live stream/session context during execution and interrupt at the limit. Below `clear_suggest_tokens` Claude continues with `--resume`; at or above it, the session saves a checkpoint and continues in a fresh session (`worker_segment` event with `action: rotate`). On non-Claude hosts, if no checkpoint was saved, ego generates a mechanical handoff from changed workspace files and the last output (`fallback: true`) and continues in a fresh session. Read-only nodes and `mock` run once, as before.

### Task JSON

```json
{
  "schema_version": 1,
  "goal": "Add a /health endpoint with tests",
  "instructions": "Prefer minimal changes.",
  "defaults": {"hosts": ["claude"], "strategy": "single", "model": "auto"},
  "nodes": [
    {
      "id": "implement",
      "kind": "agent",
      "intent": "The endpoint is what monitoring will call.",
      "prompt": "Add GET /health returning 200 and a test for it.",
      "done_when": ["tests/test_health.py passes"],
      "requirement": {"what": "GET /health returns 200", "where": "src/app.py, tests/test_health.py", "how": "Add a route and a pytest"},
      "steps": ["Add a GET /health route in src/app.py returning 200", "Add tests/test_health.py asserting the 200"],
      "write_allowlist": ["src/**", "tests/**"],
      "checks": [["pytest", "-q", "tests/test_health.py"]]
    },
    {"id": "gate", "kind": "gate", "depends_on": ["implement"], "argv": ["pytest", "-q"]}
  ]
}
```

Task-level fields:

| Field | Meaning |
|---|---|
| `goal`, `instructions` | Passed to every agent node as user instructions |
| `instructions_file` | Project-relative file with further shared instructions |
| `defaults` | `host` / `hosts`, `strategy`, `model`, `models`, `fallback_on`, and `roles` (a per-role route, e.g. `{"reviewer": {"host": "codex"}}`; node beats role beats defaults) for nodes that do not set their own |
| `nodes` | Non-empty list of nodes |

Node fields:

| Field | Applies to | Meaning |
|---|---|---|
| `id` | all | Unique ID (letters, digits, `-`, `_`) |
| `kind` | all | `agent` (default), `code` or `gate` |
| `depends_on` | all | IDs that must pass first |
| `prompt` | agent | What to achieve (required) |
| `write_allowlist` | agent, code | Glob patterns of files the node may change. Required for agent and code nodes |
| `checks` | all | Commands (argv arrays) that must exit 0 |
| `argv` | code, gate | The command to run (`argv` or `checks` is required) |
| `role` | agent | `coder`, `tester` or `reviewer`, enforced. A `tester` may only write under `tests/` (its `write_allowlist` must start with `tests/`). A `reviewer` is read-only and has no `write_allowlist` |
| `inputs`, `outputs` | agent | The I/O contract: existing files to read (up to 10) and the files or `path::function` to change. Each output must match `write_allowlist`. Dependent nodes see the prior node's `outputs` |
| `reviews`, `max_rounds`, `on_exhausted` | reviewer | `reviews` (required) is the `coder` node to send back; `max_rounds` is 0-5 (default: the config `max_rounds`, 2); `on_exhausted` is `fail` (default) or `replan` |
| `intent`, `done_when`, `non_goals`, `assumptions`, `provides` | agent | Why the stage exists, acceptance criteria, what not to do, facts relied on, what later stages depend on. A string (`intent`) or lists of up to 10 strings, each up to 800 characters. The limits come from `[tuning.plan_schema]` in `.ego/defaults.toml` ([Configuration](configuration.md)) |
| `requirement`, `steps` | agent | 5W1H (`who` `what` `when` `where` `why` `how`, strings up to 800 characters; fill `what` and `how`, and the others only when no other field already says it) and 1-20 ordered steps the worker follows exactly as written |
| `host` / `hosts`, `strategy`, `model` / `models`, `fallback_on` | agent | Routing, see below |
| `agent` | agent | Optional custom agent name (from `.claude/agents/*.md`, `.agents/agents/*.md`, `.opencode/agents/*.md`, or `.codex/agents/*.toml`); its definition is embedded into the node prompt |
| `mcp` | agent | List of 1–8 required MCP server names (e.g. `["alter"]`). Keeps host MCP servers available even when `worker_context = "lean"`, and adds domain/glossary retrieval instructions. With `knowledge = "alter"` they use only alter (`alter_task_context` / `alter_search` / `alter_get_note`); with file knowledge they read `.ego/knowledge/index.md` (`ego knowledge`). Automatically enabled for `knowledge = "alter"` |
| `instructions_file`, `context_files` | agent | Extra instructions; up to 24 reference files |
| `max_attempts`, `max_turns`, `timeout_seconds` | all | Per-node limits. A value above the config cap is rejected with `INVALID_TASK` (never clamped) |

Commands are argv arrays, never shell strings, and must start with a name in `allow_commands`.

### Reviewer and loops

A `reviewer` node must depend on at least one `gate` or `code` node, so it runs only after the deterministic checks pass. It ends with `VERDICT: APPROVE` or `VERDICT: REJECT`; each finding must be `- path:line: issue`, and a finding without that evidence is discarded (a rejection left with no evidence counts as approval).

A rejection with evidence, or a gate/code failure inside the reviewer's loop, reruns only the reviewed `coder` node and the nodes the reviewer waits on, with the findings as feedback. This repeats up to `max_rounds` times per node (default 2).

- `on_exhausted = "fail"` (default): when the rounds are used up, the reviewer node fails.
- `on_exhausted = "replan"`: when the rounds are used up, the plan is revised ([Replans](#replans-max_replans)).
- Stall: if a retry would leave the staging tree and the feedback unchanged, the loop stops even if rounds remain, emits `loop_stalled`, and the reviewer fails. No replan happens in this case.

### Choosing host and model

Precedence, highest first: node → task `defaults` → CLI flags (`--host`, `--model`, `--models`) → `models` in `.ego/config.toml` → the CLI's own default. `"auto"` always means the CLI's default.

| Strategy | Hosts | Behavior |
|---|---|---|
| `single` | exactly 1 | Default |
| `fallback` | 2+ | Try the next host when the previous one is `UNAVAILABLE`, `TIMEOUT` or `RATE_LIMIT` (configurable with `fallback_on`) |
| `compare` | 2+ | Run all hosts and keep each result for review. Terminal nodes only; a run containing one cannot be applied |

A node with several hosts must name its strategy, so that extra vendors are never started by accident. Code and gate nodes cannot select a host or model.

```bash
ego work run --task task.json --hosts claude,codex --strategy fallback --models '{"claude":"sonnet","codex":"auto"}' --trust-cli
```

## Replans (`max_replans`)

The task's `max_replans` is the number of times the whole plan may be revised (default 1, capped by the config `max_replans`). When a reviewer with `on_exhausted = "replan"` uses up its rounds, verified nodes are kept and the rest is replanned. A planned node with the same id as a verified one replaces it. `on_exhausted = "replan"` requires `max_replans >= 1`. A value above the config cap is rejected with `INVALID_TASK`.

## Token cap

`max_run_tokens` (default `0` = unlimited, so runs do not stop mid-way by default) caps the billable tokens of a single `run` or `resume` invocation only when set to `>= 1000` in config or via `--max-run-tokens`; `cache_read` tokens are not counted, and the budget resets on `resume`. When an explicit cap is reached, the run stops between steps and `final_status` is `BUDGET_EXHAUSTED`. There is no whole-run time limit.

## Post-session learning and LoopGuard

### LoopGuard (stagnation detection and staged intervention)

For `claude` workers in managed runs, LoopGuard folds trace events after each segment to detect unproductive loops (configured under `[loopguard]`):

1. **Dry-run observation (default `dry_run = true`):** Evaluates stagnation conditions (`min_turns`, edit churn, error outputs, failure signature repetition ratio, or exceeding the per-repo turn envelope) and emits `loopguard` progress events without interrupting execution.
2. **One nudge (when `dry_run = false`):** On the first stagnation detection, injects a single redirection nudge (`max_nudges_per_node = 1`) into the next segment mirroring the repeated failure signature and up to 3 tried commands.
3. **`STAGNATION` halt:** If stagnation persists after the nudge, the worker exits with `FAIL` (`reason: STAGNATION`) and records the `stagnation` summary in `record.json` and retry feedback.
4. **Replan / alter ego-plan report:** Downstream handling can trigger replanning (`max_replans`) or, for alter-backed plans, report the node failure and stagnation state via `alter ego-plan report`.

### EnvFacts lifecycle and `alter_knowledge_feedback`

Learned environment facts (such as working test/build commands and tools) follow an evidence-based lifecycle stored in `.ego/state/envfacts.json`:

1. **`candidate` → `active`:** Once observed working across `min_confirmations` distinct sessions (default `2`), a fact becomes `active` and is injected at the prompt tail up to `max_injected_facts` items (`max_injected_chars` characters).
2. **`active` → `demoted`:** When failures reach `contradiction_demote_threshold` (default `2`), the fact is demoted and no longer injected.
3. **`ego knowledge facts` / `forget <id>`:** Inspect learned facts with `ego knowledge facts`, or mark one `forgotten` with `ego knowledge forget <id>` so it is never injected again.
4. **`alter_knowledge_feedback`:** When `knowledge = "alter"` and `lifecycle.alter_feedback = true`, demotions and `ego knowledge forget <id>` send `alter_knowledge_feedback` to alter (transient failures queue in `.ego/state/alter-feedback-pending.jsonl` and flush on the next attempt).

### Async fold after session end

The session end hook never blocks on post-processing; it spawns `ego fold` in a detached background process (fail-open):

- Acquires a non-blocking lock (`.ego/locks/fold.lock`) to avoid concurrent folds.
- Aggregates transcript token usage into the usage ledger (`ego usage summary`).
- Extracts and updates EnvFacts, sending `alter_knowledge_feedback` when applicable.
- Logs any transcript or processing error to `.ego/state/fold-errors.jsonl` and exits cleanly without blocking session end.

## Supporting commands

| Command | Purpose |
|---|---|
| `ego knowledge add '<fact>' --keyword <w>` / `ego knowledge search '<words>'` | Record and find project knowledge (`knowledge = "file"`) |
| `ego memory add '<fact>' --evidence <ref>` / `ego memory search '<words>'` | Store and recall verified process facts |
| `ego code search <query>` / `symbol` / `impact` / `affected` | Code lookup through the configured provider, with a text-search fallback |
