from pathlib import Path
import json
import pytest
from ego.work import execute,validate_task,status
from ego.evals import run_eval,compare,load_suite
from ego.util import SEHError

def test_code_workflow_staged_apply(project):
    (project/"generate.py").write_text("from pathlib import Path\nPath('output.txt').write_text('ok')\n")
    (project/"check.py").write_text("from pathlib import Path\nassert Path('output.txt').read_text() == 'ok'\n")
    task={"nodes":[{"id":"write","kind":"code","argv":["python3","generate.py"],"write_allowlist":["output.txt"],"checks":[["python3","check.py"]]},
                   {"id":"gate","kind":"gate","depends_on":["write"],"argv":["python3","check.py"]}]}
    result=execute(project,task)
    assert result["final_status"]=="PASSED"
    assert not (project/"output.txt").exists()
    assert status(project,result["run_id"])["final_status"]=="PASSED"
    with pytest.raises(SEHError):execute(project,task,apply=True)
    applied=execute(project,task,apply=True,approve=True)
    assert applied["applied"]
    assert (project/"output.txt").read_text()=="ok"

def test_workflow_scope_rejected(project):
    (project/"write.py").write_text("from pathlib import Path\nPath('unrelated.txt').write_text('bad')\n")
    task={"nodes":[{"id":"write","kind":"code","argv":["python3","write.py"],"write_allowlist":["allowed.txt"],"max_attempts":1}]}
    result=execute(project,task)
    assert result["final_status"]=="FAILED"
    assert not (project/"unrelated.txt").exists()

def test_workflow_cycles(project):
    with pytest.raises(SEHError):validate_task({"nodes":[{"id":"a","kind":"agent","prompt":"x","write_allowlist":["a"],"depends_on":["b"]},{"id":"b","kind":"agent","prompt":"x","write_allowlist":["b"],"depends_on":["a"]}]})

def test_evaluation(project):
    path=project/"suite.json"
    path.write_text(json.dumps({"cases":[
        {"id":"yes","prompt":"[[MOCK:success]]","split":"training","checks":{"contains":["success"]}},
        {"id":"neg","prompt":"[[MOCK:OK]]","split":"security","checks":{"not_contains":["secret"]}},
        {"id":"hold","prompt":"[[MOCK:done]]","split":"holdout","checks":{"contains":["done"]}},
        {"id":"reg","prompt":"[[MOCK:test]]","split":"regression","checks":{"regex":["test"]}}
    ]}),encoding="utf8")
    result=run_eval(project,path,host="mock",trials=2)
    assert result["all_passed"]
    assert compare(result,result)["comparison"]=="NO_GAIN"
    assert load_suite(path)
    assert run_eval(project,path,host="mock",split="training")["all_passed"]

def test_eval_regression(project):
    path=project/"suite.json"
    path.write_text(json.dumps([{"id":"x","prompt":"[[MOCK:hello]]","checks":{"contains":["hello"]}}]))
    baseline=run_eval(project,path,host="mock",save=False)
    other=run_eval(project,path,host="mock",prompt_prefix="Preamble",save=False)
    assert compare(baseline,other)["comparison"]=="NO_GAIN"
    other["summary"]["x"]["passes"]=0
    assert compare(baseline,other)["comparison"]=="REGRESSION"
