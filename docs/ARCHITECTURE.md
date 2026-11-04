# Architecture and runtime contracts

## Single source of truth

`.harness/config.toml` controls selected hosts, code-intelligence provider, memory provider, runtime limits and deterministic test commands. Generated vendor instructions/skills/agents are projections, not independent configuration sources. Canonical Skills and Tool guidance are under `.harness/spec/`; user-owned files must not be overwritten. Ownership hashes live in `.harness/ownership.json`. Optional optimization settings are intentionally separated at `.harness/optimization.toml` and **are never loaded by the normal work dispatch path**.

## Main operations

```text
                           .harness/config.toml
                                   |
                          init/sync/ownership
                                   |
                  +----------------+----------------+
                  |                |                |
             Claude Code         Codex       OpenCode / AGY
             Skill+Agents     Skill+Agents     Skill+Agents
                  |                |                |
                  +----------------+----------------+
                                   |
             Native host execution (Fast, zero H2 calls)
                                   |
           opt-in work plan/run -> work DAG scheduler
                 |                         |
        isolated copies                 deterministic checks
                 |                         |
             candidate edits ----- Verify + bounded Repair
                 |                         |
               staged result + optional user-approved apply

           (separate explicitly invoked optimization track)
                 optimize plan/propose -> isolated paired eval
                        -> compare -> authoring gate
                        -> human approval -> CAS promote/sync
                                  -> evidence-based lesson
```

## Config/Module/Host API

- `Config` and `OptConfig` validate unknown keys rather than silently accepting them.
- `registry.discover()/resolve()` supports arbitrary declarative manifests and rejects missing dependencies, cycles and conflicts. No Python code is imported from a module manifest during discovery.
- `templates.generated_files()` generates two core Skills and four agents. Paths are intentionally distinct per vendor: Claude `.claude/`, Codex `.agents/skills` + `.codex/agents`, OpenCode `.agents/skills` + `.opencode/agents`, AGY `.agents/skills` + `.agents/agents`. OpenCode's multi-path discovery may see duplicate IDs: the generated content is the same and `author lint` checks for mismatched copies.
- `cli.main()` imports optimizer code **only** when a user explicitly invokes `seh optimize`.
- `runner.run_agent()` calls the installed host CLI directly with a subprocess argument array (no shell), captures machine-readable output when supported and terminates its process group on timeout. `mock` is deterministic and non-AI. Native AGY execution remains disabled.

## DAG execution

`TaskSpec` JSON contains `goal` and a non-empty `nodes` array. Each node has:

- `id`, `kind=agent|worker|splitter|code|gate`, `depends_on` as concrete node IDs.
- Agent-like nodes: `prompt`, `write_allowlist` (scope required), optional `checks` (argv arrays or `{"toolchain":"test","project":"services/api"}` objects).
- Code/gate nodes: `argv` or checks and an allowlist if they write. Toolchain references resolve to the specified subproject directory in the staging copy.
- Optional `max_attempts`, `timeout_seconds` within global bounds.

Cycles/missing IDs/unknown kinds are rejected. Ready nodes run in separate workspace copies, at most `max_parallel`. Each node is checked by allowed commands and a before/after file snapshot. Scope violations are rejected. Parallel edits of the same path are rejected rather than guessed. A successful wave is merged into a run-staging directory. The **real project is not changed by default**; `--apply --approve` is required and rechecks original file digests. State and evidence live under `.harness/state/runs/<run-id>/` (gitignored). `resume` relies on the saved task and staging data; `cancel` is cooperative at wave boundaries.

The `splitter` node type is a typed planning/analysis node, not permission to inject new arbitrary DAG nodes during execution. `seh work plan` generates and validates a plan separately. This avoids treating an untrusted model's JSON as executable code without review.

## Eval and output contracts

A suite contains `{ "cases": [{ "id", "split", "prompt", "checks" }] }`, split in `training|regression|holdout|security`. Checker supports `contains`, `not_contains`, `regex`, `json` field matching, `files_exist`, `exit_code`. Each trial runs in a **fresh copy** and produces `PASS/FAIL/INFRA` plus limited metrics; no LLM judgment overwrites these verdicts. `eval.compare()` rejects mismatched cases and reports improvements/regressions. Output contract parser supports JSON or `<reply>..</reply>` extraction, but **cannot pretend to enforce API-level stop sequences via vendor CLIs**.

`author.plan` records the intended change, `candidate` stores an immutable, single-field change in the canonical Skill, `gate` re-evaluates cases, and `promote` checks candidate hashes, owned files, local approval reference, stored evidence hashes, coverage, and host, then syncs host projections with a rollback ledger. Tests can explicitly allow mock gates for offline testing; **mock gates are not production evidence**.

## Optimize (Harness² concepts, opt-in)

- `off`: no optimization import, subprocess, network or persistent write on normal developer actions.
- `analyze`: candidate analysis but no probes.
- `shadow`: generate and compare stable/candidate in copied workspaces, never mutate stable.
- `assisted`: successful (not merely plausible) candidate can be submitted through independent authoring gate and local human approval to a CAS-style promotion.
- `full`: 2 lenses (`invariant`, `domain-specific`) plus a composed candidate, each separately evaluated, bounded rounds. Fully opt-in, no automatic promotion.

The ordinary task Repair loop is **within** a work unit; the DAG connects **between** units; candidate optimization is a **separate operator-controlled experiment**. Evidence-backed lessons use the existing Memory rather than a second database.

## Performance decisions

- No SDK runtime dependency and no always-on optimization hook.
- Under balanced profile, no new Claude pre-tool subprocess hook; the optional full-profile Hook incurs runtime overhead.
- Short `CLAUDE.md` / `AGENTS.md` contracts; full instructions in on-demand Skills.
- No automatic CodeGraph/Graphify indexing at session start; external indexing is explicit.
- Benchmark `seh bench` measures lightweight CLI dispatch only. For the v1.3-native vs v1.4-fast p50<=5%, p95<=10% goals, run a representative benchmark using the user's actual host CLI and hardware: they cannot be established from an offline mock.

## Language-agnostic execution

`seh project` is read-only by default and detects toolchains across supported language ecosystems; checks execute only with explicit approval. Project-specific `toolchains.toml` supports new languages without adding Python code. Native Fast mode remains passive. See [LANGUAGE_SUPPORT.md](LANGUAGE_SUPPORT.md).
