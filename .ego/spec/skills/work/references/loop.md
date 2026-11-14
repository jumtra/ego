# Goal loop

For a goal whose completion a command can prove (tests pass, a build succeeds, a check script exits 0). Without such a command, do not loop: do the work directly and report.

**Inside one interactive Claude Code session:** offer the user a ready-to-paste host goal instead of building your own loop: `/goal <one measurable end state>; verify with <command>; stop after 20 turns`. The host's checker does not run tools, so run the command and state its result each turn.

**Across fresh sessions (larger goals, any host):**

`ego work loop --root . --goal "<what and why>" --done-check "<command>" --host <host> --scope '<paths>' --max-iterations 3 --max-budget-usd <n> --execute --trust-cli`

With a plan the user approved, add `--task <task-file>` (the goal defaults to the task's): that plan runs as the first iteration, and the planner is called only after it fails. A replan departs from the approved plan, so get the user's agreement first, or use `ego work run --task` instead.

The first iteration is one direct worker repaired against the done-checks; the planner is called only after that fails (`--plan-first` plans up front for goals that clearly need decomposition). Every iteration runs the done-checks in a staged copy. A failed iteration replans from a bounded record (failed node, clipped check output, changed paths), never from a transcript, and verified work carries into the next iteration. Repeat `--done-check` for several commands.

Read `status` in the compact output:
- `VERIFIED`: the goal holds in the staged copy; nothing is applied. After authorization run `ego work loop-apply <loop_id> --root . --approve`.
- `COMPLETED`: applied (only when the loop was started with `--apply --approve`, which needs authorization up front).
- `STALLED`, `MAX_ITERATIONS`, `BUDGET_EXHAUSTED`, `TIMEOUT`: report 未完了 with the last failure. Do not relaunch the same loop unchanged; change the goal, scope or checks, or ask the user.

`ego work loop-status <loop_id>` re-reads the result; `ego work cancel <loop_id>` stops before the next iteration.
