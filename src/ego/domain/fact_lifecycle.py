# Ported from microsoft/TokenEconomizer (MIT, 1bfeccd): tokeneconomizer/microskills/lifecycle.py
"""Evidence-based EnvFacts lifecycle (candidate -> active -> demoted | forgotten)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .envfacts import Observation
from .settings import LifecycleSettings


@dataclass(frozen=True)
class Fact:
    """Tracked environment fact and its cross-session lifecycle state."""

    id: str
    key: str
    category: str
    state: str = "candidate"
    confirmations: list[str] = field(default_factory=list)
    contradictions: int = 0


def fact_id(category: str, key: str) -> str:
    """Return deterministic 12-char SHA-1 identifier for a (category, key) pair."""
    return hashlib.sha1(f"{category}:{key}".encode("utf-8")).hexdigest()[:12]


def observe(fact: Fact, obs: Observation, s: LifecycleSettings) -> Fact:
    """Pure state transition for a single environment observation."""
    if fact.state == "forgotten":
        return Fact(
            id=fact.id,
            key=fact.key,
            category=fact.category,
            state=fact.state,
            confirmations=list(fact.confirmations),
            contradictions=fact.contradictions,
        )

    confirmations = list(fact.confirmations)
    contradictions = fact.contradictions
    state = fact.state

    if obs.kind == "works":
        if obs.session not in confirmations:
            confirmations.append(obs.session)
        if state == "candidate" and len(confirmations) >= s.min_confirmations:
            state = "active"
    elif obs.kind == "fails":
        contradictions += 1
        if state == "active" and contradictions >= s.contradiction_demote_threshold:
            state = "demoted"

    return Fact(
        id=fact.id,
        key=fact.key,
        category=fact.category,
        state=state,
        confirmations=confirmations,
        contradictions=contradictions,
    )
