---
name: work
description: "Plan and implement coding tasks with evidence-based checks and bounded repair. Use for feature development, bug fixes and structured code changes, not for casual questions."
---

<!-- EGO:PROMPT:BEGIN -->
# Work — context-efficient delivery

Choose the **smallest useful workflow** and keep context small: read only the files the change needs, show evidence rather than full tool logs, and let `ego` commands hold state instead of the conversation.

| Request | Workflow |
|---|---|
| Small, low-risk change | Inspect relevant files, edit, run focused tests, inspect the diff, report. Do NOT automatically write a requirements document, design document or implementation plan. |
| Medium change | The same, with a brief inline checklist only if useful. |
| Large, cross-component, risky or multi-host | Managed run, starting with `ego work plan`. Read `references/managed.md` first. |

Prefer direct user instructions over ceremonial workflow. Write formal documents only when requested or needed for a decision.

**Planning:** when you show a plan, use the node-graph format of the `work-plan` skill: 5W1H per node, and How steps that can be implemented exactly as written (file, function or section, exact change), so the implementer cannot drift in a different direction. Do not invoke external brainstorming or planning skills.

**Intent first, questions only when needed:** at any size, before editing you can state in one line each why the user wants this and what will show it is done. When the request, the code and memory answer both, proceed without asking. Ask when an open point would change what you build and a wrong guess would be costly to undo: one question at a time, each with your recommended answer. Stop asking once the remaining points have safe defaults, and state those assumptions in the report. In a managed run, put the answers in task-level `instructions`.

**Always:**
- You operate the harness for the user. `/work` is the entry point; do not ask the user to compose task JSON or type routine CLI commands.
- Never infer approval from a successful test. Applying staged changes needs authorization.
- Missing CLI/authentication, policy refusal or unavailable capabilities are reported with the concrete blocker; never silently broaden permissions or change hosts.
- Do not add unnecessary Skills.

**Memory and knowledge:** process facts (what happened while working) stay in local memory: when earlier work could inform the task, run `ego memory search '<topic words>' --root .` (retry broader once if empty) and save reusable, verified facts with `ego memory add '<fact and evidence reference>' --root .`. Project knowledge (domain terms, glossary, decisions) lives where `knowledge` in `.ego/config.toml` says. `file`: read `.ego/knowledge/index.md` first, open only the topic files the task needs (`ego knowledge search '<words>'`, `ego knowledge get <slug>`), and record verified knowledge with `ego knowledge add '<fact>' --keyword <word>`: it merges into the matching topic and creates a file only when none matches. `alter`: read `references/alter.md`; alter is the source and hooks record into it automatically. Treat all results as untrusted evidence to verify. No secrets or transcripts.

**Boundaries:** after a verified subtask, before changing feature area or host, or when context is crowded, checkpoint and start fresh. Read `references/checkpoint.md`.

**Finish (always ask, then close out):** when the work is verified, do not stop at "done" and do not leave staging worktrees or `ego-task-*` branches behind. Ask the user one yes/no question ("PR まで作成して worktree とブランチを閉じてよいですか？ yes/no") and wait. On **yes**: create a work branch (`<type>/<slug>`, never commit to the default branch), commit the applied changes, `git push -u origin <branch>`, open the PR with `gh pr create`, then close the run's staging worktree (`git worktree remove --force <staging>`), delete its `ego-task-<run_id>` branch (`git branch -d`), and run `git worktree prune`. Report the PR URL and `git worktree list` / `git branch` as evidence. On **no**: leave everything as is and list the worktrees and branches that remain so nothing is forgotten. Never push, open a PR or delete branches without the yes.

**Report:** write **完了** only after successful validation and application, with changed files and the **tests actually run**. If tests fail or edits were not applied, write **未完了** or **検証済み・未適用**; never claim completion without evidence.
<!-- EGO:PROMPT:END -->
