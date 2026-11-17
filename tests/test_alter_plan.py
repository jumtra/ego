from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

from ego.adapters.alter_plan import AlterPlanClient
from ego.shared.util import EgoError

FAKE_SERVER = '''
import json, os, sys
log = os.environ["ALTER_LOG"]
for line in sys.stdin:
    req = json.loads(line)
    if req.get("method") == "initialize":
        out = {"jsonrpc": "2.0", "id": req["id"], "result": {"capabilities": {}}}
    elif req.get("method") == "tools/call":
        params = req["params"]
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(params) + "\\n")
        args = params["arguments"]
        key = args.get("action", params["name"])
        if key == "claim" and args.get("spec_hash") == "bad":
            out = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32000, "message": "409 spec changed"}}
        else:
            payload = {
                "alter_bootstrap": {"scope": "proj-scope"},
                "create": {"plan_id": "plan-1", "node_tasks": {"impl": "task-1"}, "state": "draft"},
                "get": {"plan_id": args.get("plan_id"), "state": "ready"},
                "claim": {"state": "taken"},
                "report": {"ok": True},
            }[key]
            out = {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
'''


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "alter_calls.jsonl"
    server = tmp_path / "fake_alter_plan.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"alter": {
        "type": "stdio", "command": sys.executable, "args": [str(server)], "env": {"ALTER_LOG": str(log)}}}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("EGO_ALTER_MCP_COMMAND", raising=False)
    return log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_create_sends_scope_and_spec_and_returns_plan_id(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    client = AlterPlanClient(tmp_path)
    spec = {"slug": "login", "goal": "Add login", "done": "tests pass", "instructions": "",
            "extra": {}, "nodes": [{"node_id": "impl", "prompt": "Implement login"}]}

    created = client.create(spec, "proj-scope")

    assert created["plan_id"] == "plan-1"
    args = _calls(log)[0]["arguments"]
    assert args["action"] == "create"
    assert args["scope"] == "proj-scope"
    assert args["slug"] == "login"
    assert args["nodes"] == spec["nodes"]


def test_get_and_report_send_contract_argument_names(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    client = AlterPlanClient(tmp_path)

    client.get("plan-1")
    client.report("plan-1", "run-1", "passed", {"impl": {"status": "passed", "attempts": 1, "failure": None}})

    get_args, report_args = [call["arguments"] for call in _calls(log)]
    assert get_args == {"action": "get", "plan_id": "plan-1"}
    assert report_args["action"] == "report"
    assert report_args["run_id"] == "run-1"
    assert report_args["state"] == "passed"
    assert report_args["nodes"]["impl"]["attempts"] == 1


def test_claim_with_stale_hash_maps_to_plan_conflict(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    client = AlterPlanClient(tmp_path)

    with pytest.raises(EgoError) as exc:
        client.claim("plan-1", "run-1", "bad")

    assert exc.value.code == "ALTER_PLAN_CONFLICT"


def test_missing_alter_config_is_unavailable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("EGO_ALTER_MCP_COMMAND", raising=False)

    with pytest.raises(EgoError) as exc:
        AlterPlanClient(tmp_path)

    assert exc.value.code == "ALTER_UNAVAILABLE"
