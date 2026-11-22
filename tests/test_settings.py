"""Typed settings sections: packaged defaults, project override, strict validation."""
import pytest
from ego.domain.config import load_config
from ego.domain.settings import load_settings
from ego.shared.util import EgoError


def _write_override(root, text):
    (root / ".ego").mkdir(exist_ok=True)
    (root / ".ego" / "defaults.toml").write_text(text, encoding="utf-8")


def test_packaged_defaults_load(tmp_path):
    # Arrange
    root = tmp_path

    # Act
    settings = load_settings(root)

    # Assert
    assert settings.usage.session_cost_warning_usd == 5.0
    assert settings.usage.track_cache_tiers is True
    assert settings.lifecycle.min_confirmations == 2
    assert settings.loopguard.dry_run is True
    assert settings.toolprune.unused_days == 14
    assert settings.mcp.alter_worker_profile == "ego-worker"
    assert settings.analysis.min_user_chars == 400


def test_loopguard_dry_run_override_applies(tmp_path):
    # Arrange
    _write_override(tmp_path, "[loopguard]\ndry_run = false\n")

    # Act
    settings = load_settings(tmp_path)

    # Assert
    assert settings.loopguard.dry_run is False
    assert settings.loopguard.enabled is True


def test_int_is_accepted_for_float_default(tmp_path):
    # Arrange
    _write_override(tmp_path, "[usage]\nsession_cost_warning_usd = 3\n")

    # Act
    settings = load_settings(tmp_path)

    # Assert
    assert settings.usage.session_cost_warning_usd == 3.0
    assert isinstance(settings.usage.session_cost_warning_usd, float)


def test_int_for_bool_is_rejected(tmp_path):
    # Arrange
    _write_override(tmp_path, "[loopguard]\ndry_run = 1\n")

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        load_settings(tmp_path)
    assert exc_info.value.code == "INVALID_CONFIG"


def test_unknown_settings_key_is_rejected(tmp_path):
    # Arrange
    _write_override(tmp_path, "[loopguard]\nno_such_knob = true\n")

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        load_settings(tmp_path)
    assert exc_info.value.code == "INVALID_CONFIG"


def test_negative_cost_warning_is_rejected(tmp_path):
    # Arrange
    _write_override(tmp_path, "[usage]\nsession_cost_warning_usd = -1\n")

    # Act & Assert
    with pytest.raises(EgoError) as exc_info:
        load_settings(tmp_path)
    assert exc_info.value.code == "INVALID_CONFIG"


def test_existing_tuning_override_still_works(tmp_path):
    # Arrange
    _write_override(tmp_path, "[tuning]\nmax_feedback_chars = 10\n")

    # Act
    settings = load_settings(tmp_path)
    cfg = load_config(tmp_path)

    # Assert
    assert cfg.tuning.max_feedback_chars == 10
    assert settings.usage.track_cache_tiers is True
