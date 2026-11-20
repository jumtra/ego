from dataclasses import replace
"""Tests for task schema definition, mechanical character validation, and planner prompt schema."""
import pytest
from ego.domain.task_schema import (
    DEFAULT_PLAN_SCHEMA_LIMITS,
    validate_task_schema,
    get_plan_prompt_schema,
)
from ego.domain.config import default_config
from ego.shared.util import EgoError
from ego.usecases.work import validate_task


def test_renamed_and_removed_node_fields_are_rejected():
    # Arrange
    base = {"id": "a", "kind": "agent", "prompt": "p", "write_allowlist": ["src/a.py"]}

    # Act & Assert
    with pytest.raises(EgoError, match="renamed to 'reviewer'"):
        validate_task({"nodes": [{**base, "role": "judge"}]})
    with pytest.raises(EgoError, match="max_rounds"):
        validate_task({"nodes": [{**base, "max_retries": 1}]})


def test_values_above_config_caps_are_rejected():
    # Arrange
    caps = default_config()
    base = {"id": "a", "kind": "agent", "prompt": "p", "write_allowlist": ["src/a.py"]}

    # Act & Assert
    for key in ("max_attempts", "max_rounds", "max_turns"):
        with pytest.raises(EgoError, match=f"exceeds config {key}"):
            validate_task({"nodes": [{**base, key: getattr(caps, key) + 1}]}, caps=caps)
    with pytest.raises(EgoError, match="exceeds config max_replans"):
        validate_task({"nodes": [base], "max_replans": caps.max_replans + 1}, caps=caps)


def test_valid_task_passes_schema_validation():
    # Arrange
    spec = {
        "goal": "Build feature with tests",
        "nodes": [
            {
                "id": "impl",
                "kind": "agent",
                "intent": "Implement feature logic",
                "prompt": "Create service implementation",
                "write_allowlist": ["src/service.py"],
                "requirement": {
                    "what": "Service implementation",
                    "where": "src/service.py",
                },
                "steps": ["Create service class in src/service.py"],
                "done_when": ["Service passes unit tests"],
            },
            {
                "id": "check",
                "kind": "gate",
                "depends_on": ["impl"],
                "argv": ["python3", "-m", "pytest", "tests/test_service.py"],
            },
        ],
    }

    # Act & Assert (should not raise)
    validated = validate_task(spec)
    assert len(validated) == 2


def test_intent_exceeding_max_chars_is_detected_mechanically():
    # Arrange
    too_long_intent = "a" * (DEFAULT_PLAN_SCHEMA_LIMITS.max_note_chars + 1)
    spec = {
        "nodes": [
            {
                "id": "impl",
                "kind": "agent",
                "intent": too_long_intent,
                "prompt": "Work on feature",
                "write_allowlist": ["src/**"],
            }
        ]
    }

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        validate_task(spec)

    assert exc_info.value.code == "INVALID_TASK"
    assert "exceeds max length of 400 characters" in str(exc_info.value)


def test_requirement_field_exceeding_max_chars_is_detected_mechanically():
    # Arrange
    too_long_text = "x" * (DEFAULT_PLAN_SCHEMA_LIMITS.max_note_chars + 1)
    spec = {
        "nodes": [
            {
                "id": "impl",
                "kind": "agent",
                "prompt": "Work on feature",
                "write_allowlist": ["src/**"],
                "requirement": {"what": too_long_text},
            }
        ]
    }

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        validate_task(spec)

    assert exc_info.value.code == "INVALID_TASK"
    assert "exceeds max length of 400 characters" in str(exc_info.value)


def test_step_item_exceeding_max_chars_is_detected_mechanically():
    # Arrange
    too_long_step = "s" * (DEFAULT_PLAN_SCHEMA_LIMITS.max_note_chars + 1)
    spec = {
        "nodes": [
            {
                "id": "impl",
                "kind": "agent",
                "prompt": "Work on feature",
                "write_allowlist": ["src/**"],
                "steps": [too_long_step],
            }
        ]
    }

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        validate_task(spec)

    assert exc_info.value.code == "INVALID_TASK"
    assert "exceeds max length of 400 characters" in str(exc_info.value)


def test_steps_exceeding_max_items_is_detected_mechanically():
    # Arrange
    too_many_steps = [f"Step {i}" for i in range(DEFAULT_PLAN_SCHEMA_LIMITS.max_step_items + 1)]
    spec = {
        "nodes": [
            {
                "id": "impl",
                "kind": "agent",
                "prompt": "Work on feature",
                "write_allowlist": ["src/**"],
                "steps": too_many_steps,
            }
        ]
    }

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        validate_task(spec)

    assert exc_info.value.code == "INVALID_TASK"
    assert f"exceeds max items of {DEFAULT_PLAN_SCHEMA_LIMITS.max_step_items}" in str(exc_info.value)


def test_plan_prompt_schema_contains_required_fields_and_limits():
    # Act
    schema = get_plan_prompt_schema()

    # Assert
    assert schema["type"] == "object"
    assert "nodes" in schema["properties"]
    node_props = schema["$defs"]["NodeSpec"]["properties"]
    intent_schema = node_props["intent"]["anyOf"][0]
    steps_schema = node_props["steps"]["anyOf"][0]
    done_when_schema = node_props["done_when"]["anyOf"][0]
    assert intent_schema["maxLength"] == DEFAULT_PLAN_SCHEMA_LIMITS.max_note_chars
    assert steps_schema["maxItems"] == DEFAULT_PLAN_SCHEMA_LIMITS.max_step_items
    assert done_when_schema["maxItems"] == DEFAULT_PLAN_SCHEMA_LIMITS.max_note_items


def test_checkpoint_schema_validation_detects_invalid_fields():
    # Arrange
    from ego.domain.session_schema import validate_checkpoint_schema
    invalid_record = {
        "schema_version": 1,
        "task_id": "t1",
        "status": "INVALID_STATUS",
        "goal": "Build something",
        "created_at": "2026-11-18T00:00:00Z",
        "updated_at": "2026-11-18T00:00:00Z",
    }

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        validate_checkpoint_schema(invalid_record)

    assert exc_info.value.code == "INVALID_HANDOFF"
    assert "status" in str(exc_info.value)


def test_deterministic_node_rejects_role_agent_mcp_and_enforces_role_allowlist():
    # Arrange
    bad_role_code = {
        "nodes": [{"id": "c1", "kind": "code", "role": "coder", "argv": ["pytest"], "write_allowlist": ["src/**"]}]
    }
    bad_mcp_gate = {
        "nodes": [{"id": "g1", "kind": "gate", "mcp": ["alter"], "argv": ["pytest"]}]
    }
    bad_tester_scope = {
        "nodes": [{"id": "t1", "kind": "agent", "role": "tester", "prompt": "test", "write_allowlist": ["src/**"]}]
    }

    # Act & Assert
    with pytest.raises(EgoError) as exc_role:
        validate_task(bad_role_code)
    assert exc_role.value.code == "INVALID_TASK"

    with pytest.raises(EgoError) as exc_mcp:
        validate_task(bad_mcp_gate)
    assert exc_mcp.value.code == "INVALID_TASK"

    with pytest.raises(EgoError, match="tests/") as exc_tester:
        validate_task(bad_tester_scope)
    assert exc_tester.value.code == "INVALID_TASK"


def test_save_checkpoint_validates_against_checkpoint_schema(tmp_path):
    # Arrange
    from ego.usecases.session import save_checkpoint

    # Act & Assert
    with pytest.raises(EgoError) as exc_status:
        save_checkpoint(tmp_path, "t1", goal="Valid goal", status="PASSED")
    assert exc_status.value.code == "INVALID_HANDOFF"

    with pytest.raises(EgoError) as exc_goal:
        save_checkpoint(tmp_path, "t1", goal="   ")
    assert exc_goal.value.code == "INVALID_HANDOFF"

    assert not (tmp_path / ".ego" / "memory" / "handoffs" / "t1.json").exists()


def test_alter_plan_spec_validates_prompt_length_and_structure():
    # Arrange
    from ego.domain.task_schema import validate_alter_plan_spec
    oversized_spec = {
        "slug": "demo",
        "goal": "g",
        "nodes": [{"node_id": "impl", "prompt": "p" * (DEFAULT_PLAN_SCHEMA_LIMITS.max_prompt_chars + 1)}],
    }
    empty_nodes_spec = {"slug": "demo", "goal": "g", "nodes": []}

    # Act & Assert
    with pytest.raises(EgoError) as exc_len:
        validate_alter_plan_spec(oversized_spec)
    assert exc_len.value.code == "INVALID_TASK"
    assert str(DEFAULT_PLAN_SCHEMA_LIMITS.max_prompt_chars) in str(exc_len.value)

    with pytest.raises(EgoError) as exc_empty:
        validate_alter_plan_spec(empty_nodes_spec)
    assert exc_empty.value.code == "INVALID_TASK"


def test_config_pydantic_schema_rejects_out_of_range_and_wrong_types():
    # Arrange
    from ego.domain.config import default_config

    # Act & Assert
    for bad_cfg in (
        replace(default_config(), max_turns=201),
        replace(default_config(), clear_suggest_tokens=500),
        replace(default_config(), cleanup_worktree="yes"),
    ):
        with pytest.raises(EgoError) as exc_info:
            bad_cfg.validate()
        assert exc_info.value.code == "INVALID_CONFIG"


def test_plan_schema_limits_from_config_override_validation_and_prompt_schema(tmp_path):
    # Arrange
    from ego.domain.config import load_config
    from ego.usecases.planner import plan_work

    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "defaults.toml").write_text("[tuning.plan_schema]\nmax_note_chars = 600\nmax_step_items = 2\nmax_goal_chars = 20\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    limits = cfg.plan_schema_limits()

    long_note_spec = {
        "goal": "short goal",
        "nodes": [
            {
                "id": "impl",
                "kind": "agent",
                "intent": "x" * 500,
                "prompt": "Implement",
                "steps": ["step 1", "step 2"],
                "write_allowlist": ["src/**"],
            }
        ],
    }
    too_many_steps_spec = {
        "goal": "short goal",
        "nodes": [
            {
                "id": "impl",
                "kind": "agent",
                "prompt": "Implement",
                "steps": ["step 1", "step 2", "step 3"],
                "write_allowlist": ["src/**"],
            }
        ],
    }

    # Act & Assert
    assert len(validate_task(long_note_spec, limits=limits)) == 1
    with pytest.raises(EgoError) as exc_steps:
        validate_task(too_many_steps_spec, limits=limits)
    assert exc_steps.value.code == "INVALID_TASK"
    assert "max items of 2" in str(exc_steps.value)

    schema = get_plan_prompt_schema(limits)
    assert schema["properties"]["goal"]["maxLength"] == 20
    steps_schema = next(s for s in schema["$defs"]["NodeSpec"]["properties"]["steps"]["anyOf"] if s.get("type") == "array")
    assert steps_schema["maxItems"] == 2

    with pytest.raises(EgoError) as exc_goal:
        plan_work(tmp_path, "x" * 21, host="mock")
    assert exc_goal.value.code == "INVALID_GOAL"


def test_validate_task_reports_string_too_short_message():
    spec = {
        "goal": "valid goal",
        "nodes": [{"id": "impl", "kind": "agent", "intent": "", "prompt": "Implement", "write_allowlist": ["src/**"]}],
    }
    with pytest.raises(EgoError) as exc:
        validate_task(spec)
    assert exc.value.code == "INVALID_TASK"
    assert "must be a nonempty string" in str(exc.value)
