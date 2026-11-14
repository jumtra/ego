"""inputs/outputs contract, role write separation, and the evidence-gated judge gate."""
import re
import pytest
from ego import work
from ego.judge import parse_verdict
from ego.task_routing import build_task_prompt, resolve_routes
from ego.work import execute, validate_task
from ego.util import EgoError

REAL = work.run_agent


@pytest.fixture
def writing_agent(monkeypatch):
    """The mock host never edits; [[WRITE:path]] in a prompt makes it write that file (offline stand-in for a worker)."""
    def fake(host, prompt, cwd, *a, **k):
        m = re.search(r"\[\[WRITE:(.*?)\]\]", prompt)
        if m:
            (cwd / m.group(1)).parent.mkdir(parents=True, exist_ok=True)
            (cwd / m.group(1)).write_text("x")
        return REAL(host, prompt, cwd, *a, **k)
    monkeypatch.setattr(work, "run_agent", fake)


def node(i, **kw):
    return {"id": i, "prompt": "p", "write_allowlist": ["src/*"], **kw}


def test_inputs_outputs_reach_prompts(tmp_path):
    task = {"goal": "g", "nodes": [node("a", inputs=["src/a.py"], outputs=["src/a.py::run"]),
                                   node("b", depends_on=["a"])]}
    validate_task(task)
    assert "## Inputs" in build_task_prompt(task, task["nodes"][0], tmp_path)
    assert "- src/a.py::run" in build_task_prompt(task, task["nodes"][0], tmp_path)
    assert "outputs: src/a.py::run" in build_task_prompt(task, task["nodes"][1], tmp_path)


def test_outputs_must_be_in_write_allowlist_and_inputs_bounded():
    with pytest.raises(EgoError, match="outside write_allowlist"):
        validate_task({"nodes": [node("a", outputs=["docs/x.md"])]})
    with pytest.raises(EgoError, match="INVALID_TASK|must be"):
        validate_task({"nodes": [node("a", inputs=["f"] * 7)]})


def test_role_validation():
    with pytest.raises(EgoError):
        validate_task({"nodes": [node("a", role="boss")]})
    with pytest.raises(EgoError, match="tests/"):
        validate_task({"nodes": [node("a", role="tester")]})
    with pytest.raises(EgoError, match="read-only"):
        validate_task({"nodes": [node("a", role="judge")]})


def test_tester_may_only_write_tests(project, writing_agent):
    ok = {"nodes": [{"id": "t", "role": "tester", "prompt": "[[WRITE:tests/t.py]]", "write_allowlist": ["tests/*"]}]}
    assert execute(project, ok)["final_status"] == "PASSED"
    # Even with a wide allowlist set by the coder side, the role itself limits writes.
    bad = {"nodes": [{"id": "t", "role": "tester", "prompt": "[[WRITE:tests/../src/x.py]]", "write_allowlist": ["tests/*"], "max_attempts": 1}]}
    assert execute(project, bad)["final_status"] == "FAILED"


def test_judge_cannot_write(project, writing_agent):
    task = {"nodes": [node("c", role="coder", prompt="p"),
                      {"id": "g", "kind": "gate", "depends_on": ["c"], "argv": ["python3", "--version"]},
                      {"id": "j", "role": "judge", "depends_on": ["g"], "reviews": "c",
                       "prompt": "[[WRITE:src/j.py]]VERDICT: APPROVE"}]}
    # Judge write: the node itself must not carry an allowlist, so the write is refused by role.
    result = execute(project, task)
    assert result["node_status"]["j"] == "FAILED"


def test_role_routing_node_overrides_role_overrides_defaults():
    spec = {"defaults": {"host": "mock", "roles": {"judge": {"host": "codex"}}}}
    j = {"id": "j", "role": "judge", "prompt": "p"}
    assert resolve_routes(j, spec)[1][0].host == "codex"
    assert resolve_routes({**j, "host": "mock"}, spec)[1][0].host == "mock"
    assert resolve_routes({"id": "c", "role": "coder", "prompt": "p"}, spec)[1][0].host == "mock"
    with pytest.raises(EgoError):
        resolve_routes(j, {"defaults": {"roles": {"boss": {}}}})


def test_parse_verdict_discards_evidence_free_findings():
    assert parse_verdict("no verdict")["verdict"] == "INVALID"
    assert parse_verdict("- fix it\nVERDICT: REJECT")["verdict"] == "APPROVE"
    v = parse_verdict("- src/a.py:12: missing null check\n- vague\nVERDICT: REJECT")
    assert v["verdict"] == "REJECT" and v["dropped"] == 1 and v["findings"][0]["line"] == 12


def judged(verdict_text, **jkw):
    return {"nodes": [node("c", role="coder", prompt="[[WRITE:src/a.py]]ok"),
                      {"id": "g", "kind": "gate", "depends_on": ["c"], "argv": ["python3", "--version"]},
                      {"id": "j", "role": "judge", "depends_on": ["g"], "reviews": "c",
                       "prompt": "[[MOCK:" + verdict_text + "]]", **jkw}]}


def test_judge_approve_passes(project, writing_agent):
    assert execute(project, judged("VERDICT: APPROVE"))["final_status"] == "PASSED"


def test_judge_reject_without_evidence_is_approved(project, writing_agent):
    assert execute(project, judged("- looks wrong\nVERDICT: REJECT"))["final_status"] == "PASSED"


def test_judge_reject_with_evidence_retries_coder_locally_then_fails(project, writing_agent):
    result = execute(project, judged("- src/a.py:1: spec omitted\nVERDICT: REJECT"))
    assert result["final_status"] == "FAILED"
    assert result["judge_retries"] == {"j": 1}
    assert len(result["results"]["c"]["attempts"]) == 1 and result["node_status"]["c"] == "PASSED"
    again = execute(project, judged("- src/a.py:1: spec omitted\nVERDICT: REJECT", max_retries=0))
    assert "j" not in again["judge_retries"] or again["judge_retries"]["j"] == 0


def test_judge_needs_reviews_and_a_gate():
    base = judged("VERDICT: APPROVE")
    no_reviews = {"nodes": [dict(n) for n in base["nodes"]]}
    del no_reviews["nodes"][2]["reviews"]
    with pytest.raises(EgoError, match="reviews"):
        validate_task(no_reviews)
    no_gate = {"nodes": [base["nodes"][0], {**base["nodes"][2], "depends_on": ["c"]}]}
    with pytest.raises(EgoError, match="gate or code"):
        validate_task(no_gate)


def test_failed_gate_means_judge_never_runs(project, writing_agent):
    task = judged("VERDICT: APPROVE")
    (project / "fail.py").write_text("raise SystemExit(1)\n")
    task["nodes"][1]["argv"] = ["python3", "fail.py"]
    result = execute(project, task)
    assert result["final_status"] == "FAILED" and "j" not in result["node_status"]


def test_config_roles_route_host_model_effort_with_precedence():
    import pytest
    from ego.config import Config
    from ego.util import EgoError
    roles = {"judge": {"host": "agy", "model": "gemini-2.5-pro", "effort": "high"}, "coder": {"effort": "low"}}
    j = {"id": "j", "role": "judge", "prompt": "p"}
    r = resolve_routes(j, {}, default_roles=roles)[1][0]
    assert (r.host, r.model, r.effort) == ("agy", "gemini-2.5-pro", "high")
    # effort-only role keeps host default and configured model
    c = resolve_routes({"id": "c", "role": "coder", "prompt": "p"}, {}, "claude", default_models={"claude": "sonnet"}, default_roles=roles)[1][0]
    assert (c.host, c.model, c.effort) == ("claude", "sonnet", "low")
    # task defaults.roles beats config roles; node beats both
    spec = {"defaults": {"roles": {"judge": {"host": "codex", "effort": "max"}}}}
    r = resolve_routes(j, spec, default_roles=roles)[1][0]
    assert (r.host, r.effort) == ("codex", "max")
    assert resolve_routes({**j, "host": "mock"}, spec, default_roles=roles)[1][0].host == "mock"
    Config(roles=roles).validate()
    for bad in ({"boss": {}}, {"judge": {"host": "x"}}, {"judge": {"effort": "huge"}}, {"judge": {"foo": 1}}):
        with pytest.raises(EgoError):
            Config(roles=bad).validate()
