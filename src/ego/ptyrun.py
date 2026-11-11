"""`ego run <host>`: the interactive CLI inside a pseudo-terminal that ego owns.

No hook can run a slash command and no CLI clears itself, but whoever holds
the terminal can type. The host runs unchanged and is passed through byte for
byte. At the configured context size, while the host is idle and the input
box is empty, ego types three things: a request to save a checkpoint, the
host's clear command, and the resume prompt.

Context size is read from each host's own session record, never estimated; a
host whose record cannot be read is simply never rotated.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import sqlite3
import sys
import termios
import time
import tty
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .session import HOST_CLEAR, RESUGGEST_GROWTH, context_tokens, latest_open_checkpoint, resume_prompt
from .util import EgoError, read_json, safe_path, write_json

RUN_ENV = "EGO_RUN"              # set for the host: its own ego hooks stand down, this process rotates instead
STATE_ENV = "EGO_RUN_STATE"      # where a host status line reports context size (hosts without a readable record)
POLL = 2.0                       # seconds between looks at the session record, and only while the screen is still
QUIET_OUTPUT = 2.0               # the host has drawn nothing for this long
QUIET_KEYS = 5.0                 # the user has typed nothing for this long
SAVE_TIMEOUT = 300.0             # no checkpoint this long after asking: give up until the context grows again
AFTER_CLEAR = 3.0                # let the host reset before the resume prompt is typed
TAIL_BYTES = 512 * 1024
# Each CLI's own switch for "approve every tool call without asking"; all it means is that CLI's meaning.
YOLO_FLAGS = {"claude": "--dangerously-skip-permissions", "codex": "--dangerously-bypass-approvals-and-sandbox",
              "opencode": "--auto", "agy": "--dangerously-skip-permissions"}


@dataclass(frozen=True)
class Reading:
    key: str            # the session these numbers belong to; a clear starts a new one
    tokens: int | None  # prompt tokens of the latest request; None when not measurable
    idle: bool          # the last turn ended and nothing awaits an answer


def _tail(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            lines = f.read().decode("utf8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _newest_first(paths, since: float) -> list[Path]:
    found = []
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            found.append((mtime, path))
    return [p for _, p in sorted(found, reverse=True)]


def _claude(root: Path, since: float) -> Reading | None:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    paths = _newest_first((home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))).glob("*.jsonl"), since)
    if not paths:
        return None
    idle = False
    for obj in reversed(_tail(paths[0])):
        if obj.get("isSidechain") or obj.get("type") not in ("user", "assistant"):
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        idle = obj["type"] == "assistant" and message.get("stop_reason") in ("end_turn", "stop_sequence")
        break
    return Reading(str(paths[0]), context_tokens(paths[0]), idle)


def _codex(root: Path, since: float) -> Reading | None:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for path in _newest_first((home / "sessions").glob("*/*/*/rollout-*.jsonl"), since):
        try:
            with path.open("rb") as f:
                meta = json.loads(f.readline(4 * 1024 * 1024))
            if meta["payload"]["cwd"] != str(root):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        tokens = idle = None
        for obj in reversed(_tail(path)):
            payload = obj.get("payload")
            if obj.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if tokens is None and kind == "token_count":
                try:
                    tokens = int(payload["info"]["last_token_usage"]["input_tokens"])  # includes cached input
                except (KeyError, TypeError, ValueError):
                    pass
            if idle is None and kind in ("task_started", "task_complete", "turn_aborted"):
                idle = kind != "task_started"
            if tokens is not None and idle is not None:
                break
        return Reading(str(path), tokens or None, bool(idle))
    return None


def _opencode(root: Path, since: float) -> Reading | None:
    db = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "opencode.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=1)
        try:
            row = con.execute("select id from session where directory=? and parent_id is null and time_updated>=? "
                              "order by time_updated desc limit 1", (str(root), int(since * 1000))).fetchone()
            if row is None:
                return None
            rows = con.execute("select data from message where session_id=? order by time_created desc limit 20",
                               (row[0],)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    tokens, idle = None, False
    for index, (data,) in enumerate(rows):
        try:
            message = json.loads(data)
        except (ValueError, TypeError):
            continue
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if index == 0:
            done = isinstance(message.get("time"), dict) and message["time"].get("completed")
            idle = bool(done) and message.get("finish") != "tool-calls"
        usage = message.get("tokens") if isinstance(message.get("tokens"), dict) else {}
        cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
        try:
            total = int(usage.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            tokens = total
            break
    return Reading(str(row[0]), tokens, idle)


def _reported(state: Path | None) -> Reading | None:
    record = read_json(state) if state else None
    if not isinstance(record, dict) or not isinstance(record.get("key"), str):
        return None
    tokens = record.get("tokens")
    return Reading(record["key"], tokens if isinstance(tokens, int) and tokens > 0 else None, record.get("idle") is True)


def measure(host: str, root: Path, since: float, state: Path | None = None) -> Reading | None:
    """The live session of `host` in `root`, from the host's own record. None when there is none to read."""
    if host == "claude":
        return _claude(root, since)
    if host == "codex":
        return _codex(root, since)
    if host == "opencode":
        return _opencode(root, since)
    return _reported(state)


def record_context(payload: dict[str, Any], state: Path) -> str:
    """Status line command for a host that reports its context there (AGY): keep it for the wrapper, return the line."""
    window = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else {}
    usage = window.get("current_usage") if isinstance(window.get("current_usage"), dict) else {}
    tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    if tokens <= 0:  # the host's own percentage of its own window size
        tokens = round(float(window.get("used_percentage") or 0) * float(window.get("context_window_size") or 0) / 100)
    key = str(payload.get("conversation_id") or payload.get("session_id") or "")[:80]
    write_json(state, {"key": key, "tokens": int(tokens), "idle": payload.get("agent_state") == "idle", "at": time.time()})
    return f"ego · ctx {tokens // 1000}k" if tokens > 0 else "ego"


_CSI_U = re.compile(rb"\x1b\[(\d+)(?::\d*)*(?:;(\d+)(?::\d+)?)?(?:;[\d:]*)?u")
_ESCAPE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[P_^][^\x1b]*\x1b\\|\x1b\[[0-?]*[ -/]*[@-~]|\x1bO.|\x1b.", re.S)


class InputState:
    """Whether the user may have an unsent draft in the host's input box.

    Typing over a draft would send it glued to our text, so rotation waits. Text
    marks the box dirty; Enter, Ctrl-C and Ctrl-U clean it. Erasing a draft with
    backspace is not tracked: staying dirty only delays a rotation.
    """

    def __init__(self) -> None:
        self.dirty = False
        self.last_key = 0.0

    def feed(self, data: bytes, at: float) -> None:
        self.last_key = at
        pos = 0
        while pos < len(data):
            match = _CSI_U.match(data, pos)  # kitty keyboard protocol: every key is an escape sequence
            if match:
                code, mods = int(match.group(1)), int(match.group(2) or 1) - 1
                if (code == 13 and not mods) or (mods & 4 and code in (99, 117)):
                    self.dirty = False
                elif 32 <= code < 57344 and code != 127 and not mods & 4:
                    self.dirty = True
                pos = match.end()
                continue
            match = _ESCAPE.match(data, pos)
            if match:
                pos = match.end()
                continue
            byte = data[pos]
            if byte in (10, 13, 3, 21):
                self.dirty = False
            elif byte >= 32 and byte != 127:
                self.dirty = True
            pos += 1


def _paste(text: str) -> bytes:
    """One line as a bracketed paste. Control characters are dropped: the text comes from a
    checkpoint file, and an escape in it could end the paste and be read as keystrokes."""
    clean = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in text.replace("\n", "  "))
    return b"\x1b[200~" + " ".join(clean.split(" ")).strip().encode("utf8") + b"\x1b[201~"


def _save_request(tokens: int, threshold: int) -> str:
    return (f"ego: this conversation's context is {tokens} tokens, at or above this project's limit ({threshold}). "
            "Do no further task work in this turn. Save this conversation's state with one "
            "`ego session checkpoint --task-id <id> --goal ... --completed ... --next ... --evidence ... "
            "--status <IN_PROGRESS|VERIFIED|BLOCKED> --root .` (facts only; include uncommitted changes and known "
            "failures; reuse the task ID if one was already checkpointed). Then stop: ego clears this session and "
            "resumes it from that checkpoint automatically.")


Keys = list[tuple[float, bytes]]  # (seconds to wait first, bytes to type)


class Rotator:
    """Decides what to type and when. Holds no terminal: `step` is given what was observed."""

    def __init__(self, root: Path, host: str, threshold: int) -> None:
        self.root, self.host, self.threshold = root, host, threshold
        self.trigger = threshold
        self.saving_since: float | None = None
        self.cleared: str | None = None  # the session just rotated away from: its record still reads as full
        self.resumed = False             # a rotation was typed and the new session's size is not yet known
        self.rotations = 0

    def step(self, reading: Reading | None, calm: bool, at: float) -> Keys:
        if reading is None or reading.key == self.cleared:
            return []
        tokens = reading.tokens
        if self.saving_since is None:
            if tokens is None:
                return []
            if self.resumed:
                # What the session costs once resumed is the floor a clear can reach. At or above the limit,
                # clearing again frees nothing and would repeat forever with nobody typing: wait for real growth.
                if not reading.idle:
                    return []
                self.resumed = False
                self.trigger = max(self.threshold, int(tokens * RESUGGEST_GROWTH))
            if tokens < self.threshold:
                self.trigger = self.threshold  # compacted: enforce again at the next crossing
                return []
            if tokens < self.trigger or not (reading.idle and calm):
                return []
            self.saving_since = at
            return [(0.0, _paste(_save_request(tokens, self.threshold))), (0.4, b"\r")]
        checkpoint = latest_open_checkpoint(self.root, at - self.saving_since)  # saved after we asked
        if checkpoint is None:
            if at - self.saving_since > SAVE_TIMEOUT:
                self.saving_since = None
                self.trigger = int((tokens or self.trigger) * RESUGGEST_GROWTH)
            return []
        if not (reading.idle and calm):
            return []
        self.saving_since, self.trigger, self.cleared = None, self.threshold, reading.key
        self.resumed = True
        self.rotations += 1
        return [(0.0, HOST_CLEAR[self.host].encode()), (0.5, b"\r"),
                (AFTER_CLEAR, _paste(resume_prompt(checkpoint))), (0.4, b"\r")]


def _write_all(fd: int, data: bytes) -> None:
    while data:
        try:
            data = data[os.write(fd, data):]
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [], 1)


def _copy_size(source: int, target: int) -> None:
    try:
        fcntl.ioctl(target, termios.TIOCSWINSZ, fcntl.ioctl(source, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        pass


def _terminated(signum, frame):
    raise SystemExit(128 + signum)


def run(host: str, argv: list[str], root: Path, threshold: int, *, env: dict[str, str] | None = None,
        state: Path | None = None, stdin_fd: int = 0, stdout_fd: int = 1,
        probe: Callable[[str, Path, float, Path | None], Reading | None] = measure, poll: float = POLL,
        quiet_output: float = QUIET_OUTPUT, quiet_keys: float = QUIET_KEYS) -> tuple[int, int]:
    """Run `argv` in a pseudo-terminal bridged to this one. Returns (exit code, rotations done)."""
    started = time.time()
    master, slave = pty.openpty()
    interactive = os.isatty(stdin_fd)
    if interactive:
        _copy_size(stdin_fd, slave)
    pid = os.fork()
    if pid == 0:
        try:
            os.close(master)
            os.login_tty(slave)
            os.chdir(root)
            os.execvpe(argv[0], argv, env if env is not None else dict(os.environ))
        finally:
            os._exit(127)
    os.close(slave)
    saved = termios.tcgetattr(stdin_fd) if interactive else None
    handlers = {}
    try:
        if interactive:
            tty.setraw(stdin_fd)
            handlers[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, lambda *_: _copy_size(stdin_fd, master))
        for signum in (signal.SIGTERM, signal.SIGHUP):  # leave through `finally`: the terminal must not stay raw
            handlers[signum] = signal.signal(signum, _terminated)
        rotator, keys = Rotator(root, host, threshold), InputState()
        queue: deque[tuple[float, bytes]] = deque()
        due = last_output = last_poll = 0.0
        watched = [master, stdin_fd]
        while True:
            try:
                ready, _, _ = select.select(watched, [], [], 0.2)
            except InterruptedError:
                continue
            at = time.time()
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    break
                _write_all(stdout_fd, data)
                last_output = at
            if stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                if not data:
                    watched.remove(stdin_fd)
                else:
                    keys.feed(data, at)
                    _write_all(master, data)
            if queue:
                if at >= due:
                    _write_all(master, queue.popleft()[1])
                    due = at + queue[0][0] if queue else 0.0
                continue
            calm = at - last_output >= quiet_output and at - keys.last_key >= quiet_keys and not keys.dirty
            if not calm or at - last_poll < poll:
                continue
            last_poll = at
            try:
                typed = rotator.step(probe(host, root, started, state), calm, at)
            except (OSError, EgoError, ValueError, KeyError, TypeError):
                typed = []  # a record we cannot read means "do nothing", never "stop the session"
            if typed:
                queue.extend(typed)
                due = at + typed[0][0]
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        os.close(master)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return (128 - code if code < 0 else code), rotator.rotations


def with_yolo(host: str, args: list[str]) -> list[str]:
    """`args` with the host's skip-permissions switch, unless the caller already passed it."""
    flag = YOLO_FLAGS[host]
    return args if flag in args or "--yolo" in args else [flag, *args]


def run_host(host: str, args: list[str], root: Path, threshold: int, yolo: bool = False) -> int:
    """`ego run`: the host CLI, interactive as usual, rotated automatically at `threshold` context tokens."""
    if yolo:
        args = with_yolo(host, args)
    if threshold <= 0:
        raise EgoError("CLEAR_THRESHOLD_UNSET", "Set clear_suggest_tokens in .ego/config.toml or pass --clear-tokens N", 2)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise EgoError("RUN_NEEDS_TTY", "ego run bridges an interactive CLI and needs a terminal", 3)
    binary = shutil.which(host)
    if binary is None:
        raise EgoError("HOST_UNAVAILABLE", f"{host} is not installed on PATH", 3)
    root = root.resolve()
    env = dict(os.environ)
    env[RUN_ENV] = host
    argv, state = [binary, *args], None
    if host == "agy" and "--settings" not in args:
        # AGY's conversation store is opaque; its status line input carries the context size and agent state.
        state = safe_path(root, f".ego/state/run/{os.getpid()}.json")
        state.parent.mkdir(parents=True, exist_ok=True)
        env[STATE_ENV] = str(state)
        probe_command = shlex.join([sys.executable, "-m", "ego", "hook", "context-probe"])
        argv = [binary, "--settings", json.dumps({"statusLine": {"type": "command", "command": probe_command}}), *args]
    try:
        code, rotations = run(host, argv, root, threshold, env=env, state=state)
    finally:
        if state is not None:
            state.unlink(missing_ok=True)
    if rotations:
        print(f"ego: cleared and resumed {rotations} time(s) at {threshold} context tokens", file=sys.stderr)
    return code
