import pytest
from ego.adapters.alter_plan import spec_to_task, task_to_spec
from ego.shared.util import EgoError
from ego.usecases.work import validate_task

def sample_task():
    return {
        'goal':'Add login','done':'tests pass','instructions':'Keep it small',
        'nodes':[
            {'id':'impl','kind':'agent','role':'coder','prompt':'Implement login','intent':'login works',
             'requirement':{'who':'user','what':'login','when':'always','where':'web','why':'access'},
             'steps':['add handler'],'write_allowlist':['src/login.py'],'done_when':['handler returns 200'],
             'checks':[['pytest','-q','tests/test_login.py']],'depends_on':[],'max_rounds':1,'model':'sonnet','agent':'coder-x'},
            {'id':'check','kind':'gate','depends_on':['impl'],'prompt':'','write_allowlist':[],'checks':[['pytest','-q']]},
            {'id':'review','kind':'agent','role':'reviewer','reviews':'impl','depends_on':['impl','check'],
             'prompt':'Review login','write_allowlist':[],'checks':[],'max_rounds':1},
        ],
    }

def test_round_trip_keeps_every_node_field():
    task=sample_task()
    restored=spec_to_task(task_to_spec(task,'login'),'plan-1','hash-1')
    assert restored['nodes']==task['nodes']

def test_prompt_over_limit_is_rejected():
    task=sample_task()
    task['nodes'][0]['prompt']='x'*201
    with pytest.raises(EgoError) as err:
        task_to_spec(task,'login')
    assert err.value.code=='INVALID_TASK'

def test_pulled_task_passes_validate_task():
    restored=spec_to_task(task_to_spec(sample_task(),'login'),'plan-1','hash-1')
    validate_task(restored)
