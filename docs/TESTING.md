# Test and acceptance plan

## Tested locally

- Run the unit/integration suite with `python -m pytest -q` after adding `pytest`.
- Run `python -m seh --version`, `python -m seh doctor`, `python -m seh optimize status` and `python -m seh modules list`.
- Copy `examples/demo_project` to an empty temporary directory, initialize with `--apply`, execute `work-task.json` with `--host mock --apply --approve`, and evaluate `eval-suite.json` with `--host mock`.
- Run `seh bench --trials 20` to measure **CLI dispatch** only. This is not a hosted model/task-latency benchmark.

## Evidence required for real deployments

1. Lock specific CLI versions, model IDs, permissions and host configuration.
2. Execute real-host positive, negative, regression, holdout and hostile prompt test cases, multiple trials per case in fresh workspaces.
3. Verify that real host outputs honor CLI schemas and that tokens/costs are reported when supported.
4. Verify CodeGraph/Graphify exact CLI flags and the installed Engram MCP server tool methods using a dedicated smoke test.
5. Validate sandbox isolation using an OS-level boundary, not simply file copies or Git worktrees.
6. Test real-team collaboration with 2 writers and a concurrent approval/promotion attempt.
7. Run repeated native-host and Fast-mode jobs to compare end-to-end elapsed times; require p50 overhead <=5% and p95 <=10%. These are **targets**, not claims of achievement.

## Important negative cases

- `seh optimize` mode OFF must not start or import its evaluator in ordinary `seh work native`.
- Reject candidate changes outside owned skill descriptions, bounded bodies and tool usage guidance.
- Reject unresolved symlinks, path escapes, stale base hashes and duplicate promotions.
- `INFRA`, `TIMEOUT`, `UNKNOWN` and incomplete Evals must not count as success.
- Real-host optimization without independent isolation requires explicit trusted-local and spending consent; `require_metered` must fail closed.
- Do not auto-run or auto-install an unreviewed module manifest.
- Mock agent tests are integration tests, **not** behavioral proof of AI model competence.

## CI

`.github/workflows/ci.yml` runs the offline tests on Python 3.11, 3.12 and 3.13. It does not require any paid AI service, cannot certify a real AI CLI workflow, and should be supplemented with opt-in credential-backed CI only in a sandbox and with a spending budget.
