# Easy setup, update, status and removal (v1.4.2)

The ZIP ships a Python 3.11+ standard-library CLI, `setup.sh`, and an optional offline-install wheel. No Agent SDK or API key is needed for initial configuration.

## First run

```sh
unzip seh_standalone_easy_setup.zip
cd seh_standalone_easy_setup
./setup.sh /path/to/project
```

On a TTY, setup detects supported AI CLI binaries and the project's language/toolchains, then asks for host selection, profile, code graph provider, memory provider and final approval. It shows all planned file changes before writing. The default profile is `balanced`; Harness² stays OFF. If no supported AI CLI is installed, the default is Claude Code config-only with a warning. AGY remains config-only.

For non-interactive environments, setup is a dry-run unless **`--yes` is explicit**:

```sh
./setup.sh /path/to/project --non-interactive
./setup.sh /path/to/project --yes --hosts auto
./setup.sh /path/to/project --yes --hosts all
./bin/seh setup --root /path/to/project --json
./bin/seh setup --root /path/to/project --yes --json
```

`--hosts all` generates Claude, Codex, OpenCode and AGY files. `--hosts auto` uses detected supported host CLI installations. Profiles are `minimal`, `balanced`, `full`; code graph is `none`, `codegraph`, or `graphify`; memory is `file` or `engram`.

## Status and project-specific checks

```sh
./bin/seh status --root /path/to/project
./bin/seh doctor --root /path/to/project
./bin/seh project checks --root /path/to/project
```

`status` is read-only, quick and does not load the optimizer or contact models. `project checks` detects test/build/lint commands from project manifests, including monorepos and non-Python languages. **It does not execute them.** Scripts can only run with `project check ... --execute --approve`.

## Change or remove the installation

```sh
./bin/seh setup --root /path/to/project --hosts codex,opencode
./bin/seh uninstall --root /path/to/project
./bin/seh uninstall --root /path/to/project --yes
./bin/seh migrate rollback snapshot-XXXXXXXXXXXX --root /path/to/project
```

Re-running setup with unchanged options is idempotent (`UNCHANGED`). During host switching, only SEH-owned generated files and owned root-instruction blocks are changed; user notes remain intact. Uninstall removes only tracked generated files. Memory, logs, snapshots, backups and user files are **retained**.

Rollback refuses to overwrite edits made after a snapshot. `OWNERSHIP_CONFLICT`, `SYMLINK_DENIED` or `ROLLBACK_CONFLICT` require manual investigation; `--yes` is not a force flag.

## Optional offline CLI install

Without pip, `./bin/seh` or `python3 run_seh.py` works from the extracted package. To let generated AI Skills call `seh` anywhere, add this package's `bin/` directory to `PATH` or install the bundled wheel in your desired environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --no-index ./dist/self_evolve_harness_standalone-1.4.2-py3-none-any.whl
seh --version
```

## Behavior and limitations

- Setup never installs external CLI/provider dependencies, runs project scripts, contacts models, indexes code, enables Harness² or deploys changes.
- Linux/macOS POSIX setup script; Windows may use the Python entry point in a compatible environment, but platform-specific locking is not implemented for native Windows.
- Config generation does not guarantee a host CLI is present, authenticated or permitted to run. AGY model execution from the harness is disabled.
- User-managed existing files are not force-merged or overwritten. Conflict details are available in `--json` output.
- `seh setup` configures a project; it does not install/symlink the AI CLI executables themselves.
