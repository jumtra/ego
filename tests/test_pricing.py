"""Shared pricing table, cost formula and C2 usage rows. Pure functions only; no model calls."""
import pytest

from ego.domain.pricing import cost_usd, load_table, resolve_model
from ego.domain.usage import CACHE_TIER_KEYS, CATEGORIES, cache_hit_rate, usage_row

MILLION = 1_000_000


def test_pricing_table_loads_via_importlib_resources():
    # Arrange / Act
    table = load_table()

    # Assert
    assert table["schema_version"] == 1
    assert table["unit"] == "usd_per_mtok"
    assert "claude-opus-5-5" in table["models"]


def test_all_fields_at_one_million_tokens_cost_sum_of_unit_prices():
    # Arrange
    usage = {
        "input_tokens": MILLION,
        "output_tokens": MILLION,
        "cache_read_tokens": MILLION,
        "cache_write_5m_tokens": MILLION,
        "cache_write_1h_tokens": MILLION,
    }

    # Act
    cost = cost_usd(usage, "claude-opus-5-5")

    # Assert
    assert cost == pytest.approx(37.2)


def test_one_hour_cache_write_is_priced_separately_from_five_minute():
    # Arrange
    five_min = {"cache_write_5m_tokens": MILLION}
    one_hour = {"cache_write_1h_tokens": MILLION}

    # Act
    cost_5m = cost_usd(five_min, "claude-opus-5-5")
    cost_1h = cost_usd(one_hour, "claude-opus-5-5")

    # Assert
    assert cost_5m == pytest.approx(5.0)
    assert cost_1h == pytest.approx(8.0)


def test_provider_prefix_and_alias_resolve_to_same_model():
    # Arrange
    usage = {"input_tokens": 1000, "output_tokens": 500}

    # Act
    resolved_full = resolve_model("anthropic/claude-haiku-5-5")
    resolved_alias = resolve_model("haiku")

    # Assert
    assert resolved_full == resolved_alias == "claude-haiku-5-5"
    assert cost_usd(usage, "anthropic/claude-haiku-5-5") == cost_usd(usage, "haiku")


def test_dated_model_suffix_and_dots_resolve_to_table_key():
    # Arrange / Act / Assert
    assert resolve_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert resolve_model("claude-haiku-4.5") == "claude-haiku-4-5"


def test_unknown_model_has_no_price():
    # Arrange
    usage = {"input_tokens": 1000}

    # Act
    cost = cost_usd(usage, "gpt-6-sol")

    # Assert
    assert resolve_model("gpt-6-sol") is None
    assert cost is None


def test_cache_creation_tokens_fallback_is_priced_as_five_minute_write():
    # Arrange
    usage = {"cache_creation_tokens": MILLION}

    # Act
    cost = cost_usd(usage, "claude-opus-5-5")

    # Assert
    assert cost == pytest.approx(5.0)


def test_cache_hit_rate_zero_denominator_is_none():
    # Arrange
    tokens = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0}

    # Act
    rate = cache_hit_rate(tokens)

    # Assert
    assert rate is None


def test_cache_hit_rate_uses_read_over_all_input_side_tokens():
    # Arrange
    tokens = {"input_tokens": 100, "cache_read_tokens": 300, "cache_write_5m_tokens": 50, "cache_write_1h_tokens": 50}

    # Act
    rate = cache_hit_rate(tokens)

    # Assert
    assert rate == pytest.approx(300 / 500)


def test_usage_row_has_c2_keys_and_cost():
    # Arrange
    usage = {"input_tokens": MILLION, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_1h_tokens": MILLION}

    # Act
    row = usage_row(usage, model="opus", category="agent", caller="worker", run_id="r1", node="n1")

    # Assert
    assert {"input_tokens", "output_tokens", "cache_read_tokens", *CACHE_TIER_KEYS, "cost_usd", "category", "caller", "model", "source"} <= set(row)
    assert row["cache_write_1h_tokens"] == MILLION and row["cache_write_5m_tokens"] == 0
    assert row["cost_usd"] == pytest.approx(4.0 + 8.0)
    assert row["source"] == "ego" and row["run_id"] == "r1" and row["node"] == "n1"
    assert row["at"].endswith("+00:00")


def test_usage_row_unknown_model_has_null_cost():
    # Arrange / Act
    row = usage_row({"input_tokens": 10}, model="gpt-6-sol", category="overhead", caller="alter-sync", source="alter")

    # Assert
    assert row["cost_usd"] is None
    assert row["category"] == "overhead" and row["source"] == "alter"


def test_usage_row_rejects_unknown_category():
    # Arrange / Act / Assert
    assert "agent" in CATEGORIES
    with pytest.raises(ValueError):
        usage_row({}, model="haiku", category="other", caller="x")
