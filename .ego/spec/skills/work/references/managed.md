# Managed run

For large or cross-component work needing dependency scheduling or several hosts. Check `ego work run --help` before first use.

1. Inspect the project and choose an available authenticated host and a narrow write scope. Run `ego work plan --root . --goal "<goal>" --host <host> --scope '<paths>' --execute --out <task-file>`. This invokes an external planning CLI; respect existing execution permissions. Do not use mock for real implementation.
2. Read the generated task JSON. Verify prompts, dependencies, host/model routes, write_allowlist and checks. Node prompts say what to achieve and why, with acceptance criteria; they do not dictate implementation. Add focused test commands as argv arrays in each node's checks, and a final gate with integration checks depending on all implementation nodes. Explain the short plan in conversation and obtain any required approval.
3. Run `ego work run --root . --task <task-file> --host <host> --trust-cli`. The output is a compact summary: run_id, final_status, node_status, changed_files, clipped failures and usage. Open `record_path` or pass `--verbose` only when the summary is not enough. A PASSED run without applied=true is verified but unapplied. Report failures; do not apply failed runs.
4. After reviewing a PASSED result and obtaining authorization to apply, run `ego work resume <run-id> --root . --task <task-file> --host <host> --trust-cli --apply --approve`. Keep the task JSON unchanged. Never infer approval from a successful test or bypass STALE_BASE. Inspect the applied diff and report evidence.

Managed worker prompts describe only their assigned implementation and tests. Do not recursively invoke /work or start another managed run from a worker. Workers start in fresh sessions and, by default, without the user's MCP servers (`worker_context = "lean"` in `.ego/config.toml`).
