# Migrating an existing self-evolve-harness installation

This package is an independently authored implementation, *not* an upstream clone. Project files that were edited by the user must be preserved. The migration functions never assume all `.claude/skills/` files are replaceable.

1. Make a Git commit or filesystem backup of the project.
2. Inspect `seh migrate plan --root <project>` and `seh init --root <project>` (both dry-runs).
3. Explicitly select hosts and providers; do not install or run external binaries in init.
4. Review the manifest of generated/owned files. Do not overwrite a file with ambiguous ownership. Only then execute `seh init --root <project> --apply`.
5. Check `seh doctor` and validate generated host-specific instructions and active skill trigger behavior.
6. Remove old skills only when *exact* old file hashes are backed up and have been approved. `seh migrate remove-legacy` consumes a caller-provided hash manifest; manually changed files must be rejected.
7. Preserve old configurations and set optimization OFF until the baseline and audit are complete.
8. If errors occur, use `seh migrate rollback <snapshot-id> --root <project>` or restore your version-control backup.

`seh sync` is repeatable, but it does not merge arbitrary competing YAML/TOML fragments into a valid host configuration; inspect updates if other plugins also own the same settings. The generated files and paths are specific to this standalone implementation and are not binary-compatible with every upstream version.
