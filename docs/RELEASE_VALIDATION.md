# Release validation — standalone 1.4

The following checks were executed in the packaging environment (Linux, Python 3.13) on 2026-11-04. They establish offline software behavior only.

| Check | Result | Interpretation |
|---|---|---|
| Python test suite | 44 tests PASS | Covers init/sync/ownership, module registry, host templates, tool schema, local CodeGraph adapter simulation, DAG, tests, Eval, Author, Evolution, Optimizer, permissions and CLI |
| Python `compileall` | PASS | No syntax errors in shipped modules under Python 3.13 |
| Fresh offline demo | PASS | `init --apply`, `work run --host mock --apply --approve` generated `result.txt`, four-split `eval run` PASS |
| Harness² Full offline lab | PASS | Three candidate branches with checks and compose, no autonomous promotion (mock) |
| No-pip entrypoint | PASS | `run_seh.py --version` and `optimize status` in a fresh Python venv with no pip |
| Editable pip installation | PASS | Build and install into build environment with `--no-build-isolation` |
| Prebuilt wheel offline install | PASS | Fresh venv installed wheel with `pip --no-index`, executable `seh` worked |

**Not certified:** Real vendor CLI model sessions; any external MCP server; real CodeGraph/Graphify indexes; `gh pr create`; real `AGY` CLI execution; strict OS-level sandbox; absolute dollar-spend caps; comparative p50/p95 performance with real native coding tasks.

`seh bench` was run for five trials and measures the elapsed time of launching a Python subprocess for `seh work native` **only** (build host p50 ~0.94s, p95 ~1.22s). This is not a full production-speed comparison. The intended Fast workflow invokes the host natively and does not run `seh work native` as a required intermediate layer. User-visible code verification should use the host's actual test results, not the mock status.
