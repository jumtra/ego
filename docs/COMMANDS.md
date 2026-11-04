# CLI command index

Run `seh <command> --help` for the authoritative current flags. All commands support `--root` immediately after the command or a nested action, e.g. `seh work run --root /tmp/app ...`.

| Command group | Main actions | Failure conditions |
|---|---|---|
| `init` | detect/setup plan, `--apply` | ownership conflict, invalid config |
| `sync` | re-render plan, `--apply` | conflicting generated artifacts |
| `doctor` | check host tools, providers, config | missing optional components reported |
| `modules` | list, resolve, scaffold | conflicts/missing dependency |
| `migrate` | plan, remove-legacy, restore-legacy, rollback | hash mismatch, missing approval |
| `project` | detect, checks, check (preview by default; `--execute --approve` to run) | missing/ambiguous toolchain, unapproved script, timeout |
| `work` | native, plan, run, resume, status, cancel | invalid DAG, check failure, trust boundary |
| `eval` | run, compare | failed cases, incompatible suites |
| `author` | lint, plan, candidate, gate, promote, rollback | invalid skill, missing evidence, approval |
| `evolve` | observe, propose, lesson | missing evidence/observation |
| `optimize` | status, doctor, configure, plan, propose, run, report, promote, full, disable | disabled, isolation, budget, negative comparison |
| `memory` | add, search, list, delete | unsupported MCP method |
| `code` | doctor, search, symbol, index | missing provider, unsupported feature |
| `tool` | list, call | invalid inputs or command permission |
| `prompt` | register, show, extract | invalid contract |
| `hook` | pre-tool | blocked command/policy |
| `events` | tail | log not present |
| `pr` | create, reviews | `gh` absent, missing authorization |
| `bench` | repeated native dispatch | process execution failure |

Status and output fields are JSON. Nonzero statuses distinguish validation, missing dependency, safety/policy refusal and failed validation. Paths in examples are relative to where the command is run unless an explicit absolute path is used. In `optimize`, all active experiments require a separately configured mode; normal development never starts optimization implicitly.

## Simple onboarding (v1.4.2)

```sh
./setup.sh /path/to/project            # guided if interactive, otherwise preview
seh setup --root . --yes --hosts auto  # explicit application
seh setup --root . --yes --hosts all   # render all host formats
seh setup --root . --json             # preview for automation
seh status --root .                    # fast installation status
seh uninstall --root .                # removal preview
seh uninstall --root . --yes          # remove only SEH-managed files
seh migrate rollback snapshot-ID --root .  # restore previous snapshot
```

The `setup` command auto-detects project languages and installed AI host CLIs. It never executes project code or contacts an AI model. `init` / `sync` remain available as low-level JSON-oriented commands. See `docs/EASY_SETUP.md` for ownership rules and troubleshooting.
