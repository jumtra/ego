"""Non-Python projects, mixed repositories, custom toolchains and safety gates."""
from pathlib import Path
import json
import subprocess
import sys
import pytest
from seh.detect import detect
from seh.toolchains import detect_projects, resolve_check
from seh.work import execute, validate_task
from seh.config import Config, save_config
from seh.util import SEHError
from seh.policy import check_command


def marker(root, rel, text=''):
    path = root / rel
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text, encoding='utf8')


@pytest.mark.parametrize('manifest,ecosystem,lang',[
    ('Cargo.toml','rust','rust'), ('go.mod','go','go'),
    ('service.csproj','dotnet','csharp'),('service.fsproj','dotnet','fsharp'),
    ('pom.xml','maven','java'),('build.gradle','gradle','java'),
    ('Package.swift','swift','swift'),('Gemfile','ruby','ruby'),
    ('composer.json','php','php'),('pubspec.yaml','dart','dart'),
    ('CMakeLists.txt','cmake','cpp'),('meson.build','meson','c'),
    ('mix.exs','elixir','elixir'),('build.zig','zig','zig'),
    ('project.godot','godot','gdscript'),
    ('Main.uproject','unreal','cpp'),
])
def test_ecosystem_detection(tmp_path,manifest,ecosystem,lang):
    marker(tmp_path, manifest)
    p=detect_projects(tmp_path)
    assert any(i['ecosystem']==ecosystem and lang in i['languages'] for i in p)


def test_node_package_manager_scripts_and_typescript(tmp_path):
    marker(tmp_path,'package.json',json.dumps({
        'packageManager':'pnpm@10.0.0',
        'scripts':{'test':'vitest run','lint':'eslint .','typecheck':'tsc --noEmit','build':'vite build'},
        'devDependencies':{'typescript':'^5.0'}}))
    marker(tmp_path,'tsconfig.json','{}')
    checks=detect_projects(tmp_path)[0]
    assert checks['ecosystem']=='node'
    assert 'typescript' in checks['languages']
    assert checks['checks']['test']==['pnpm','run','test']
    assert checks['checks']['typecheck']==['pnpm','run','typecheck']
    assert 'npm' not in checks['checks']['test']


def test_node_no_invented_test(tmp_path):
    marker(tmp_path,'package.json',json.dumps({'scripts':{'start':'node app.js'}}))
    assert 'test' not in detect_projects(tmp_path)[0]['checks']
    with pytest.raises(SEHError) as exc:resolve_check(tmp_path,'test')
    assert exc.value.code == 'CHECK_NOT_FOUND'


def test_monorepo_project_scoping_and_no_execution(tmp_path):
    marker(tmp_path,'apps/web/package.json',json.dumps({'scripts':{'test':'vitest run'}}))
    marker(tmp_path,'services/api/go.mod','module example.com/api\n')
    paths={(p['path'],p['ecosystem']) for p in detect_projects(tmp_path)}
    assert ('apps/web','node') in paths and ('services/api','go') in paths
    with pytest.raises(SEHError) as exc:resolve_check(tmp_path,'test')
    assert exc.value.code=='AMBIGUOUS_CHECK'
    check=resolve_check(tmp_path,'test',project='services/api')
    assert check['argv']==['go','test','./...']
    assert check['cwd']==str(tmp_path/'services/api')
    assert not (tmp_path/'apps/web'/'test-results.xml').exists()


def test_user_defined_language_toolchain(tmp_path):
    marker(tmp_path,'apps/z/hello.zig','const x=1;')
    marker(tmp_path,'.harness/toolchains.toml','''schema_version = 1
[[projects]]
path = "apps/z"
ecosystem = "zig-custom"
languages = ["zig"]
[projects.checks]
test = ["zig", "test", "hello.zig"]
''')
    found=resolve_check(tmp_path,'test',project='apps/z',ecosystem='zig-custom')
    assert found['argv']==['zig','test','hello.zig']
    assert found['source']=='custom'


def test_custom_toolchain_escape_rejected(tmp_path):
    marker(tmp_path,'.harness/toolchains.toml','''schema_version = 1
[[projects]]
path = "../outside"
ecosystem = "custom"
''')
    with pytest.raises(SEHError) as exc:detect_projects(tmp_path)
    assert exc.value.code=='INVALID_TOOLCHAINS'


def test_custom_symlink_escape_rejected(tmp_path):
    outside=tmp_path.parent/'external'
    outside.mkdir(exist_ok=True)
    (tmp_path/'ext').symlink_to(outside,target_is_directory=True)
    marker(tmp_path,'.harness/toolchains.toml','''schema_version = 1
[[projects]]
path = "ext"
ecosystem = "custom"
''')
    with pytest.raises(SEHError):detect_projects(tmp_path)


def test_source_only_language_is_visible_without_fake_commands(tmp_path):
    marker(tmp_path,'main.swift','print("hello")')
    projects=detect_projects(tmp_path)
    assert 'swift' in projects[0]['languages']
    assert projects[0]['checks']=={}


def test_untrusted_commands_rejected():
    allow=Config().allow_commands
    for command in (['npm','install'], ['go','install','x'], ['cargo','publish'],
                    ['node','-e','require("fs")'], ['/tmp/fake/python3','test.py'],
                    ['composer','install'], ['swift','package','resolve']):
        with pytest.raises(SEHError):check_command(command,allow)


def test_custom_language_workflow_check_runs_in_subproject(project):
    marker(project,'apps/web/verify.py','from pathlib import Path\nassert Path("flag.txt").read_text() == "ok"\n')
    marker(project,'make_flag.py','from pathlib import Path\nPath("apps/web/flag.txt").write_text("ok")\n')
    marker(project,'.harness/toolchains.toml','''schema_version = 1
[[projects]]
path = "apps/web"
ecosystem = "custom"
languages = ["typescript"]
[projects.checks]
test = ["python3", "verify.py"]
''')
    task={'nodes':[{'id':'make','kind':'code','argv':['python3','make_flag.py'],
                    'write_allowlist':['apps/web/flag.txt'],
                    'checks':[{'toolchain':'test','project':'apps/web','ecosystem':'custom'}]}]}
    result=execute(project,task)
    assert result['final_status']=='PASSED'
    assert not (project/'apps/web/flag.txt').exists()  # staged until approved


def test_invalid_checks_structure_rejected():
    with pytest.raises(SEHError):validate_task({'nodes':[{'id':'a','kind':'code',
        'argv':['go','test','./...'],'checks':{'bad':'not-a-list'},'write_allowlist':['x']}]})


def test_project_cli_preview_and_approval(tmp_path):
    entry=Path(__file__).parents[1]/'run_seh.py'
    marker(tmp_path,'package.json',json.dumps({'scripts':{'test':'node test.js'}}))
    def run(*args):
        return subprocess.run([sys.executable,str(entry),'project',*args,'--root',str(tmp_path)],capture_output=True,text=True)
    preview=run('check','--kind','test')
    assert preview.returncode==0,preview.stderr
    obj=json.loads(preview.stdout)
    assert obj['executed'] is False
    assert obj['argv']==['npm','run','test']
    denied=run('check','--kind','test','--execute')
    assert denied.returncode!=0
    assert 'APPROVAL_REQUIRED' in denied.stderr


def test_project_cli_custom_exec_requires_approval_and_runs(tmp_path):
    entry=Path(__file__).parents[1]/'run_seh.py'
    marker(tmp_path,'check.py','print("multilingual test ready")')
    marker(tmp_path,'.harness/toolchains.toml','''schema_version = 1
[[projects]]
path = "."
ecosystem = "custom"
languages = ["lua"]
[projects.checks]
test = ["python3", "check.py"]
''')
    result=subprocess.run([sys.executable,str(entry),'project','check','--kind','test',
        '--project','.','--ecosystem','custom','--execute','--approve','--root',str(tmp_path)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['result']['status']=='PASS'


def test_multilanguage_diagnostics_are_read_only(tmp_path):
    marker(tmp_path,'build.gradle.kts','plugins {}')
    before=(tmp_path/'build.gradle.kts').read_bytes()
    d=detect(tmp_path)
    assert 'gradle' in d['stack']
    assert (tmp_path/'build.gradle.kts').read_bytes()==before
    assert not (tmp_path/'.harness').exists()
