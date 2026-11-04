# v1.4.2 Easy Setup + Polyglot distribution verification

**Verified locally (offline tests)**

- New standalone setup command: safe preview when no terminal, interactive approval when TTY, explicit `--yes` apply, and machine-readable JSON mode.
- Host auto-selection and `--hosts all`; unsupported AGY remains config-only.
- Bounded project language detection including JS/TS, Go, Rust, JVM, .NET, Swift, C/C++, Godot, Unity, Unreal and custom TOML toolchains.
- Reinstall of an unchanged project results in `UNCHANGED` with no new snapshot.
- Setup never auto-runs project tests, downloads packages, starts a model or enables Harness².
- Existing `CLAUDE.md`/`AGENTS.md` contents outside the SEH markers are retained. User-owned conflicts and symlink escapes fail without changes.
- `seh status`, `seh uninstall --yes`, ownership-aware host switching and snapshot rollback are verified.
- Project knowledge (`.harness/knowledge/domain.md`) remains user data after reconfiguration/uninstall.
- Python wheel built and installed offline into a fresh virtual environment, followed by `seh --version`, setup, status and uninstall smoke tests.
- Automated tests: **86 passed** (three independent batches: 36 + 29 + 21). The complete one-shot suite was interrupted by the execution environment time limit, so batch verification is the supported result.

**Not verified**

- Authenticated external Claude, Codex or OpenCode sessions.
- AGY CLI execution (disabled by design).
- CodeGraph, Graphify and Engram external installations.
- Native Windows setup (POSIX shell / flock environment required).
- Performance targets for model-based coding tasks (requires real host baselines).

**Distribution contents**: Python source, launcher, setup.sh, offline wheel, Skills/Agents template generators, demo, tests, Japanese/English README, docs.
