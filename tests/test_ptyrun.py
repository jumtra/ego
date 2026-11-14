"""`ego run`: context measured from each host's own record, and the save / clear / resume it types."""
import json
import os
import pty
import sqlite3
import sys
import threading
import time
from pathlib import Path

from ego import ptyrun
from ego.cli import main
from ego.ptyrun import InputState, Reading, Rotator, _paste, ctx_label, measure, record_context, run
from ego.session import arm_auto_resume, save_checkpoint
from ego.util import write_json


def _lines(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_claude_is_measured_from_its_transcript(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "my.proj"
    path = tmp_path / "cfg" / "projects" / str(root).replace("/", "-").replace(".", "-").replace("_", "-") / "a.jsonl"
    usage = {"input_tokens": 10, "cache_read_input_tokens": 90000, "cache_creation_input_tokens": 5}
    _lines(path, [{"type": "user", "message": {"content": "hi"}},
                  {"type": "assistant", "message": {"usage": usage, "stop_reason": "tool_use"}}])
    assert measure("claude", root, 0) == Reading(str(path), 90015, False)  # a tool call is pending: not idle
    _lines(path, [{"type": "assistant", "message": {"usage": usage, "stop_reason": "end_turn"}},
                  {"type": "system", "subtype": "turn_duration"}])
    assert measure("claude", root, 0) == Reading(str(path), 90015, True)
    assert measure("claude", root, time.time() + 60) is None  # written before this run started: someone else's


def _two_claude_sessions(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """Sessions a (150k, older) and b (10k, written last) of one project, as two parallel runs leave them."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "proj"
    folder = tmp_path / "cfg" / "projects" / str(root).replace("/", "-").replace(".", "-").replace("_", "-")
    def transcript(name, tokens):
        usage = {"input_tokens": tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        return _lines(folder / f"{name}.jsonl", [{"type": "assistant", "message": {"usage": usage, "stop_reason": "end_turn"}}])
    a, b = transcript("a", 150000), transcript("b", 10000)
    os.utime(a, (time.time() - 60, time.time() - 60))
    return root, a, b


def test_parallel_claude_runs_each_read_their_own_session(tmp_path: Path, monkeypatch):
    root, a, b = _two_claude_sessions(tmp_path, monkeypatch)
    runs = root / ".ego" / "state" / "run"
    write_json(runs / "111.session.json", {"session_id": "a", "transcript_path": str(a)})
    write_json(runs / "222.session.json", {"session_id": "b", "transcript_path": str(b)})
    assert measure("claude", root, 0, None, runs / "111.session.json").tokens == 150000
    assert measure("claude", root, 0, None, runs / "222.session.json").tokens == 10000


def test_an_unbound_run_does_not_guess_beside_a_live_run(tmp_path: Path, monkeypatch):
    root, a, b = _two_claude_sessions(tmp_path, monkeypatch)
    runs = root / ".ego" / "state" / "run"
    own, other = runs / "999999.session.json", runs / f"{os.getpid()}.session.json"
    write_json(own, {})
    write_json(other, {})
    assert measure("claude", root, 0, None, own) is None  # the newest record may be the other run's
    other.unlink()
    assert measure("claude", root, 0, None, own).key == str(b)  # alone: the newest record, as before


def test_codex_is_measured_from_the_rollout_of_this_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    root = tmp_path / "proj"
    def rollout(name, cwd, events):
        return _lines(tmp_path / "sessions" / "2026" / "11" / "11" / f"rollout-{name}.jsonl",
                      [{"type": "session_meta", "payload": {"cwd": cwd}}] + [{"type": "event_msg", "payload": e} for e in events])
    count = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 247583, "cached_input_tokens": 247296}}}
    mine = rollout("a", str(root), [{"type": "task_started"}, count, {"type": "task_complete"}])
    rollout("b", "/elsewhere", [count])
    assert measure("codex", root, 0) == Reading(str(mine), 247583, True)
    rollout("a", str(root), [count, {"type": "task_complete"}, {"type": "task_started"}])
    assert measure("codex", root, 0).idle is False


def test_opencode_is_measured_from_its_database(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = tmp_path / "proj"
    assert measure("opencode", root, 0) is None
    (tmp_path / "opencode").mkdir()
    con = sqlite3.connect(tmp_path / "opencode" / "opencode.db")
    con.execute("create table session (id text, directory text, parent_id text, time_updated integer)")
    con.execute("create table message (id text, session_id text, time_created integer, data text)")
    now = int(time.time() * 1000)
    con.execute("insert into session values ('ses_1', ?, null, ?)", (str(root), now))
    con.execute("insert into session values ('ses_child', ?, 'ses_1', ?)", (str(root), now + 5))
    done = {"role": "assistant", "finish": "stop", "time": {"completed": now},
            "tokens": {"input": 12000, "output": 900, "cache": {"read": 30000, "write": 500}}}
    con.execute("insert into message values ('m1', 'ses_1', 1, ?)", (json.dumps(done),))
    con.commit()
    assert measure("opencode", root, 0) == Reading("ses_1", 42500, True)
    con.execute("insert into message values ('m2', 'ses_1', 2, ?)", (json.dumps({"role": "user"}),))
    con.commit(); con.close()
    assert measure("opencode", root, 0) == Reading("ses_1", 42500, False)  # a turn is running


def test_parallel_codex_runs_each_read_their_own_rollout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    root = tmp_path / "proj"
    def rollout(name, tokens):
        count = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": tokens}}}
        return _lines(tmp_path / "sessions" / "2026" / "11" / "11" / f"rollout-{name}.jsonl",
                      [{"type": "session_meta", "payload": {"cwd": str(root)}}]
                      + [{"type": "event_msg", "payload": e} for e in (count, {"type": "task_complete"})])
    a = rollout("a", 247583)
    os.utime(a, (time.time() - 60, time.time() - 60))
    rollout("b", 1000)  # the other run's session, written last
    bound = root / ".ego" / "state" / "run" / "111.session.json"
    write_json(bound, {"session_id": "a", "transcript_path": str(a)})
    assert measure("codex", root, 0, None, bound).tokens == 247583


def test_parallel_opencode_runs_each_read_their_own_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = tmp_path / "proj"
    (tmp_path / "opencode").mkdir()
    con = sqlite3.connect(tmp_path / "opencode" / "opencode.db")
    con.execute("create table session (id text, directory text, parent_id text, time_updated integer)")
    con.execute("create table message (id text, session_id text, time_created integer, data text)")
    now = int(time.time() * 1000)
    def done(tokens):
        return json.dumps({"role": "assistant", "finish": "stop", "time": {"completed": now}, "tokens": {"input": tokens}})
    con.execute("insert into session values ('ses_1', ?, null, ?)", (str(root), now))
    con.execute("insert into session values ('ses_2', ?, null, ?)", (str(root), now + 5))  # the other run's, newer
    con.execute("insert into message values ('m1', 'ses_1', 1, ?)", (done(42500),))
    con.execute("insert into message values ('m2', 'ses_2', 1, ?)", (done(1000),))
    con.commit(); con.close()
    bound = root / ".ego" / "state" / "run" / "111.session.json"
    write_json(bound, {"session_id": "ses_1", "transcript_path": None})
    assert measure("opencode", root, 0, None, bound) == Reading("ses_1", 42500, True)


def test_agy_reports_through_its_status_line(tmp_path: Path, monkeypatch, capsys):
    state = tmp_path / "state.json"
    payload = {"conversation_id": "c1", "agent_state": "idle",
               "context_window": {"context_window_size": 1000000, "used_percentage": 12.5, "current_usage": None}}
    assert record_context(payload, state) == "ego · ctx 125k"
    assert measure("agy", tmp_path, 0, state) == Reading("c1", 125000, True)
    monkeypatch.setenv("EGO_RUN_STATE", str(state))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({**payload, "agent_state": "running"})))
    assert main(["hook", "context-probe", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "ego · ctx 125k"
    assert measure("agy", tmp_path, 0, state).idle is False
    assert measure("agy", tmp_path, 0, None) is None


def test_an_unmeasured_context_is_shown_as_a_dash(tmp_path: Path):
    assert ctx_label(None) == "ego · ctx —" and ctx_label(0) == "ego · ctx —"
    assert ctx_label(125000) == "ego · ctx 125k"
    empty = {"context_window": {"context_window_size": 0, "used_percentage": 0}}
    assert record_context(empty, tmp_path / "state.json") == "ego · ctx —"


def test_the_bar_keeps_the_last_row_and_the_host_gets_the_rest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ptyrun, "BAR_QUIET", 0)
    screen_master, screen = pty.openpty()
    ptyrun._set_size(screen, 10, 40)
    read_end, write_end = os.pipe()
    chunks: list[bytes] = []

    def drain():
        while True:
            try:
                data = os.read(screen_master, 4096)
            except OSError:
                return
            if not data:
                return
            chunks.append(data)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    host = ("import fcntl,struct,termios,time\n"
            "time.sleep(0.4)\n"
            "rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\0' * 8))\n"
            "print('SIZE', rows, cols, flush=True)\n")
    try:
        code, rotations = run("claude", [sys.executable, "-c", host], tmp_path, 40000, stdin_fd=read_end,
                              stdout_fd=screen, probe=lambda *_: Reading("s1", 12000, False),
                              poll=0.05, quiet_output=0.05, quiet_keys=0)
    finally:
        os.close(read_end)
        os.close(write_end)
        os.close(screen)
    reader.join(5)
    out = b"".join(chunks)
    assert (code, rotations) == (0, 0)
    assert b"SIZE 9 40" in out  # the host draws in 9 rows; the tenth is ego's
    assert b"\x1b[1;9r" in out and b"\x1b[10;1H" in out  # scrolling stops above the bar row
    assert "ego · ctx 12k".encode() in out  # the probe's reading reached the bar
    assert out.endswith(b"\x1b[r\x1b[10;1H\x1b[2K")  # the region is released on exit
    os.close(screen_master)


def test_a_draft_in_the_input_box_is_noticed():
    keys = InputState()
    keys.feed(b"\x1b[I\x1b[A\x1b]11;rgb:0000/0000/0000\x07", 1)  # focus, arrow, a terminal's colour reply
    assert not keys.dirty
    keys.feed(b"fix th", 2)
    assert keys.dirty and keys.last_key == 2
    keys.feed(b"e bug\r", 3)
    assert not keys.dirty
    keys.feed(b"\x1b[97u\x1b[98;2u", 4)  # kitty protocol: "a", shift+"b"
    assert keys.dirty
    keys.feed(b"\x1b[13;2u", 5)          # shift+enter adds a line, it does not send
    assert keys.dirty
    keys.feed(b"\x1b[13u", 6)
    assert not keys.dirty
    keys.feed(b"x\x1b[99;5u", 7)          # ctrl+c
    assert not keys.dirty


def test_pasted_text_cannot_carry_keystrokes():
    out = _paste("Goal: ship\nNext: \x1b[201~\rrm -rf /\x03")
    assert out.startswith(b"\x1b[200~") and out.endswith(b"\x1b[201~")
    assert not any(b < 32 for b in out[6:-6])


def test_rotation_saves_first_and_clears_only_after_a_new_checkpoint(tmp_path: Path):
    rot = Rotator(tmp_path, "opencode", 100000)
    assert rot.step(None, True, 0) == []
    assert rot.step(Reading("s1", 99000, True), True, 1) == []
    assert rot.step(Reading("s1", 150000, False), True, 2) == []   # still working
    assert rot.step(Reading("s1", 150000, True), False, 3) == []   # the user is typing
    save_checkpoint(tmp_path, "old", goal="earlier work")           # saved before we asked: not this state
    time.sleep(0.05)
    typed = rot.step(Reading("s1", 150000, True), True, time.time())
    assert b"ego session checkpoint" in typed[0][1] and typed[-1][1] == b"\r"
    assert rot.step(Reading("s1", 150000, True), True, time.time()) == []
    save_checkpoint(tmp_path, "feat", goal="ship it", next_steps=["run tests"])
    assert rot.step(Reading("s1", 151000, False), True, time.time()) == []  # the save turn has not ended
    typed = rot.step(Reading("s1", 151000, True), True, time.time())
    assert [k for _, k in typed][:2] == [b"/new", b"\r"]
    assert b"Resume task feat" in typed[2][1] and b"run tests" in typed[2][1] and rot.rotations == 1
    assert rot.step(Reading("s1", 151000, True), True, time.time()) == []   # the cleared session still reads as full
    assert rot.step(Reading("s2", 3000, True), True, time.time()) == []


def test_a_resumed_session_already_over_the_limit_is_not_rotated_again(tmp_path: Path):
    rot = Rotator(tmp_path, "claude", 1000)  # below what a fresh session costs: clearing cannot get under it
    assert rot.step(Reading("s1", 70000, True), True, time.time())
    save_checkpoint(tmp_path, "feat", goal="ship it")
    assert rot.step(Reading("s1", 72000, True), True, time.time()) and rot.rotations == 1
    assert rot.step(Reading("s2", None, False), True, time.time()) == []    # the resume turn is running
    for _ in range(3):                                                      # resumed, idle, nobody typing
        assert rot.step(Reading("s2", 50000, True), True, time.time()) == []
    assert rot.step(Reading("s2", 60000, True), True, time.time()) == []    # ordinary growth
    assert rot.step(Reading("s2", 63000, True), True, time.time())          # a quarter larger: clearing frees context again
    assert rot.rotations == 1


def test_without_a_checkpoint_nothing_is_cleared(tmp_path: Path):
    rot = Rotator(tmp_path, "claude", 100000)
    assert rot.step(Reading("s1", 120000, True), True, 1000)
    assert rot.step(Reading("s1", 121000, True), True, 1000 + ptyrun.SAVE_TIMEOUT + 1) == []
    assert rot.step(Reading("s1", 130000, True), True, 2000) == []      # asks again only after real growth
    assert rot.step(Reading("s1", 160000, True), True, 2001)
    assert rot.rotations == 0


def test_a_run_waits_only_for_its_own_checkpoint(tmp_path: Path):
    rot = Rotator(tmp_path, "claude", 100000, "mine")
    asked = time.time() - 10
    assert rot.step(Reading("s1", 120000, True), True, asked)  # asks this session to save
    save_checkpoint(tmp_path, "task-b", goal="b", run="other")  # a parallel run's session saved first
    assert rot.step(Reading("s1", 120000, True), True, asked + 20) == []
    save_checkpoint(tmp_path, "task-a", goal="a", run="mine")
    typed = b"".join(keys for _, keys in rot.step(Reading("s1", 120000, True), True, asked + 30))
    assert b"Resume task task-a" in typed and b"task-b" not in typed


HOST = r"""
import os, sys, tty
tty.setraw(0)
log = open(sys.argv[1], "ab", buffering=0)
while True:
    data = os.read(0, 4096)
    log.write(data)
    if not data or b"Q" in data:
        break
"""


def test_the_wrapped_cli_is_typed_into(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ptyrun, "AFTER_CLEAR", 0.1)
    log = tmp_path / "typed.log"
    script = tmp_path / "host.py"
    script.write_text(HOST)
    read_end, write_end = os.pipe()
    sink = os.open(os.devnull, os.O_WRONLY)
    deadline = time.time() + 30

    def probe(host, root, since, state, session=None):
        typed = log.read_bytes() if log.exists() else b""
        if b"Resume task feat" in typed or time.time() > deadline:
            if typed.endswith(b"\r") or time.time() > deadline:
                os.write(write_end, b"Q")
            return Reading("s2", 100, True)
        if b"ego session checkpoint" in typed and not (tmp_path / ".ego/memory/handoffs/feat.json").exists():
            save_checkpoint(tmp_path, "feat", goal="ship it", next_steps=["run tests"])
        return Reading("s1", 50000, True)

    try:
        code, rotations = run("claude", [sys.executable, str(script), str(log)], tmp_path, 40000, stdin_fd=read_end,
                              stdout_fd=sink, probe=probe, poll=0.05, quiet_output=0.05, quiet_keys=0)
    finally:
        for fd in (read_end, write_end, sink):
            os.close(fd)
    typed = log.read_bytes()
    assert (code, rotations) == (0, 1)
    save, clear, resume = typed.index(b"ego session checkpoint"), typed.index(b"/clear\r"), typed.index(b"Resume task feat")
    assert save < clear < resume and b"run tests" in typed


def test_hooks_stand_down_inside_ego_run(project: Path, monkeypatch, capsys):
    save_checkpoint(project, "feat", goal="ship it")
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude" / "settings.json").write_text('{"hooks": "ego hook session-start"}')
    assert arm_auto_resume(project, "feat") is True
    monkeypatch.setenv("EGO_RUN", "claude")
    assert arm_auto_resume(project, "feat") is False
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"source": "clear"})))
    assert main(["hook", "session-start", "--root", str(project)]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_run_refuses_without_a_threshold_or_a_terminal(project: Path, capsys):
    assert main(["run", "claude", "--root", str(project)]) == 2
    assert "CLEAR_THRESHOLD_UNSET" in capsys.readouterr().err
    assert main(["run", "claude", "--clear-tokens", "50000", "--root", str(project), "--", "--root", "x"]) == 3
    assert "RUN_NEEDS_TTY" in capsys.readouterr().err


def test_yolo_adds_each_clis_own_flag_once():
    assert ptyrun.with_yolo("claude", ["--model", "x"]) == ["--dangerously-skip-permissions", "--model", "x"]
    assert ptyrun.with_yolo("codex", []) == ["--dangerously-bypass-approvals-and-sandbox"]
    assert ptyrun.with_yolo("opencode", []) == ["--auto"]
    assert ptyrun.with_yolo("agy", ["--dangerously-skip-permissions"]) == ["--dangerously-skip-permissions"]
    assert ptyrun.with_yolo("codex", ["--yolo"]) == ["--yolo"]  # the CLI's own alias was already given


def test_yolo_comes_from_the_flag_or_the_config(project: Path, monkeypatch):
    seen = []
    monkeypatch.setattr(ptyrun, "run_host", lambda host, args, root, threshold, yolo=False: seen.append((host, args, yolo)) or 0)
    assert main(["run", "claude", "--root", str(project), "--clear-tokens", "100000"]) == 0
    assert main(["run", "codex", "--no-yolo", "--root", str(project), "--clear-tokens", "100000", "--", "--model", "x"]) == 0
    config = project / ".ego/config.toml"
    config.write_text(config.read_text().replace("run_yolo = true", "run_yolo = false"))
    assert main(["run", "agy", "--root", str(project), "--clear-tokens", "100000"]) == 0
    assert main(["run", "agy", "--yolo", "--root", str(project), "--clear-tokens", "100000"]) == 0
    assert seen == [("claude", [], True), ("codex", ["--model", "x"], False), ("agy", [], False), ("agy", [], True)]
