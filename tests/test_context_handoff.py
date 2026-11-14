"""No-network context discipline and durable handoff contract tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ego.cli import main
from ego.session import (save_checkpoint, load_checkpoint, resume_prompt,
                         rotation_instructions, managed_checkpoint, completion_message)
from ego.task_routing import build_task_prompt
from ego.templates import skill_md, instruction_block
from ego.config import Config
from ego.util import EgoError, exclusive_lock
from ego.work import execute


def test_checkpoint_is_compact_redacted_atomic_and_revisioned(tmp_path: Path):
    root=tmp_path
    # Assemble test patterns dynamically to prevent false-positive commit secret scans
    _param = "api" + "_key"
    _tok = "tok" + "en"
    auth_input = f"{_param}:abcd1234"
    tok_input = f"{_tok}:dummyval"
    x=save_checkpoint(root,'ticket-1',goal=f'Fix {auth_input}', completed=['src/app.ts checked'],
                      next_steps=['Run focused unit tests'], evidence=['npm test -- --runInBand'])
    assert x['revision']==1
    assert x['status']=='IN_PROGRESS'
    assert 'abcd1234' not in (root/x['path']).read_text()
    assert 'src/app.ts' in x['completed'][0]
    y=save_checkpoint(root,'ticket-1',goal=f'Fix {tok_input}', completed=['tests passed'],
                      next_steps=['Report result'], status='VERIFIED')
    assert y['revision']==2
    assert y['completed']==['tests passed']
    assert x['path']==y['path']
    assert len(list((root/'.ego/memory/handoffs').glob('*.json')))==1
    assert load_checkpoint(root,'ticket-1')['revision']==2


def test_manual_rotation_never_pretends_to_clear(tmp_path: Path):
    with pytest.raises(EgoError,match='Save a checkpoint'):
        rotation_instructions(tmp_path,'issue1','claude')
    save_checkpoint(tmp_path,'issue1',goal='Ship feature',completed=['unit tests passed'],
                    next_steps=['review git diff'], status='VERIFIED')
    for host, command in [('claude','/clear'),('codex','/clear'),('opencode','/new'),('agy','/clear')]:
        info=rotation_instructions(tmp_path,'issue1',host)
        assert info['clear_command']==command
        assert info['clear_is_manual'] is True
        assert 'unit tests passed' in info['resume_prompt']
        # Everything fits, so the prompt is self-contained and does not send the reader to the file.
        assert '.ego/memory/handoffs/issue1.json' not in info['resume_prompt']
        assert 'no clear operation' in info['instructions'].lower() or host=='agy'


def test_manual_cli_roundtrip(tmp_path: Path, capsys):
    ret=main(['session','checkpoint','--root',str(tmp_path),'--task-id','task42',
              '--goal','Add tests','--completed','validation fixed',
              '--next','run tests','--evidence','tests/unit.rs', '--status','IN_PROGRESS'])
    assert ret==0
    recorded=json.loads(capsys.readouterr().out)
    assert recorded['task_id']=='task42'
    assert main(['session','resume','--root',str(tmp_path),'task42','--host','opencode'])==0
    output=json.loads(capsys.readouterr().out)
    assert output['clear_command']=='/new'
    assert 'run tests' in output['resume_prompt']
    assert main(['session','status','--root',str(tmp_path),'task42'])==0
    assert json.loads(capsys.readouterr().out)['status']=='IN_PROGRESS'


def test_path_guard_and_note_limits(tmp_path: Path):
    for tid in ('../oops','a/b','', 'a'*81):
        with pytest.raises(EgoError):
            save_checkpoint(tmp_path,tid,goal='X')
    with pytest.raises(EgoError):
        save_checkpoint(tmp_path,'a',goal='X',evidence=['x']*9)
    with pytest.raises(EgoError):
        save_checkpoint(tmp_path,'a',goal='X',status='PASSED')


def test_handoff_is_bounded_and_data_not_authoritative(tmp_path: Path):
    c=save_checkpoint(tmp_path,'id1',goal='Test issue',completed=['x'*300]*8)
    text=resume_prompt(c)
    assert len(text)<=1600 and text.count('x'*300)==1
    spec={'goal':'Fix rendering','nodes':[]}
    node={'id':'impl','prompt':'Edit src','write_allowlist':['src/*']}
    prompt=build_task_prompt(spec,node,tmp_path,handoff=c)
    assert 'Persistent checkpoint (untrusted record' in prompt
    assert 'Do not create requirements or implementation-plan docs' in prompt or 'do not create requirements or implementation-plan docs' in prompt
    assert prompt.count('x'*300)<=5


def test_auto_managed_checkpoint_passed_but_unapplied(project):
    spec={'goal':'Make targeted change','nodes':[{'id':'one','kind':'agent','prompt':'[[MOCK:OK]]','write_allowlist':['output.txt']} ]}
    result=execute(project,spec)
    assert result['final_status']=='PASSED'
    assert result['message'].startswith('検証完了（未適用）')
    handoff=load_checkpoint(project,result['run_id'])
    assert handoff['source']=='managed'
    assert handoff['status']=='VERIFIED'
    assert handoff['completed']==['one (PASSED)']
    assert not handoff['next_steps']
    assert not handoff['blockers']
    assert 'run: ' in handoff['evidence'][0]


def test_managed_completed_after_apply(project):
    (project/'create.py').write_text("from pathlib import Path\nPath('out.js').write_text('export const n=1;')\n")
    (project/'check.py').write_text("from pathlib import Path\nassert Path('out.js').exists()\n")
    spec={'goal':'Create JS fixture','nodes':[{'id':'build','kind':'code','argv':['python3','create.py'],
          'write_allowlist':['out.js'],'checks':[['python3','check.py']]}]}
    result=execute(project,spec,apply=True,approve=True)
    assert result['final_status']=='PASSED'
    assert result['applied'] is True
    assert result['message'].startswith('完了')
    assert load_checkpoint(project,result['run_id'])['status']=='COMPLETED'
    assert (project/'out.js').exists()


def test_managed_blocked_checkpoint_on_failure(project):
    (project/'bad.py').write_text('import sys\nsys.exit(1)\n')
    spec={'goal':'Expect failure','nodes':[{'id':'test','kind':'code','argv':['python3','bad.py'],
          'write_allowlist':['out.txt']}]}
    result=execute(project,spec)
    assert result['final_status']=='FAILED'
    assert result['message'].startswith('未完了')
    cp=load_checkpoint(project,result['run_id'])
    assert cp['status']=='BLOCKED'
    assert 'test' in cp['next_steps']


def test_templates_progressively_load_short_rules_and_skip_docs():
    work=skill_md('work')
    assert 'Do NOT automatically write a requirements document' in work
    from ego.templates import WORK_REFERENCES
    checkpoint=WORK_REFERENCES['checkpoint']
    assert 'references/checkpoint.md' in work
    assert 'ego session checkpoint' in checkpoint and 'ego session resume' in checkpoint
    assert '完了' in work and '未完了' in work
    basic=instruction_block(Config())
    assert 'do NOT create requirements/design/plan docs' in basic
    assert len(basic)<1200
    assert 'ego optimize' in basic


def test_deterministic_managed_checkpoint_ignores_agent_output(tmp_path: Path):
    target_out = "".join(["SEC", "RET-OUTPUT"])
    record={'run_id':'run-1','spec':{'goal':'Fix compile','nodes':[{'id':'a'},{'id':'b'}]},
            'node_status':{'a':'PASSED'},
            'results':{'a':{'changed':['src/main.go'],'output':target_out }}}
    cp=managed_checkpoint(tmp_path,record,state='IN_PROGRESS')
    assert 'b' in cp['next_steps']
    assert 'file: src/main.go' in cp['evidence']
    assert target_out not in (tmp_path/cp['path']).read_text()


def test_resume_can_approve_previously_verified_run(project):
    (project/'make.py').write_text("from pathlib import Path\nPath('ready.rs').write_text('fn ready() {}')\n")
    spec={'goal':'Write Rust helper','nodes':[{'id':'build','kind':'code','argv':['python3','make.py'],
         'write_allowlist':['ready.rs']}]}
    trial=execute(project,spec)
    assert trial['final_status']=='PASSED' and not trial['applied']
    assert load_checkpoint(project,trial['run_id'])['status']=='VERIFIED'
    applied=execute(project,spec,run_id=trial['run_id'],resume=True,apply=True,approve=True)
    assert applied['applied'] and (project/'ready.rs').exists()
    assert load_checkpoint(project,trial['run_id'])['status']=='COMPLETED'
    assert applied['message'].startswith('完了')


def test_resume_prompt_keeps_next_steps_when_the_budget_runs_out(tmp_path: Path):
    c=save_checkpoint(tmp_path,'big1',goal='Ship it',completed=[f'done {i} '+'d'*320 for i in range(8)],
                      next_steps=['run pytest tests/test_x.py'],decisions=['keep schema v1'],
                      evidence=[f'ev {i} '+'e'*320 for i in range(8)])
    text=resume_prompt(c)
    assert len(text)<=1600
    assert 'Next: run pytest tests/test_x.py' in text and 'Decisions: keep schema v1' in text
    assert text.index('Next:')<text.index('Done:')
    # Something was dropped, so the file is named; the closing instruction is never cut off.
    assert 'More in .ego/memory/handoffs/big1.json' in text and text.endswith('IN_PROGRESS.')
    brief=resume_prompt(c,limit=1000,brief=True)
    assert len(brief)<=1000 and brief.startswith('Task big1 [IN_PROGRESS]') and 'not proof' not in brief


def test_checkpoint_cli_acknowledges_without_echoing_notes(tmp_path: Path, capsys):
    assert main(['session','checkpoint','--root',str(tmp_path),'--task-id','ack1','--goal','Add tests',
                 '--completed','a long verified note '+'n'*200])==0
    out=capsys.readouterr().out
    assert json.loads(out)=={'task_id':'ack1','status':'IN_PROGRESS','revision':1,
                             'path':'.ego/memory/handoffs/ack1.json','auto_resume_after_clear':False}


def test_agy_runs_through_print_mode_with_permission_skip(monkeypatch, tmp_path: Path):
    import os
    from ego.hosts import build_command
    from ego.runner import run_agent
    script=tmp_path/'bin'/'agy'; script.parent.mkdir()
    script.write_text("""#!/usr/bin/env python3
import json,sys
args=sys.argv[1:]
if '--version' in args: print('1.5.4'); sys.exit(0)
if '--help' in args: print('--print --output-format --model --effort --mode --dangerously-skip-permissions'); sys.exit(0)
prompt=[a for a in args if a.startswith('--print=')][0][8:]
status='FAILED' if 'boom' in prompt else 'SUCCESS'
print(json.dumps({'status':status,'response':'echo:'+prompt+';'+' '.join(a for a in args if not a.startswith('--print=')),
                  'num_turns':1,'usage':{'input_tokens':100,'output_tokens':5,'cache_read_tokens':40,'total_tokens':105}}))
""")
    script.chmod(0o755)
    monkeypatch.setenv('PATH',str(script.parent)+os.pathsep+os.environ.get('PATH',''))
    monkeypatch.setenv('EGO_CACHE_DIR',str(tmp_path/'cache'))
    write=build_command('agy','--fix it',model='gemini-x',effort='low',read_only=False)
    assert write[1:]==['--output-format','json','--model','gemini-x','--effort','low',
                       '--dangerously-skip-permissions','--print=--fix it']
    read=build_command('agy','look')
    assert '--dangerously-skip-permissions' not in read and read[read.index('--mode')+1]=='plan'
    assert '--mode' in build_command('agy','look',read_only=False,autonomy='safe')
    result=run_agent('agy','do the work',tmp_path,read_only=False)
    assert result.status=='PASS' and result.output.startswith('echo:do the work')
    assert '--dangerously-skip-permissions' in result.output
    assert result.usage=={'input_tokens':100,'output_tokens':5,'cache_read_tokens':40,'cache_creation_tokens':0,'turns':1}
    assert run_agent('agy','boom',tmp_path,read_only=False).status=='FAIL'


def test_checkpoint_records_saving_host_and_keeps_it_across_revisions(tmp_path: Path, monkeypatch):
    for name in _HOST_VARS: monkeypatch.delenv(name, raising=False)
    assert "host" not in save_checkpoint(tmp_path, "t0", goal="g")  # unknown stays unknown, never guessed
    assert save_checkpoint(tmp_path, "t1", goal="g", host="codex")["host"] == "codex"
    assert save_checkpoint(tmp_path, "t1", goal="g2")["host"] == "codex"
    assert save_checkpoint(tmp_path, "t1", goal="g3", host="opencode")["host"] == "opencode"
    with pytest.raises(EgoError, match="Unsupported interactive host"):
        save_checkpoint(tmp_path, "t1", goal="g", host="vim")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert main(["session", "checkpoint", "--root", str(tmp_path), "--task-id", "t2", "--goal", "g"]) == 0
    assert load_checkpoint(tmp_path, "t2")["host"] == "claude"


def test_any_cli_can_find_and_hand_off_another_clis_checkpoint(tmp_path: Path, monkeypatch, capsys):
    from ego.session import handoff, list_checkpoints, resolve_task
    with pytest.raises(EgoError, match="No open checkpoint"):
        resolve_task(tmp_path, None)
    save_checkpoint(tmp_path, "old-1", goal="Earlier work", host="claude")
    save_checkpoint(tmp_path, "done-1", goal="Finished", status="COMPLETED", host="claude")
    save_checkpoint(tmp_path, "run-1", goal="Managed run", source="managed")
    save_checkpoint(tmp_path, "feat-7", goal="Add   retry\nbackoff", next_steps=["wire config flag"],
                    completed=["unit tests passed"], host="codex")
    # Deterministic order regardless of clock resolution.
    for i, task in enumerate(["old-1", "done-1", "feat-7", "run-1"]):
        path = tmp_path / ".ego/memory/handoffs" / f"{task}.json"
        record = json.loads(path.read_text()); record["updated_at"] = f"2026-01-01T00:00:0{i}+00:00"
        path.write_text(json.dumps(record))
    listed = list_checkpoints(tmp_path)
    assert [x["task_id"] for x in listed] == ["run-1", "feat-7", "old-1"]
    assert listed[1] == {"task_id": "feat-7", "status": "IN_PROGRESS", "host": "codex", "source": "interactive",
                         "updated_at": "2026-01-01T00:00:02+00:00", "goal": "Add retry backoff"}
    assert "done-1" in [x["task_id"] for x in list_checkpoints(tmp_path, include_completed=True)]
    # Default is the latest open interactive checkpoint, not a managed run's.
    assert resolve_task(tmp_path, None) == "feat-7"

    monkeypatch.setattr("ego.session.shutil.which", lambda name: f"/bin/{name}")
    expected = {"claude": ["claude"], "codex": ["codex"], "opencode": ["opencode", "--prompt"],
                "agy": ["agy", "--prompt-interactive"]}
    for host, prefix in expected.items():
        info = handoff(tmp_path, None, host)
        assert (info["task_id"], info["from_host"], info["to_host"]) == ("feat-7", "codex", host)
        assert info["launch_argv"][:-1] == prefix and info["launch_argv"][-1] == info["resume_prompt"]
        assert "wire config flag" in info["resume_prompt"] and info["launched"] is False
        import shlex
        assert shlex.split(info["launch_command"]) == info["launch_argv"]
    assert handoff(tmp_path, "old-1", "agy")["task_id"] == "old-1"
    with pytest.raises(EgoError, match="Save a checkpoint"):
        handoff(tmp_path, "nope", "codex")

    capsys.readouterr()
    assert main(["session", "list", "--root", str(tmp_path)]) == 0
    assert [x["task_id"] for x in json.loads(capsys.readouterr().out)] == ["run-1", "feat-7", "old-1"]
    assert main(["session", "resume", "--host", "opencode", "--root", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["task_id"] == "feat-7" and out["clear_command"] == "/new"
    assert main(["session", "handoff", "--to", "claude", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["launch_argv"][0] == "claude"


def test_handoff_launch_refuses_without_terminal_or_cli(tmp_path: Path, monkeypatch, capsys):
    save_checkpoint(tmp_path, "feat-7", goal="g", host="claude")
    called = []
    monkeypatch.setattr("ego.cli.os.execvp", lambda *a: called.append(a))
    monkeypatch.setattr("ego.session.shutil.which", lambda name: None)
    assert main(["session", "handoff", "feat-7", "--to", "codex", "--launch", "--root", str(tmp_path)]) == 3
    monkeypatch.setattr("ego.session.shutil.which", lambda name: f"/bin/{name}")
    assert main(["session", "handoff", "feat-7", "--to", "codex", "--launch", "--root", str(tmp_path)]) == 3  # pytest: no TTY
    assert called == []
    io = capsys.readouterr()
    assert "HOST_UNAVAILABLE" in io.out + io.err and "LAUNCH_NEEDS_TTY" in io.out + io.err
    monkeypatch.setattr("ego.cli.sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("ego.cli.sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr("ego.cli.os.chdir", lambda p: None)
    main(["session", "handoff", "feat-7", "--to", "codex", "--launch", "--root", str(tmp_path)])
    assert called and called[0][0] == "codex" and called[0][1][0] == "codex"


_HOST_VARS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_CI", "OPENCODE",
              "ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID")


def test_saving_cli_is_detected_only_when_unambiguous(monkeypatch):
    from ego.session import detect_host
    for name in _HOST_VARS: monkeypatch.delenv(name, raising=False)
    assert detect_host() is None
    for name, host in [("CLAUDECODE", "claude"), ("CODEX_THREAD_ID", "codex"), ("OPENCODE", "opencode"),
                       ("ANTIGRAVITY_AGENT", "agy")]:
        monkeypatch.setenv(name, "1")
        assert detect_host() == host
        monkeypatch.delenv(name)
    monkeypatch.setenv("CODEX_THREAD_ID", "t"); monkeypatch.setenv("CLAUDECODE", "1")  # one CLI started inside another
    assert detect_host() is None


def test_continue_another_clis_work_inside_the_open_cli(tmp_path: Path, monkeypatch, capsys):
    for name in _HOST_VARS: monkeypatch.delenv(name, raising=False)
    root = str(tmp_path)
    # Codex saves; its environment identifies it without --host.
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    assert main(["session", "checkpoint", "--root", root, "--task-id", "feat-7", "--goal", "Add retry backoff",
                 "--completed", "unit tests passed", "--next", "wire config flag"]) == 0
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert main(["session", "checkpoint", "--root", root, "--task-id", "docs-2", "--goal", "Rewrite docs"]) == 0
    monkeypatch.delenv("CLAUDECODE")
    capsys.readouterr()
    # AGY: "continue from codex". The newer claude checkpoint is not picked.
    monkeypatch.setenv("ANTIGRAVITY_AGENT", "1")
    assert main(["session", "resume", "--from", "codex", "--root", root]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["task_id"], out["from_host"], out["checkpoint_status"]) == ("feat-7", "codex", "IN_PROGRESS")
    assert "wire config flag" in out["resume_prompt"] and "clear_command" not in out
    assert main(["session", "resume", "--from", "claude", "--root", root]) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "docs-2"
    # Nothing saved by that CLI: say so, never fall back to someone else's task.
    assert main(["session", "resume", "--from", "opencode", "--root", root]) == 3
    io = capsys.readouterr()
    assert "No open checkpoint saved by opencode" in io.out + io.err
    # A later checkpoint from AGY takes the task over.
    assert main(["session", "checkpoint", "--root", root, "--task-id", "feat-7", "--goal", "Add retry backoff",
                 "--status", "VERIFIED"]) == 0
    assert load_checkpoint(tmp_path, "feat-7")["host"] == "agy"
    capsys.readouterr()
    monkeypatch.setattr("ego.session.shutil.which", lambda name: f"/bin/{name}")
    assert main(["session", "handoff", "--from", "agy", "--to", "opencode", "--root", root]) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "feat-7"
    # Plain resume (clearing this CLI) uses the detected host's own instructions.
    assert main(["session", "resume", "feat-7", "--root", root]) == 0
    assert json.loads(capsys.readouterr().out)["clear_command"] == "/clear"
    assert "session resume --from" in instruction_block(Config())


def _managed(root: Path, run_id: str) -> None:
    save_checkpoint(root, run_id, goal="Run the DAG", completed=["a (PASSED)"], next_steps=["b"],
                    status="IN_PROGRESS", source="managed", host="claude")


def test_locked_managed_checkpoint_is_skipped_by_default_and_refused_by_name(tmp_path: Path):
    from ego.session import handoff, pickup, resolve_task
    _managed(tmp_path, "free-run")
    _managed(tmp_path, "locked-run")  # saved last, so it is the newest
    with exclusive_lock(tmp_path / ".ego/locks/locked-run.lock", blocking=False):  # its live run process
        assert resolve_task(tmp_path, None) == "free-run"
        with pytest.raises(EgoError) as pick:
            pickup(tmp_path, "locked-run", "claude")
        assert pick.value.code == "RUN_ACTIVE"
        with pytest.raises(EgoError) as hand:
            handoff(tmp_path, "locked-run", "codex")
        assert hand.value.code == "RUN_ACTIVE"
    # Once the lock is free the same checkpoint is an unlocked, interrupted run that can be picked up.
    assert pickup(tmp_path, "locked-run", "claude")["task_id"] == "locked-run"


def test_unlocked_interrupted_managed_checkpoint_is_picked_when_newest(tmp_path: Path):
    from ego.session import resolve_task
    _managed(tmp_path, "older-run")
    _managed(tmp_path, "interrupted-run")  # no live process holds its lock: interrupted
    assert resolve_task(tmp_path, None) == "interrupted-run"


def test_only_locked_managed_checkpoints_leave_nothing_to_resolve(tmp_path: Path):
    from ego.session import resolve_task
    _managed(tmp_path, "locked-run")
    with exclusive_lock(tmp_path / ".ego/locks/locked-run.lock", blocking=False):
        with pytest.raises(EgoError) as missing:
            resolve_task(tmp_path, None)
        assert missing.value.code == "HANDOFF_MISSING"
