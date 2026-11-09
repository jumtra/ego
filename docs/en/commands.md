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
| `work` | native, plan, run, resume, status, cancel | invalid DAG, check failure, trust boundary |
| `eval` | run, compare | failed cases, incompatible suites |
| `author` | lint, plan, candidate, gate, promote, rollback | invalid skill, missing evidence, approval |
| `evolve` | observe, propose, lesson | missing evidence/observation |
| `optimize` | status, doctor, configure, plan, propose, run, report, promote, full, disable | disabled, isolation, budget, negative comparison |
| `session` | checkpoint, status, resume | missing checkpoint, invalid task ID |
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
ego update --check
ego update --yes --sync --root /your/project
```

`ego` is the primary console command installed by `make install` or `pip install -e .`. For Task JSON examples, see [examples/multihost/task.json](../examples/multihost/task.json).
