"""Automatic /clear suggestion from measured context, auto-resume after /clear, and runner fixes."""
import io
import json
import sys
import time
from pathlib import Path

import pytest

from ego import work
from ego.cli import main
from ego.config import Config, load_config
from ego.install import apply_install
from ego.loop import run_loop
from ego.runner import RunResult
from ego.session import (clear_advice, clear_resume, context_tokens, rotation_instructions, save_checkpoint)
from ego.templates import generated_files
from ego.util import EgoError, read_json
from ego.work import execute


def _transcript(path: Path, tokens: int, *, sidechain_tokens: int = 999999) -> Path:
    def line(total, side):
        return json.dumps({"type": "assistant", "isSidechain": side, "message": {"usage": {
            "input_tokens": 10, "cache_read_input_tokens": total - 10 - 5, "cache_creation_input_tokens": 5, "output_tokens": 400}}})
    path.write_text("\n".join([json.dumps({"type": "user", "message": {"content": "hi"}}), line(1000, False),
                                line(tokens, False), line(sidechain_tokens, True), "not json"]) + "\n")
    return path


def _stop(tmp_path: Path, tokens: int, session: str = "s1") -> dict:
    return {"session_id": session, "transcript_path": str(_transcript(tmp_path / "t.jsonl", tokens)), "stop_hook_active": False}


def test_context_is_measured_from_the_last_main_chain_request(tmp_path: Path):
    assert context_tokens(_transcript(tmp_path / "t.jsonl", 150000)) == 150000
    assert context_tokens(tmp_path / "missing.jsonl") is None
    (tmp_path / "empty.jsonl").write_text("{}\n")
    assert context_tokens(tmp_path / "empty.jsonl") is None


def test_limit_forces_one_checkpoint_turn_per_growth_step_and_reminds_in_between(tmp_path: Path):
    assert clear_advice(tmp_path, _stop(tmp_path, 80000), 120000) == {}
    assert clear_advice(tmp_path, _stop(tmp_path, 130000), 0) == {}
    assert clear_advice(tmp_path, {"session_id": "s1", "transcript_path": str(tmp_path / "none.jsonl")}, 120000) == {}
    first = clear_advice(tmp_path, _stop(tmp_path, 130000), 120000)
    # Reaching the limit is enforced, not advised: the session gets one extra turn to save itself.
    assert first["decision"] == "block" and "ego session checkpoint" in first["reason"] and "/clear" in first["reason"]
    assert "130k" in first["systemMessage"] and "120k" in first["systemMessage"]
    # That extra turn ending is never blocked again; above the limit every turn reminds the user.
    ending = clear_advice(tmp_path, {**_stop(tmp_path, 131000), "stop_hook_active": True}, 120000)
    assert set(ending) == {"systemMessage"} and "/clear" in ending["systemMessage"]
    reminder = clear_advice(tmp_path, _stop(tmp_path, 140000), 120000)
    assert set(reminder) == {"systemMessage"} and "140k" in reminder["systemMessage"]
    assert clear_advice(tmp_path, _stop(tmp_path, 170000), 120000)["decision"] == "block"
    assert clear_advice(tmp_path, _stop(tmp_path, 130000, session="other"), 120000)["decision"] == "block"
    # After compaction drops below the limit, a later crossing is enforced again.
    assert clear_advice(tmp_path, _stop(tmp_path, 40000), 120000) == {}
    assert clear_advice(tmp_path, _stop(tmp_path, 125000), 120000)["decision"] == "block"


def test_a_turn_another_hook_extended_does_not_use_up_the_forced_save(tmp_path: Path):
    extended = clear_advice(tmp_path, {**_stop(tmp_path, 130000), "stop_hook_active": True}, 120000)
    assert set(extended) == {"systemMessage"}
    assert clear_advice(tmp_path, _stop(tmp_path, 130000), 120000)["decision"] == "block"


def test_growth_step_needs_a_checkpoint_newer_than_the_previous_step(tmp_path: Path):
    save_checkpoint(tmp_path, "t1", goal="g", status="IN_PROGRESS")
    saved = clear_advice(tmp_path, _stop(tmp_path, 130000), 120000)
    assert set(saved) == {"systemMessage"} and "t1" in saved["systemMessage"]  # already saved: no extra turn
    # 25% more context since then is not in that checkpoint: save again.
    assert clear_advice(tmp_path, _stop(tmp_path, 170000), 120000)["decision"] == "block"
    save_checkpoint(tmp_path, "t1", goal="g", completed=["more"], status="IN_PROGRESS")
    after = clear_advice(tmp_path, {**_stop(tmp_path, 171000), "stop_hook_active": True}, 120000)
    assert set(after) == {"systemMessage"} and "t1" in after["systemMessage"]


def test_fresh_checkpoint_makes_clear_resume_automatic_exactly_once(tmp_path: Path):
    save_checkpoint(tmp_path, "bug-142", goal="Fix timeout validation", completed=["validation fixed"],
                    next_steps=["review diff"], evidence=["pytest -q PASS"], status="VERIFIED")
    save_checkpoint(tmp_path, "done-1", goal="Finished work", status="COMPLETED")
    message = clear_advice(tmp_path, _stop(tmp_path, 150000), 120000)["systemMessage"]
    assert "bug-142" in message and "VERIFIED" in message and "/clear" in message
    assert clear_resume(tmp_path, {"source": "startup"}) == {}
    injected = clear_resume(tmp_path, {"source": "clear"})
    context = injected["hookSpecificOutput"]["additionalContext"]
    assert injected["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Resume task bug-142" in context and "review diff" in context and len(context) <= 2400
    # The host adds this context silently and starts no turn: the user must be told to send a message.
    assert "続けて" in message
    assert "bug-142" in injected["systemMessage"] and "続けて" in injected["systemMessage"]
    assert clear_resume(tmp_path, {"source": "clear"}) == {}


def test_cli_checkpoint_then_clear_injects_resume_without_stop_hook(tmp_path: Path, capsys):
    # Manual flow: "save checkpoint" (ego session checkpoint) then /clear. No Stop hook fires in between.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        '{"hooks": {"SessionStart": [{"matcher": "clear", "hooks": [{"type": "command", '
        '"command": "ego hook session-start --root \\"$CLAUDE_PROJECT_DIR\\""}]}]}}')
    assert main(["session", "checkpoint", "--root", str(tmp_path), "--task-id", "manual-9",
                 "--goal", "Ship token report", "--completed", "pytest exit 0", "--status", "VERIFIED"]) == 0
    capsys.readouterr()
    injected = clear_resume(tmp_path, {"source": "clear"})
    assert "Resume task manual-9" in injected["hookSpecificOutput"]["additionalContext"]


def test_stale_pending_resume_is_not_injected_into_an_unrelated_session(tmp_path: Path):
    save_checkpoint(tmp_path, "old-task", goal="Earlier work", status="IN_PROGRESS")
    (tmp_path / ".ego/state").mkdir(parents=True)
    (tmp_path / ".ego/state/clear_pending.json").write_text(json.dumps({"task_id": "old-task", "at": time.time() - 7200}))
    assert clear_resume(tmp_path, {"source": "clear"}) == {}
    assert not (tmp_path / ".ego/state/clear_pending.json").exists()


def test_session_resume_arms_auto_resume_only_when_the_hook_is_installed(tmp_path: Path):
    save_checkpoint(tmp_path, "t1", goal="g", status="IN_PROGRESS")
    manual = rotation_instructions(tmp_path, "t1", "claude")
    assert manual["auto_resume_after_clear"] is False and "then send resume_prompt" in manual["instructions"]
    assert not (tmp_path / ".ego/state/clear_pending.json").exists()
    cfg = Config(); cfg.clear_suggest_tokens = 120000
    apply_install(tmp_path, cfg)
    armed = rotation_instructions(tmp_path, "t1", "claude")
    assert armed["auto_resume_after_clear"] is True and "injected automatically" in armed["instructions"]
    assert list(read_json(tmp_path / ".ego/state/clear_pending.json")["tasks"]) == ["t1"]
    assert rotation_instructions(tmp_path, "t1", "codex")["auto_resume_after_clear"] is True  # the default hosts include Codex
    assert rotation_instructions(tmp_path, "t1", "opencode")["auto_resume_after_clear"] is True


def test_hooks_are_generated_only_on_request_and_call_the_installed_command(project: Path):
    assert ".claude/settings.json" not in generated_files(Config(clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0, memory="file"))
    cfg = Config(memory="file"); cfg.clear_suggest_tokens = 120000
    hooks = json.loads(generated_files(cfg)[".claude/settings.json"])["hooks"]
    assert set(hooks) == {"Stop", "SessionStart"} and hooks["SessionStart"][0]["matcher"] == "startup|resume|clear"
    assert hooks["Stop"][0]["hooks"][0]["command"] == 'ego hook stop --root "$CLAUDE_PROJECT_DIR"'
    cfg.profile = "full"
    full = json.loads(generated_files(cfg)[".claude/settings.json"])["hooks"]
    assert full["PreToolUse"][0]["hooks"][0]["command"] == "ego hook pre-tool --root ."
    (project / ".ego/config.toml").write_text("clear_suggest_tokens = 5\n")
    with pytest.raises(EgoError):
        load_config(project)


def test_auto_resume_is_on_by_default_without_clear_threshold(project: Path):
    assert Config().auto_resume is True
    cfg = Config(clear_suggest_tokens=0)  # SessionStart(clear) must not depend on the context threshold
    hooks = json.loads(generated_files(cfg)[".claude/settings.json"])["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == 'ego hook session-start --root "$CLAUDE_PROJECT_DIR"'
    assert "session-start" in generated_files(cfg)[".codex/hooks.json"]
    assert "session-start" in generated_files(cfg)[".agents/hooks.json"]


def test_hook_cli_never_fails_the_host_turn(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("clear_suggest_tokens = 120000\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_stop(tmp_path, 200000))))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    assert "200k" in json.loads(capsys.readouterr().out)["systemMessage"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "clear"})))
    assert main(["hook", "session-start", "--root", str(tmp_path)]) == 0
    # memory defaults to alter, so session start always carries the alter_bootstrap requirement.
    out = json.loads(capsys.readouterr().out)
    assert "alter_bootstrap" in out["hookSpecificOutput"]["additionalContext"]
    # AGY gets the requirement as an injected step, without a "checkpoint was loaded" announcement.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "clear"})))
    assert main(["hook", "session-start", "--host", "agy", "--root", str(tmp_path)]) == 0
    step = json.loads(capsys.readouterr().out)["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "alter_bootstrap" in step and "checkpoint was loaded" not in step
    (tmp_path / ".ego/config.toml").write_text('memory = "file"\n')
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "clear"})))
    assert main(["hook", "session-start", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_merge_conflict_does_not_leave_the_run_marked_running(tmp_path: Path):
    (tmp_path / "w.py").write_text("from pathlib import Path\nimport os\nPath('same.txt').write_text(str(os.getpid()))\n")
    spec = {"goal": "g", "nodes": [{"id": n, "kind": "code", "argv": ["python3", "w.py"], "write_allowlist": ["same.txt"]} for n in ("a", "b")]}
    with pytest.raises(EgoError) as conflict:
        execute(tmp_path, spec, run_id="run-conflict")
    assert conflict.value.code == "MERGE_CONFLICT"
    assert read_json(tmp_path / ".ego/state/runs/run-conflict/record.json")["final_status"] == "MERGE_CONFLICT"


def test_record_keeps_bounded_worker_output(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(work, "run_agent", lambda host, prompt, cwd, timeout=180, **kw:
                        RunResult("PASS", "x" * 500000 + " END", "", 0, 0.0, host, cost_usd=0.0))
    spec = {"goal": "g", "nodes": [{"id": "impl", "kind": "agent", "prompt": "p", "host": "claude", "write_allowlist": ["src/**"]}]}
    result = execute(tmp_path, spec, host="claude", trust_cli=True, run_id="run-big")
    assert result["final_status"] == "PASSED"
    stored = result["results"]["impl"]
    assert len(stored["output"]) <= 20000 and stored["output"].endswith("END")
    assert len(stored["attempts"][0]["execution"]["output"]) <= 20000
    assert (tmp_path / ".ego/state/runs/run-big/record.json").stat().st_size < 120000


def test_direct_loop_iteration_runs_the_done_check_once(tmp_path: Path, monkeypatch):
    counter = tmp_path.parent / (tmp_path.name + "-count.txt")
    (tmp_path / "check.py").write_text(f"open({str(counter)!r}, 'a').write('x')\n")
    monkeypatch.setattr(work, "run_agent", lambda host, prompt, cwd, timeout=180, **kw:
                        RunResult("PASS", "ok", "", 0, 0.0, host, cost_usd=0.0))
    result = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], host="claude", trust_cli=True)
    assert result["status"] == "VERIFIED"
    assert counter.read_text() == "x"


def test_parallel_sessions_never_receive_each_others_checkpoint(tmp_path: Path, monkeypatch):
    from ego.session import arm_auto_resume
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("ego hook session-start")
    for task, session in (("taskA", "sess-a"), ("taskB", "sess-b")):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
        save_checkpoint(tmp_path, task, goal=f"{task} work", next_steps=[f"{task} next"])
        assert arm_auto_resume(tmp_path, task, "IN_PROGRESS") is True
    # Two are pending: a /clear in either terminal names both and injects neither task's content.
    both = clear_resume(tmp_path, {"source": "clear"})
    context = both["hookSpecificOutput"]["additionalContext"]
    assert "taskA, taskB" in context and "taskA next" not in context and "taskB next" not in context
    assert "taskA, taskB" in both["systemMessage"]
    # Each session's Stop hook offers only the checkpoint that session saved.
    assert "taskA" in clear_advice(tmp_path, _stop(tmp_path, 150000, session="sess-a"), 120000)["systemMessage"]
    third = clear_advice(tmp_path, _stop(tmp_path, 150000, session="sess-c"), 120000)
    assert "taskA" not in str(third) and "taskB" not in str(third) and third["decision"] == "block"
    # Finishing one task leaves a single candidate, which is then injected exactly once.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-b")
    save_checkpoint(tmp_path, "taskB", goal="taskB work", status="COMPLETED")
    assert arm_auto_resume(tmp_path, "taskB", "COMPLETED") is False
    only = clear_resume(tmp_path, {"source": "clear"})
    assert "Resume task taskA" in only["hookSpecificOutput"]["additionalContext"]
    assert clear_resume(tmp_path, {"source": "clear"}) == {}
    assert not (tmp_path / ".ego/state/clear_pending.json").exists()


def test_concurrent_checkpoints_and_arming_lose_nothing(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor
    from ego.session import _mark_clear_pending, _pending_tasks, load_checkpoint
    def work(i):
        save_checkpoint(tmp_path, f"par-{i % 4}", goal=f"goal {i}", next_steps=[f"step {i}"])
        _mark_clear_pending(tmp_path, f"par-{i % 4}", f"s{i % 4}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(40)))
    assert sorted(_pending_tasks(tmp_path)) == ["par-0", "par-1", "par-2", "par-3"]
    # Same-ID writers serialize: every save is counted and the file is always one whole record.
    assert [load_checkpoint(tmp_path, f"par-{k}")["revision"] for k in range(4)] == [10, 10, 10, 10]


def _status(root: Path, five: float | None, seven: float | None = None, resets: float | None = None) -> dict:
    import time
    resets = resets or time.time() + 3600
    limits = {}
    if five is not None: limits["five_hour"] = {"used_percentage": five, "resets_at": resets}
    if seven is not None: limits["seven_day"] = {"used_percentage": seven, "resets_at": resets + 86400}
    return {"session_id": "s1", "cwd": str(root), "workspace": {"project_dir": str(root)}, "rate_limits": limits}


def test_usage_limit_is_recorded_only_from_the_host_report(tmp_path: Path):
    from ego.session import record_usage, usage_advice
    (tmp_path / ".ego").mkdir()
    assert record_usage({"workspace": {"project_dir": str(tmp_path)}}) == ""  # API key / before first response
    assert not (tmp_path / ".ego/state/usage_limits.json").exists()
    assert usage_advice(tmp_path, {"session_id": "s1"}, 85, ["claude", "codex"]) == {}  # unmeasured: silent
    assert record_usage(_status(tmp_path, 41.6, 12)) == "ego · 5h 42% · 7d 12%"
    assert read_json(tmp_path / ".ego/state/usage_limits.json")["rate_limits"]["five_hour"]["used_percentage"] == 41.6
    assert usage_advice(tmp_path, {"session_id": "s1"}, 85, ["claude", "codex"]) == {}  # below threshold
    import time
    assert record_usage(_status(tmp_path, 99, resets=time.time() - 5)) == ""  # window already reset: dropped


def test_near_usage_limit_asks_for_a_checkpoint_and_proposes_installed_clis_once(tmp_path: Path, monkeypatch):
    from ego.session import record_usage, usage_advice
    (tmp_path / ".ego").mkdir()
    monkeypatch.setattr("ego.session.shutil.which", lambda name: f"/bin/{name}" if name in ("codex", "agy") else None)
    record_usage(_status(tmp_path, 91.2, 40))
    hosts = ["claude", "codex", "opencode", "agy"]
    assert usage_advice(tmp_path, {"session_id": "s1"}, 0, hosts) == {}  # off by default
    assert usage_advice(tmp_path, {"session_id": "s1", "stop_hook_active": True}, 85, hosts) == {}  # never loops
    out = usage_advice(tmp_path, {"session_id": "s1"}, 85, hosts)
    assert out["decision"] == "block" and "ego session checkpoint" in out["reason"]
    assert "ego session handoff" in out["reason"] and "(codex, agy)" in out["reason"] and "opencode" not in out["reason"]
    assert "91%" in out["systemMessage"] and "5時間枠" in out["systemMessage"]
    assert usage_advice(tmp_path, {"session_id": "s1"}, 85, hosts) == {}  # once per session and window
    assert usage_advice(tmp_path, {"session_id": "s2"}, 85, hosts)["decision"] == "block"  # other sessions save too
    # A new window (after the reset) can trigger again; the weekly window counts as well.
    record_usage(_status(tmp_path, 10, 97))
    again = usage_advice(tmp_path, {"session_id": "s1"}, 85, hosts)
    assert "7日枠" in again["systemMessage"] and "seven_day 97%" in again["reason"]
    # Nothing else installed: no switch target is invented.
    monkeypatch.setattr("ego.session.shutil.which", lambda name: None)
    alone = usage_advice(tmp_path, {"session_id": "s3"}, 85, hosts)
    assert "ego session handoff" not in alone["reason"] and "after the limit resets" in alone["reason"]
    # A stale reading is not acted on.
    state = read_json(tmp_path / ".ego/state/usage_limits.json"); state["at"] -= 3600
    (tmp_path / ".ego/state/usage_limits.json").write_text(json.dumps(state))
    assert usage_advice(tmp_path, {"session_id": "s4"}, 85, hosts) == {}


def test_usage_switch_hooks_and_statusline_cli(tmp_path: Path, monkeypatch, capsys):
    cfg = Config(clear_suggest_tokens=0, auto_resume=False, memory="file"); cfg.usage_switch_percent = 85
    settings = json.loads(generated_files(cfg)[".claude/settings.json"])
    assert set(settings["hooks"]) == {"Stop"}  # no SessionStart(clear) hook when auto_resume is off
    assert settings["statusLine"] == {"type": "command", "command": "ego hook statusline"}
    cfg.usage_switch_percent = 30
    with pytest.raises(EgoError):
        cfg.validate()
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("usage_switch_percent = 85\nclear_suggest_tokens = 120000\n")
    monkeypatch.chdir(tmp_path.parent)  # the status line command gets its project from the input, not the cwd
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_status(tmp_path, 93))))
    assert main(["hook", "statusline"]) == 0
    assert capsys.readouterr().out.strip() == "ego · 5h 93%"
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert main(["hook", "statusline"]) == 0 and capsys.readouterr().out.strip() == ""
    # The usage limit outranks the context-size suggestion on the same Stop.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_stop(tmp_path, 200000))))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    # The extra turn that usage block asked for is ending: the context limit only reminds, it cannot block again.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({**_stop(tmp_path, 200000), "stop_hook_active": True})))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "decision" not in out and "/clear" in out["systemMessage"]


# --- Codex / OpenCode / AGY hooks -------------------------------------------------------------

def _rollout(path: Path, tokens: int, *, five: float = 10.0, week: float = 20.0) -> Path:
    event = {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": tokens}},
             "rate_limits": {"primary": {"used_percent": five, "window_minutes": 300, "resets_at": time.time() + 3600},
                             "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": time.time() + 86400}}}}
    path.write_text(json.dumps({"type": "session_meta"}) + "\n" + json.dumps(event) + "\n")
    return path


def _agy_transcript(path: Path, tokens: int) -> Path:
    path.write_text(json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT"}) + "\n" +
                    json.dumps({"type": "PLANNER_RESPONSE", "source": "MODEL", "input_tokens": tokens - 100, "cache_read_tokens": 100}) + "\n")
    return path


def _hook(monkeypatch, capsys, tmp_path: Path, event: str, host: str, payload: dict) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook", event, "--host", host, "--root", str(tmp_path)]) == 0
    return json.loads(capsys.readouterr().out)


def _project(tmp_path: Path, **settings) -> Path:
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("".join(f"{k} = {v}\n" for k, v in settings.items()))
    return tmp_path


def test_context_is_measured_from_codex_and_agy_records(tmp_path: Path):
    assert context_tokens(_rollout(tmp_path / "r.jsonl", 150000), host="codex") == 150000
    assert context_tokens(_agy_transcript(tmp_path / "a.jsonl", 90000), host="agy") == 90000
    assert context_tokens(tmp_path / "missing.jsonl", host="codex") is None


def test_codex_stop_hook_asks_for_a_checkpoint_at_the_context_limit(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, clear_suggest_tokens=100000)
    rollout = _rollout(tmp_path / "r.jsonl", 150000)
    out = _hook(monkeypatch, capsys, tmp_path, "stop", "codex",
                {"session_id": "s1", "transcript_path": str(rollout), "cwd": str(tmp_path), "stop_hook_active": False})
    assert out["decision"] == "block" and "clear_suggest_tokens = 100000" in out["reason"]
    again = _hook(monkeypatch, capsys, tmp_path, "stop", "codex",
                  {"session_id": "s1", "transcript_path": str(rollout), "cwd": str(tmp_path), "stop_hook_active": True})
    assert "decision" not in again


def test_codex_stop_hook_switches_when_the_reported_usage_limit_is_nearly_spent(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, usage_switch_percent=85)
    low = _rollout(tmp_path / "low.jsonl", 1000, five=40, week=50)
    assert _hook(monkeypatch, capsys, tmp_path, "stop", "codex", {"session_id": "s", "transcript_path": str(low)}) == {}
    high = _rollout(tmp_path / "high.jsonl", 1000, five=91, week=50)
    out = _hook(monkeypatch, capsys, tmp_path, "stop", "codex", {"session_id": "s", "transcript_path": str(high)})
    assert out["decision"] == "block" and "Codex usage limit" in out["reason"] and "five_hour 91%" in out["reason"]


def test_agy_stop_hook_uses_its_own_output_format(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, clear_suggest_tokens=100000)
    transcript = _agy_transcript(tmp_path / "a.jsonl", 150000)
    payload = {"conversationId": "c1", "transcriptPath": str(transcript), "cwd": str(tmp_path), "hookEventName": "Stop"}
    out = _hook(monkeypatch, capsys, tmp_path, "stop", "agy", payload)
    assert out["decision"] == "continue" and "ego session checkpoint" in out["reason"]
    assert _hook(monkeypatch, capsys, tmp_path, "stop", "agy", {**payload, "continuationActive": True}) == {}
    assert _hook(monkeypatch, capsys, tmp_path, "stop", "agy", {**payload, "conversationId": "c2", "executionNum": 1}) == {}  # AGY reports continuationActive only sometimes


def test_opencode_plugin_reports_its_measured_context(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, clear_suggest_tokens=100000)
    out = _hook(monkeypatch, capsys, tmp_path, "stop", "opencode", {"session_id": "o1", "context_tokens": 130000})
    assert out["decision"] == "block"
    assert _hook(monkeypatch, capsys, tmp_path, "stop", "opencode", {"session_id": "o2", "context_tokens": 5000}) == {}


def test_agy_and_opencode_have_no_usage_limit_to_act_on(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, usage_switch_percent=60)
    (tmp_path / ".ego/state").mkdir()
    (tmp_path / ".ego/state/usage_limits.json").write_text(json.dumps(
        {"at": time.time(), "rate_limits": {"five_hour": {"used_percentage": 99, "resets_at": time.time() + 600}}}))
    for host, payload in (("agy", {"conversationId": "c"}), ("opencode", {"session_id": "o", "context_tokens": 1})):
        assert _hook(monkeypatch, capsys, tmp_path, "stop", host, payload) == {}  # Claude's reading is not theirs


def test_session_start_injects_the_pending_checkpoint_in_each_hosts_format(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, clear_suggest_tokens=100000)
    for host, payload in (("codex", {"source": "clear"}), ("agy", {"conversationId": "c", "cwd": str(tmp_path)}),
                          ("opencode", {"source": "clear", "session_id": "o"})):
        save_checkpoint(tmp_path, f"t-{host}", goal=f"goal {host}", status="IN_PROGRESS")
        from ego.session import _mark_clear_pending
        _mark_clear_pending(tmp_path, f"t-{host}")
        out = _hook(monkeypatch, capsys, tmp_path, "session-start", host, payload)
        text = json.dumps(out)
        assert f"t-{host}" in text
        if host == "agy":
            assert out["injectSteps"][0]["systemMessage"]["systemMessage"]
        else:
            assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_hook_files_are_generated_for_codex_agy_and_opencode_on_request(tmp_path: Path):
    assert not {".codex/hooks.json", ".agents/hooks.json", ".opencode/plugins/ego.ts"} & set(generated_files(Config(clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0, memory="file")))
    cfg = Config(memory="file"); cfg.clear_suggest_tokens = 120000
    files = generated_files(cfg)
    codex = json.loads(files[".codex/hooks.json"])["hooks"]
    assert codex["Stop"][0]["hooks"][0]["command"] == "ego hook stop --host codex --root ."
    assert codex["SessionStart"][0]["matcher"] == "startup|resume|clear"
    agy = json.loads(files[".agents/hooks.json"])["ego"]
    assert agy["Stop"][0]["command"] == "ego hook stop --host agy --root ." and agy["Stop"][0]["type"] == "command"
    assert "session.idle" in files[".opencode/plugins/ego.ts"] and "ego" in files[".opencode/plugins/ego.ts"]
    usage_only = Config(clear_suggest_tokens=0, auto_resume=False, memory="file"); usage_only.usage_switch_percent = 85
    only = generated_files(usage_only)
    assert set(json.loads(only[".codex/hooks.json"])["hooks"]) == {"Stop"}  # Codex reports its limits
    assert ".agents/hooks.json" not in only and ".opencode/plugins/ego.ts" not in only  # AGY / OpenCode do not
    cfg.hosts = ["claude"]
    assert not {".codex/hooks.json", ".agents/hooks.json", ".opencode/plugins/ego.ts"} & set(generated_files(cfg))


def test_opencode_plugin_pins_the_ego_binary_and_reports_failures(monkeypatch):
    import ego.templates as templates
    monkeypatch.setattr(templates.shutil, "which", lambda name: "/opt/tools/bin/ego" if name == "ego" else None)
    plugin = generated_files(Config(clear_suggest_tokens=200000))[".opencode/plugins/ego.ts"]
    assert 'const EGO_BIN = "/opt/tools/bin/ego"' in plugin and "__EGO_BIN__" not in plugin
    assert 'spawnSync(EGO_BIN, ["hook"' in plugin and "systemMessage" in plugin
    monkeypatch.setattr(templates.shutil, "which", lambda name: None)
    assert 'const EGO_BIN = "ego"' in generated_files(Config(clear_suggest_tokens=200000))[".opencode/plugins/ego.ts"]


def test_a_hook_under_ego_run_records_its_session(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("clear_suggest_tokens = 120000\n")
    bound = tmp_path / ".ego/state/run/1.session.json"
    monkeypatch.setenv("EGO_RUN", "claude")
    monkeypatch.setenv("EGO_RUN_SESSION", str(bound))
    payload = json.dumps({"session_id": "s-9", "transcript_path": "/x/s-9.jsonl"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    record = read_json(bound)
    assert record["session_id"] == "s-9" and record["transcript_path"] == "/x/s-9.jsonl"
    elsewhere = tmp_path / "elsewhere.json"
    monkeypatch.setenv("EGO_RUN_SESSION", str(elsewhere))  # never a file outside this project's run state
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    assert not elsewhere.exists()


def test_checkpoint_under_ego_run_records_the_run(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("EGO_RUN_ID", "r1")
    assert main(["session", "checkpoint", "--root", str(tmp_path), "--task-id", "t1", "--goal", "g"]) == 0
    assert read_json(tmp_path / ".ego/memory/handoffs/t1.json")["run"] == "r1"


def test_clear_resume_ignores_non_clear_sources(tmp_path: Path):
    for source in ("startup", "resume", "compact", None):
        assert clear_resume(tmp_path, {"source": source} if source else {}) == {}


def _fake_alter_usage(monkeypatch, payload):
    from ego import memory

    monkeypatch.delenv("EGO_ALTER_SESSION_RULES", raising=False)
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["alter-mcp"], None))
    monkeypatch.setattr(
        memory.MCPStdio, "call_tool",
        lambda self, name, args: {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]} if name == "alter_ai_usage" else {},
    )


def test_alter_project_rules_are_injected_at_session_start(tmp_path: Path, monkeypatch):
    from ego import session

    _fake_alter_usage(monkeypatch, {"scope": "project/x", "source": "project",
                                    "text": "- 日本語: 日本語で回答する\n- 検証: テストを先に確認する\n-  \nnot a rule"})
    context = session.with_alter_mandate({}, tmp_path)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(session.ALTER_SESSION_MANDATE_INJECTED)
    assert "call `alter_ai_usage`" not in context
    assert "- 日本語: 日本語で回答する" in context and "- 検証: テストを先に確認する" in context
    assert "not a rule" not in context


def test_alter_common_fallback_rules_are_not_applied(tmp_path: Path, monkeypatch):
    from ego import session

    _fake_alter_usage(monkeypatch, {"scope": "project/x", "source": "common_fallback", "text": "- 共通: 共通ルール"})
    assert session.alter_project_rules(tmp_path) == []


def test_alter_project_rules_fail_open(tmp_path: Path, monkeypatch):
    from ego import memory, session

    _fake_alter_usage(monkeypatch, {})
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: None)
    assert session.alter_project_rules(tmp_path) == []
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["alter-mcp"], None))
    monkeypatch.setattr(memory.MCPStdio, "call_tool", lambda self, name, args: (_ for _ in ()).throw(memory.EgoError("MCP_TIMEOUT", name, 5)))
    assert session.alter_project_rules(tmp_path) == []
    assert session.with_alter_mandate({}, tmp_path)["hookSpecificOutput"]["additionalContext"] == session.ALTER_SESSION_MANDATE


def _fake_alter_context(monkeypatch, payload, calls=None):
    from ego import memory

    monkeypatch.delenv("EGO_ALTER_PROMPT_CONTEXT", raising=False)
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["alter-mcp"], None))

    def call(self, name, args):
        if calls is not None:
            calls.append((name, args))
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

    monkeypatch.setattr(memory.MCPStdio, "call_tool", call)


def test_alter_prompt_context_looks_up_evidence_for_the_prompt(tmp_path: Path, monkeypatch):
    from ego import session

    calls: list = []
    _fake_alter_context(monkeypatch, {"evidence": [{"title": "決定A", "excerpt": "\n## 内容\n理由 B", "source_ref": "note:1@r1"}, {"excerpt": "no title"}]}, calls)
    out = session.alter_prompt_context(tmp_path, "/work 過去の決定を探したい")
    assert "- 決定A: 内容 理由 B [note:1@r1]" in out and "no title" not in out
    assert calls == [("alter_task_context", {"task": "過去の決定を探したい", "cwd": str(tmp_path)})]


def test_alter_prompt_context_is_silent_when_nothing_to_say(tmp_path: Path, monkeypatch):
    from ego import memory, session

    calls: list = []
    _fake_alter_context(monkeypatch, {"evidence": []}, calls)
    assert session.alter_prompt_context(tmp_path, "十分に長いプロンプトです") == ""
    assert session.alter_prompt_context(tmp_path, "/clear") == "" and session.alter_prompt_context(tmp_path, "ok") == ""
    assert len(calls) == 1  # the short ones never reach alter
    monkeypatch.setenv("EGO_ALTER_PROMPT_CONTEXT", "0")
    assert session.alter_prompt_context(tmp_path, "十分に長いプロンプトです") == ""
    monkeypatch.delenv("EGO_ALTER_PROMPT_CONTEXT")
    monkeypatch.setattr(memory.MCPStdio, "call_tool", lambda self, name, args: (_ for _ in ()).throw(memory.EgoError("MCP_TIMEOUT", name, 5)))
    assert session.alter_prompt_context(tmp_path, "十分に長いプロンプトです") == ""


def test_alter_memory_uses_the_host_configured_server(tmp_path: Path, monkeypatch):
    from ego import memory

    monkeypatch.delenv("EGO_ALTER_MCP_COMMAND", raising=False)
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["alter-mcp"], {"K": "v"}))
    assert memory.AlterMemory(tmp_path).mcp is not None
    assert memory.memory_health(tmp_path, "alter")["mcp_resolved"] is True
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: None)
    assert memory.AlterMemory(tmp_path).mcp is None
    assert memory.memory_health(tmp_path, "alter")["mcp_resolved"] is False


def test_alter_memory_installs_the_user_prompt_hook(tmp_path: Path):
    from ego.config import Config
    from ego.templates import generated_files as render_files

    files = render_files(Config(memory="alter", hosts=["claude", "codex"], auto_resume=False, clear_suggest_tokens=0, usage_switch_percent=0))
    assert "ego hook user-prompt" in files[".claude/settings.json"] and "ego hook user-prompt" in files[".codex/hooks.json"]
    plain = render_files(Config(memory="file", hosts=["claude"], auto_resume=False, clear_suggest_tokens=0, usage_switch_percent=0))
    assert ".claude/settings.json" not in plain


def test_alter_memory_installs_the_same_hooks_on_every_host(tmp_path: Path):
    from ego.config import Config
    from ego.templates import generated_files as render_files

    files = render_files(Config(memory="alter", hosts=["claude", "codex", "opencode", "agy"], auto_resume=False, clear_suggest_tokens=0, usage_switch_percent=0))
    claude = json.loads(files[".claude/settings.json"])["hooks"]
    codex = json.loads(files[".codex/hooks.json"])["hooks"]
    agy = json.loads(files[".agents/hooks.json"])["ego"]
    assert "session-end" in claude["SessionEnd"][0]["hooks"][0]["command"]
    assert "SessionStart" in claude and "UserPromptSubmit" in claude  # the requirement and rules, then the per-prompt lookup
    assert "SessionStart" in codex and "UserPromptSubmit" in codex
    assert "session-start --host agy" in agy["SessionStart"][0]["command"]
    assert agy["PreInvocation"][0]["command"] == "ego hook user-prompt --host agy --root ."
    plugin = files[".opencode/plugins/ego.ts"]
    assert "const ALTER = true" in plugin and '"chat.message"' in plugin and '"user-prompt"' in plugin
    off = render_files(Config(memory="file", hosts=["opencode"], auto_resume=True, clear_suggest_tokens=0, usage_switch_percent=0))
    assert "const ALTER = false" in off[".opencode/plugins/ego.ts"]
    assert "PreInvocation" not in render_files(Config(memory="file", hosts=["agy"], auto_resume=True))[".agents/hooks.json"]


def test_opencode_plugin_is_valid_typescript_text():
    from ego.config import Config
    from ego.templates import generated_files as render_files

    plugin = render_files(Config(memory="alter", hosts=["opencode"]))[".opencode/plugins/ego.ts"]
    for line in plugin.splitlines():  # a string literal must never be split across lines
        assert line.count('"') % 2 == 0, line


def test_opencode_plugin_covers_resumed_sessions_and_hides_injected_context():
    from ego.config import Config
    from ego.templates import generated_files as render_files

    plugin = render_files(Config(memory="alter", hosts=["opencode"]))[".opencode/plugins/ego.ts"]
    # A resumed session fires no session.created: chat.message injects the requirement (not a checkpoint resume).
    assert 'started.has(sessionID)' in plugin and 'source: "startup"' in plugin
    # Injected context is synthetic, so it is hidden and never looked up as a user prompt again.
    assert plugin.count("synthetic: true") == 2


def test_session_start_for_alter_alone_does_not_resume_a_checkpoint(tmp_path: Path, monkeypatch, capsys):
    from ego import session

    apply_install(tmp_path, Config(memory="alter", auto_resume=False, clear_suggest_tokens=0, usage_switch_percent=0))
    monkeypatch.setattr(session, "clear_resume", lambda root, obj: (_ for _ in ()).throw(AssertionError("resume must stay off")))
    out = _hook(monkeypatch, capsys, tmp_path, "session-start", "claude", {"source": "clear", "cwd": str(tmp_path)})
    assert "alter_bootstrap" in out["hookSpecificOutput"]["additionalContext"]


def _agy_prompts(path: Path, *prompts: str) -> None:
    rows = []
    for i, text in enumerate(prompts):
        rows.append({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": text, "step_index": i * 2})
        rows.append({"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "ok", "step_index": i * 2 + 1})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf8")


def test_agy_prompt_is_handed_out_once_per_user_message(tmp_path: Path):
    from ego import session

    (tmp_path / ".ego").mkdir()
    transcript = tmp_path / "t.jsonl"
    payload = {"session_id": "c1", "transcript_path": str(transcript)}
    assert session.new_transcript_prompt(tmp_path, payload) == ""  # nothing written yet
    _agy_prompts(transcript, "最初の依頼です")
    assert session.new_transcript_prompt(tmp_path, payload) == "最初の依頼です"
    assert session.new_transcript_prompt(tmp_path, payload) == ""  # PreInvocation fires again between tool calls
    _agy_prompts(transcript, "最初の依頼です", "次の依頼です")
    assert session.new_transcript_prompt(tmp_path, payload) == "次の依頼です"
    assert session.new_transcript_prompt(tmp_path, {"session_id": "c2", "transcript_path": str(transcript)}) == "次の依頼です"  # per session
    assert session.new_transcript_prompt(tmp_path, {"transcript_path": str(transcript)}) == ""


def test_agy_user_prompt_hook_injects_alter_evidence_as_a_system_message(tmp_path: Path, monkeypatch, capsys):
    apply_install(tmp_path, Config(memory="alter", clear_suggest_tokens=0, usage_switch_percent=0))
    transcript = tmp_path / "t.jsonl"
    _agy_prompts(transcript, "過去の決定を探したい")
    _fake_alter_context(monkeypatch, {"evidence": [{"title": "決定A", "excerpt": "理由", "source_ref": "note:1@r1"}]})
    payload = ({"conversationId": "c9", "transcriptPath": str(transcript), "cwd": str(tmp_path)})
    for expect in (True, False):  # the second model call of the same prompt adds nothing
        out = _hook(monkeypatch, capsys, tmp_path, "user-prompt", "agy", payload)
        if expect:
            assert "決定A" in out["injectSteps"][0]["systemMessage"]["systemMessage"]
        else:
            assert out == {}
