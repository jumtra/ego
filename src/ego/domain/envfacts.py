# Ported from microsoft/TokenEconomizer (MIT, 1bfeccd): tokeneconomizer/microskills/observe.py
"""Deterministic EnvFacts extraction from host-neutral trace events."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .trace_schema import CommandResultEvent, TraceEvent

_ENV_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)+")
_ENV_CMD_RE = re.compile(r"^env(?:\s+(?:-[a-zA-Z]+(?:\s+\S+)?|--[A-Za-z-]+(?:=\S+)?))*\s+")
_CONDA_RUN_HEAD_RE = re.compile(r"^(?:conda|mamba|micromamba)\s+run\b\s*")
_CONDA_OPT_RE = re.compile(
    r"^(?:--no-capture-output|--live-stream|(?:-n|--name|-p|--prefix)(?:=\S+|\s+\S+))\s+"
)
_LAUNCHER_RUN_RE = re.compile(r"^(?:uv|poetry|pipenv)\s+run\b\s*")
_RUN_OPTION_RE = re.compile(
    r"^(?:(?:--with(?:-editable|-requirements)?|--extra|"
    r"--(?:only-|no-)?group|--python|-p|--package|--project|--directory|"
    r"--env-file|--index|--pyproject)(?:=|\s+)[^\s-]\S*|"
    r"--?[A-Za-z][\w-]*(?:=\S+)?|--)\s+"
)
_COVERAGE_RUN_RE = re.compile(r"^(?:\S*/)?coverage\s+run\b\s*")
_COVERAGE_VALUE_OPT_RE = re.compile(
    r"^(?:--rcfile|--source|--omit|--include|--data-file|"
    r"--context|--concurrency|--debug|--save-signal)(?:=\S+|\s+\S+)\s+"
)
_COVERAGE_FLAG_OPT_RE = re.compile(r"^(?!-m\b)--?[A-Za-z][\w-]*(?:=\S+)?\s+")
_NPX_RE = re.compile(r"^npx\s+(?:(?:--yes|-y|--no-install|--no)\s+)*")
_PYTHON_M_RE = re.compile(r"^(?:\S*/)?python3?\s+-m\s+(?=\S)")

_THREE_TOKEN_CATEGORIES: dict[tuple[str, str, str], Literal["test", "build", "lint"]] = {
    ("npm", "run", "test"): "test",
    ("npm", "run", "build"): "build",
    ("npm", "run", "lint"): "lint",
}
_TWO_TOKEN_CATEGORIES: dict[tuple[str, str], Literal["test", "build", "lint"]] = {
    ("npm", "test"): "test",
    ("node", "--test"): "test",
    ("go", "test"): "test",
    ("cargo", "test"): "test",
    ("make", "test"): "test",
    ("cargo", "build"): "build",
    ("go", "build"): "build",
}
_ONE_TOKEN_CATEGORIES: dict[str, Literal["test", "build", "lint"]] = {
    "pytest": "test",
    "tox": "test",
    "unittest": "test",
    "tsc": "build",
    "make": "build",
    "ruff": "lint",
    "biome": "lint",
    "eslint": "lint",
    "mypy": "lint",
    "flake8": "lint",
}


@dataclass(frozen=True)
class Observation:
    """Single deterministic environment fact observed from a command execution."""

    kind: Literal["works", "fails"]
    key: str
    category: str
    session: str


def _last_segment(cmd: str) -> str:
    """Return the last non-empty command segment split on '&&' or ';' outside quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\\" and i + 1 < len(cmd):
                i += 1
                buf.append(cmd[i])
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
        elif ch == "&" and cmd[i : i + 2] == "&&":
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    for seg in reversed(parts):
        stripped = seg.strip()
        if stripped:
            return stripped
    return ""


def _strip_leading_env(s: str) -> str:
    """Strip leading K=V assignments and 'env K=V' prefixes."""
    while True:
        prev = s
        s = _ENV_ASSIGN_RE.sub("", s, count=1).lstrip()
        if m := _ENV_CMD_RE.match(s):
            s = s[m.end() :].lstrip()
            s = _ENV_ASSIGN_RE.sub("", s, count=1).lstrip()
        if s == prev:
            return s


def _strip_one_wrapper(s: str) -> str:
    if m := _CONDA_RUN_HEAD_RE.match(s):
        rest = s[m.end() :]
        while opt := _CONDA_OPT_RE.match(rest):
            rest = rest[opt.end() :]
        return rest.lstrip()
    if m := _LAUNCHER_RUN_RE.match(s):
        rest = s[m.end() :]
        while opt := _RUN_OPTION_RE.match(rest):
            rest = rest[opt.end() :]
            if opt.group(0).strip() == "--":
                break
        return rest.lstrip()
    if m := _COVERAGE_RUN_RE.match(s):
        rest = s[m.end() :]
        while True:
            if opt := _COVERAGE_VALUE_OPT_RE.match(rest):
                rest = rest[opt.end() :]
            elif opt := _COVERAGE_FLAG_OPT_RE.match(rest):
                rest = rest[opt.end() :]
            else:
                break
        if rest.startswith("-m ") or rest.startswith("-m\t"):
            mod_rest = rest[2:].lstrip()
            if mod_rest:
                return mod_rest
        return rest.lstrip()
    if m := _NPX_RE.match(s):
        return s[m.end() :].lstrip()
    if m := _PYTHON_M_RE.match(s):
        return s[m.end() :].lstrip()
    return s


def normalize_command(cmd: str) -> str:
    """Normalize a shell command by taking its last segment and stripping env/launcher wrappers."""
    s = _last_segment(cmd or "")
    while s:
        prev = s
        s = _strip_leading_env(s)
        s = _strip_one_wrapper(s)
        if s == prev:
            break
    return " ".join(s.split())


def classify(cmd: str) -> Literal["test", "build", "lint"] | None:
    """Classify a normalized command into 'test', 'build', 'lint', or None."""
    toks = cmd.split()
    if not toks:
        return None
    head = toks[0].rsplit("/", 1)[-1]
    if len(toks) >= 3:
        three = (head, toks[1], toks[2])
        if three in _THREE_TOKEN_CATEGORIES:
            return _THREE_TOKEN_CATEGORIES[three]
    if len(toks) >= 2:
        two = (head, toks[1])
        if two in _TWO_TOKEN_CATEGORIES:
            return _TWO_TOKEN_CATEGORIES[two]
    return _ONE_TOKEN_CATEGORIES.get(head)


def extract(events: Iterable[TraceEvent], session: str) -> list[Observation]:
    """Extract deterministic EnvFacts observations from CommandResultEvents using is_error only."""
    observations: list[Observation] = []
    for event in events:
        if not isinstance(event, CommandResultEvent):
            continue
        key = normalize_command(event.command)
        category = classify(key)
        if category is None:
            continue
        kind: Literal["works", "fails"] = "fails" if event.is_error else "works"
        observations.append(
            Observation(kind=kind, key=key, category=category, session=session)
        )
    return observations
