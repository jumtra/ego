# Security model and bounded execution

The harness is not a general-purpose OS sandbox. A copied folder or worktree protects against accidental merges but does **not** prevent an executed process from reading `HOME`, other paths, environment variables or contacting the network. **This limitation is deliberately enforced by defaults and reflected by `ego doctor`.**

- `ego init` and `ego sync` are dry-run by default. User-authored files are never silently deleted. Ownership hashes, marker blocks and snapshots protect generated content. Legacy Skill removal requires matching SHA-256 hashes and explicit approval.
- Managed work does not mutate the original workspace without `--apply --approve` and checks write allowlists and stale base file hashes. Real agents can invoke arbitrary tools inside their own CLI process, so `--trust-cli` is required. Test commands run as argv arrays, not via `shell=True`; allowed command names alone are **not** an OS security boundary.
- Optimize is OFF by default. Unmetered sessions require explicit `--spend-approval human:<ref>`. Real model experiments refuse to run without a verified sandbox unless an operator explicitly sets `--trusted-local`, which **removes the sandbox guarantee**. An API's estimated cost after execution is not a hard cost cap: `require_metered` blocks without a cost-limiter integration.
- Skill/prompt candidate changes are limited to a harness-owned Skill description, a marker-bounded Skill body or the tool guidance file. Never auto-edit scripts, CI, policy, security rules, permission configurations or non-owned user files. Promote requires independent Eval evidence and approval; all promotions are reversible until external edits invalidate the base hash.
- Env settings, agent outputs and external text are untrusted. Tool subprocesses use argv not a shell, no implicit `curl | sh`, and a redactor removes typical credentials from persisted run results. Credentials embedded in arbitrary source files, diagrams or badly formatted transcripts may not be detected: handle sensitive projects with stronger controls.
- An `approval_ref` is a **local operator acknowledgement/audit identifier**, not proof of identity. Production multi-user approval would need an external authentication and policy service.
- The optional Engram MCP provider starts an explicit trusted external command from `SEH_ENGRAM_MCP_COMMAND`. Tool names are configured separately by the user. Its compatibility is not presumed until a real server handshake passes.
- AGY is run through its CLI like the other hosts when the `agy` binary is installed. Whether your AGY plan permits automated CLI use is the operator's responsibility to confirm. AGY print mode cannot answer a permission prompt, so write runs pass `--dangerously-skip-permissions` without requiring `--attest-external-sandbox`; read-only nodes and `--autonomy safe` use `--mode plan`. Edits still land in the run's staging copy or worktree, but commands AGY runs are not sandboxed by the harness.
- External CodeGraph/Graphify are never silently installed. `ego code index --execute` uses their documented commands and may write local indexes. External generated Skill/Hook files should not be layered on top of harness-owned files without inspecting conflicts.

### Deployment guidance

For important repositories, use an external sandbox or disposable VM with no production credentials, fixed network policies, read-only source snapshots and least-privilege tokens. Enable GitHub branch protection / required checks separately; local CLI code cannot enforce server-side protections. Do not check `.ego/state`, logs, index artifacts or secrets into Git.

## Vendor CLI drift and self-update hardening

- Flag compatibility is probed by executing only vendor `--version` / `--help` commands and cached by executable fingerprint and TTL. No downloaded help text is executed as a script, and missing required flags fail closed.
- External OS-level isolation is NOT inferred from temp directories or Git worktrees. Raw permission bypass is an explicitly attested risk, not the normal default.
- Updates pin the GitHub repository, HTTPS scheme, release wheel naming, stable version, GitHub API asset SHA256, local wheel metadata, and package identity. No arbitrary remote sources, shell pipelines or automatic dependency installation.
- `ego update` needs a verified published release; it does not claim end-to-end signature validation or guaranteed automatic rollback. Keep the previous wheel/venv available. `--sync` affects only owned generated files; existing project state remains the source of truth.
