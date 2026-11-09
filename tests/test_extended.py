from pathlib import Path
import json
import os
import sys
import subprocess
import tomllib
import pytest
from seh.util import SEHError,read_json,filehash
from seh.config import load_config,save_config,OptConfig,save_opt
from seh.install import apply_install,legacy_plan,remove_legacy,restore_legacy
from seh.codeintel import impact,affected_tests,search,health
from seh.hosts import capability
from seh.author import gate
from seh.candidates import new_candidate,promote
from seh.planner import plan_work
from seh.prompting import extract_reply,register_prompt,get_prompt
from seh.detect import detect
from seh.optimize import plan as optplan,propose as optpropose,run as optrun,full as optfull
from seh.events import emit,tail
from seh.runner import parse_host_response
from seh.memory import MCPStdio


def test_host_agent_formats(project):
    for role in ("implementer","reviewer","fixer","evolver"):
        assert (project/f".codex/agents/{role}.toml").exists()
        assert (project/f".agents/agents/{role}.md").exists()
        assert (project/f".opencode/agents/{role}.md").exists()
        with (project/f".codex/agents/{role}.toml").open("rb") as f:
            assert tomllib.load(f)["name"]==role
    assert not (project/".claude/settings.json").exists()
    cfg=load_config(project);cfg.profile="full"
    apply_install(project,cfg)
    assert (project/".claude/settings.json").exists()

def test_project_detection(tmp_path):
    (tmp_path/"pyproject.toml").write_text('[project]\nname="x"\n')
    (tmp_path/"tests").mkdir()
    result=detect(tmp_path)
    assert "python" in result["stack"]
    assert ["python3","-m","pytest","-q"] in result["test_commands"]

def test_codegraph_commands_are_real_when_installed(project,monkeypatch,tmp_path):
    bin_dir=tmp_path/"bin";bin_dir.mkdir()
    exe=bin_dir/"codegraph"
    exe.write_text('#!/usr/bin/env python3\nimport sys,json\nprint(json.dumps({"args":sys.argv[1:]}))\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH",str(bin_dir)+os.pathsep+os.environ["PATH"])
    r=search(project,"service lookup","codegraph")
    assert r["status"]=="PASS" and r["result"]["args"][0]=="query"
    assert impact(project,"lookup","codegraph")["result"]["args"][0]=="impact"
    assert affected_tests(project,["src/thing.py"],"codegraph")["result"]["args"][0]=="affected"

def test_prompt_contracts(project):
    assert extract_reply("hello <reply>OK</reply> trailer","reply")["reply"]=="OK"
    assert extract_reply('{"ok":true}',"json")["reply"]["ok"] is True
    with pytest.raises(SEHError):extract_reply("missing tag","reply")
    p=register_prompt(project,"task-prompt","Do X",role="reviewer")
    assert get_prompt(project,"task-prompt",p["id"])["body"]=="Do X"
    with pytest.raises(SEHError):register_prompt(project,"task-prompt","Do X")

def test_work_plan_and_events(project):
    plan=plan_work(project,"Fix token expiration",host="mock",scope=["src/**"])
    assert plan["task"]["nodes"][0]["write_allowlist"]==["src/**"]
    event=emit(project,"task.finished","unit-a",{"passed":True})
    assert tail(project)[0]["id"]==event["id"]

def test_legacy_restore(project):
    p=project/".claude/skills/brainstorm/SKILL.md";p.parent.mkdir(parents=True)
    p.write_text("legacy")
    items=legacy_plan(project)
    result=remove_legacy(project,{items[0]["path"]:items[0]["sha256"]})
    assert not p.exists()
    restored=restore_legacy(project,Path(result["backup"]).name)
    assert p.read_text()=="legacy"
    with pytest.raises(SEHError):restore_legacy(project,Path(result["backup"]).name)

def test_author_gate_requires_real_evidence(project):
    c=new_candidate(project,"work","skill.description","Use for software editing tasks","Hypothesis")
    with pytest.raises(SEHError):promote(project,c,approval_ref="human:ticket",gate={"status":"PASS","evidence":"invented","host":"claude"})

def test_full_opt_in_budget(project):
    save_opt(project,OptConfig(mode="shadow",profile="full",max_candidates=3,max_probe_runs=6,max_rounds=1))
    suite=project/"tiny_suite.json"
    suite.write_text(json.dumps([{"id":"one","prompt":"[[MOCK:OK]]","checks":{"contains":["OK"]}}]))
    result=optfull(project,suite,"mock","work","skill.description",1,execute=True,spending_ref="human:offline")
    assert result["status"]=="COMPLETED_WITHOUT_PROMOTION"
    assert len(result["rounds"][0]["experiment_ids"])==3

def test_cli_root_after_action(project):
    cmd=[sys.executable,"-m","seh","memory","add","some note","--root",str(project)]
    env={**os.environ,"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")}
    res=subprocess.run(cmd,capture_output=True,text=True,env=env,timeout=20)
    assert res.returncode==0,res.stderr
    assert json.loads(res.stdout)["text"]=="some note"

def test_cli_fast_does_not_import_optimizer():
    script="import sys; import seh.cli; print('seh.optimize' in sys.modules)"
    env={**os.environ,"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")}
    r=subprocess.run([sys.executable,"-c",script],capture_output=True,text=True,env=env)
    assert r.stdout.strip()=="False"

def test_mock_and_claude_parsing():
    raw='{"type":"result","result":"done","total_cost_usd":0.4,"usage":{"input_tokens":10,"output_tokens":5}}'
    assert parse_host_response("claude",raw)==("done",15,0.4)
