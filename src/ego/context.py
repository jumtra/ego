"""Context budgeting: deterministic trimming and a static context audit.

No model call is made here. Trimming is observation masking (keep the head
and the tail, drop the middle) rather than LLM summarisation, and the audit
only measures files that a host loads into its context window.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any

# Per-host budgets, in estimated tokens. Always-on text is paid on every turn
# of every session, so it gets the tight budget; skill bodies load on demand.
ALWAYS_ON_BUDGET = 600
SKILL_BODY_BUDGET = 1500
INSTRUCTION_FILE_MAX_LINES = 200


def clip_output(text: str, limit: int = 4000, head_ratio: float = 0.25) -> str:
    """Bound verifier output, keeping the tail where failures are reported."""
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n...[{len(text) - limit} chars omitted]...\n"
    keep = max(limit - len(marker), 0)
    head = int(keep * head_ratio)
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def approx_tokens(text: str) -> int:
    """Rough estimate only: ~4 ASCII chars or ~1 non-ASCII char per token."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return (ascii_chars + 3) // 4 + (len(text) - ascii_chars)


def _split_frontmatter(text: str) -> tuple[str, str]:
    if text.startswith("---\n") and "\n---\n" in text[4:]:
        front, body = text[4:].split("\n---\n", 1)
        return front, body
    return "", text


def _front_value(front: str, key: str) -> str:
    for line in front.splitlines():
        if line.split(":", 1)[0].strip() == key and ":" in line:
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def _item(root: Path, path: Path, load: str, text: str, part: str = "file") -> dict[str, Any]:
    return {"path": path.relative_to(root).as_posix(), "part": part, "load": load,
            "chars": len(text), "est_tokens": approx_tokens(text)}


def _host_items(root: Path, instructions: str, base: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    path = root / instructions
    if path.is_file():
        items.append(_item(root, path, "always", path.read_text(encoding="utf8", errors="replace")))
    for rule in sorted((root / base / "rules").glob("*.md")):
        text = rule.read_text(encoding="utf8", errors="replace")
        front, _ = _split_frontmatter(text)
        # Path-scoped rules load only when a matching file is read.
        items.append(_item(root, rule, "on_demand" if _front_value(front, "paths") or "paths:" in front else "always", text))
    for skill in sorted((root / base / "skills").glob("*/SKILL.md")):
        front, body = _split_frontmatter(skill.read_text(encoding="utf8", errors="replace"))
        items.append(_item(root, skill, "always", _front_value(front, "description"), "description"))
        items.append(_item(root, skill, "on_demand", body, "body"))
        for ref in sorted(skill.parent.glob("references/*")):
            if ref.is_file():
                items.append(_item(root, ref, "on_demand", ref.read_text(encoding="utf8", errors="replace"), "reference"))
    for agent in sorted((root / base / "agents").glob("*.md")):
        front, body = _split_frontmatter(agent.read_text(encoding="utf8", errors="replace"))
        items.append(_item(root, agent, "always", _front_value(front, "description"), "description"))
        items.append(_item(root, agent, "on_demand", body, "body"))
    return items


def audit(root: Path) -> dict[str, Any]:
    """Measure what each host loads up front versus on demand."""
    root = root.resolve()
    hosts: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    for host, instructions, base in (("claude", "CLAUDE.md", ".claude"), ("agents", "AGENTS.md", ".agents")):
        items = _host_items(root, instructions, base)
        if not items:
            continue
        always = sum(i["est_tokens"] for i in items if i["load"] == "always")
        hosts[host] = {"always_on_est_tokens": always,
                       "on_demand_est_tokens": sum(i["est_tokens"] for i in items if i["load"] == "on_demand"),
                       "items": items}
        if always > ALWAYS_ON_BUDGET:
            findings.append({"host": host, "problem": "always-on context over budget",
                             "est_tokens": always, "budget": ALWAYS_ON_BUDGET,
                             "fix": "Move workflow detail into a skill or a path-scoped rule"})
        for i in items:
            if i["part"] == "body" and "/skills/" in i["path"] and i["est_tokens"] > SKILL_BODY_BUDGET:
                findings.append({"host": host, "path": i["path"], "problem": "skill body over budget",
                                 "est_tokens": i["est_tokens"], "budget": SKILL_BODY_BUDGET,
                                 "fix": "Move rarely needed procedure into references/ and link to it"})
        path = root / instructions
        if path.is_file():
            lines = len(path.read_text(encoding="utf8", errors="replace").splitlines())
            if lines > INSTRUCTION_FILE_MAX_LINES:
                findings.append({"host": host, "path": instructions, "problem": "instruction file too long",
                                 "lines": lines, "budget": INSTRUCTION_FILE_MAX_LINES,
                                 "fix": "Keep only requirements the agent cannot infer from the repository"})
    return {"status": "PASS" if not findings else "OVER_BUDGET", "token_counts_are_estimates": True,
            "budgets": {"always_on": ALWAYS_ON_BUDGET, "skill_body": SKILL_BODY_BUDGET,
                        "instruction_file_lines": INSTRUCTION_FILE_MAX_LINES},
            "hosts": hosts, "findings": findings}
