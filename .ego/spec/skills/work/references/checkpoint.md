# Checkpoint and fresh start

Write a compact factual handoff:

`ego session checkpoint --task-id <id> --goal "..." --completed "verified result" --next "next action" --evidence "file/test reference" --status <status> --root .`

Status is IN_PROGRESS while work remains, BLOCKED on a blocker, VERIFIED for verified but unapplied work, COMPLETED only after successful validation and application. Include decisions, known failures and uncommitted changes, not raw transcripts or secrets. Confirm that the checkpoint succeeded.

Then run `ego session resume <id> --host <host> --root .`. In an idle interactive Claude Code or Codex session use `/clear`, in OpenCode `/new`, then send `resume_prompt`. If a safe clear is unavailable, instruct the user to start a new session; never claim the context was cleared when it wasn't. During an indivisible step, prefer `/compact` over clearing. Only request manual user actions when the host cannot safely perform them.

Managed runs and goal loops checkpoint automatically under their run or loop ID, with a new CLI session per node.
