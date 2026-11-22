"""Usage ledger: Claude cache tiers and model capture, JSONL rows, agent/overhead summary and the CLI. No model calls."""
import json

import pytest

from ego.adapters.runner import parse_host_usage
from ego.cli import main
from ego.domain.usage import usage_row
from ego.usecases import ledger, work


def _claude_result() -> str:
    return json.dumps({
        "type": "result",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 200,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
        },
        "modelUsage": {"claude-haiku-5-5": {"outputTokens": 20}, "claude-opus-5-5": {"outputTokens": 180}},
    })


def _record(model: str = "claude-opus-5-5") -> dict:
    return {
        "run_id": "r1",
        "planner_usage": {"input_tokens": 100, "output_tokens": 10, "model": "claude-haiku-5-5"},
        "results": {"a": {"attempts": [{"execution": {"host": "claude", "model": model, "usage": {
            "input_tokens": 1000, "output_tokens": 100, "cache_write_5m_tokens": 50, "cache_write_1h_tokens": 0}}}]}},
    }


def test_claude_result_captures_cache_tiers_and_model():
    usage = parse_host_usage("claude", _claude_result())
    assert usage["cache_write_5m_tokens"] == 100
    assert usage["cache_write_1h_tokens"] == 200
    assert usage["cache_creation_tokens"] == 300
    assert usage["input_tokens"] == 10 and usage["cache_read_tokens"] == 500
    assert usage["model"] == "claude-opus-5-5"


def test_claude_result_without_cache_creation_dict_adds_no_tier_keys():
    raw = json.dumps({"type": "result", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3}})
    usage = parse_host_usage("claude", raw)
    assert "cache_write_5m_tokens" not in usage and "cache_write_1h_tokens" not in usage
    assert "model" not in usage


def test_summarize_skips_malformed_line(tmp_path):
    ledger.append_rows(tmp_path, [usage_row({"input_tokens": 1000, "output_tokens": 100}, model="claude-haiku-5-5", category="agent", caller="node:a")])
    with (tmp_path / ledger.LEDGER).open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    summary = ledger.summarize(tmp_path, 7)
    assert summary["skipped_lines"] == 1
    assert summary["by_category"]["agent"]["calls"] == 1


def test_agent_and_overhead_split_and_unknown_model_is_unpriced(tmp_path):
    agent_row = usage_row({"input_tokens": 1000, "output_tokens": 500, "cache_write_5m_tokens": 100, "cache_write_1h_tokens": 200},
                          model="claude-opus-5-5", category="agent", caller="node:a")
    overhead_row = usage_row({"input_tokens": 200}, model="claude-opus-5-5", category="overhead", caller="planner")
    unknown_row = usage_row({"input_tokens": 50}, model="native-default", category="agent", caller="node:b")
    assert unknown_row["cost_usd"] is None
    ledger.append_rows(tmp_path, [agent_row, overhead_row, unknown_row])
    summary = ledger.summarize(tmp_path, 7)
    assert summary["by_category"]["agent"]["calls"] == 2
    assert summary["by_category"]["overhead"]["calls"] == 1
    assert summary["by_category"]["agent"]["unpriced_calls"] == 1
    assert summary["by_category"]["overhead"]["unpriced_calls"] == 0
    assert summary["unpriced_calls"] == 1
    assert summary["total_cost_usd"] == pytest.approx(agent_row["cost_usd"] + overhead_row["cost_usd"])
    assert summary["by_caller"]["planner"]["calls"] == 1


def test_write_ledger_is_idempotent(tmp_path):
    record = _record()
    work._write_ledger(tmp_path, record, "r1")
    work._write_ledger(tmp_path, record, "r1")
    rows = [json.loads(line) for line in (tmp_path / ledger.LEDGER).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert {row["category"] for row in rows} == {"agent", "overhead"}
    assert {row["caller"] for row in rows} == {"planner", "node:a"}


def test_usage_summary_splits_agent_and_overhead_with_estimated_cost():
    total = work.usage_summary(_record())
    assert total["by_category"]["agent"]["tokens"] == 1100
    assert total["by_category"]["overhead"]["tokens"] == 110
    assert total["cache_write_5m_tokens"] == 50
    assert total["estimated_cost_usd"] is not None
    assert total["estimated_cost_usd"] == pytest.approx(total["by_category"]["agent"]["cost_usd"] + total["by_category"]["overhead"]["cost_usd"])


def test_usage_summary_estimated_cost_is_none_for_unknown_model():
    total = work.usage_summary(_record(model="native-default"))
    assert total["by_category"]["agent"]["cost_usd"] is None
    assert total["estimated_cost_usd"] is None


def test_cli_usage_summary_prints_json(tmp_path, capsys):
    ledger.append_rows(tmp_path, [usage_row({"input_tokens": 1}, model="claude-haiku-5-5", category="overhead", caller="planner")])
    assert main(["usage", "summary", "--root", str(tmp_path), "--days", "7"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["by_category"]["overhead"]["calls"] == 1
    assert out["days"] == 7
