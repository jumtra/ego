from pathlib import Path
import json
import sys
import os
import pytest
from seh.util import SEHError,safe_path
from seh.registry import scaffold,discover,resolve
from seh.tools import available_tools,invoke_registered
from seh.config import Config,load_config,save_config
from seh.policy import check_command,check_write
from seh.evals import run_eval,compare
from seh.work import execute,cancel


def test_declarative_tool_and_scaffold(project):
    result=scaffold(project,"extra-utils",apply=True)
    assert result["applied"]
    mod=project/".ego/modules/extra-utils"
    (mod/"module.toml").write_text((mod/"module.toml").read_text()+"tool_specs = [\"echo.json\"]\n")
    (project/"echo.py").write_text("import json,sys\nprint(json.dumps({'echo':sys.argv[1]}))\n")
    (mod/"echo.json").write_text(json.dumps({"name":"echo-text","argv":["python3","echo.py","{text}"],"inputs":{"text":"str"}}))
    assert "echo-text" in available_tools(project)
    with pytest.raises(SEHError): invoke_registered("echo-text",{"text":"hello"},project)
    result=invoke_registered("echo-text",{"text":"hello"},project,approve_external=True)
    assert result=={"echo":"hello"}
    with pytest.raises(SEHError):invoke_registered("echo-text",{"text":"hello","bad":"extra"},project,approve_external=True)
    assert "extra-utils" in discover(project)


def test_modules_disallow_cycles(project):
    for mod,require in (("cycle-a","cycle-b"),("cycle-b","cycle-a")):
        scaffold(project,mod,apply=True)
        f=project/f".ego/modules/{mod}/module.toml"
        f.write_text(f.read_text().replace('requires = ["core"]',f'requires = ["{require}"]'))
    with pytest.raises(SEHError):resolve(project,["cycle-a"])


def test_symlink_escape_and_command_policy(project,tmp_path):
    (project/"ln").symlink_to(tmp_path)
    with pytest.raises(SEHError):safe_path(project,"ln/escape")
    with pytest.raises(SEHError):check_write(project,".ego/ownership.json",["**"])
    with pytest.raises(SEHError):check_command(["python3","-c","print(1)"],["python3"])
    with pytest.raises(SEHError):check_command(["bash","-c","rm -rf /"],["bash"])


def test_parallel_conflict_is_not_applied(project):
    (project/"write1.py").write_text("from pathlib import Path\nPath('shared.txt').write_text('one')\n")
    (project/"write2.py").write_text("from pathlib import Path\nPath('shared.txt').write_text('two')\n")
    spec={"nodes":[{"id":"a","kind":"code","argv":["python3","write1.py"],"write_allowlist":["shared.txt"]},
                   {"id":"b","kind":"code","argv":["python3","write2.py"],"write_allowlist":["shared.txt"]}]}
    with pytest.raises(SEHError):execute(project,spec,host="mock",apply=True,approve=True)
    assert not (project/"shared.txt").exists()


def test_eval_infra_is_not_a_pass(project):
    suite=project/"suite.json"
    suite.write_text(json.dumps([{"id":"yes","prompt":"anything","checks":{"contains":["x"]}}]))
    report=run_eval(project,suite,host="nonexistent",save=False)
    assert not report["all_passed"]
    assert report["summary"]["yes"]["infra"]==1


def test_config_manual_toml_is_authoritative(project):
    cfg=load_config(project)
    cfg.memory="file";cfg.profile="minimal";save_config(project,cfg)
    from seh.install import installation_plan,apply_install
    diff=installation_plan(project,cfg)
    assert all(z["action"]!="conflict" for z in diff["changes"])
    apply_install(project,cfg)
    assert load_config(project).profile=="minimal"
