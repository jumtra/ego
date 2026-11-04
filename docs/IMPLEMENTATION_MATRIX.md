# Implementation and validation matrix (standalone 1.4)

Symbols: **offline tested** means a deterministic test covers the code path. **Adapter only** means the external service is absent in this build environment and must be validated separately. **Explicitly unavailable** means a safety/terms boundary prevents invoking it.

| Requested area | Implementation | Verification scope |
|---|---|---|
| Project detection / init / sync | `detect.py`, `install.py`, `templates.py` | offline tested |
| Two Skills, four Agents, native host output | `templates.py` | generated files checked offline; real host discovery **not** tested |
| Multi-host agent invocation | `hosts.py`, `runner.py` | mock and parser tested; real CLI is adapter only |
| Bounded workflow DAG / code gate / repair / parallel merge | `work.py` | offline tested with deterministic code and mock |
| Eval-first, training/regression/holdout/security | `evals.py`, `author.py` | offline tested; real skill activation needs host tracing |
| Versioned prompt / output contract | `prompting.py` | offline tested |
| Tool schema / deterministic tools | `tools.py` | offline tested |
| File Memory | `memory.py` | offline tested |
| Engram MCP | `memory.py` | protocol adapter only; actual MCP tool naming/config needed |
| CodeGraph / Graphify selectable | `codeintel.py` | source fallback tested, external adapter only |
| Manifest module extensions | `registry.py` | offline tested |
| Safe ownership / migration rollback | `install.py`, `candidates.py` | offline tested |
| Self-evolution and lessons | `evolve.py`, `candidates.py` | offline tested; model-driven proposals need authenticated CLI |
| Harness² opt-in Lite/Full | `optimize.py` | mock tests; real model outcomes and improvement rates not tested |
| 4-host native execution | host-specific invocation templates | Claude, Codex, OpenCode adapters; AGY automation unavailable |
| GitHub PR automation | `github.py` | command adapter only; external `gh` required |
| Fast performance | `seh bench` | dispatch microbenchmark only; full 5%/10% target not yet measured |
| OS sandbox / hard USD spending limit | outside builtin feature set | **not provided**; trusted-local explicit bypass, metered policy rejects |
| Unknown X optional feature | specification not verified | not implemented; never enable guessed functionality |

The phrase "standalone" means the project itself is installable and its core offline workflows run without importing the original harness or agent SDK. It does **not** imply that it bundles proprietary AI models, paid vendor CLIs, CodeGraph, Graphify or Engram.
