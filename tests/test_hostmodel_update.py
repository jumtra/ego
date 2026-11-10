"""Offline contract tests for task-specific models, CLI version drift and updater."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import zipfile
import pytest

from ego import hostcompat, selfupdate
from ego.hosts import build_command
from ego.task_routing import resolve_routes, build_task_prompt
from ego.work import execute
from ego.util import SEHError


def fake_hosts(monkeypatch, tmp_path, *, claude_exit=0):
    folder=tmp_path/'vendor-bin'
    folder.mkdir()
    paths={}
    for name in ('claude','codex','opencode'):
        script=folder/name
        script.write_text('#!/usr/bin/env python3\n' + f'''import json,sys
args=sys.argv[1:]
if '--version' in args:
    print('{name} 99.1.0'); sys.exit(0)
if '--help' in args:
    print('--model --effort --output-format --max-turns --permission-mode --dangerously-skip-permissions --json --yolo --dangerously-bypass-approvals-and-sandbox --sandbox --ask-for-approval --format --auto auto')
    sys.exit(0)
if {name!r} == 'claude' and {claude_exit}:
    sys.stderr.write('rate limit: retry later'); sys.exit({claude_exit})
model='native-default'
if '--model' in args: model=args[args.index('--model')+1]
msg='host={name};model='+model+';'+sys.stdin.read(4096)[:30]
if {name!r} == 'claude':
    print(json.dumps({{'type':'result','result':msg}}))
elif {name!r} == 'codex':
    print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':msg}}}}))
else:
    print(json.dumps({{'type':'text','part':{{'text':msg}}}}))
''')
        script.chmod(0o755)
        paths[name]=script
    monkeypatch.setenv('PATH',str(folder)+os.pathsep+os.environ.get('PATH',''))
    monkeypatch.setenv('SEH_CACHE_DIR',str(tmp_path/'cache'))
    return paths


def test_host_probe_invalidates_cache_on_binary_change(monkeypatch, tmp_path):
    paths=fake_hosts(monkeypatch,tmp_path)
    initial=hostcompat.inspect_host('claude')
    assert initial['supported'] and '--model' in initial['flags']
    with paths['claude'].open('a') as f:f.write('\n# changed\n')
    newer=hostcompat.inspect_host('claude')
    assert newer['fingerprint'] != initial['fingerprint']
    assert newer['checked_at'] >= initial['checked_at']


def test_flags_model_and_permission_profiles(monkeypatch,tmp_path):
    fake_hosts(monkeypatch,tmp_path)
    claude=build_command('claude','hello',model='sonnet',read_only=False)
    assert '--model' in claude and 'sonnet' in claude
    assert '--permission-mode' in claude and 'auto' in claude
    claude_effort=build_command('claude','hello',model='sonnet',effort='medium')
    assert '--effort' in claude_effort and 'medium' in claude_effort
    claude_auto=build_command('claude','hello',model='sonnet',effort='auto')
    assert '--effort' not in claude_auto
    codex=build_command('codex','hello',model='gpt-test',read_only=False)
    assert '--sandbox' in codex and 'workspace-write' in codex
    assert '--ask-for-approval' in codex and 'never' in codex
    open_code=build_command('opencode','hello',model='provider/model-x',read_only=False)
    assert '--auto' in open_code and '--model' in open_code
    with pytest.raises(SEHError,match='isolated'):
        build_command('codex','hello',autonomy='bypass')
    assert '--yolo' in build_command('codex','hello',autonomy='bypass',isolated=True)
    assert '--dangerously-skip-permissions' in build_command('claude','hello',autonomy='bypass',isolated=True)


def test_task_model_overrides_and_instruction_passing(tmp_path):
    (tmp_path/'global.md').write_text('Use TypeScript.\n')
    (tmp_path/'feature.md').write_text('Implement validation.\n')
    (tmp_path/'api.ts').write_text('export const ok = true;\n')
    task={'goal':'Build feature', 'instructions':'Do not change API schema', 'instructions_file':'global.md',
          'defaults':{'hosts':['claude','codex'],'strategy':'fallback','models':{'claude':'sonnet','codex':'gpt-a'}},
          'nodes':[{'id':'do','kind':'agent','prompt':'Fix source files','models':{'codex':'gpt-b'},
                    'instructions_file':'feature.md','context_files':['api.ts'],'write_allowlist':['src/*.ts']}]}
    strategy,routes,_=resolve_routes(task['nodes'][0],task)
    assert strategy=='fallback'
    assert [(r.host,r.model) for r in routes]==[('claude','sonnet'),('codex','gpt-b')]
    prompt=build_task_prompt(task,task['nodes'][0],tmp_path)
    assert 'Build feature' in prompt and 'Use TypeScript' in prompt
    assert 'Implement validation' in prompt and 'api.ts' in prompt


def test_config_models_fallback_in_routes():
    # task has no models configured, should inherit from default_models (from config)
    task = {'goal': 'Build feature', 'defaults': {'hosts': ['claude', 'codex'], 'strategy': 'fallback'},
            'nodes': [{'id': 'do', 'kind': 'agent', 'prompt': 'Do work'}]}
    config_models = {'claude': 'sonnet-3.5', 'codex': 'gpt-5-codex'}
    strategy, routes, _ = resolve_routes(task['nodes'][0], task, default_models=config_models)
    assert strategy == 'fallback'
    assert [(r.host, r.model) for r in routes] == [('claude', 'sonnet-3.5'), ('codex', 'gpt-5-codex')]

    # task explicit override beats default_models
    task_override = {'goal': 'Build feature',
                     'defaults': {'hosts': ['claude', 'codex'], 'strategy': 'fallback',
                                  'models': {'claude': 'opus'}},
                     'nodes': [{'id': 'do', 'kind': 'agent', 'prompt': 'Do work'}]}
    _, routes_overridden, _ = resolve_routes(task_override['nodes'][0], task_override, default_models=config_models)
    assert [(r.host, r.model) for r in routes_overridden] == [('claude', 'opus'), ('codex', 'gpt-5-codex')]

    # table configuration with effort support
    config_table = {
        'claude': {'model': 'sonnet', 'effort': 'medium'},
        'codex': {'model': 'gpt-5-codex', 'effort': 'auto'},
    }
    _, routes_table, _ = resolve_routes(task['nodes'][0], task, default_models=config_table)
    assert routes_table[0].host == 'claude'
    assert routes_table[0].model == 'sonnet'
    assert routes_table[0].effort == 'medium'
    assert routes_table[1].host == 'codex'
    assert routes_table[1].model == 'gpt-5-codex'
    assert routes_table[1].effort is None  # 'auto' normalizes to None


def test_fallback_only_on_unavailability(monkeypatch,project,tmp_path):
    fake_hosts(monkeypatch,tmp_path,claude_exit=2)
    spec={'goal':'Report host and model', 'defaults':{'hosts':['claude','codex'], 'strategy':'fallback',
         'models':{'claude':'sonnet','codex':'gpt-example'}},
         'nodes':[{'id':'one','prompt':'Say hi','kind':'agent','write_allowlist':['output.txt'],'max_attempts':1}]}
    result=execute(project,spec,host='claude',trust_cli=True)
    assert result['final_status']=='PASSED'
    attempts=result['results']['one']['attempts']
    assert [a['host'] for a in attempts]==['claude','codex']
    assert attempts[1]['model']=='gpt-example'
    assert 'host=codex' in result['results']['one']['output']


def test_compare_never_merges_or_applies(monkeypatch,project,tmp_path):
    fake_hosts(monkeypatch,tmp_path)
    spec={'defaults':{'hosts':['claude','codex'],'strategy':'compare','models':{'claude':'sonnet','codex':'gpt-z'}},
         'nodes':[{'id':'review','kind':'agent','prompt':'Review','write_allowlist':['review.txt']}]}
    res=execute(project,spec,trust_cli=True)
    assert res['final_status']=='COMPARISON_READY'
    assert res['changed_files']==[]
    assert len(res['results']['review']['comparison'])==2
    with pytest.raises(SEHError,match='Compare'):
        execute(project,spec,trust_cli=True,apply=True,approve=True)


def fake_wheel(version: str) -> bytes:
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr(f'ego-{version}.dist-info/METADATA',
                   'Metadata-Version: 2.1\nName: ego\nVersion: '+version+'\n')
    return buf.getvalue()


def test_selfupdate_digest_and_version(monkeypatch,tmp_path):
    wheel=fake_wheel('1.4.2')
    digest=hashlib.sha256(wheel).hexdigest()
    rel={'tag_name':'v1.4.2','draft':False,'prerelease':False,'html_url':f'https://github.com/{selfupdate.REPO}/releases/tag/v1.4.2',
         'assets':[{'name':'ego-1.4.2-py3-none-any.whl',
                    'browser_download_url':f'https://github.com/{selfupdate.REPO}/releases/download/v1.4.2/x.whl',
                    'digest':'sha256:'+digest}]}
    def download(url,limit,timeout=12):return json.dumps(rel).encode() if url==selfupdate.API else wheel
    monkeypatch.setattr(selfupdate,'_read_url',download)
    info=selfupdate.latest_release('1.4.1')
    assert info['can_install']
    path=selfupdate.fetch_verified_wheel(info,tmp_path/'update.whl')
    assert path.read_bytes()==wheel
    rel['assets'][0]['digest']='sha256:'+'0'*64
    with pytest.raises(SEHError,match='digest mismatch'):
        selfupdate.fetch_verified_wheel(selfupdate.latest_release('1.4.1'),tmp_path/'invalid.whl')
    assert not (tmp_path/'invalid.whl').exists()


def test_update_requires_approval_and_venv(monkeypatch,tmp_path):
    with pytest.raises(SEHError,match='--yes'):
        selfupdate.install_wheel(tmp_path/'x.whl','1.4.2')
    monkeypatch.setattr(selfupdate.sys,'prefix',getattr(selfupdate.sys,'base_prefix',selfupdate.sys.prefix))
    with pytest.raises(SEHError,match='virtual environment'):
        selfupdate.install_wheel(tmp_path/'x.whl','1.4.2',approved=True)


def test_node_precedence_over_task_and_code_node_under_compare_defaults(project):
    from ego.task_routing import resolve_routes
    spec={"defaults":{"hosts":["claude","codex"],"strategy":"fallback","models":{"codex":"default-codex"}}}
    n={"id":"x","host":"codex","model":"node-codex","prompt":"x","write_allowlist":["x.txt"]}
    strategy,routes,_=resolve_routes(n,spec)
    assert strategy=="single" and routes[0].model=="node-codex"
    (project/"runme.py").write_text("from pathlib import Path\nPath('x.txt').write_text('hi')\n")
    spec={"defaults":{"hosts":["claude","codex"],"strategy":"compare"},
          "nodes":[{"id":"x","kind":"code","argv":["python3","runme.py"],"write_allowlist":["x.txt"]}]}
    result=execute(project,spec)
    assert result["final_status"]=="PASSED"
    assert "x.txt" in result["changed_files"]


def test_cli_flag_removal_fails_closed(monkeypatch,tmp_path):
    paths=fake_hosts(monkeypatch,tmp_path)
    assert '--json' in build_command('codex','hello')
    # Simulate an incompatible vendor update: same executable path, new contents.
    script=paths['codex']
    data=script.read_text()
    data=data.replace('print(\'--model --effort --output-format --max-turns --permission-mode --dangerously-skip-permissions --json --yolo --dangerously-bypass-approvals-and-sandbox --sandbox --ask-for-approval --format --auto auto\')',
                      'print(\'--model --sandbox --ask-for-approval\')')
    assert data!=script.read_text()
    script.write_text(data)
    with pytest.raises(SEHError,match='required CLI switches absent'):
        build_command('codex','hello')


def test_local_wheel_requires_integrity_and_newer_version(tmp_path):
    wheel=fake_wheel('1.4.2')
    path=tmp_path/'ego-1.4.2-py3-none-any.whl'
    path.write_bytes(wheel)
    with pytest.raises(SEHError,match='digest mismatch'):
        selfupdate.update_from_local_wheel(path,'0'*64,'1.4.1',yes=True)
    with pytest.raises(SEHError,match='newer stable'):
        selfupdate.update_from_local_wheel(path,hashlib.sha256(wheel).hexdigest(),'1.4.2',yes=True)


def test_host_models_do_not_refresh_on_task(monkeypatch,tmp_path,project):
    from ego import models
    def unwanted(*args,**kwargs):raise AssertionError('Work path triggered a model refresh')
    monkeypatch.setattr(models,'refresh_catalog',unwanted)
    spec={'defaults':{'model':'auto'},'nodes':[{'id':'noop','kind':'agent','prompt':'[[MOCK:hello]]',
          'write_allowlist':['result.txt']}]}
    assert execute(project,spec)['final_status']=='PASSED'


def test_bin_wrappers_work_via_symlink(tmp_path):
    import subprocess
    repo_root=Path(__file__).resolve().parent.parent
    bin_dir=tmp_path/'local'/'bin'
    bin_dir.mkdir(parents=True)
    for name in ('ego','seh'):
        link=bin_dir/name
        link.symlink_to(repo_root/'bin'/name)
        proc=subprocess.run([str(link),'--version'],capture_output=True,text=True,timeout=10,check=False)
        assert proc.returncode==0, proc.stderr
        assert 'ego ' in proc.stdout


def test_selfupdate_ssl_context_falls_back_to_system_ca(monkeypatch,tmp_path):
    import ssl
    fake_ca=tmp_path/'cert.pem'
    fake_ca.write_text('-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n')
    loaded=[]
    class DummyCtx:
        def load_verify_locations(self,cafile=None):
            loaded.append(cafile)
    monkeypatch.setattr(ssl,'create_default_context',lambda:DummyCtx())
    monkeypatch.setattr(ssl,'get_default_verify_paths',lambda:type('P',(),{'cafile':None,'capath':None})())
    monkeypatch.setattr(selfupdate,'SYSTEM_CA_BUNDLES',(str(fake_ca),))
    selfupdate._ssl_context()
    assert loaded==[str(fake_ca)]


def test_selfupdate_allows_api_github_com_redirect(monkeypatch):
    class DummyResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def geturl(self):
            return "https://api.github.com/repositories/12345/releases/latest"
        def read(self, limit):
            return b"{}"

    monkeypatch.setattr(selfupdate, "urlopen", lambda req, timeout=12, context=None: DummyResponse())
    data = selfupdate._read_url("https://api.github.com/repos/jumtra/ego/releases/latest", 1024)
    assert data == b"{}"


def test_make_install_creates_symlinks_without_clobbering_existing_seh(tmp_path):
    import subprocess
    repo_root=Path(__file__).resolve().parent.parent
    bin_dir=tmp_path/'bin'
    bin_dir.mkdir()
    existing_seh=bin_dir/'seh'
    existing_seh.write_text('#!/bin/sh\necho existing\n')
    existing_seh.chmod(0o755)
    proc=subprocess.run(['make','install',f'BINDIR={bin_dir}'],cwd=repo_root,capture_output=True,text=True,timeout=10,check=False)
    assert proc.returncode==0, proc.stderr
    assert (bin_dir/'ego').is_symlink()
    assert not (bin_dir/'harness').exists()
    assert not existing_seh.is_symlink() and existing_seh.read_text()=='#!/bin/sh\necho existing\n'
    ver=subprocess.run([str(bin_dir/'ego'),'--version'],capture_output=True,text=True,timeout=10,check=False)
    assert ver.returncode==0 and 'ego ' in ver.stdout
    clean_bin=tmp_path/'clean_bin'
    proc2=subprocess.run(['make','install',f'BINDIR={clean_bin}'],cwd=repo_root,capture_output=True,text=True,timeout=10,check=False)
    assert proc2.returncode==0, proc2.stderr
    assert (clean_bin/'ego').is_symlink() and (clean_bin/'seh').is_symlink()
    assert not (clean_bin/'harness').exists()


def test_load_config_accepts_v2_nested_config(tmp_path):
    from ego.config import load_config
    h=tmp_path/'.ego'
    h.mkdir()
    (h/'config.toml').write_text('''schema_version = "2.0"
profile = "standard"

[hosts]
enabled = ["claude", "codex", "opencode", "antigravity"]

[code_intelligence]
provider = "none"

[memory]
provider = "files"

[runtime]
default_mode = "native"
max_concurrency = 2
max_fix_iterations = 3
timeout_sec = 300

[sdk]
mode = "auto"

[authoring]
require_eval = true

[evaluation]
trials = 1

[evolution]
auto_promote = false
''')
    cfg=load_config(tmp_path)
    assert cfg.profile=='balanced'
    assert cfg.hosts==['claude','codex','opencode','agy']
    assert cfg.code_intelligence=='none'
    assert cfg.memory=='file'


def test_codex_approve_for_me_and_agy_models_parsing(monkeypatch,tmp_path):
    paths=fake_hosts(monkeypatch,tmp_path)
    script=paths['codex']
    script.write_text(script.read_text().replace('--ask-for-approval','--approve-for-me'))
    cmd=build_command('codex','hello',read_only=False,autonomy='auto')
    assert '--approve-for-me' in cmd
    from ego.models import parse_agy_models
    parsed=parse_agy_models('gemini-4-argon-medium\tGemini 4 Argon (Medium)\nclaude-opus-5-5-high\tClaude Opus 5.5 (High)\n')
    assert parsed==['claude-opus-5-5-high','gemini-4-argon-medium']



