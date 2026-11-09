---
name: work
description: "Plan and implement coding tasks with evidence-based checks and bounded repair. Use for feature development, bug fixes and structured code changes, not for casual questions."
---

<!-- SEH:PROMPT:BEGIN -->
# Work — context-efficient delivery

Choose the **smallest useful workflow**. A small, low-risk change: inspect only relevant files, edit, run focused tests, inspect diff, report outcome. Do NOT automatically write a requirements document, design document or implementation plan. Medium changes: a brief inline checklist only if useful. Large, cross-component, risky, or multi-host changes: use a scoped TaskSpec and staged verification; write formal documents only when requested or genuinely needed for coordination/decisions. Prefer direct user instructions over ceremonial workflow. Keep outputs concise; show evidence rather than full tool logs.

**At a natural boundary** (verified subtask finished, before changing feature area/host, or context getting crowded): write a compact factual handoff with `seh session checkpoint --task-id <id> --goal "..." --completed "verified result" --next "next action" --evidence "file/test reference" --root .`. Include decisions, known failures and uncommitted changes, not raw transcripts or secrets. Confirm that the checkpoint succeeded. Then run `seh session resume <id> --host <host> --root .`; in an idle interactive Claude Code or Codex session, use `/clear`, and in OpenCode use `/new`, then paste `resume_prompt`. If a safe clear is unavailable, instruct the user to start a new session; never claim the context was cleared when it wasn't. During an indivisible step, prefer `/compact` over clearing. For managed DAG runs, checkpoints are written automatically after verified waves, with new CLI sessions per node.

After completion, explicitly write **完了** and briefly state changed files plus **tests actually run**. If tests fail or edits were not applied, say **未完了** or **検証済み・未適用** instead; never claim completion without evidence. Use existing tools, bounded repair, and the narrowest checks. For changing Skill or prompt behavior use the existing author/eval gate; do not optimize routine tasks or add unnecessary Skills. Respect approvals, ownership, and host capabilities.
<!-- SEH:PROMPT:END -->
