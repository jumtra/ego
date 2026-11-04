import json
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_code_index_impact_affected_options(tmp_path):
    entry = ROOT / 'run_seh.py'
    for action in (['code','index'], ['code','impact','Some.symbol'], ['code','affected','tests/test_unit.py']):
        p=subprocess.run([sys.executable,str(entry), *action,'--root',str(tmp_path)],capture_output=True,text=True)
        assert p.returncode == 0, p.stderr
        obj=json.loads(p.stdout)
        assert obj.get('status') in ('NOT_APPLICABLE','UNSUPPORTED'),obj
    p=subprocess.run([sys.executable,str(entry),'code','index','--root',str(tmp_path),'--execute'],capture_output=True,text=True)
    assert p.returncode==0,p.stderr
