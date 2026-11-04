# Host / Task / Model / CLI compatibility / updater: implementation contract

**Package:** `self-evolve-harness-standalone 1.4.1`. The code is a standalone implementation, not an in-place patch of the upstream GitHub checkout.

## 1. The host is never locked during harness initialization

`seh init --hosts claude,codex,opencode,agy` generates host configuration. It does NOT fix the choice of host or model for future tasks. Run-time routing is resolved at execution:

1. Node `host` / `hosts` and `model` / `models`
2. Task-level `defaults` (and explicit command-line default overrides)
3. The CLI `--host` default
4. Native host default model when model is absent or `auto`

A node may choose a different host and model than the rest of the graph. Nodes whose dependencies are finished can execute concurrently up to the project `max_parallel` limit. Comparing two hosts runs both in separate staging copies; it does not automatically merge or promote either result.

## 2. How instructions are passed

Use a TaskSpec JSON object containing `goal` (overall target), `instructions` or `instructions_file` (shared constraints), and `nodes[].prompt` (stage-specific instruction). Individual nodes additionally accept `instructions_file`, `context_files` (path references to untrusted input, not instructions), `role`, `write_allowlist`, `checks`, and `depends_on`.

- Claude Code: structured prompt assembled from the user instructions and passed via **stdin** to `claude -p`, parsed as JSON.
- Codex: the same prompt passed via **stdin** to `codex exec -`, parsed as JSONL.
- OpenCode: passed as a single argv element to `opencode run`, parsed as JSONL. No shell interpolation.
- AGY: native app configuration output only, managed execution blocked in this release.

The runner records the route, selected model, checks and elapsed time in a run record. `auto` means the vendor CLI's current native default and is **not** a hard-coded model ID. Explicit models are pinned to the task and are never rewritten by catalog updates.

Example:

```bash
seh work run --root /your/project --task /your/project/task.json --trust-cli
seh work run --root /your/project --task /your/project/task.json \
  --hosts claude,codex --strategy fallback \
  --models '{"claude":"sonnet","codex":"auto"}' --trust-cli
```

For multi-host orchestration, see `examples/multihost/task.json` (illustrative; set actual project paths and checks first). Node-specific routing wins over command-line *defaults*.

## 3. Strategies and speed

- `single` (default): one agent CLI for each agent node. No extra agent sessions.
- `fallback`: try the next host only for `UNAVAILABLE`, `TIMEOUT`, or `RATE_LIMIT`. Failed partial changes are discarded before retrying in a clean copy. Semantic code/test failure is **not** silently routed elsewhere.
- DAG parallelism: independent nodes with per-node `host`/`model` can run concurrently. Distinct write scopes are recommended; collision detection blocks unsafe merges.
- `compare`: run alternative hosts on the same task in separate copies, store reviewable results; **no automatic diff merge or apply**. Compare nodes must be terminal.

The native Fast path does not run Harness² or refresh model catalogs. A real managed agent invocation incurs the normal vendor CLI startup/runtime cost; no claim of a fully free wrapper.

## 4. Vendor CLI argument drift

`seh hosts inspect` reads *cached* local version/flag capabilities. `seh hosts refresh` re-runs the installed CLI's `--version` and `--help` (`exec --help` for Codex, `run --help` for OpenCode). On a changed executable fingerprint, the next managed execution automatically invalidates the cache and probes again. Cache TTL is 7 days. Probes have timeouts and do not make model requests.

Flags are selected only from **reviewed known variants** observed in local help. For example, Codex bypass uses `--yolo` if supported, otherwise the documented long form if supported. If a required flag is removed, SEH reports `CLI_INCOMPATIBLE` instead of guessing a new command. When permitted by strategy, that condition may trigger a fallback host.

Arbitrary future CLI syntax cannot be safely synthesized from `--help`; full semantic compatibility requires a reviewed harness adapter update (`harness update`). The CLI versions used in a task should be recorded alongside model selections for reproducibility.

## 5. Autonomy and dangerous bypass switches

`--autonomy auto` is the default high-productivity preset **without removing all safety boundaries**:

- Claude: `--permission-mode auto` if documented by that installed CLI; `acceptEdits` fallback if available; otherwise native permission defaults.
- Codex: `--sandbox workspace-write --ask-for-approval never` when supported (or read-only when requested).
- OpenCode: `--auto` if documented by the installed `run --help`; explicit denies still apply.

`--autonomy safe`: read-only/plan where available. `--autonomy bypass`: only for **externally sandboxed** environments with the caller's explicit `--attest-external-sandbox` and `--trust-cli`. This emits Claude `--dangerously-skip-permissions` or Codex `--yolo` / equivalent if supported by the installed version. The attestation is a **user assertion**, not an independently verified OS sandbox; the operator is responsible for isolating filesystem, credentials and network. Do not use bypass on an ordinary laptop checkout.

```bash
# Only from an independently isolated/containerized workspace with restricted secrets/network:
seh work run --task task.json --trust-cli \
  --autonomy bypass --attest-external-sandbox
```

## 6. Updating model information

`seh models list` refreshes stale model information on demand (TTL 24h). `seh models refresh` forces a refresh. OpenCode uses its documented `opencode models --refresh`; Claude uses current vendor-maintained aliases, Codex native default instead of pretending a reliable public CLI model list, and AGY can display `agy models` for the user's manual selection. **Catalog listings do not prove model/account authorization.**

```bash
seh models list --host opencode
seh models refresh --host opencode
seh hosts refresh
```

To keep model choice current without unexpected behavior changes, use `"model": "auto"`. To preserve reproducibility on high-stakes tasks, pin explicit model IDs per node. No silent overwrite of pinned choices; invalid IDs are surfaced by the host CLI.

## 7. Harness update and project migration

`harness` and `seh` are equivalent installed console scripts from 1.4.1 onward:

```bash
harness update --check       # Query latest stable release; no files changed
harness update               # Verify release wheel and ask before installing
harness update --yes         # Non-interactive opt-in installation
harness update --yes --sync --root /your/project
```

`harness update` queries the **pinned** publisher repository `jumtra/self-evolve-harness` GitHub Releases API. Installation is allowed only when a newer stable release includes a matching pure-Python wheel and a GitHub SHA-256 asset digest, both verified. The process does not invoke a shell or install additional dependencies, and refuses to mutate system Python (use a venv/pipx). **The repository must publish a verified wheel for remote updates to function**; the provided ZIP is not such a published release. HTTPS, release validation, wheel METADATA and digest checks all fail closed. The digest establishes integrity relative to GitHub's release metadata, not a separate developer signature.

The installed package itself is upgraded; existing `.harness/config.toml`, project code and memory are retained. `--sync` runs the newly installed `seh sync --apply` against managed host files after update. Owned-file conflicts still cause an error rather than overwriting a user edit. Take a backup/commit before switching to a new version.

**One-time upgrade from original 1.4.0:** That release does not contain `harness update`; install the new bundled wheel once in the same virtual environment:

```bash
python -m pip install --no-index --no-deps --upgrade dist/self_evolve_harness_standalone-1.4.1-py3-none-any.whl
harness --version
```

For offline verified upgrades after 1.4.1:

```bash
shasum -a 256 dist/self_evolve_harness_standalone-1.4.2-py3-none-any.whl
harness update --wheel dist/self_evolve_harness_standalone-1.4.2-py3-none-any.whl \
  --sha256 YOUR_VERIFIED_64_HEX_DIGEST --yes
```

The above 1.4.2 filename is a **future example**, not a currently released file. Current self-update does not guarantee automatic rollback if pip fails after partial installation: maintain your original wheel/virtualenv or reinstall the previous release. Further hardening can use versioned isolated environments and an atomic launcher switch.

## 8. Project language is independent of agent host

SEH itself is Python, but **project code may be TypeScript/JavaScript, Go, Rust, C#, Swift, Java, Ruby, PHP, C/C++, Godot, Unity, Unreal, etc.** The project detector recognizes common manifest files; the project's test commands are argv arrays such as `npm test`, `go test ./...`, `cargo test`, `dotnet test`. No framework has to use Python to be eligible. The task's `checks` and file scopes are selected for the project; only those commands on the configured allowlist can be run.

## 9. Verification status

Offline tests exercise command planning, probe cache invalidation, model routing, fallback, compare without merge, digest verification, and the demo project. Real vendor CLI authenticated E2E and real network release installation are **not yet verified**. Both are prerequisites for a production-grade compatibility guarantee.
