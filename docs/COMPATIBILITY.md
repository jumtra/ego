# Feature compatibility and verification status

| Capability | Offline without vendor CLIs | Claude Code | Codex | OpenCode | AGY |
|---|---|---|---|---|---|
| Install/sync Skills, instructions | Tested | Generated | Generated | Generated | Generated |
| Four custom agent files | Tested syntax by format | `.claude/agents` | `.codex/agents` | `.opencode/agents` | `.agents/agents` |
| Native agent coding | No: mock is deterministic | External CLI required | External CLI required | External CLI required | Native app only |
| Managed task plan/DAG/check/retry | Tested with mock and local argv | CLI adapter present (real CLI not verified here) | Same | Same | Policy blocked |
| Deterministic Eval & authoring | Tested offline | Real CLI required for behavioral evidence | Same | Same | Policy blocked |
| Memory File | Tested | Shared | Shared | Shared | Shared |
| Engram MCP | Protocol adapter present, real Engram mapping not verified | External server | External server | External server | External server |
| Code intelligence | Source search tested | CodeGraph or Graphify external | Same | Same | Same |
| Harness² Lite & Full | Testable with mock but **mock not promotable** | Requires real CLI and isolation policy | Same | Same | Execution blocked |
| GitHub PR | External `gh` only | Optional | Optional | Optional | Optional |

## Clarifications

- **Independent implementation**: `jumtra/self-evolve-harness` was not cloned into this package. It does not include every private or undocumented script from that repository, nor claim exact migration of every file's history. It provides a standalone compatible *workflow design* and a safe migration path, not binary compatibility.
- **Real CLI E2E**: There is no Claude, Codex, OpenCode, AGY, CodeGraph, Graphify, Engram or GitHub CLI binary installed in the build environment. Their live auth/network/model interactions were **not** executed. `seh doctor` reports availability and restrictions at the destination machine.
- **Provider capability honesty**: A local text search is never falsely labeled a call graph result. CodeGraph/Graphify commands follow published CLI documentation but version-specific compatibility should be smoke-tested after installation.
- **Harness² fidelity**: This is an **adaptation**, not a copy of the Google Research benchmark or a drop-in implementation of its LAB/JobBench/WorkBuddy adapters. Lite compares stable/candidate; Full performs two lenses plus independent compose/probe. No second SDK or evaluator is introduced. Provider usage costs and benchmark gain claims are not assumed transferable.
- **Prompt behavior**: Model-inferred Skill triggers cannot be proved by static YAML lint alone. E2E evaluation records observed outputs; true internal Skill invocation needs compatible per-host tool events or vendor instrumentation. Report fields do not assert otherwise.
- **Performance**: Benchmark targets p50 +5%, p95 +10% are acceptance thresholds, not verified real-host metrics. Offline CLI dispatch can be measured with `seh bench`; real task latency depends on network, model, tool calls and project size.
- **Safety**: `--trusted-local` means you explicitly accept a lower isolation guarantee. Sandbox detection does not auto-certify a Docker/podman binary as safe. `require_metered` blocks because the code does not yet implement a pre-execution dollar hard cap.
- **Unverified X reference**: The second user-supplied social post was not accessible in verified text, so the optional module is declared `SPEC_UNVERIFIED` and OFF. It is deliberately not implemented based on guessed content.

## v1.4.1 capability extensions

- Task and individual DAG nodes can select `host`, `model`, `models`, and `strategy`. Multi-host `fallback` handles unavailable/rate-limited/timed-out CLIs; `compare` does not apply results. Independent DAG nodes can run in parallel with distinct hosts. These are **offline-tested adapters**, not authenticated production E2E claims.
- `seh hosts inspect|refresh` checks installed version/flags and invalidates the cache after binary replacement. Known flag variants can be chosen automatically; unknown new semantics require a reviewed adapter release.
- `seh models list|refresh` caches model inventories/aliases. `auto` delegates to native defaults; pinned model IDs do not change without editing the Task.
- `harness update` is implemented with a publisher-pinned GitHub Releases API, SHA256 verification, wheel METADATA checks and user approval. Its remote channel is functional **only after a matching official wheel is published to the specified repository**; v1.4.0 needs an initial pip upgrade.
- `--autonomy auto` is the unattended-but-sandboxed default; raw `--yolo` and `--dangerously-skip-permissions` require an explicit external-isolation attestation and `--trust-cli`. The harness does not prove the external sandbox exists.
