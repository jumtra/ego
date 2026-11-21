"""Externalized defaults: packaged defaults.toml, project .ego/defaults.toml [tuning] override, legacy config.toml."""
import pytest
from ego.domain.config import load_config
from ego.shared.util import EgoError


def test_project_tuning_override_is_applied(tmp_path):
    # Arrange
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "defaults.toml").write_text("[tuning]\nmax_feedback_chars = 10\n", encoding="utf-8")

    # Act
    cfg = load_config(tmp_path)

    # Assert
    assert cfg.tuning.max_feedback_chars == 10


def test_project_override_reaches_context_hits(tmp_path):
    # Arrange
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "defaults.toml").write_text("[tuning]\nmax_context_hits = 2\n", encoding="utf-8")

    # Act
    cfg = load_config(tmp_path)

    # Assert
    assert cfg.tuning.max_context_hits == 2


def test_unknown_tuning_key_is_rejected(tmp_path):
    # Arrange
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "defaults.toml").write_text("[tuning]\nno_such_limit = 10\n", encoding="utf-8")

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        load_config(tmp_path)
    assert exc_info.value.code == "INVALID_CONFIG"


def test_plan_schema_in_config_toml_names_new_location(tmp_path):
    # Arrange
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "config.toml").write_text("plan_schema = {max_note_chars = 600}\n", encoding="utf-8")

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        load_config(tmp_path)
    assert exc_info.value.code == "INVALID_CONFIG"
    assert ".ego/defaults.toml [tuning.plan_schema]" in str(exc_info.value)


def test_existing_config_without_new_keys_uses_package_defaults(tmp_path):
    # Arrange
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "config.toml").write_text('schema_version = 1\nprofile = "minimal"\n', encoding="utf-8")

    # Act
    cfg = load_config(tmp_path)

    # Assert
    assert (cfg.max_rounds, cfg.max_replans, cfg.max_run_tokens) == (2, 1, 0)
    assert cfg.tuning.max_summary_chars == 800


def test_max_run_tokens_zero_is_accepted_by_load_config(tmp_path):
    # Arrange
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego" / "config.toml").write_text("max_run_tokens = 0\n", encoding="utf-8")

    # Act
    cfg_zero = load_config(tmp_path)
    (tmp_path / ".ego" / "config.toml").write_text("max_run_tokens = 250000\n", encoding="utf-8")
    cfg_pos = load_config(tmp_path)

    # Assert
    assert cfg_zero.max_run_tokens == 0
    assert cfg_pos.max_run_tokens == 250000
