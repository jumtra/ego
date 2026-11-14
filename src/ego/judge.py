"""Evidence-gated judge verdicts: a finding without path:line evidence is discarded."""
from __future__ import annotations
import re
from .context import clip_output

_VERDICT = re.compile(r"^VERDICT:\s*(APPROVE|REJECT)\s*$", re.M)
_FINDING = re.compile(r"^[-*]\s*(\S+?):(\d+)\s*:\s*(.+)$")


def parse_verdict(output: str) -> dict:
    matched = _VERDICT.findall(output or "")
    if not matched:
        return {"verdict": "INVALID", "findings": [], "dropped": 0}
    findings, dropped = [], 0
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        m = _FINDING.match(line)
        if m:
            findings.append({"path": m.group(1), "line": int(m.group(2)), "text": m.group(3).strip()})
        else:
            dropped += 1
    verdict = matched[-1]
    if verdict == "REJECT" and not findings:
        verdict = "APPROVE"  # a rejection without evidence is discarded
    return {"verdict": verdict, "findings": findings, "dropped": dropped}


def format_findings(findings: list[dict]) -> str:
    return clip_output("\n".join(f"{f['path']}:{f['line']}: {f['text']}" for f in findings), 4000)
