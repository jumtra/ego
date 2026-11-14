---
name: work-plan
description: "Like work, but interviews the user and gets a node-graph plan approved before editing. Use when the user wants a plan first for a coding task."
---

<!-- EGO:PROMPT:BEGIN -->
# Work Plan — work with a mandatory intake and plan

This skill is `work` with two additions: **intake questions** and **a plan the user approves before any edit**. Everything else follows `work` (`.claude/skills/work/SKILL.md` and its `references/`).

## 1. Intake (always, before reading code or editing)

Ask the user about the points that would change what you build. Rules:

- Always ask at least one question, even when the request looks clear. Confirm the goal and what "done" means.
- One question at a time, each with your recommended answer, so the user can reply "yes" or pick another option.
- Ask about: the goal and why, scope (in and out), the acceptance check, constraints (compatibility, files not to touch, deadlines), and risks you see.
- Stop asking when the remaining points have safe defaults, and state those defaults as assumptions. Use memory and code only to sharpen questions.

## 2. Plan as a node graph (inline, then wait for approval)

The graph is both the requirements and the plan. Do not invoke external brainstorming or planning skills, and do not write spec or plan files unless the user asks.

Write one line for the goal and one for done, then the dependency graph (`N1 ──► N2`). For each node:

- **Who / What / When / Where / Why:** one line each. When = the nodes it waits for. Where = exact file paths. Why = why the goal needs this node.
- **How:** numbered steps written so they can be implemented exactly as written, so whoever implements them cannot drift in a different direction. Each step names the file, the function or section (or line), and the exact change: code, text or command. Leave no design decision open (no "consider" or "as appropriate"); if a step cannot be written concretely, read more code or ask first.
- **Done when:** 1-4 observable acceptance criteria, each a test, command or behavior that proves it.
- **Non-goals / Assumptions / Provides:** only where they apply, one short line each. Non-goals = tempting work this node must not do. Assumptions = unverified facts the node relies on. Provides = what dependent nodes rely on.

Close with plan-wide assumptions (defaults from intake), tests to run (exact commands) and out of scope. Then ask: "この計画で進めてよいですか？" and stop. Edit only after the user approves, or asks for changes that you then fold into the plan and confirm again.

## 3. Execute (after approval): always through ego's managed engine

Do not edit project files yourself. The engine runs each node in an isolated copy and enforces roles, `write_allowlist` and the judge gate. Your job is to start the run, relay events and report.

1. Write the approved plan as the task JSON in `.ego/state/tasks/<slug>.json`; do not plan it again with `ego work plan`. One node per graph node: `depends_on` from When, `write_allowlist` from Where, 5W1H into `requirement`, How into `steps`, Why into `intent`, Done when into `done_when` (commands as argv arrays in `checks`), Non-goals, Assumptions and Provides into `non_goals`, `assumptions` and `provides`. Intake answers go into task-level `instructions`. Then save an interactive checkpoint: `ego session checkpoint --task-id <slug> --goal "<goal>" --completed "plan approved" --next "run-<slug>" --status IN_PROGRESS --root .`.
2. Start the run: `ego work run --root . --task <task-file> --host <host> --trust-cli --run-id run-<slug>`. In Claude Code use Bash `run_in_background`, elsewhere the foreground (`managed.md` has the flags). Each node is saved as it finishes, also inside a parallel wave, so a crash loses only the nodes still running.
3. Receive progress. In Claude Code start Monitor with `ego work watch run-<slug> --root .` and `timeout_ms` 1800000; if it expires before `run_finished`, `run_error` or `run_stopped`, restart it with `--after <last seq>`. Elsewhere read `progress` from `ego work status run-<slug> --root .`.
4. Tell the user each event in one line as it happens: node started, attempt passed or failed (with the failed check), judge verdict, `worker_segment` rotate.
5. After `run_finished`, read `ego work status run-<slug> --root .` once (`nodes`, `failures`, `changed_files`, `next_action`).
6. `INTERRUPTED` (in `ego work status`, or a `run_stopped` event) means the process died while the run was RUNNING. Completed nodes stay saved. Report it as interrupted and resumable, then run `ego work resume run-<slug> --root . --task .ego/state/tasks/<slug>.json --host <host> --trust-cli`; PASSED nodes are skipped. Never start a new run under the same ID.
7. `RUN_ACTIVE` means another live process holds `.ego/locks/run-<slug>.lock`. Do not delete the lock or start a second run, and do not resume or hand off its checkpoint until that process finishes or the run reports `INTERRUPTED`.
8. A PASSED run is **検証済み・未適用**. Show the changed files and ask for approval to apply. After approval run the `next_action` command, then inspect `git diff`.
9. If a node fails, a step cannot be done as written, needs a file outside its Where, or one of the node's assumptions turns out false, stop and ask instead of improvising: explain, and get approval before changing the plan or rerunning.
10. After approval, correct only the failed nodes in the task file, keep the others unchanged, and rerun step 2's command with `--run-id run-<slug>-2 --from run-<slug>`: unchanged verified nodes are not run again.

Use `ego work loop --task` (`.claude/skills/work/references/loop.md`) only when a command proves the whole goal and the user agrees to replanning.

## 4. Report

Use `work`'s report rules: write **完了** only after successful validation and application, listing changed files, the run ID, each node's status and checks, and the **tests actually run** with exit statuses. Otherwise write **未完了** or **検証済み・未適用**. Never call a FAIL or UNKNOWN result a PASS.
<!-- EGO:PROMPT:END -->
