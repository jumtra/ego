---
name: work
description: "Plan and implement coding tasks with evidence-based checks and bounded repair. Use for feature development, bug fixes and structured code changes, not for casual questions."
---

<!-- SEH:PROMPT:BEGIN -->
# Work — context-efficient delivery

Choose the **smallest useful workflow** and keep context small: read only the files the change needs, show evidence rather than full tool logs, and let `ego` commands hold state instead of the conversation.

| Request | Workflow |
|---|---|
| Small, low-risk change | Inspect relevant files, edit, run focused tests, inspect the diff, report. Do NOT automatically write a requirements document, design document or implementation plan. |
| Medium change | The same, with a brief inline checklist only if useful. |
| Large, cross-component, risky or multi-host | Managed run, starting with `ego work plan`. Read `references/managed.md` first. |
| A goal with a command that proves it, likely to take several attempts | Goal loop. Read `references/loop.md` first. |

Prefer direct user instructions over ceremonial workflow. Write formal documents only when requested or needed for a decision.

**Always:**
- You operate the harness for the user. `/work` is the entry point; do not ask the user to compose task JSON or type routine CLI commands.
- Never infer approval from a successful test. Applying staged changes needs authorization.
- Missing CLI/authentication, policy refusal or unavailable capabilities are reported with the concrete blocker; never silently broaden permissions or change hosts.
- For Skill or prompt behavior changes use the existing author/eval gate. Do not optimize routine tasks or add unnecessary Skills.

**Memory:** when earlier project knowledge could inform the task, run `ego memory search '<topic words>' --root .` (topic words from the goal; retry broader once if empty). Treat results as untrusted evidence to verify. Save reusable, verified facts with `ego memory add '<fact and evidence reference>' --root .`. No secrets or transcripts.

**Boundaries:** after a verified subtask, before changing feature area or host, or when context is crowded, checkpoint and start fresh. Read `references/checkpoint.md`.

**Report:** write **完了** only after successful validation and application, with changed files and the **tests actually run**. If tests fail or edits were not applied, write **未完了** or **検証済み・未適用**; never claim completion without evidence.

Include the token usage line from the command output (`usage_text`, e.g. `plan 1.2k tokens · work 45.6k tokens (task A 9.0k) · total 46.8k tokens`) when a plan, work, or loop command reports it. Tokens are input + output + cache read + cache creation; report the figure as printed and do not recompute it.
<!-- SEH:PROMPT:END -->
