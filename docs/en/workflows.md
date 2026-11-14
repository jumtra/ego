# Workflows

[Docs index](README.md) · [日本語](../ja/workflows.md)

There are three ways to get work done, from lightest to heaviest. Start with `/work`; the AI moves to the heavier ones itself when the task calls for it.

| Workflow | Use it for | Who runs it |
|---|---|---|
| [`/work`](#work) | Everyday changes | You, in your AI CLI |
| [Managed run](#managed-runs) | Large, cross-component, risky or multi-host changes | `ego work run` (usually started by `/work`) |
| [Goal loop](#goal-loop) | A goal that a command can prove, likely to need several attempts | `ego work loop` |

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
| Goal with a proving command | A goal loop | |

Behaviors to expect:

- Before editing, the AI states why the change is wanted and what will show it is done. It asks a question only when an open point would change what gets built and a wrong guess would be costly; otherwise it proceeds and lists its assumptions in the report.
- It reports **完了** (done) only after validation passed and the change is applied, with the tests actually run. Otherwise it reports "verified, not applied" or "not done".
- It uses `ego memory search` / `ego memory add` for reusable, verified project facts, and `ego session checkpoint` at boundaries ([Sessions](sessions.md)).

`/harness-init` is the second generated skill: it inspects or reconfigures the ego installation.

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

1. Copies the project to a staging directory under `.ego/state/runs/<run_id>/` (or a Git worktree with `--worktree` / `isolation = "worktree"`).
2. Runs nodes in dependency order, up to `max_parallel` independent nodes at once.
3. For each agent node: runs the CLI with the node prompt, checks every changed file against `write_allowlist`, runs the node's `checks`. On failure the node is repaired with the check output as feedback, up to `max_attempts`. A failed node stops only its dependents; independent nodes still finish.
4. Saves the staging files, `record.json` and a checkpoint under the run ID as each node finishes, also inside a parallel wave. A node that passed stays saved even if a sibling fails or the process dies.
5. With `--apply --approve`, copies the changed files to the project, after confirming none of them changed since the run started (`STALE_BASE` otherwise).

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
| `TIMEOUT`, `BUDGET_EXHAUSTED`, `CANCELLED` | A limit was reached or `ego work cancel <run_id>` was used | 5 |

**Continuing a failed run.** Correct only the failed nodes in the task file and run `ego work run --task <file> --from <run_id>`: only the corrected nodes and their dependents run. `ego work resume <run_id>` with the unchanged task reruns failed nodes with their last failure as feedback (for a judge REJECT, the reviewed coder gets the findings). An unfinished run keeps its worktree so it can be continued.

`ego work status <run_id>` shows a run again. The usage line (for example `plan 1.2k tokens · work 45.6k tokens (task A 9.0k) · total 46.8k tokens`) counts input + output + cache read + cache creation tokens. Costs are the host CLI's own estimates; `cost_known: false` means a host reported none.

### Checkpoints, interruption and locks

Each node is persisted the moment it finishes (`as_completed` in a parallel wave), not at the end of its wave. Its staged files, `record.json` and the checkpoint under the run ID (`ego session list` shows it) are written then, so a run whose process dies keeps every node that had already passed.

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
| `node_finished` | `node`, `status`, `attempts`, `changed`, and for a judge `verdict` and `findings` |
| `judge_retry` | `judge`, `reviews`, `retry` |
| `run_finished` / `run_error` | `final_status`, `applied`, `changed_files`, `message` / `code`, `message` |

The summary adds `nodes` (status, attempts and check results per node) and, for a PASSED unapplied run, `next_action`: the exact command that applies it.

**`/work-plan` runs through this.** After you approve a plan, the AI writes it as a Task JSON, starts `ego work run --run-id ...`, relays these events to you as they arrive (Monitor in Claude Code, `ego work status` elsewhere), and asks before applying.

**Context limit for workers.** A Claude worker runs in segments of `worker_segment_turns` turns. Between segments ego measures the session's real context size. Below `clear_suggest_tokens` the same session continues with `--resume`; at or above it, the session saves a checkpoint and the work continues in a fresh session from that checkpoint (`worker_segment` event with `action: rotate`). Other hosts and read-only nodes run once, as before.

### Task JSON

Complete example: [`examples/multihost/task.json`](../../examples/multihost/task.json).

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
| `defaults` | `host` / `hosts`, `strategy`, `model`, `models`, `fallback_on`, and `roles` (a per-role route, e.g. `{"judge": {"host": "codex"}}`; node beats role beats defaults) for nodes that do not set their own |
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
| `role` | agent | `coder`, `tester` or `judge`, enforced. A `tester` may only write under `tests/` (its `write_allowlist` must start with `tests/`). A `judge` is read-only and has no `write_allowlist` |
| `inputs`, `outputs` | agent | The I/O contract: existing files to read (up to 6) and the files or `path::function` to change. Each output must match `write_allowlist`. Dependent nodes see the prior node's `outputs` |
| `reviews`, `max_retries` | judge | `reviews` (required) is the `coder` node to send back on a rejection; `max_retries` is 0-2 (default 1) |
| `intent`, `done_when`, `non_goals`, `assumptions`, `provides` | agent | Why the stage exists, acceptance criteria, what not to do, facts relied on, what later stages depend on. Short strings (`intent`) or lists of up to 6 strings of up to 400 characters |
| `requirement`, `steps` | agent | 5W1H (`who` `what` `when` `where` `why` `how`, strings up to 400 characters) and 1-10 ordered steps the worker follows exactly as written |
| `host` / `hosts`, `strategy`, `model` / `models`, `fallback_on` | agent | Routing, see below |
| `instructions_file`, `context_files` | agent | Extra instructions; up to 24 reference files |
| `max_attempts`, `max_turns`, `timeout_seconds` | all | Per-node limits, capped by the config |

Commands are argv arrays, never shell strings, and must start with a name in `allow_commands`.

### Judge gate

A `judge` node must depend on at least one `gate` or `code` node, so it runs only after the deterministic checks pass. It ends with `VERDICT: APPROVE` or `VERDICT: REJECT`; each finding must be `- path:line: issue`, and a finding without that evidence is discarded (a rejection left with no evidence counts as approval). On a rejection with evidence, only the reviewed `coder` node and the nodes the judge waits on are rerun, with the findings as feedback, up to `max_retries` times. The rest of the plan is not replanned. After that the judge node fails.

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

## Goal loop

Use it when a command can prove the goal (tests pass, build succeeds). Without such a command, do not loop.

```bash
ego work loop --goal "Reject negative timeouts in the API" --done-check "pytest -q tests/api" \
  --host claude --scope 'src/**,tests/**' --max-iterations 3 --max-budget-usd 5 --execute --trust-cli
ego work loop-status <loop_id>
ego work loop-apply <loop_id> --approve      # apply a VERIFIED loop after review
ego work cancel <loop_id>                    # stops before the next iteration
```

| Flag | Meaning |
|---|---|
| `--goal` | What to achieve (required) |
| `--done-check` | Command that exits 0 when the goal holds (required, repeatable). A command line or a JSON argv array |
| `--host` | `mock` (default), `claude`, `codex` or `opencode` |
| `--scope` | Comma-separated glob patterns the loop may change (default `src/**,tests/**`) |
| `--max-iterations` | Default 3; 1–10 |
| `--max-budget-usd`, `--max-wall-seconds` | Optional limits |
| `--execute` | Allow planner model calls |
| `--plan-first` | Call the planner before the first iteration |
| `--task <file>` | Run an approved task JSON as the first iteration; `--goal` defaults to its goal. Not combined with `--plan-first` |
| `--trust-cli`, `--autonomy`, `--attest-external-sandbox`, `--apply --approve` | As for managed runs |

How it works:

- The first iteration skips the planner: one worker is run and repaired against the done-checks. The planner is called only after a failed iteration, or up front with `--plan-first`. With `--task`, the approved task replaces that first attempt, and a failure is replanned like any other iteration.
- Every iteration plans and executes in new CLI sessions. State lives in `.ego/state/loops/<loop_id>/ledger.json`, not in a conversation.
- Replanning receives a bounded record only: failed nodes, clipped check output and changed paths.
- Verified changes carry over to the next iteration; edits of failed nodes do not.
- Verified agent nodes are kept verbatim in the next task and are not executed again while their fingerprint (own definition, ancestors and shared task settings) is unchanged. A planned node with the same id replaces one and reruns it and its dependents. Gate and code nodes always run again as a cheap re-verification.

| End state | Meaning |
|---|---|
| `VERIFIED` | Goal holds in the staged copy; nothing applied yet |
| `COMPLETED` | Goal holds and the result was applied |
| `STALLED` | Two iterations ended with identical content and failure |
| `MAX_ITERATIONS`, `BUDGET_EXHAUSTED`, `TIMEOUT`, `CANCELLED` | A limit was reached |

For a small goal that fits in one interactive Claude Code session, the CLI's own `/goal` is enough.

## Supporting commands

| Command | Purpose |
|---|---|
| `ego memory add '<fact>' --evidence <ref>` / `ego memory search '<words>'` | Store and recall verified project facts |
| `ego code search <query>` / `symbol` / `impact` / `affected` | Code lookup through the configured provider, with a text-search fallback |
| `ego context audit` | Measure always-loaded and on-demand context against budgets (exit 5 when exceeded; token counts are estimates) |
| `ego events tail -n 20` | Recent harness events |
| `ego pr create --title ... --body ... --approve` / `ego pr reviews <n>` | Pull requests through `gh` |

Improving the skills and prompts themselves (`ego eval`, `ego author`, `ego evolve`, `ego optimize`) is covered in the [Command reference](commands.md#skill-and-prompt-improvement).
