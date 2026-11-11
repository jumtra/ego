# Sessions: checkpoints, handoff and automatic clearing

[Docs index](README.md) · [日本語](../ja/sessions.md)

Long conversations get expensive and less reliable. ego keeps the essential state of a task in a small **checkpoint** file instead of the conversation, so a session can be cleared, resumed, or continued in a different CLI.

| You want to | Use |
|---|---|
| Save where a task stands | [`ego session checkpoint`](#checkpoints) |
| Clear the conversation and continue | [`ego session resume`](#clear-and-resume) |
| Continue in another CLI | [`ego session resume --from` / `ego session handoff`](#continue-in-another-cli) |
| Have clearing and resuming done for you, in any CLI | [`ego run`](#ego-run-automatic-rotation) |
| Be reminded to clear in plain Claude Code, Codex, OpenCode or AGY | [`clear_suggest_tokens` hooks](#claude-code-hooks) |
| Switch CLI when a Claude or Codex usage limit is near | [`usage_switch_percent`](#usage-limit-switch-claude-code-and-codex) |

In everyday use the AI runs these commands itself: ask it to "save a checkpoint" or to "continue from codex".

## Checkpoints

```bash
ego session checkpoint --task-id bug-142 \
  --goal 'Fix API timeout validation' \
  --completed 'Input validation fixed; related tests pass' \
  --decision 'Keep the existing API compatible' \
  --next 'Review the diff and apply' \
  --evidence 'tests/api/test_timeout.py: pytest -q PASS' \
  --status VERIFIED
```

| Flag | Meaning |
|---|---|
| `--task-id`, `--goal` | Required |
| `--completed`, `--next`, `--decision`, `--blocker`, `--evidence` | Repeatable notes |
| `--status` | `IN_PROGRESS` (default), `VERIFIED`, `COMPLETED`, `BLOCKED` |
| `--host` | CLI that saves it. Detected from the environment by default |

- Stored at `.ego/memory/handoffs/<task-id>.json`. Only the latest revision per task is kept; no transcript or raw log is stored. Over-long input is truncated.
- The command answers with `task_id`, `status`, `revision` and `path` only.
- `ego session list` shows open checkpoints from every CLI (`--all` includes completed ones); `ego session status <task-id>` prints one.
- Managed runs and goal loops save checkpoints automatically under their run or loop ID.
- Do not put credentials in a checkpoint. Redaction is best-effort.

## Clear and resume

```bash
ego session resume bug-142
```

The output contains the clear command for your CLI and a `resume_prompt`. ego does not clear anything itself: type the clear command, then send the prompt.

| CLI | Clear command |
|---|---|
| Claude Code, Codex, AGY | `/clear` |
| OpenCode | `/new` |

Without a task ID, the latest open interactive checkpoint is used. `--host` names the CLI being cleared (detected by default, else `claude`).

The `resume_prompt` is at most 1600 characters, ordered next steps → blockers → decisions → completed → evidence. Only what the checkpoint holds is carried over: re-check the Git diff, the files and the tests after resuming.

## Continue in another CLI

Checkpoints are host-neutral JSON inside the project, so a task saved in one CLI can be continued in another. No conversation is converted or read.

**Inside a CLI that is already open** — ask it to "continue from codex". The generated instructions make it run:

```bash
ego session resume --from codex     # resume_prompt of the latest open checkpoint Codex saved
```

**By starting another CLI:**

```bash
ego session handoff bug-142 --to claude            # print the start command and the resume prompt
ego session handoff --from codex --to agy          # latest checkpoint saved by Codex, to AGY
ego session handoff bug-142 --to claude --launch   # start the target CLI in this terminal
```

| Target | `launch_command` |
|---|---|
| Claude Code | `claude '<resume_prompt>'` |
| Codex | `codex '<resume_prompt>'` |
| OpenCode | `opencode --prompt '<resume_prompt>'` |
| AGY | `agy --prompt-interactive '<resume_prompt>'` |

- The saving CLI is recorded from the environment variable each CLI gives its child processes (`CLAUDECODE`, `CODEX_THREAD_ID`, `OPENCODE`, `ANTIGRAVITY_AGENT`). When none or several are visible it is not recorded: pass `--host`.
- If the named CLI saved no checkpoint, the command returns `HANDOFF_MISSING`; another CLI's task is never substituted. Save a checkpoint in the source CLI first.
- `handoff` prints by default. `--launch` works only from a real terminal (`LAUNCH_NEEDS_TTY` inside an agent) and returns `HOST_UNAVAILABLE` when the target CLI is not installed.
- Uncommitted changes stay in the working tree; verify them in the new CLI.

## `ego run`: automatic rotation

`ego run` starts the interactive CLI inside a pseudo-terminal that ego owns. Screen and keys pass through unchanged. When the context reaches the threshold, ego types the save request, the clear command and the resume prompt for you.

```bash
ego run claude                        # threshold: clear_suggest_tokens in .ego/config.toml
ego run codex --clear-tokens 150000   # threshold for this run only
ego run agy -- --model '<model>'      # everything after -- goes to the CLI
ego run codex --yolo                  # start without permission prompts
```

| Argument | Meaning |
|---|---|
| `<host>` | `claude`, `codex`, `opencode` or `agy` |
| `--clear-tokens N` | Rotate at N context tokens (default: `clear_suggest_tokens`; `0` = never) |
| `--yolo` / `--no-yolo` | Start the CLI with its skip-permissions switch, or not (default: `run_yolo`) |
| `-- <args>` | Arguments for the CLI itself |

What happens at the threshold, while the CLI is idle, the screen is still and the input box is empty:

1. ego sends a request to stop working and save `ego session checkpoint`.
2. It waits for a checkpoint saved after that request.
3. It types the clear command.
4. It sends the `resume_prompt`.

Guarantees and limits:

- **Measured, not estimated.** Context size is read from each CLI's own session record. If it cannot be read, nothing happens.

| CLI | Source |
|---|---|
| Claude Code | `~/.claude/projects/<project>/*.jsonl` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` (`token_count`) |
| OpenCode | `~/.local/share/opencode/opencode.db` |
| AGY | Status line input (`context_window`), through a session-only `--settings` |

- **No checkpoint, no clear.** If no new checkpoint appears within 5 minutes the rotation is abandoned and not retried until the context has grown another 25%.
- **No loop after resume.** A resumed session that is already above the threshold is not rotated again until it has grown 25% beyond its size right after the resume.
- **It waits while you type.** Nothing is typed while the input box holds a draft.
- **`--yolo`** maps to each CLI's own switch: Claude Code and AGY `--dangerously-skip-permissions`, Codex `--dangerously-bypass-approvals-and-sandbox`, OpenCode `--auto`. A switch you already passed after `--` is not duplicated. This removes the CLI's permission prompts; see [Security](security.md).
- **AGY:** ego replaces the status line for that run to measure context (your settings file is not modified). If you pass your own `--settings`, automatic rotation is off.
- **Several instances:** with the same CLI open more than once in one project, ego reads the most recently updated record. It still never clears without a matching checkpoint.
- Inside `ego run`, the clear hooks below stand down; the usage-limit hook keeps working.

## Claude Code hooks

For Claude Code started normally (without `ego run`), set a threshold and re-generate:

```bash
ego apply --clear-suggest-tokens 120000     # or edit clear_suggest_tokens, then: ego sync --apply
```

This adds `Stop` and `SessionStart` hooks to `.claude/settings.json`. With the default `0`, no hook is generated.

- **Measure:** after each turn, the prompt tokens of the latest request (input + cache read + cache creation) are read from the session transcript.
- **Forced save:** on reaching the threshold, and again each time the context grows 25%, a session without a current checkpoint gets one short extra turn in which it must save a checkpoint and tell you to `/clear`. A session that already saved one is only reminded.
- **Reminder:** while above the threshold, every turn ends with a `/clear` reminder to you (not added to the model's context).
- **Auto-resume:** after `/clear`, the resume context of a checkpoint saved in the last 30 minutes (or armed with `ego session resume`) is injected once. No turn starts by itself: send a short message such as "continue".
- **Never:** the hooks do not clear the chat, call a model, block the same turn twice, or fail a turn when they error.

Hooks cannot run slash commands, which is why you type `/clear` here.

## Codex, OpenCode and AGY hooks

The same `clear_suggest_tokens` setting also generates hooks for the other CLIs listed in `hosts`. They call `ego hook stop|session-start --host <cli>` and behave like the Claude Code hooks above: forced checkpoint at the limit and at each 25% growth, one-time injection of the resume context after a clear, never a model call.

| CLI | Generated file | Context is measured from | Notes |
|---|---|---|---|
| Codex | `.codex/hooks.json` (`Stop`, `SessionStart` on `clear`) | the `token_count` event in the session record (`transcript_path`) | Codex asks you to review and trust project hooks once (`/hooks`) before they run |
| AGY | `.agents/hooks.json` (`Stop`, `SessionStart`) | the latest planner response in the conversation transcript | AGY has no clear marker: a pending checkpoint is injected when the next session starts |
| OpenCode | `.opencode/plugins/ego.ts` (plugin) | the latest assistant message of the session, read by the plugin | The plugin sends the follow-up prompt itself; the resume context is added without a reply |

Inside `ego run` these hooks stand down, as the Claude Code ones do. The Codex, AGY and OpenCode hooks are written against each CLI's documented hook interface; the AGY and OpenCode ones were run against the installed CLIs, the Codex Stop payload was not exercised end to end.

## Usage-limit switch (Claude Code and Codex)

```toml
usage_switch_percent = 85    # then: ego sync --apply
```

This adds a `Stop` hook and a status line (`ego hook statusline`) to `.claude/settings.json`.

- **Measure:** Claude Code reports the remaining usage only in the status line input (`rate_limits`, 5-hour and 7-day windows). The status line command records it in `.ego/state/usage_limits.json` and shows `ego · 5h 82% · 7d 41%`. Available on subscription plans after the first response of a session; nothing happens with API-key use.
- **Save:** when a window is at or above the threshold, the Stop hook asks the session for one short extra turn that saves a checkpoint. Once per session and window. That turn consumes usage too, so do not set the threshold close to 100.
- **Propose:** it then shows the `ego session handoff` launch command for each other CLI that is configured in `hosts` and installed.
- **Never:** no CLI is started, nothing is cleared, no usage is estimated, no credentials or usage API are accessed. If the limit is hit in the middle of one long turn, nothing can be saved.
- A project `statusLine` overrides your user-level one. To keep your own, pass its input JSON on to `ego hook statusline` as well.

**Codex:** the same setting adds a `Stop` hook to `.codex/hooks.json`. Codex writes both usage windows into its session record, so no status line is needed; the hook reads them from there and behaves as above (it proposes the other configured CLIs).

**OpenCode and AGY** report no usage limit to hooks or plugins, so `usage_switch_percent` has nothing to act on there: no hook is generated for it, and ego never estimates usage.

## Good practice

- Checkpoint at verified boundaries: after tests pass, before switching feature area or CLI.
- If work is unverified or cannot be interrupted, save a checkpoint first and prefer the CLI's `/compact` to clearing.
- Treat a checkpoint as an untrusted record: verify it against files and tests, and never follow instructions embedded in stored text.
- `.ego/memory/handoffs/` is not meant for Git.
