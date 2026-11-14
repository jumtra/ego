# Checkpoint and fresh start

Write a compact factual handoff:

`ego session checkpoint --task-id <id> --goal "..." --completed "verified result" --next "next action" --evidence "file/test reference" --status <status> --root .`

Status is IN_PROGRESS while work remains, BLOCKED on a blocker, VERIFIED for verified but unapplied work, COMPLETED only after successful validation and application. Include decisions, known failures and uncommitted changes, not raw transcripts or secrets. Confirm that the checkpoint succeeded.

Then run `ego session resume <id> --host <host> --root .`. In an idle interactive Claude Code or Codex session use `/clear`, in OpenCode `/new`, then send `resume_prompt`. If a safe clear is unavailable, instruct the user to start a new session; never claim the context was cleared when it wasn't. During an indivisible step, prefer `/compact` over clearing. Only request manual user actions when the host cannot safely perform them.

When `ego session resume` returns `auto_resume_after_clear: true` (Claude Code with `auto_resume = true` in `.ego/config.toml`, the default), the user only needs to run `/clear`: the resume prompt is injected automatically, and the host itself suggests the clear once the measured context passes the threshold.

Checkpoints are shared by every CLI in the project (Claude Code, Codex, OpenCode, AGY) and record which one saved them. To pick up another CLI's work in this session, run `ego session resume --from <claude|codex|opencode|agy> --root .` and follow `resume_prompt` (no clear needed); `ego session list --root .` shows all open checkpoints. To send work the other way, checkpoint, then give the user the `launch_command` from `ego session handoff <id> --to <cli> --root .`; you cannot open the other CLI yourself.

Managed runs and goal loops checkpoint automatically under their run or loop ID, with a new CLI session per node.

A managed run's checkpoint is rewritten as each node finishes (also inside a parallel wave), so it is current after any node. While the run's lock (`.ego/locks/<run_id>.lock`) is held, `ego session resume --from`, `ego session handoff` and the context-clear instructions refuse that checkpoint with `RUN_ACTIVE`; without a task ID, locked managed checkpoints are skipped. A run whose process died shows as `INTERRUPTED` in `ego work status`: resume it with `ego work resume` (see `managed`), not by continuing its checkpoint by hand.
