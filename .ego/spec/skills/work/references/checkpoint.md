# Checkpoint and fresh start

Write a compact factual handoff:

`ego session checkpoint --task-id <id> --goal "..." --completed "verified result" --next "next action" --evidence "file/test reference" --status <status> --root .`

Status is IN_PROGRESS while work remains, BLOCKED on a blocker, VERIFIED for verified but unapplied work, COMPLETED only after successful validation and application. Include decisions, known failures and uncommitted changes, not raw transcripts or secrets. Confirm that the checkpoint succeeded.

Then run `ego session resume <id> --host <host> --root .`. In an idle interactive Claude Code or Codex session use `/clear`, in OpenCode `/new`, then send `resume_prompt`. If a safe clear is unavailable, instruct the user to start a new session; never claim the context was cleared when it wasn't. During an indivisible step, prefer `/compact` over clearing. Only request manual user actions when the host cannot safely perform them.

When `ego session resume` returns `auto_resume_after_clear: true` (Claude Code with `clear_suggest_tokens` set in `.ego/config.toml`), the user only needs to run `/clear`: the resume prompt is injected automatically, and the host itself suggests the clear once the measured context passes the threshold.

Managed runs and goal loops checkpoint automatically under their run or loop ID, with a new CLI session per node.
