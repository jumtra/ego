"""Tests for EnvFacts lifecycle, atomic store, prompt-tail injection, alter feedback, and CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ego.adapters import envfact_store
from ego.adapters.runner import RunResult
from ego.cli import main
from ego.domain.config import default_config, save_config
from ego.domain.envfacts import Observation
from ego.domain.fact_lifecycle import Fact, fact_id, observe
from ego.domain.settings import LifecycleSettings, load_settings
from ego.usecases.work import _run_node


def _settings(root: Path | None = None) -> LifecycleSettings:
    return load_settings(root).lifecycle


def test_single_session_stays_candidate_and_not_injected(tmp_path: Path):
    s = _settings()
    fid = fact_id("test", "pytest")
    f0 = Fact(id=fid, key="pytest", category="test")
    f1 = observe(f0, Observation(kind="works", key="pytest", category="test", session="s1"), s)
    assert f1.state == "candidate"
    assert f1.confirmations == ["s1"]
    assert f0.state == "candidate" and f0.confirmations == []

    # Same session repeated does not promote
    f1_dup = observe(f1, Observation(kind="works", key="pytest", category="test", session="s1"), s)
    assert f1_dup.state == "candidate"
    assert f1_dup.confirmations == ["s1"]

    envfact_store.save(tmp_path, {fid: f1_dup})
    assert envfact_store.active_facts(tmp_path, s) == []


def test_second_session_promotes_to_active_and_injects(tmp_path: Path):
    s = _settings()
    fid = fact_id("test", "pytest")
    f = Fact(id=fid, key="pytest", category="test")
    f = observe(f, Observation(kind="works", key="pytest", category="test", session="s1"), s)
    f = observe(f, Observation(kind="works", key="pytest", category="test", session="s2"), s)
    assert f.state == "active"
    assert f.confirmations == ["s1", "s2"]

    envfact_store.save(tmp_path, {fid: f})
    assert envfact_store.active_facts(tmp_path, s) == ["test: pytest"]


def test_active_facts_caps_items_and_chars(tmp_path: Path):
    s = LifecycleSettings(
        enable_envfacts=True,
        min_confirmations=2,
        contradiction_demote_threshold=2,
        max_injected_facts=2,
        max_injected_chars=25,
        alter_feedback=True,
    )
    facts = {
        "a": Fact(id="a", key="pytest -q", category="test", state="active", confirmations=["s1", "s2"]),
        "b": Fact(id="b", key="ruff check", category="lint", state="active", confirmations=["s1", "s2"]),
        "c": Fact(id="c", key="cargo test", category="test", state="active", confirmations=["s1", "s2"]),
    }
    envfact_store.save(tmp_path, facts)
    lines = envfact_store.active_facts(tmp_path, s)
    assert lines == ["test: pytest -q"]  # second item would exceed 25 chars


def test_two_fails_demote_and_notify_alter_in_run_node(tmp_path: Path, monkeypatch):
    cfg = default_config(tmp_path)
    cfg.knowledge = "alter"
    save_config(tmp_path, cfg)

    fid = fact_id("test", "pytest")
    active = Fact(id=fid, key="pytest", category="test", state="active", confirmations=["s1", "s2"])
    envfact_store.save(tmp_path, {fid: active})

    claude_dir = tmp_path / "claude_cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    captured_prompts: list[str] = []
    notified: list[tuple[str, str, int]] = []

    def fake_run_worker(host, prompt, work, timeout, **kwargs):
        captured_prompts.append(prompt)
        # Write a Claude transcript with two failing pytest invocations in the same session
        import re
        proj_slug = re.sub(r"[^A-Za-z0-9]", "-", str(work))
        tpath = claude_dir / "projects" / proj_slug / "sess-fail.jsonl"
        tpath.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "pytest"}},
                    {"type": "tool_use", "id": "tu2", "name": "Bash", "input": {"command": "pytest"}},
                ]},
            }),
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "is_error": True, "content": "fail 1"},
                    {"type": "tool_result", "tool_use_id": "tu2", "is_error": True, "content": "fail 2"},
                ]},
            }),
        ]
        tpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return RunResult("PASS", "ok", "", 0, 0.1, "claude", session_id="sess-fail")

    def fake_notify(root, fact, kind):
        notified.append((fact.id, kind, fact.contradictions))

    monkeypatch.setattr("ego.usecases.work.run_worker", fake_run_worker)
    monkeypatch.setattr("ego.usecases.work.notify_alter", fake_notify)
    monkeypatch.setattr("ego.usecases.session.alter_prompt_context", lambda root, q: "")

    node = {"id": "n1", "kind": "agent", "intent": "run tests", "prompt": "check", "host": "claude"}
    spec = {"goal": "g", "nodes": [node]}
    res = _run_node(node, spec, tmp_path, "claude", cfg, 30, trust_cli=True, project_root=tmp_path)
    assert res["status"] == "PASSED"
    assert captured_prompts and "## Observed environment facts\n- test: pytest" in captured_prompts[0]
    assert notified == [(fid, "contradiction", 2)]
    assert envfact_store.load(tmp_path)[fid].state == "demoted"


def test_forgotten_stays_forgotten_after_three_works():
    s = _settings()
    fid = fact_id("test", "pytest")
    f = Fact(id=fid, key="pytest", category="test", state="forgotten", confirmations=["s1"])
    for idx in range(3):
        f = observe(f, Observation(kind="works", key="pytest", category="test", session=f"w{idx}"), s)
        assert f.state == "forgotten"
        assert f.confirmations == ["s1"]


def test_save_retries_permission_error_twice_then_succeeds(tmp_path: Path, monkeypatch):
    real_replace = envfact_store.os.replace
    attempts = {"count": 0}
    sleeps: list[float] = []

    def flaky_replace(src, dst):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(envfact_store.os, "replace", flaky_replace)
    monkeypatch.setattr(envfact_store.time, "sleep", sleeps.append)

    fid = fact_id("test", "pytest")
    envfact_store.save(tmp_path, {fid: Fact(id=fid, key="pytest", category="test", state="active")})
    assert attempts["count"] == 3
    assert sleeps == [0.05, 0.05]
    assert envfact_store.load(tmp_path)[fid].state == "active"


def test_notify_alter_pending_feedback_resend(tmp_path: Path, monkeypatch):
    cfg = default_config(tmp_path)
    cfg.knowledge = "alter"
    save_config(tmp_path, cfg)

    calls: list[tuple[str, dict]] = []
    should_fail = {"flag": True}

    class FakeMCP:
        def __init__(self, cmd, timeout=10, env=None):
            self.cmd = cmd

        def call_tool(self, name, args):
            if should_fail["flag"]:
                raise RuntimeError("alter down")
            calls.append((name, args))
            return {"ok": True}

    monkeypatch.setattr(envfact_store, "_alter_mcp_spec", lambda root: (["fake-alter"], None))
    monkeypatch.setattr(envfact_store, "MCPStdio", FakeMCP)

    f1 = Fact(id="aaa111", key="pytest", category="test", state="demoted", contradictions=2)
    f2 = Fact(id="bbb222", key="ruff check", category="lint", state="forgotten", contradictions=0)

    envfact_store.notify_alter(tmp_path, f1, "contradiction")
    pending_file = tmp_path / ".ego" / "state" / "alter-feedback-pending.jsonl"
    assert pending_file.is_file()
    assert len(pending_file.read_text(encoding="utf-8").splitlines()) == 1

    should_fail["flag"] = False
    envfact_store.notify_alter(tmp_path, f2, "forget")
    assert not pending_file.exists()
    assert [c[1]["kind"] for c in calls] == ["contradiction", "forget"]
    assert calls[0][1]["evidence"] == ["ego envfact aaa111 contradictions=2"]
    assert calls[1][1]["text"] == "lint: ruff check"


def test_cli_knowledge_facts_and_forget_work_under_alter_mode(tmp_path: Path, monkeypatch, capsys):
    cfg = default_config(tmp_path)
    cfg.knowledge = "alter"
    save_config(tmp_path, cfg)

    fid = fact_id("test", "pytest")
    envfact_store.save(tmp_path, {fid: Fact(id=fid, key="pytest", category="test", state="active", confirmations=["s1", "s2"])})

    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(envfact_store, "notify_alter", lambda root, fact, kind: notified.append((fact.id, kind)))

    assert main(["knowledge", "facts", "--root", str(tmp_path)]) == 0
    facts_out = json.loads(capsys.readouterr().out)
    assert len(facts_out) == 1 and facts_out[0]["id"] == fid

    assert main(["knowledge", "forget", fid, "--root", str(tmp_path)]) == 0
    forget_out = json.loads(capsys.readouterr().out)
    assert forget_out["state"] == "forgotten"
    assert notified == [(fid, "forget")]

    assert main(["knowledge", "forget", "missing-id", "--root", str(tmp_path)]) == 1
