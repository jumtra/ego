# self-evolve-harness — Standalone 1.4

**CLI First · SDK Optional · Fast by default · Evidence-gated improvement**

A standalone Python coding harness with built-in templates, a bounded workflow runner, deterministic evaluators, evidence-gated prompt/Skill changes, a local memory backend and an opt-in Harness²-inspired optimization engine. It does **not** require Claude Agent SDK or a cloud API to install, generate configuration, run offline fixtures, or run deterministic tasks.

> **Scope and compatibility:** This distribution is an independently implemented, self-contained version of the design discussed for `jumtra/self-evolve-harness`. It is **not a git checkout or byte-for-byte upgrade of that upstream repository**. Before migrating an existing workspace, inspect the generated diff and retain a Git backup. See [compatibility and limitations](docs/COMPATIBILITY.md).

## Requirements

- Python **3.11+**, macOS or Linux. Windows is not a tested target.
- For real agent work: install and authenticate the vendor CLI for Claude Code, OpenAI Codex or OpenCode. **AGY configuration generation is available, but third-party CLI wrapping is disabled** under known consumer-plan restrictions.
- For CodeGraph/Graphify: install the chosen external tool separately and explicitly opt into indexing.
- For PR operations: GitHub CLI (`gh`) installed and authenticated.

## Install

**No installation or network required:**

```bash
unzip seh_full_standalone.zip
cd seh_full_standalone
python3 run_seh.py --version
python3 run_seh.py modules list
```

**Optional shell command installation** (requires setuptools; network access may be needed to fetch build dependencies):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
seh --version
# Optional: python -m pip install -e '.[dev]' && python -m pytest -q
```

Runtime dependencies: **Python standard library only**. For the included automated suite, install `pytest` separately (`pip install pytest`). If pip is offline or cannot download setuptools, use `python3 run_seh.py` or `./bin/seh` directly (no install).

## First use (dry-run first)

```bash
seh init --root /path/to/your/project --profile balanced --hosts claude,codex,opencode,agy --graph none
seh init --root /path/to/your/project --profile balanced --hosts claude,codex,opencode,agy --graph none --apply
seh doctor --root /path/to/your/project
seh sync --root /path/to/your/project      # diff only
seh sync --root /path/to/your/project --apply
```

Profile `balanced` installs two Skills (`harness-init`, `work`), four Subagent definitions per available host, short host instructions, no always-on Python Hook, and generated file ownership manifests. Full profile optionally adds a narrow Bash pre-use Hook. All unrelated project files are retained. The first CLI invocation never installs vendor CLIs, MCPs, CodeGraph, or Graphify.

## Fast / Verified / Optimize

| Mode | When | Runtime behavior |
|---|---|---|
| **Fast** (default) | Daily coding | Use vendor coding CLI directly and native `/work` Skill; **no extra optimization calls** and no always-on custom Hook in balanced profile. `seh work native` is just a hint. |
| **Verified** | Multi-file / higher-risk tasks | `seh work plan`, `seh work run`, independent tests, bounded repairs, checkpoints, opt-in apply. |
| **Optimize** (OFF by default) | Improve the harness itself | Explicit `seh optimize` tasks, base/candidate isolated copies, compare, independent authoring gate, human approval for promotion. |

For a fully offline end-to-end example:

```bash
seh init --root examples/demo_project --apply
seh work run --root examples/demo_project --task examples/demo_project/work-task.json --host mock
seh work run --root examples/demo_project --task examples/demo_project/work-task.json --host mock --apply --approve
seh eval run --root examples/demo_project --suite examples/demo_project/eval-suite.json --host mock
seh memory add 'Check auth expiry first' --root examples/demo_project --tags auth
seh code search 'expiry' --root examples/demo_project
seh bench --root examples/demo_project --trials 10
```

`mock` is a deterministic offline test adapter, **not a model**. Mock results cannot be used as real proof of prompting improvements or automatic promotion.

### Agent work with a real host

Create or obtain a JSON task spec (see [example](examples/demo_project/work-task.json)) and use:

```bash
seh work plan --root . --goal 'Add timeout validation' --host codex --scope 'src/**,tests/**' --execute --out task.json
seh work run --root . --task task.json --host codex --trust-cli
# Inspect run / staged diff before any user workspace apply:
seh work status <run-id> --root .
seh work run --root . --task task.json --host codex --trust-cli --apply --approve
```

**Security:** `--trust-cli` is a trust decision. A copy of the working tree or Git worktree is **not an OS sandbox**. Do not run untrusted scripts with secrets in the environment. Read [SECURITY.md](docs/SECURITY.md) before giving a model write permission. For fully isolated runs you must provide and verify an external OS sandbox; this distribution does not pretend that an unchecked container is safe.

### Prompt/Skill authoring (two public skills, not nine)

```bash
seh author lint --root .
seh author plan --root . --skill work --field skill.description --goal 'Reduce false triggering'
seh author candidate --root . --skill work --field skill.description --replacement-file new-description.txt --goal 'Reduce false triggering'
seh author gate --root . --candidate .harness/state/candidates/<candidate-id>.json --suite tests/fixtures/authoring.json --host codex --out gate.json
seh author promote --root . --candidate .harness/state/candidates/<candidate-id>.json --gate gate.json --approval-ref human:decision-123 --approve
seh author rollback <promotion-id> --root .
```

Authoring follows **Explore → Decide → Plan → Baseline → Implement → Verify & Repair → Compare → Approve**. Gates require independent testing, including training, regression, holdout and security cases. Claude/Codex/OpenCode differences are adapter-specific. A local approval reference is an audit reference, **not a cryptographic identity proof**.

### Optimization (off unless explicitly configured)

```bash
seh optimize status --root .
seh optimize doctor --root . --host codex
seh optimize configure --root . --mode shadow --profile lite --cost-policy manual_consent --apply
seh optimize plan --root . --fixture tests/fixtures/authoring.json --host codex --model <fixed-model-id> --skill work --field skill.description
seh optimize propose <experiment-id> --root . --replacement-file new-description.txt
seh optimize run <experiment-id> --root . --execute --spend-approval human:spending-123
seh optimize report <experiment-id> --root .
```

By default real-host optimization is blocked until external isolation is verified; a knowledgeable user can explicitly opt into trusted-local mode with `--trusted-local` and a spending approval. **This removes isolation guarantees**, even though runs are copied to separate working directories. No actual money cap can be enforced purely from a CLI's post-hoc usage report; `require_metered` fails closed.

**Full Harness²-style variant** is explicitly optional and research-oriented: configure `--profile full --max-candidates 3 --max-probe-runs 6`, then call `seh optimize full --fixture ... --host codex --rounds 1 --execute --spend-approval ...`; it produces invariant/domain/composed candidates and independent probes. It never auto-promotes.

## Components / commands

| Command | Function |
|---|---|
| `init`, `sync`, `doctor`, `migrate` | Detect, generate host files, keep ownership, snapshot and restore |
| `modules list/resolve/scaffold` | Declarative extension registry; never autoexecute module manifests |
| `work native/plan/run/status/resume/cancel` | Fast or managed bounded DAG and checkpoint |
| `eval run/compare` | Structured, fresh-workspace deterministic fixtures and regression comparison |
| `author lint/plan/candidate/gate/promote/rollback` | Prompt/Skill authoring, evidence gate, human-approved CAS promotion |
| `evolve observe/propose/lesson` | Learn from actual evidence and recommend revisions |
| `optimize ...` | Harness² Lite and optional Full Probe/Compose |
| `memory ...` | File memory or configured Engram-compatible MCP (requires actual server tool names) |
| `code ...` | CodeGraph / Graphify / local text fallback, with honest capability reporting |
| `tool list/call`, `prompt ...` | Validated deterministic tools, custom manifest tools, output contracts/versioned prompts |
| `pr ...` | Optional `gh` PR creation and feedback lookup; writes require approval |
| `hook pre-tool`, `events tail`, `bench` | Lightweight checks, append-only events, startup-overhead measurement |

For full capabilities, examples and explicit external limitations, see [COMPATIBILITY.md](docs/COMPATIBILITY.md), [ARCHITECTURE.md](docs/ARCHITECTURE.md), [SECURITY.md](docs/SECURITY.md), [TESTING.md](docs/TESTING.md), and [implementation matrix](docs/IMPLEMENTATION_MATRIX.md).

## Provenance

This package implements original standalone code based on concepts from the user's `self-evolve-harness` and Google's [Harness²](https://github.com/google-research/harness2) research, plus the supplied videos. The full upstream repository was not cloned in this environment; no upstream source files were copied. It uses the **MIT license** for its new code. Vendor tools and third-party providers have their own licenses and terms.


## v1.4.1: Task routing and verified self-update

Task-level / DAG node-level host and model routing (`single`, `fallback`, `compare`), version-sensitive local CLI flag probing, cached model discovery, and the `harness update` / `seh update` command are documented in [the host/task/model/update contract](docs/HOST_TASK_MODEL_UPDATES.md). The new version supports multiple project languages without requiring Python code in the target project. No external vendor CLI was authenticated for this build; offline tests do not imply vendor E2E compliance.
