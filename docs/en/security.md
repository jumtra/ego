# Security model

[Docs index](README.md) · [日本語](../ja/security.md)

**ego is not an OS sandbox.** A staging copy or Git worktree protects your working tree from unreviewed changes. It does not stop a process that an AI CLI starts from reading your home directory, environment variables or other paths, or from using the network. `ego doctor` reports the sandbox as unverified for this reason.

## What ego does protect

| Area | Protection |
|---|---|
| Your files during setup | `ego init` and `ego sync` preview unless `--apply`. Generated files are tracked by hash; a file you edited is never overwritten (`BLOCKED`). `CLAUDE.md` / `AGENTS.md` are changed only inside the managed block. Each apply creates a snapshot for `ego rollback` |
| Your working tree during managed runs | Work happens in a staging copy or worktree. Applying needs `--apply --approve`, every changed file must match a `write_allowlist`, and the target must be unchanged since the run started (`STALE_BASE`) |
| Check commands | Run as argv arrays without a shell, limited to `allow_commands`. Shells, `python -c`, and destructive patterns are refused. In `strict` policy, `git push`, `reset`, `clean` and `rebase` are refused too |
| Starting real AI CLIs | Requires `--trust-cli`. Skipping permissions requires `--autonomy bypass` together with `--attest-external-sandbox` |
| Prompts | Output of earlier nodes is never appended to later prompts verbatim; later nodes read the verified files. Reference files and checkpoints are labeled as untrusted data |
| Skill and prompt changes | Limited to harness-owned skill descriptions, marker-bounded skill bodies and the tool guide. Promotion needs evaluation evidence and `--approve`, and is reversible |
| Spending | `ego optimize` is off by default; unmetered experiments need `--spend-approval`. Planner model calls need `--execute` |
| Updates | `ego update` pins the GitHub repository, HTTPS, the wheel name, a stable version and the SHA256 from the GitHub API. No arbitrary URLs, shell pipelines or dependency installation. A git checkout is only fast-forwarded |
| Stored data | Checkpoints and memory stay in the project. No conversation transcripts are collected. A redactor removes typical credential formats from persisted run results |

## What it does not protect

- **Processes started by an AI CLI.** Allowed command names and policy hooks are advisory, not a security boundary.
- **`--yolo` and `bypass`.** `ego run --yolo`, `run_yolo = true` and `--autonomy bypass` remove the CLI's own permission prompts. Use them only where that is acceptable.
- **AGY write runs.** AGY's print mode cannot answer a permission prompt, so managed write runs pass `--dangerously-skip-permissions` even without `--attest-external-sandbox`; read-only nodes and `--autonomy safe` use plan mode. Edits still land in the staging copy, but commands AGY runs are not contained. Whether your AGY plan permits automated CLI use is yours to confirm.
- **Costs.** Reported costs are the CLI's own estimates after the fact, not a hard cap. `--max-budget-usd` stops a run or loop between steps once the reported cost exceeds it.
- **Secrets in content.** Redaction is best-effort; credentials inside source files, diagrams or unusual formats may not be detected.
- **Identity.** An `--approval-ref` is a local audit note, not proof of who approved.
- **Server-side rules.** Branch protection and required checks must be configured on GitHub.
- **`relaxed` policy.** It tells the AI it may push and edit configuration on request, and turns off most hook checks.

## External tools

- AI CLIs, CodeGraph, Graphify and Engram are never installed by ego. `ego code index --execute` runs the provider's documented indexing command and may write a local index.
- Engram is started from `SEH_ENGRAM_MCP_COMMAND`, a command you configure and trust. Its compatibility is not assumed until a real server responds.
- CLI flag support is detected by running only the vendor's `--version` / `--help`, cached per executable. A required flag that is missing fails closed.

## Recommendations

For important repositories, run managed work in an external sandbox or disposable VM with no production credentials, a fixed network policy and least-privilege tokens. Keep `policy_mode = "strict"`. Do not commit `.ego/state/`, `.ego/backups/`, `.ego/locks/`, `.ego/memory/handoffs/`, logs or secrets.

## What is verified

The test suite runs offline with the `mock` host. It covers configuration, installation and rollback, task routing, the run and loop engines, checkpoints, hooks, the `ego run` wrapper logic and the updater's verification. Behavior of the real vendor CLIs is reached through adapters that are checked against each CLI's reported flags, not exercised end to end in the tests. The Engram, CodeGraph, Graphify and `gh` integrations are adapters that depend on the external tool. Confirm these on your machine with `ego doctor` and a small trial run.
