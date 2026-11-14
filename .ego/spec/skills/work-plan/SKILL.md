---
name: work-plan
description: "Like work, but interviews the user and gets a node-graph plan approved before editing. Use when the user wants a plan first for a coding task."
---

<!-- EGO:PROMPT:BEGIN -->
# Work Plan — work with a mandatory intake and plan

This skill is `work` with two additions: **intake questions** and **a plan the user approves before any edit**. Everything else (workflow choice, memory, boundaries, reporting) follows `work`. Read `.claude/skills/work/SKILL.md` and its `references/` for those details.

## 1. Intake (always, before reading code or editing)

Ask the user about the points that would change what you build. Rules:

- Always ask at least one question, even when the request looks clear. Confirm the goal and what "done" means.
- One question at a time, each with your recommended answer, so the user can reply "yes" or pick another option.
- Ask about: the goal and why, the scope (in and out), the acceptance check (the test or behavior that proves it works), constraints (compatibility, files not to touch, deadlines), and risks you see.
- Stop asking when the remaining points have safe defaults. State those defaults as assumptions in the plan.
- Use memory and the code only to make questions sharper, not to skip them.

## 2. Plan as a node graph (inline, then wait for approval)

The graph is both the requirements and the plan. Do not invoke external brainstorming or planning skills, and do not write spec or plan files unless the user asks.

Write one line for the goal and one for done, then the dependency graph (`N1 ──► N2`). For each node:

- **Who / What / When / Where / Why:** one line each. When = the nodes it waits for. Where = exact file paths. Why = why the goal needs this node.
- **How:** numbered steps written so they can be implemented exactly as written, so whoever implements them cannot drift in a different direction. Each step names the file, the function or section (or line), and the exact change: code, text or command. Never write "consider", "as appropriate", "refactor as needed" or other steps that leave a design decision open. If you cannot write a step that concretely, read more code or ask first. Before asking for approval, reread each How step and rewrite any that two implementers could carry out differently.
- **Done when:** 1-4 observable acceptance criteria, each a test, command or behavior that proves it.
- **Non-goals / Assumptions / Provides:** only where they apply, one short line each. Non-goals = tempting work this node must not do. Assumptions = unverified facts the node relies on. Provides = files or interfaces that dependent nodes rely on.

Close with plan-wide assumptions (defaults from intake), tests to run (exact commands) and out of scope. Then ask: "この計画で進めてよいですか？" and stop. Edit only after the user approves, or asks for changes that you then fold into the plan and confirm again.

## 3. Execute (after approval): always through ego's managed engine

Do not edit project files yourself. ego's engine runs every node in an isolated copy, enforces roles, `write_allowlist` and the judge gate, repairs failures within bounds, and keeps each worker under the context-token limit (it checkpoints and continues in a fresh session by itself). Your job is to start the run, relay what happens, and report.

1. Write the approved plan itself as the task JSON in `.ego/state/tasks/<slug>.json`; do not plan it again with `ego work plan`. One task node per graph node: `depends_on` from When, `write_allowlist` from Where, 5W1H into `requirement`, How into `steps`, Why into `intent`, Done when into `done_when` (its commands as argv arrays in `checks`), and Non-goals, Assumptions and Provides into `non_goals`, `assumptions` and `provides`. Put the user's intake answers into task-level `instructions`.
2. Start the run: `ego work run --root . --task <task-file> --host <host> --trust-cli --run-id run-<slug>`. In Claude Code start it with Bash `run_in_background`; in other hosts run it in the foreground. Check `.claude/skills/work/references/managed.md` for host and flag details.
3. Receive progress. In Claude Code start Monitor with `ego work watch run-<slug> --root .` and `timeout_ms` 1800000; if it expires before a `run_finished` or `run_error` event, start it again with `--after <last seq>`. In other hosts read `progress` from `ego work status run-<slug> --root .`.
4. Tell the user each event in one line: node started, attempt passed or failed (name the failed check), judge verdict and findings, `worker_segment` rotate. Report a node failure or a judge REJECT when it happens, not when the run ends.
5. After `run_finished`, read `ego work status run-<slug> --root .` once for `nodes`, `failures`, `changed_files` and `next_action`.
6. A PASSED run is **検証済み・未適用**. Show the changed files and ask for approval to apply. After approval run the `next_action` command, then inspect `git diff`.
7. If a node fails, a step cannot be done as written, needs a file outside its Where, or one of the node's assumptions turns out false, stop and ask instead of improvising: explain, and get approval before changing the plan or rerunning.
8. After approval, correct only the failed nodes in the task file, keep the others unchanged, and rerun step 2's command with `--run-id run-<slug>-2 --from run-<slug>`: unchanged verified nodes are not run again.

Use `ego work loop --task` (`.claude/skills/work/references/loop.md`) instead only when a command proves the whole goal and the user agreed that a failed iteration may be replanned.

## 4. Report

Use `work`'s report rules: write **完了** only after successful validation and application, with changed files and the **tests actually run** with their exit statuses, the run ID, and each node's status and checks from the status summary. Otherwise write **未完了** or **検証済み・未適用**. Never call a FAIL or UNKNOWN result a PASS.
<!-- EGO:PROMPT:END -->
