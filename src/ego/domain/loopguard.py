# Ported from microsoft/TokenEconomizer (MIT, 1bfeccd): tokeneconomizer/loopguard/{shell_edits,state,rules,envelope}.py
"""Pure-domain loop detection, shell-edit detection, and per-repo turn envelope."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shlex
from typing import Any, Iterable

from . import envfacts
from .settings import LoopGuardSettings
from .trace_schema import CommandResultEvent, ToolCallEvent, TraceEvent, TurnEvent

_ENVELOPE_REL_PATH = Path(".ego/state/loopguard-envelope.json")
_ENVELOPE_HISTORY = 200
_MAX_TRIED_COMMANDS_SHOWN = 3

# `<<<` is a here-string: its word is the input itself, so no body follows on the next lines.
_HEREDOC_START = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_OPS = {"&&", "||", ";", ";;", "|", "|&", "&", "\n", "(", ")"}
_OUT = {">", ">>", ">|", "&>", "&>>"}
_SKIP_OPERAND = {"<", "<<", "<<<", "<&", ">&", "<>"}
_SOURCE_FILE = re.compile(
    r"\.(?:py|pyi|pyx|pxd|js|jsx|mjs|cjs|ts|tsx|rs|go|java|kt|kts|scala|c|cc|cpp|cxx|h|hh|hpp|"
    r"rb|php|cs|swift|m|mm|sh|bash|zsh|lua|jl|ex|exs|erl|hs|ml|clj|sql|toml|ya?ml|json|cfg|ini|"
    r"conf|md|rst|html?|css|scss|vue|svelte|xml|gradle|properties|in|txt)$",
    re.I,
)
_PYTHON = re.compile(r"(?:^|[\s/;&|(])python\d?(?:\.\d+)?\b")
_NODE = re.compile(r"(?:^|[\s/;&|(])node\b")
_PY_OPEN_LIT = re.compile(r"open\(\s*(['\"])([^'\"\n]+)\1\s*,\s*(['\"])([^'\"]*)\3")
_PY_OPEN_VAR = re.compile(r"open\(\s*([A-Za-z_]\w*)\s*,\s*(['\"])([^'\"]*)\2")
_PY_WRITE_PATH_LIT = re.compile(
    r"(?:pathlib\.)?Path\(\s*(['\"])([^'\"\n]+)\1\s*\)\.write_(?:text|bytes)\("
)
_PY_WRITE_PATH_VAR = re.compile(r"\b([A-Za-z_]\w*)\.write_(?:text|bytes)\(")
_NODE_WRITE_LIT = re.compile(
    r"(?:fs\.)?(?:writeFileSync|writeFile|appendFileSync|appendFile|createWriteStream)\(\s*(['\"])([^'\"\n]+)\1"
)
_NODE_WRITE_ANY = re.compile(
    r"(?:fs\.)?(?:writeFileSync|writeFile|appendFileSync|appendFile|createWriteStream)\("
)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)

_GIT_VALUE_OPTS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--attr-source",
}

_WRAPPERS = {
    "sudo": {
        "-u", "-g", "-p", "-C", "-D", "-R", "-r", "-t", "-T", "-U",
        "--user", "--group", "--prompt", "--close-from", "--chdir", "--chroot",
        "--role", "--type", "--command-timeout", "--other-user",
    },
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "time": {"-f", "-o", "--format", "--output"},
    "command": set(),
    "nohup": set(),
}


def basename(p: str) -> str:
    """Return the last path component regardless of separator."""
    return p.replace("\\", "/").rstrip("/").split("/")[-1] if p else (p or "")


def _not_scratch(path: str) -> bool:
    return bool(path) and not path.startswith(("/dev/", "/proc/", "/tmp/"))


def _unquote(t: str) -> str:
    return t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t


def _split_heredocs(cmd: str) -> tuple[str, list[str]]:
    """Split a shell command into (shell_without_heredoc_bodies, heredoc_bodies)."""
    shell: list[str] = []
    bodies: list[str] = []
    tag: str | None = None
    body: list[str] = []
    for line in cmd.split("\n"):
        if tag is not None:
            if line.strip() == tag:
                bodies.append("\n".join(body))
                tag, body = None, []
            else:
                body.append(line)
            continue
        shell.append(line)
        m = _HEREDOC_START.search(line)
        if m:
            tag = m.group(2)
    if tag is not None:
        bodies.append("\n".join(body))
    return "\n".join(shell), bodies


def _tokens(text: str) -> list[str]:
    lex = shlex.shlex(text, posix=False, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    return list(lex)


def _commands(shell: str) -> tuple[list[list[str]], list[str]]:
    """Parse shell text into (simple commands, redirect write targets)."""
    try:
        streams = [_tokens(shell)]
    except ValueError:
        streams = []
        for line in shell.split("\n"):
            try:
                streams.append(_tokens(line) + ["\n"])
            except ValueError:
                continue
    cmds: list[list[str]] = []
    writes: list[str] = []
    for toks in streams:
        cur: list[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in _OPS:
                if cur:
                    cmds.append(cur)
                cur = []
            elif t in _OUT or t in _SKIP_OPERAND:
                if cur and cur[-1].isdigit():
                    cur.pop()
                if t in _OUT and i + 1 < len(toks) and toks[i + 1] not in _OPS:
                    writes.append(_unquote(toks[i + 1]))
                i += 1
            else:
                cur.append(_unquote(t))
            i += 1
        if cur:
            cmds.append(cur)
    return cmds, writes


def _inplace_files(tokens: list[str], inplace_flag, script_flag) -> list[str]:
    if not any(inplace_flag(t) for t in tokens[1:]):
        return []
    files: list[str] = []
    have_script = False
    skip = False
    for t in tokens[1:]:
        if skip:
            skip, have_script = False, True
            continue
        if not t or t.startswith("-"):
            where = script_flag(t) if t else None
            if where == "next":
                skip = True
            elif where == "inline":
                have_script = True
            continue
        if not have_script:
            have_script = True
            continue
        files.append(t)
    return files


def _short_script_flag(t: str, script_letters: str, stop_letters: str) -> str | None:
    if t.startswith("--") or len(t) < 2:
        return None
    for k, c in enumerate(t[1:], start=2):
        if c in stop_letters:
            return None
        if c in script_letters:
            return "inline" if t[k:] else "next"
    return None


def _sed_script_flag(t: str) -> str | None:
    if t in ("--expression", "--file"):
        return "next"
    if t.startswith(("--expression=", "--file=")):
        return "inline"
    return _short_script_flag(t, "ef", "i")


def _perl_script_flag(t: str) -> str | None:
    return _short_script_flag(t, "eE", "iIMmxdDC")


def _py_writes(text: str) -> list[str]:
    out: list[str] = []
    for m in _PY_OPEN_LIT.finditer(text):
        if set(m.group(4)) & set("wax+"):
            out.append(m.group(2))
    variables = [m.group(1) for m in _PY_OPEN_VAR.finditer(text) if set(m.group(3)) & set("wax+")]
    out += [m.group(2) for m in _PY_WRITE_PATH_LIT.finditer(text)]
    variables += [m.group(1) for m in _PY_WRITE_PATH_VAR.finditer(text)]
    for var in dict.fromkeys(variables):
        found = re.findall(
            rf"\b{re.escape(var)}\s*=\s*(?:(?:pathlib\.)?Path\(\s*)?(['\"])([^'\"\n]+)\1",
            text,
        )
        loop = re.search(rf"\bfor\s+{re.escape(var)}\s+in\s+[\[(]([^\])]*)[\])]", text)
        paths = [p for _, p in found]
        if loop:
            paths += re.findall(r"['\"]([^'\"\n]+)['\"]", loop.group(1))
        out += paths or ["(script)"]
    return out


def _node_writes(text: str) -> list[str]:
    out = [m.group(2) for m in _NODE_WRITE_LIT.finditer(text)]
    if not out and _NODE_WRITE_ANY.search(text):
        out.append("(script)")
    return out


def _git_args(tokens: list[str]) -> list[str]:
    if tokens[0].rsplit("/", 1)[-1] != "git":
        return []
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in _GIT_VALUE_OPTS else 1
    return tokens[i:]


def _restored(tokens: list[str]) -> list[str]:
    args = _git_args(tokens)
    if len(args) < 2 or args[0] not in ("checkout", "restore"):
        return []
    rest = (
        args[args.index("--") + 1:]
        if "--" in args
        else args[1:]
        if args[0] == "restore"
        else []
    )
    return [t for t in rest if not t.startswith("-")]


def _past_wrapper(tokens: list[str]) -> list[str]:
    takes_value = _WRAPPERS[tokens[0]]
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        t = tokens[i]
        i += 1
        if t == "--":
            break
        if t.startswith("--"):
            if t in takes_value:
                i += 1
            continue
        for n, letter in enumerate(t[1:], 2):
            if "-" + letter in takes_value:
                if n == len(t):
                    i += 1
                break
    if tokens[0] == "timeout":
        i += 1
    return tokens[i:]


def _bare(cmds: list[list[str]]) -> list[list[str]]:
    out: list[list[str]] = []
    for tokens in cmds:
        while tokens:
            if tokens[0] in _WRAPPERS:
                tokens = _past_wrapper(tokens)
            elif tokens[0] == "rtk" and len(tokens) > 1:
                tokens = tokens[2:] if tokens[1] == "proxy" and len(tokens) > 2 else tokens[1:]
            elif re.match(r"^\w+=", tokens[0]):
                tokens = tokens[1:]
            else:
                break
        if tokens:
            out.append(tokens)
    return out


def shell_edit_targets(cmd: str) -> list[str]:
    """Return basenames of files written by a shell command, in order, without duplicates."""
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    shell, bodies = _split_heredocs(cmd)
    cmds, writes = _commands(shell)
    found = [w for w in writes if _SOURCE_FILE.search(w) or (bodies and _not_scratch(w))]
    for tokens in _bare(cmds):
        head = tokens[0].rsplit("/", 1)[-1]
        if head == "sed":
            found += _inplace_files(
                tokens,
                lambda t: bool(re.match(r"^(?:-[a-zA-Z]*i|--in-place)", t)),
                _sed_script_flag,
            )
        elif head == "perl":
            found += _inplace_files(
                tokens,
                lambda t: bool(re.match(r"^-[a-zA-Z]*i", t)),
                _perl_script_flag,
            )
        elif head == "tee":
            found += [t for t in tokens[1:] if not t.startswith("-") and _SOURCE_FILE.search(t)]
        elif head == "apply_patch":
            found += _PATCH_FILE.findall("\n".join(bodies + [" ".join(tokens[1:])])) or ["(patch)"]
        elif head == "patch" or _git_args(tokens)[:1] == ["apply"]:
            found.append("(patch)")
        else:
            found += _restored(tokens)
    combined = "\n".join([shell] + bodies)
    if _PYTHON.search(shell):
        found += _py_writes(combined)
    if _NODE.search(shell):
        found += _node_writes(combined)
    names = [
        p if p.startswith("(") else basename(p)
        for p in found
        if p.startswith("(") or _not_scratch(p)
    ]
    return list(dict.fromkeys(n for n in names if n))


def is_shell_edit(cmd: str) -> bool:
    """Return True if a shell command writes or modifies a file."""
    return bool(shell_edit_targets(cmd))


@dataclass
class LoopState:
    """Folded loop-detection state across trace events."""

    turns: int = 0
    edits_by_file: dict[str, int] = field(default_factory=dict)
    error_outputs: int = 0
    failure_sigs: list[str] = field(default_factory=list)
    last_progress_turn: int = 0
    tried: list[str] = field(default_factory=list)


def _normalize_failure_sig(output_head: str) -> str:
    """Normalize the first non-empty line of a failing command output (paths->P, digits->N)."""
    if not output_head:
        return ""
    first_line = next((ln.strip() for ln in output_head.splitlines() if ln.strip()), "")
    if not first_line:
        return ""
    norm = re.sub(r"(?:[A-Za-z]:)?(?:[/\\][^\s:'\"]+)+", "P", first_line)
    norm = re.sub(r"\d+", "N", norm)
    return norm.strip()


def _record_edit(
    target: str,
    turns: int,
    edits_by_file: dict[str, int],
    failure_sigs: list[str],
) -> int | None:
    new_progress: int | None = None
    if target not in edits_by_file:
        new_progress = turns
        failure_sigs.clear()
    edits_by_file[target] = edits_by_file.get(target, 0) + 1
    return new_progress


def update(state: LoopState, events: Iterable[TraceEvent]) -> LoopState:
    """Fold trace events into a new LoopState without mutating the input."""
    turns = state.turns
    edits_by_file = dict(state.edits_by_file)
    error_outputs = state.error_outputs
    failure_sigs = list(state.failure_sigs)
    last_progress_turn = state.last_progress_turn
    tried = list(state.tried)

    for event in events:
        if isinstance(event, TurnEvent):
            if event.role == "assistant":
                turns += 1
        elif isinstance(event, ToolCallEvent):
            if event.command and (not tried or tried[-1] != event.command):
                tried.append(event.command)
            if event.file_path:
                target = basename(event.file_path)
                if target:
                    prog = _record_edit(target, turns, edits_by_file, failure_sigs)
                    if prog is not None:
                        last_progress_turn = prog
            elif event.command and is_shell_edit(event.command):
                for target in shell_edit_targets(event.command):
                    prog = _record_edit(target, turns, edits_by_file, failure_sigs)
                    if prog is not None:
                        last_progress_turn = prog
        elif isinstance(event, CommandResultEvent):
            if event.command and (not tried or tried[-1] != event.command):
                tried.append(event.command)
            if event.is_error:
                error_outputs += 1
                sig = _normalize_failure_sig(event.output_head)
                if sig:
                    failure_sigs.append(sig)
            else:
                category = envfacts.classify(envfacts.normalize_command(event.command))
                if category == "test":
                    last_progress_turn = turns
                    failure_sigs.clear()

    return LoopState(
        turns=turns,
        edits_by_file=edits_by_file,
        error_outputs=error_outputs,
        failure_sigs=failure_sigs,
        last_progress_turn=last_progress_turn,
        tried=tried,
    )


def repeat_ratio(failure_sigs: list[str]) -> float:
    """Return 1 - distinct/len for failure signatures, or 0.0 when empty."""
    if not failure_sigs:
        return 0.0
    return 1.0 - (len(set(failure_sigs)) / len(failure_sigs))


def is_stuck(state: LoopState, s: LoopGuardSettings) -> bool:
    """Strict heuristic: enough turns AND churn/scatter AND sustained errors AND repetition."""
    if state.turns < s.min_turns:
        return False
    max_edits = max(state.edits_by_file.values()) if state.edits_by_file else 0
    churny = max_edits >= s.edit_churn or len(state.edits_by_file) >= s.distinct_edit_files
    noisy = state.error_outputs >= s.error_outputs
    repeating = repeat_ratio(state.failure_sigs) >= s.stagnation_ratio
    return churny and noisy and repeating


def _percentile(xs: list[int], p: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    i = (p / 100.0) * (n - 1)
    lo = int(i)
    frac = i - lo
    if lo + 1 >= n:
        return float(ordered[-1])
    return ordered[lo] + frac * (ordered[lo + 1] - ordered[lo])


def _load_samples(root: Path) -> list[int]:
    path = root / _ENVELOPE_REL_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("samples") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [int(x) for x in raw if isinstance(x, (int, float)) and not isinstance(x, bool)]


def record(root: Path, turns: int) -> None:
    """Append a resolved turn count to the per-repo envelope, keeping the last 200 samples."""
    samples = _load_samples(root)
    samples.append(int(turns))
    samples = samples[-_ENVELOPE_HISTORY:]
    path = root / _ENVELOPE_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"samples": samples}), encoding="utf-8")


def limit(root: Path, s: LoopGuardSettings) -> float | None:
    """Return the learned turn percentile when enough samples exist, else None."""
    samples = _load_samples(root)
    if len(samples) < s.envelope_min_samples:
        return None
    return _percentile(samples, s.envelope_percentile)


def over_envelope(state: LoopState, lim: float | None, s: LoopGuardSettings) -> bool:
    """Return True when turns exceed the learned envelope margin or min_turns fallback."""
    threshold = lim * s.envelope_margin if lim is not None else s.min_turns
    return state.turns > threshold


def stagnation_summary(state: LoopState) -> dict[str, Any]:
    """Return the most repeated failure signature, its count, and up to 3 tried commands."""
    if state.failure_sigs:
        pattern, count = Counter(state.failure_sigs).most_common(1)[0]
    else:
        pattern, count = "", 0
    commands = list(dict.fromkeys(state.tried))[-_MAX_TRIED_COMMANDS_SHOWN:]
    return {"pattern": pattern, "count": count, "commands": commands}


def nudge_text(state: LoopState) -> str:
    """Mirror only the agent's own repeated failure and tried commands; redirect without halting."""
    summary = stagnation_summary(state)
    pattern = summary["pattern"] or "(unknown failure)"
    count = summary["count"]
    commands = summary["commands"]
    cmds_text = ", ".join(f"`{c}`" for c in commands) if commands else "(none)"
    return (
        f"Process check: the same failure `{pattern}` has recurred {count} time(s) "
        f"across recent commands ({cmds_text}). "
        "Please step back and try a materially different approach before editing again."
    )
