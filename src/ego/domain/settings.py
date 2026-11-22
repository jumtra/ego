"""Typed settings sections loaded from defaults.toml (packaged) and .ego/defaults.toml (project override)."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from .defaults import section_values


@dataclass(frozen=True)
class UsageSettings:
    track_cache_tiers: bool
    track_overhead_separately: bool
    session_cost_warning_usd: float


@dataclass(frozen=True)
class LifecycleSettings:
    enable_envfacts: bool
    min_confirmations: int
    contradiction_demote_threshold: int
    max_injected_facts: int
    max_injected_chars: int
    alter_feedback: bool


@dataclass(frozen=True)
class LoopGuardSettings:
    enabled: bool
    dry_run: bool
    envelope_min_samples: int
    envelope_percentile: int
    min_turns: int
    edit_churn: int
    distinct_edit_files: int
    error_outputs: int
    stagnation_ratio: float
    envelope_margin: float
    backstop_mult: float
    max_nudges_per_node: int
    escalate_to_replan: bool


@dataclass(frozen=True)
class ToolPruneSettings:
    unused_days: int
    min_sessions: int


@dataclass(frozen=True)
class McpSettings:
    alter_worker_profile: str


@dataclass(frozen=True)
class AnalysisSettings:
    min_user_chars: int


@dataclass(frozen=True)
class Settings:
    usage: UsageSettings
    lifecycle: LifecycleSettings
    loopguard: LoopGuardSettings
    toolprune: ToolPruneSettings
    mcp: McpSettings
    analysis: AnalysisSettings


def load_settings(root: Path | None) -> Settings:
    return Settings(
        usage=UsageSettings(**section_values(root, "usage")),
        lifecycle=LifecycleSettings(**section_values(root, "lifecycle")),
        loopguard=LoopGuardSettings(**section_values(root, "loopguard")),
        toolprune=ToolPruneSettings(**section_values(root, "toolprune")),
        mcp=McpSettings(**section_values(root, "mcp")),
        analysis=AnalysisSettings(**section_values(root, "analysis")),
    )
