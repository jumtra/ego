"""Session and checkpoint schema definition and validation using Pydantic."""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ..shared.util import EgoError

SCHEMA = 1
MAX_ITEM_CHARS = 350
MAX_GOAL_CHARS = 800
MAX_NOTES = 8
MAX_PROMPT_CHARS = 1600
CHECKPOINT_KINDS = ("ongoing", "plan_ready", "plan_resume", "completed")
CheckpointKindLiteral = Literal["ongoing", "plan_ready", "plan_resume", "completed"]


@dataclass(frozen=True)
class SessionSchemaLimits:
    """Configurable character and item limits for session checkpoints and resume prompts."""
    max_goal_chars: int = MAX_GOAL_CHARS
    max_item_chars: int = MAX_ITEM_CHARS
    max_notes: int = MAX_NOTES
    max_prompt_chars: int = MAX_PROMPT_CHARS


DEFAULT_SESSION_SCHEMA_LIMITS = SessionSchemaLimits()


@lru_cache(maxsize=16)
def build_checkpoint_model(limits: SessionSchemaLimits = DEFAULT_SESSION_SCHEMA_LIMITS) -> type[BaseModel]:
    """Build and cache the Pydantic CheckpointSpec model bound to the given limits."""
    note_str = Annotated[str, Field(max_length=limits.max_item_chars, description="Short bounded note string")]
    notes_list = Annotated[
        list[note_str],
        Field(max_length=limits.max_notes, description=f"List of at most {limits.max_notes} bounded notes"),
    ]

    class _CheckpointSpec(BaseModel):
        """Pydantic schema for session checkpoint and handoff files."""
        model_config = ConfigDict(extra="allow")

        schema_version: Literal[1] = Field(description="Checkpoint format version")
        task_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", max_length=64, description="Alphanumeric task ID")]
        status: Literal["IN_PROGRESS", "VERIFIED", "COMPLETED", "BLOCKED"]
        kind: CheckpointKindLiteral = "ongoing"
        goal: Annotated[str, Field(min_length=1, max_length=limits.max_goal_chars, pattern=r"\S")]
        completed: notes_list = Field(default_factory=list)
        next_steps: notes_list = Field(default_factory=list)
        decisions: notes_list = Field(default_factory=list)
        blockers: notes_list = Field(default_factory=list)
        evidence: notes_list = Field(default_factory=list)
        source: Literal["interactive", "managed"] = "interactive"
        created_at: str
        updated_at: str
        revision: Annotated[int, Field(ge=1)] = 1
        host: Literal["claude", "codex", "opencode", "agy"] | None = None
        run: Annotated[str, Field(max_length=64)] | None = None

    return _CheckpointSpec


def validate_checkpoint_schema(record: dict, *, limits: SessionSchemaLimits | None = None) -> None:
    """Mechanically validate a checkpoint dictionary against CheckpointSpec."""
    if not isinstance(record, dict):
        raise EgoError("INVALID_HANDOFF", "Checkpoint must be an object", 2)
    model = build_checkpoint_model(limits or DEFAULT_SESSION_SCHEMA_LIMITS)
    try:
        model.model_validate(record)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise EgoError("INVALID_HANDOFF", f"Checkpoint schema invalid: {loc} ({first.get('msg')})", 2) from err
