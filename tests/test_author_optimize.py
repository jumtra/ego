from pathlib import Path
import json
import pytest
from ego.author import lint,plan,gate
from ego.candidates import new_candidate,validate_candidate,promote,rollback_promotion
from ego.evolve import observe,propose,lesson
from ego.optimize import plan as opt_plan,propose as opt_propose,run as opt_run,doctor as opt_doctor,report as opt_report
from ego.config import OptConfig,save_opt
from ego.util import EgoError, read_json

def author_suite(project):
    suite=project/"author_suite.json"
    suite.write_text(json.dumps({"cases":[
        {"id":part,"split":part,"prompt":"[[MOCK:ok]]","checks":{"contains":["ok"]}} for part in ("training","regression","holdout","security")
    ]}))
    return suite

def test_authored_candidate_promotion_rollback(project):
    assert plan(project,"work","skill.description","Narrow trigger")["state"]=="EXPLORED"
    candidate=new_candidate(project,"work","skill.description","Use on feature updates and bug fixes","Narrow triggers")
    assert "Use on feature" in validate_candidate(project,candidate)
    with pytest.raises(EgoError):promote(project,candidate,approval_ref="human:ticket1",gate={"status":"PASS","evidence":"run1","host":"mock"})
    suite=author_suite(project)
    g=gate(project,candidate,suite,host="mock",allow_mock=True)
    assert g["status"]=="PASS"
    promoted=promote(project,candidate,approval_ref="human:ticket1",gate=g,allow_mock=True)
    assert "Use on feature updates" in (project/".agents/skills/work/SKILL.md").read_text()
    with pytest.raises(EgoError):promote(project,candidate,approval_ref="human:ticket1",gate=g,allow_mock=True)
    assert rollback_promotion(project,promoted["promotion_id"])["status"]=="ROLLED_BACK"
    assert lint(project)["status"]=="PASS"

def test_evolution(project):
    obs=observe(project,"Trigger missed auth regression",evidence=["eval1"])
    candidate=propose(project,obs["id"],"work","skill.description","mock",replacement="Use for coding and bug fixing tasks")
    assert candidate["source"]=="evolve"
    lesson_obj=lesson(project,["eval1"],"Check authentication expiration first","mock","auth")
    assert lesson_obj["evidence"]==["eval1"]

def test_optimize_is_off(project):
    assert opt_doctor(project)["config"]["mode"]=="off"
    p=opt_plan(project,author_suite(project),"mock")
    assert p["mode"]=="off"
    with pytest.raises(EgoError):opt_propose(project,p["experiment_id"],replacement="test")

def test_optimization_shadow_no_promotion(project):
    save_opt(project,OptConfig(mode="shadow"))
    suite=author_suite(project)
    plan=opt_plan(project,suite,"mock")
    candidate=opt_propose(project,plan["experiment_id"],replacement="Use for coding tasks and fixing software bugs")
    with pytest.raises(EgoError):opt_run(project,plan["experiment_id"],execute=False)
    report=opt_run(project,plan["experiment_id"],execute=True,spending_ref="human:offline-test")
    assert report["comparison"]=="NO_GAIN"
    assert not report["comparison_details"]["promotable"]
    assert opt_report(project,plan["experiment_id"])["experiment_id"]==plan["experiment_id"]
    with pytest.raises(EgoError):opt_run(project,plan["experiment_id"],execute=True,spending_ref="human:offline-test")

def test_optimize_requires_spending_approval(project):
    save_opt(project,OptConfig(mode="shadow"))
    plan=opt_plan(project,author_suite(project),"mock")
    opt_propose(project,plan["experiment_id"],replacement="Use for software tasks")
    with pytest.raises(EgoError,match="Use --spend-approval"):
        opt_run(project,plan["experiment_id"],execute=True)


def test_gate_runs_the_suite_on_the_requested_model(project, monkeypatch):
    import ego.author as author
    seen = {}

    def fake_eval(root, suite, **kw):
        seen.update(kw)
        return {"run_id": "eval-x", "suite_sha256": "s", "all_passed": True}

    monkeypatch.setattr(author, "run_eval", fake_eval)
    candidate = new_candidate(project, "work", "skill.description", "Use on feature updates and bug fixes", "Narrow triggers")
    g = gate(project, candidate, author_suite(project), host="claude", model="haiku")
    assert seen["model"] == "haiku" and g["model"] == "haiku" and g["status"] == "PASS"
