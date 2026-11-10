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
from ego.util import SEHError, read_json
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


def test_suggestion_fires_once_per_growth_step_and_stays_silent_when_unmeasured(tmp_path: Path):
    assert clear_advice(tmp_path, _stop(tmp_path, 80000), 120000) == {}
    assert clear_advice(tmp_path, _stop(tmp_path, 130000), 0) == {}
    assert clear_advice(tmp_path, {"session_id": "s1", "transcript_path": str(tmp_path / "none.jsonl")}, 120000) == {}
    first = clear_advice(tmp_path, _stop(tmp_path, 130000), 120000)
    assert "130k" in first["systemMessage"] and "チェックポイントを保存して" in first["systemMessage"]
    assert set(first) == {"systemMessage"}  # user-facing only: no block, nothing added to model context
    assert clear_advice(tmp_path, _stop(tmp_path, 140000), 120000) == {}
    assert "170k" in clear_advice(tmp_path, _stop(tmp_path, 170000), 120000)["systemMessage"]
    assert "130k" in clear_advice(tmp_path, _stop(tmp_path, 130000, session="other"), 120000)["systemMessage"]
    # After compaction drops below the threshold, a later crossing suggests again.
    assert clear_advice(tmp_path, _stop(tmp_path, 40000), 120000) == {}
    assert "125k" in clear_advice(tmp_path, _stop(tmp_path, 125000), 120000)["systemMessage"]


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
    assert read_json(tmp_path / ".ego/state/clear_pending.json")["task_id"] == "t1"
    assert rotation_instructions(tmp_path, "t1", "codex")["auto_resume_after_clear"] is False


def test_hooks_are_generated_only_on_request_and_call_the_installed_command(project: Path):
    assert ".claude/settings.json" not in generated_files(Config())
    cfg = Config(); cfg.clear_suggest_tokens = 120000
    hooks = json.loads(generated_files(cfg)[".claude/settings.json"])["hooks"]
    assert set(hooks) == {"Stop", "SessionStart"} and hooks["SessionStart"][0]["matcher"] == "clear"
    assert hooks["Stop"][0]["hooks"][0]["command"] == 'ego hook stop --root "$CLAUDE_PROJECT_DIR"'
    cfg.profile = "full"
    full = json.loads(generated_files(cfg)[".claude/settings.json"])["hooks"]
    assert full["PreToolUse"][0]["hooks"][0]["command"] == "ego hook pre-tool --root ."
    (project / ".ego/config.toml").write_text("clear_suggest_tokens = 5\n")
    with pytest.raises(SEHError):
        load_config(project)


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
    assert json.loads(capsys.readouterr().out) == {}


def test_merge_conflict_does_not_leave_the_run_marked_running(tmp_path: Path):
    (tmp_path / "w.py").write_text("from pathlib import Path\nimport os\nPath('same.txt').write_text(str(os.getpid()))\n")
    spec = {"goal": "g", "nodes": [{"id": n, "kind": "code", "argv": ["python3", "w.py"], "write_allowlist": ["same.txt"]} for n in ("a", "b")]}
    with pytest.raises(SEHError) as conflict:
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
