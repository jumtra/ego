"""Session and checkpoint schema definition and validation using Pydantic."""
from __future__ import annotations
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ..shared.util import EgoError

SCHEMA = 1
MAX_ITEM_CHARS = 350
MAX_GOAL_CHARS = 800
MAX_NOTES = 8

SessionNote = Annotated[str, Field(max_length=MAX_ITEM_CHARS, description="Short bounded note string")]
SessionNotesList = Annotated[list[SessionNote], Field(max_length=MAX_NOTES, description="List of at most 8 bounded notes")]


class CheckpointSpec(BaseModel):
    """Pydantic schema for session checkpoint and handoff files."""
    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = Field(description="Checkpoint format version")
    task_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=64, description="Alphanumeric task ID")]
    status: Literal["IN_PROGRESS", "VERIFIED", "COMPLETED", "BLOCKED"]
    goal: Annotated[str, Field(min_length=1, max_length=MAX_GOAL_CHARS, pattern=r"\S")]
    completed: SessionNotesList = Field(default_factory=list)
    next_steps: SessionNotesList = Field(default_factory=list)
    decisions: SessionNotesList = Field(default_factory=list)
    blockers: SessionNotesList = Field(default_factory=list)
    evidence: SessionNotesList = Field(default_factory=list)
    source: Literal["interactive", "managed"] = "interactive"
    created_at: str
    updated_at: str
    revision: Annotated[int, Field(ge=1)] = 1
    host: Literal["claude", "codex", "opencode", "agy"] | None = None
    run: Annotated[str, Field(max_length=64)] | None = None


def validate_checkpoint_schema(record: dict) -> None:
    """Mechanically validate a checkpoint dictionary against CheckpointSpec."""
    if not isinstance(record, dict):
        raise EgoError("INVALID_HANDOFF", "Checkpoint must be an object", 2)
    try:
        CheckpointSpec.model_validate(record)
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise EgoError("INVALID_HANDOFF", f"Checkpoint schema invalid: {loc} ({first.get('msg')})", 2) from err
