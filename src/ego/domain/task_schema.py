"""Task and plan schema definition and mechanical validation using Pydantic."""
from __future__ import annotations
from dataclasses import dataclass
import fnmatch
from functools import lru_cache
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator, model_validator
from ..shared.util import EgoError

# Centralized character and item limits (Single Source of Truth)
MAX_GOAL_CHARS = 6000
MAX_INSTRUCTIONS_CHARS = 65536
MAX_INSTRUCTIONS_FILE_CHARS = 512
MAX_NOTE_CHARS = 400
MAX_NOTE_ITEMS = 6
MAX_STEP_ITEMS = 10
MAX_AGENT_CHARS = 128
MAX_MCP_ITEMS = 8
MAX_MCP_CHARS = 64
MAX_CONTEXT_FILES = 24
MAX_NODE_ID_CHARS = 64
MAX_PROMPT_CHARS = 200

REQUIREMENT_KEYS = ["who", "what", "when", "where", "why", "how"]


@dataclass(frozen=True)
class PlanSchemaLimits:
    """Configurable character and item limits for task and plan schemas."""
    max_goal_chars: int = MAX_GOAL_CHARS
    max_instructions_chars: int = MAX_INSTRUCTIONS_CHARS
    max_instructions_file_chars: int = MAX_INSTRUCTIONS_FILE_CHARS
    max_note_chars: int = MAX_NOTE_CHARS
    max_note_items: int = MAX_NOTE_ITEMS
    max_step_items: int = MAX_STEP_ITEMS
    max_agent_chars: int = MAX_AGENT_CHARS
    max_mcp_items: int = MAX_MCP_ITEMS
    max_mcp_chars: int = MAX_MCP_CHARS
    max_context_files: int = MAX_CONTEXT_FILES
    max_node_id_chars: int = MAX_NODE_ID_CHARS
    max_prompt_chars: int = MAX_PROMPT_CHARS


DEFAULT_PLAN_SCHEMA_LIMITS = PlanSchemaLimits()


@dataclass(frozen=True)
class TaskSchemaModels:
    """Compiled Pydantic models bound to a specific PlanSchemaLimits instance."""
    RequirementSpec: type[BaseModel]
    NodeSpec: type[BaseModel]
    TaskSpec: type[BaseModel]
    AlterPlanNodeSpec: type[BaseModel]
    AlterPlanSpec: type[BaseModel]


@lru_cache(maxsize=16)
def build_task_schema_models(limits: PlanSchemaLimits = DEFAULT_PLAN_SCHEMA_LIMITS) -> TaskSchemaModels:
    """Build and cache Pydantic task/plan models for the given limits."""
    note_string = Annotated[
        str,
        Field(
            min_length=1,
            max_length=limits.max_note_chars,
            pattern=r"\S",
            description=f"Non-empty concise string (at most {limits.max_note_chars} characters)",
        ),
    ]
    note_list = Annotated[
        list[note_string],
        Field(
            max_length=limits.max_note_items,
            description=f"List of at most {limits.max_note_items} concise note strings",
        ),
    ]

    class RequirementSpec(BaseModel):
        """5W1H implementation requirement breakdown. Each field should be one short concrete sentence."""
        model_config = ConfigDict(extra="forbid")

        who: note_string | None = Field(default=None, description="Who executes this node (e.g. worker or role name)")
        what: note_string | None = Field(default=None, description="What concrete deliverable or change to achieve")
        when: note_string | None = Field(default=None, description="When to run relative to dependencies")
        where: note_string | None = Field(default=None, description="Where in the codebase (exact files or directories)")
        why: note_string | None = Field(default=None, description="Why this node is needed to satisfy the goal")
        how: note_string | None = Field(default=None, description="How to implement it concretely")

        @model_validator(mode="after")
        def _validate_non_empty(self) -> RequirementSpec:
            if not any(v is not None for v in (self.who, self.what, self.when, self.where, self.why, self.how)):
                raise ValueError("requirement must be a nonempty object")
            return self

    class NodeSpec(BaseModel):
        """Pydantic model representing a single task node with field & length constraints."""
        model_config = ConfigDict(extra="allow")

        id: Annotated[
            str,
            Field(
                pattern=r"^[a-zA-Z0-9_-]+$",
                max_length=limits.max_node_id_chars,
                description="Unique alphanumeric node identifier",
            ),
        ]
        kind: Annotated[
            Literal["worker", "agent", "code", "gate", "splitter"],
            Field(description="Node type: 'agent' for AI worker, 'code' or 'gate' for deterministic commands (every code/gate node MUST include argv or checks)"),
        ] = "agent"
        role: Annotated[
            Literal["coder", "tester", "judge"] | None,
            Field(default=None, description="Optional role for agent nodes: 'coder', 'tester' (writes under tests/ only), or 'judge' (read-only review gate; omit unless the change is risky)"),
        ] = None
        depends_on: Annotated[
            list[str] | None,
            Field(default=None, description="IDs of prerequisite nodes. Use minimal real dependencies only"),
        ] = None
        intent: Annotated[
            note_string | None,
            Field(default=None, description="One sentence explaining why the goal needs this node"),
        ] = None
        prompt: Annotated[
            str | None,
            Field(default=None, description="Required for agent nodes: what to achieve (concise summary; put detailed implementation steps in steps)"),
        ] = None
        write_allowlist: Annotated[
            list[str] | None,
            Field(default=None, description="Allowed file path patterns to write/modify (e.g. ['src/**', 'tests/**'])"),
        ] = None
        argv: Annotated[
            list[str] | None,
            Field(default=None, min_length=1, description="Command argument array for code/gate nodes (e.g. ['pytest', 'tests/test_x.py'])"),
        ] = None
        checks: Annotated[
            list[Annotated[list[str], Field(min_length=1)]] | None,
            Field(default=None, description="List of command argument arrays to run as verification checks"),
        ] = None
        requirement: Annotated[
            RequirementSpec | None,
            Field(default=None, description="5W1H breakdown (who, what, when, where, why, how)"),
        ] = None
        steps: Annotated[
            list[note_string] | None,
            Field(
                default=None,
                min_length=1,
                max_length=limits.max_step_items,
                description=f"1-{limits.max_step_items} concrete ordered steps naming file, section, and exact change. Never write 'consider', 'as appropriate' or 'refactor as needed'; inspect candidate files until specific.",
            ),
        ] = None
        done_when: Annotated[
            note_list | None,
            Field(default=None, description="1-4 observable acceptance criteria for node completion"),
        ] = None
        non_goals: Annotated[
            note_list | None,
            Field(default=None, description="Explicit out-of-scope temptations this node must not do"),
        ] = None
        assumptions: Annotated[
            note_list | None,
            Field(default=None, description="Unverified facts relied upon; worker halts if false"),
        ] = None
        provides: Annotated[
            note_list | None,
            Field(default=None, description="Files or interfaces this node provides for dependent nodes"),
        ] = None
        inputs: Annotated[
            note_list | None,
            Field(default=None, description=f"Existing files to read (at most {limits.max_note_items})"),
        ] = None
        outputs: Annotated[
            note_list | None,
            Field(default=None, description="Files or symbols to change, as 'path' or 'path::function', inside write_allowlist"),
        ] = None
        agent: Annotated[
            str | None,
            Field(
                default=None,
                min_length=1,
                max_length=limits.max_agent_chars,
                pattern=r"^[^/\\]+$",
                description="Optional custom agent name from available agents list",
            ),
        ] = None
        mcp: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=limits.max_mcp_chars)]] | None,
            Field(default=None, min_length=1, max_length=limits.max_mcp_items, description="Required MCP servers (e.g. ['alter'])"),
        ] = None
        context_files: Annotated[
            list[str] | None,
            Field(default=None, max_length=limits.max_context_files, description=f"Up to {limits.max_context_files} context file paths"),
        ] = None
        instructions_file: Annotated[str | None, Field(default=None, max_length=limits.max_instructions_file_chars)] = None
        strategy: Annotated[Literal["single", "fallback", "compare"] | None, Field(default=None)] = None
        host: Annotated[str | None, Field(default=None)] = None
        hosts: Annotated[list[str] | None, Field(default=None)] = None
        model: Annotated[str | dict[str, Any] | None, Field(default=None)] = None
        models: Annotated[dict[str, Any] | None, Field(default=None)] = None
        fallback_on: Annotated[list[Literal["UNAVAILABLE", "TIMEOUT", "RATE_LIMIT"]] | None, Field(default=None)] = None
        reviews: Annotated[str | None, Field(default=None, description="For role='judge': coder node ID to review (judge must also depend on a gate or code node)")] = None
        max_retries: Annotated[StrictInt | None, Field(default=None, ge=0, le=2)] = None
        max_attempts: Annotated[StrictInt | None, Field(default=None, ge=1)] = None
        source: Annotated[dict[str, Any] | None, Field(default=None)] = None

        @field_validator("agent")
        @classmethod
        def _validate_agent(cls, v: str | None) -> str | None:
            if v is not None and ".." in v:
                raise ValueError("agent name cannot contain '..'")
            return v

        @model_validator(mode="after")
        def _validate_deterministic_node(self) -> NodeSpec:
            if self.kind in ("code", "gate"):
                if self.role is not None:
                    raise ValueError(f"Deterministic node {self.id} cannot have a role")
                if self.agent is not None or self.mcp is not None:
                    raise ValueError(f"Deterministic node {self.id} cannot have agent or mcp")
            return self

    class TaskSpec(BaseModel):
        """Pydantic model representing a complete Ego task specification."""
        model_config = ConfigDict(extra="allow")

        goal: Annotated[str, Field(max_length=limits.max_goal_chars, description="Overall goal statement")] = ""
        instructions: Annotated[str, Field(max_length=limits.max_instructions_chars, description="Task-wide instructions")] = ""
        instructions_file: Annotated[str | None, Field(default=None, max_length=limits.max_instructions_file_chars)] = None
        defaults: Annotated[dict[str, Any] | None, Field(default=None, description="Task defaults for host/models")] = None
        source: Annotated[dict[str, Any] | None, Field(default=None)] = None
        nodes: Annotated[list[NodeSpec], Field(min_length=1, description="Non-empty list of DAG execution nodes")]

        @model_validator(mode="after")
        def _validate_task_nodes(self) -> TaskSpec:
            ids = [n.id for n in self.nodes]
            if len(ids) != len(set(ids)):
                raise ValueError("Invalid/duplicate node ids")
            for n in self.nodes:
                if n.kind in ("code", "gate") and not (n.argv or n.checks):
                    raise ValueError("Code/gate nodes need argv or checks")
                if n.kind not in ("code", "gate") and n.prompt is None:
                    raise ValueError("Agent nodes need prompt")
                if n.role == "judge" and n.write_allowlist:
                    raise ValueError(f"judge {n.id} is read-only: no write_allowlist")
                if n.role == "tester" and any(not str(p).startswith("tests/") for p in (n.write_allowlist or [])):
                    raise ValueError(f"tester {n.id} may only write under tests/")
                if n.kind not in ("gate", "splitter") and n.role != "judge" and not n.write_allowlist:
                    raise ValueError(f"write_allowlist required for {n.id}")
                if n.kind not in ("code", "gate"):
                    allow = n.write_allowlist or []
                    for item in n.outputs or []:
                        path = item.split("::")[0].strip()
                        if not any(fnmatch.fnmatch(path, pat) for pat in allow):
                            raise ValueError(f"output {item} of {n.id} is outside write_allowlist")
            return self

    class AlterPlanNodeSpec(BaseModel):
        """Pydantic schema for a single node inside an alter plan specification."""
        model_config = ConfigDict(extra="allow")

        node_id: Annotated[str, Field(min_length=1)]
        prompt: Annotated[str, Field(max_length=limits.max_prompt_chars)] = ""
        depends_on: list[str] = Field(default_factory=list)
        intent: str = ""
        requirement: dict[str, Any] = Field(default_factory=dict)
        steps: list[str] = Field(default_factory=list)
        where: list[str] = Field(default_factory=list)
        done_when: list[str] = Field(default_factory=list)
        checks: list[str] = Field(default_factory=list)
        non_goals: list[str] = Field(default_factory=list)
        assumptions: list[str] = Field(default_factory=list)
        provides: list[str] = Field(default_factory=list)
        extra: dict[str, Any] = Field(default_factory=dict)

    class AlterPlanSpec(BaseModel):
        """Pydantic schema for an alter plan specification exchanged with alter_ego_plan."""
        model_config = ConfigDict(extra="allow")

        slug: Annotated[str, Field(min_length=1)]
        goal: str = ""
        done: str = ""
        instructions: str = ""
        extra: dict[str, Any] = Field(default_factory=dict)
        nodes: Annotated[list[AlterPlanNodeSpec], Field(min_length=1)]

    # Rebuild models with local type namespace because `from __future__ import annotations` defers evaluation.
    ns = {
        "note_string": note_string,
        "note_list": note_list,
        "RequirementSpec": RequirementSpec,
        "NodeSpec": NodeSpec,
        "TaskSpec": TaskSpec,
        "AlterPlanNodeSpec": AlterPlanNodeSpec,
        "AlterPlanSpec": AlterPlanSpec,
    }
    for cls in (RequirementSpec, NodeSpec, TaskSpec, AlterPlanNodeSpec, AlterPlanSpec):
        cls.model_rebuild(_types_namespace=ns)

    return TaskSchemaModels(
        RequirementSpec=RequirementSpec,
        NodeSpec=NodeSpec,
        TaskSpec=TaskSpec,
        AlterPlanNodeSpec=AlterPlanNodeSpec,
        AlterPlanSpec=AlterPlanSpec,
    )


_DEFAULT_MODELS = build_task_schema_models(DEFAULT_PLAN_SCHEMA_LIMITS)
NodeSpec = _DEFAULT_MODELS.NodeSpec


class AlterPushNodeSpec(BaseModel):
    """Required fields enforced before pushing a task plan to alter."""
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["worker", "agent", "code", "gate", "splitter"] = "agent"
    role: Literal["coder", "tester", "judge"] | None = None
    prompt: str | None = None
    steps: list[str] | None = None
    write_allowlist: list[str] | None = None

    @model_validator(mode="after")
    def _validate_push_requirements(self) -> AlterPushNodeSpec:
        errors: list[str] = []
        is_judge = self.role == "judge"
        if not str(self.prompt or "").strip():
            errors.append(f"{self.id}: prompt (title) is empty")
        if self.kind not in ("code", "gate") and not self.steps:
            errors.append(f"{self.id}: steps are empty")
        if self.kind != "gate" and not is_judge and not self.write_allowlist:
            errors.append(f"{self.id}: write_allowlist (where) is empty")
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _format_pydantic_error(err: ValidationError, prefix: str = "", limits: PlanSchemaLimits = DEFAULT_PLAN_SCHEMA_LIMITS) -> str:
    """Format Pydantic ValidationError into a clear message containing 'must be' for test compatibility."""
    first = err.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    target = f"{prefix}.{loc}" if (prefix and loc) else prefix or loc or "root"
    err_type = first.get("type", "")
    ctx = first.get("ctx", {})

    if "string_too_long" in err_type:
        limit = ctx.get("max_length", limits.max_note_chars)
        return f"{target} exceeds max length of {limit} characters; must be <= {limit} chars"
    if "too_many_items" in err_type or err_type == "too_long":
        limit = ctx.get("max_length", limits.max_note_items)
        return f"{target} exceeds max items of {limit}; must be <= {limit} items"
    if "string_too_short" in err_type:
        return f"{target} must be a nonempty string"
    if "too_short" in err_type:
        limit = ctx.get("min_length", 1)
        return f"{target} requires at least {limit} items; must be at least {limit} items"
    if "string_pattern_mismatch" in err_type:
        return f"{target} format is invalid; must be valid pattern"
    if "missing" in err_type:
        return f"{target} missing required property; must be provided"
    if "extra_forbidden" in err_type:
        return f"{target} is not allowed; must be valid"
    if "literal_error" in err_type:
        return f"{target} must be one of {ctx.get('expected', '')}"
    return f"{target}: {first.get('msg', 'validation error')}; must be valid"


def _models_for(limits: PlanSchemaLimits | None) -> tuple[PlanSchemaLimits, TaskSchemaModels]:
    resolved = limits or DEFAULT_PLAN_SCHEMA_LIMITS
    return resolved, build_task_schema_models(resolved)


def validate_task_schema(spec: dict, *, limits: PlanSchemaLimits | None = None) -> None:
    """Validate full task dictionary via Pydantic model."""
    if not isinstance(spec, dict):
        raise EgoError("INVALID_TASK", "Task must be an object", 2)
    resolved, models = _models_for(limits)
    try:
        models.TaskSpec.model_validate(spec)
    except ValidationError as err:
        raise EgoError("INVALID_TASK", _format_pydantic_error(err, limits=resolved), 2) from err


def validate_node_schema(node: dict, *, limits: PlanSchemaLimits | None = None) -> None:
    """Validate single node dictionary via Pydantic NodeSpec model."""
    if not isinstance(node, dict):
        raise EgoError("INVALID_TASK", "Node must be an object", 2)
    prefix = str(node.get("id", "node"))
    resolved, models = _models_for(limits)
    try:
        models.NodeSpec.model_validate(node)
    except ValidationError as err:
        raise EgoError("INVALID_TASK", _format_pydantic_error(err, prefix=prefix, limits=resolved), 2) from err


def validate_alter_plan_spec(spec: dict, *, limits: PlanSchemaLimits | None = None) -> None:
    """Validate a full alter plan dictionary via AlterPlanSpec."""
    if not isinstance(spec, dict):
        raise EgoError("INVALID_TASK", "Alter plan spec must be an object", 2)
    resolved, models = _models_for(limits)
    try:
        models.AlterPlanSpec.model_validate(spec)
    except ValidationError as err:
        first = err.errors()[0]
        loc_parts = first.get("loc", ())
        if len(loc_parts) >= 3 and loc_parts[0] == "nodes" and isinstance(loc_parts[1], int):
            nodes = spec.get("nodes") or []
            idx = loc_parts[1]
            node_id = str(nodes[idx].get("node_id", f"nodes[{idx}]")) if idx < len(nodes) and isinstance(nodes[idx], dict) else f"nodes[{idx}]"
            if loc_parts[2:] == ("prompt",) and "string_too_long" in first.get("type", ""):
                raise EgoError(
                    "INVALID_TASK",
                    f"prompt of {node_id} exceeds {resolved.max_prompt_chars} characters; shorten it and move detail into steps",
                    2,
                ) from err
        raise EgoError("INVALID_TASK", _format_pydantic_error(err, limits=resolved), 2) from err


def get_plan_prompt_schema(limits: PlanSchemaLimits | None = None) -> dict[str, Any]:
    """Return JSON Schema generated directly by Pydantic for planner model prompts."""
    _, models = _models_for(limits)
    return models.TaskSpec.model_json_schema()
